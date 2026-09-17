"""Build, validate, summarize, and mutate declarative mart plans.

Builders, attacks, and the compiler share the same operations. Validation uses
parsed SQL facts and succeeds exactly when plan compilation does.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from elt_taskgen.models import (
    AttackKind,
    ColumnType,
    JoinType,
    MartColumn,
    MartColumnKind,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    SemanticPattern,
    TaskIR,
)
from elt_taskgen.sql_identifiers import quote_sql_identifier

#: details keys with COMPILER meaning; every OTHER key on an aggregate op is a
#: ``alias -> aggregate expression`` measure. Reserved on EVERY op kind, so a
#: measure named e.g. `order_by` is never swallowed as a compiler key.
RESERVED_AGGREGATE_DETAIL_KEYS = frozenset(
    {
        "name", "select", "mode", "sql", "group_by", "function", "expression",
        "description",
        "partition_by", "order_by", "tie_break", "units", "null_result",
        "rounding", "domain", "boundary",
    }
)

#: Op kinds that compile to a GROUP BY (one shared compiler branch); the kind
#: labels the strongest construct present, certified against the measure AST.
AGGREGATE_FAMILY_KINDS: frozenset[MartOpKind] = frozenset(
    {MartOpKind.AGGREGATE, MartOpKind.FILTERED_AGGREGATE, MartOpKind.DISTINCT}
)

#: Op kinds that compile to a plain projection with an explicit select list.
PROJECTION_KINDS: frozenset[MartOpKind] = frozenset(
    {MartOpKind.DERIVE, MartOpKind.WINDOW, MartOpKind.CONDITIONAL, MartOpKind.RATIO}
)

#: Which MartColumnKind an op kind CLAIMS about the columns it names. Ops
#: absent from this map make no claim (they carry passthrough and derived alike).
OP_COLUMN_KIND: dict[MartOpKind, MartColumnKind] = {
    MartOpKind.AGGREGATE: MartColumnKind.AGGREGATED,
    MartOpKind.FILTERED_AGGREGATE: MartColumnKind.AGGREGATED,
    MartOpKind.DISTINCT: MartColumnKind.AGGREGATED,
    MartOpKind.EXTREMA: MartColumnKind.RANKED,
    MartOpKind.WINDOW: MartColumnKind.RANKED,
    MartOpKind.CONDITIONAL: MartColumnKind.CATEGORICAL,
    MartOpKind.RATIO: MartColumnKind.DERIVED,
}

#: Strongest claim wins when several ops name the same column (a measure is
#: named by its AGGREGATE op AND again by the final COALESCE derive).
_COLUMN_KIND_PRECEDENCE: tuple[MartColumnKind, ...] = (
    MartColumnKind.RANKED,
    MartColumnKind.AGGREGATED,
    MartColumnKind.CATEGORICAL,
    MartColumnKind.DERIVED,
    MartColumnKind.PASSTHROUGH,
)

#: Window functions that NAVIGATE rows rather than aggregate them: DuckDB
#: rejects a frame clause on these, so the mandatory-frame rule exempts them.
NAVIGATION_WINDOW_FUNCTIONS: frozenset[str] = frozenset(
    {"Lag", "Lead", "RowNumber", "Rank", "DenseRank", "PercentRank", "CumeDist", "NTile"}
)

_DIRECTION_RE = re.compile(r"\b(ASC|DESC)\b", re.IGNORECASE)

# Matched against the AUTHORED text, never sqlglot output: the DuckDB generator
# elides `NULLS LAST` and fills `nulls_first` with the default, so a round-trip
# cannot tell "author stated it" from "author stated nothing".
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


# --- Op contracts: what each op kind must carry ---
# Determinism is the point: without a TOTAL order the gold depends on DuckDB's
# row order, so a correct solver can score below 1.0.

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


# --- Column-kind derivation (the instrument's fail-closed gate) ---

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


#: The AST node whose presence in a projection proves the op's construct
#: actually produced that column (rather than merely carrying it forward).
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


# Only registered detectors may certify semantic-pattern declarations.  Adding
# an enum value makes the vocabulary available to plans and coverage policy;
# adding it here is the separate implementation milestone that makes the claim
# executable.  This split lets the implementation fail closed while patterns
# are delivered one vertical slice at a time.
SEMANTIC_PATTERN_CERTIFIERS: dict[SemanticPattern, Callable[[MartPlan], bool]] = {
    SemanticPattern.AGGREGATE_THEN_FILTER: _certifies_aggregate_then_filter,
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
    mart_cols = {c.name for c in mart.columns}
    all_task_cols = {c.name for t in task.tables for c in t.columns}
    produced: set[str] = set()
    #: Intermediate relations bound by earlier ops — the same namespace
    #: compile_plan_sql builds (details['name'], else the op's single table).
    bound: set[str] = set()

    for idx, op in enumerate(plan.ops):
        loc = f"op[{idx}] ({op.kind.value})"
        problems.extend(op_problems(op, loc=loc))

        unknown_tables = [
            t for t in op.tables if t not in table_names and t not in bound
        ]
        if unknown_tables:
            problems.append(f"{loc}: unknown tables {unknown_tables}")
        known_op_tables = [t for t in op.tables if t in table_names]
        touches_intermediate = any(t in bound and t not in table_names for t in op.tables)

        if known_op_tables and not touches_intermediate:
            valid_cols: set[str] = set()
            for t in known_op_tables:
                valid_cols |= {c.name for c in task.table(t).columns}
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


#: Details keys carrying literal SQL for the compiler.  This legacy roster is
#: retained for internal diagnostic summaries; public surfaces use the closed
#: TaskIR/MartSpec projection below instead.
_COMPILER_ONLY_DETAIL_KEYS = frozenset({"select", "sql", "name"})

#: Closed vocabulary of genuinely semantic detail fields which may cross the
#: solver information barrier.  Every other detail key is compiler state (for
#: example ``group_by``/``partition_by``), a generated relation binding, or an
#: aggregate alias whose value is executable SQL.  Descriptions carry the
#: corresponding business rule in prose.
SOLVER_PUBLIC_DETAIL_KEYS = frozenset(
    {
        "boundary",
        "domain",
        "mode",
        "no_activity",
        "null_result",
        "rounding",
        "tie_break",
        "units",
    }
)


def _solver_public_semantic_details(
    task: TaskIR,
    mart: MartSpec,
    op: MartOp,
) -> dict[str, str]:
    """Return closed, non-SQL semantic parameters for one public rule.

    Even a normally public field can contain a generated compiler alias (the
    common example is ``tie_break=f_label``).  Such a value is implementation
    state, while the operation/column description already states the public
    tie rule, so omit it rather than publishing a private alias.
    """

    public_names = {
        *(table.name.casefold() for table in task.tables),
        *(
            column.name.casefold()
            for table in task.tables
            for column in table.columns
        ),
        *(column.name.casefold() for column in mart.columns),
    }
    internal_names = {
        str(candidate).casefold()
        for candidate in (
            *(item.details.get("name", "") for item in mart.plan.ops),
            *(
                table
                for item in mart.plan.ops
                for table in item.tables
                if table.casefold() not in public_names
            ),
            *(
                column
                for item in mart.plan.ops
                for column in item.columns
                if column.casefold() not in public_names
            ),
        )
        if candidate and str(candidate).casefold() not in public_names
    }
    result: dict[str, str] = {}
    for key in sorted(SOLVER_PUBLIC_DETAIL_KEYS & op.details.keys()):
        value = str(op.details[key]).strip()
        if not value:
            continue
        tokens = {
            token.casefold()
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", value)
        }
        if tokens & internal_names:
            continue
        if key == "tie_break" and not tokens.issubset(public_names):
            # Publish tie-breaks only when every identifier is public; internal
            # aliases would leak implementation details into the prose contract.
            continue
        value = _sayable_semantic_value(value)
        if not value:
            continue
        result[key] = value
    return result


#: A trailing parenthetical of a published semantic value.
_SEMANTIC_PARENTHETICAL_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _sayable_semantic_value(value: str) -> str:
    """Return the author-safe portion of a published semantic value.

    Remove trailing implementation parentheticals until the text satisfies
    declarative-prose rules. This keeps solver-facing behavior, such as a ratio
    result for a zero denominator, without requiring forbidden implementation
    terms such as operator names. Return an empty string when no safe text
    remains.
    """
    from elt_taskgen.review import declarative_prose as _declarative

    text = value.strip()
    while _declarative.operator_problems(text):
        trimmed = _SEMANTIC_PARENTHETICAL_RE.sub("", text).strip()
        if trimmed == text:
            return ""
        text = trimmed
    return text


def solver_safe_condition_requirements(
    task: TaskIR,
    mart: MartSpec,
    op: MartOp,
) -> dict[str, list[str]]:
    """Project a condition into identifiers and literal specification values.

    Parse ``MartOp.predicate`` and retain only TaskIR/MartSpec identifiers and
    scalar literals needed by prose-fidelity checks. Never expose predicate
    text, aliases, operators, or expression structure. Leave unparseable prose
    predicates to ``op.description`` rather than applying a lossy regex.
    """

    text = op.predicate.strip()
    if not text:
        return {}
    parsed = _parse_expression(text)
    if parsed is None or isinstance(parsed, exp.Query) or parsed.find(exp.Select):
        return {}

    public_tables = {table.name.casefold(): table.name for table in task.tables}
    public_columns = {
        column.name.casefold(): column.name
        for table in task.tables
        for column in table.columns
    }
    public_columns.update(
        {column.name.casefold(): column.name for column in mart.columns}
    )
    identifiers: list[str] = []
    literals: list[str] = []

    def append_once(target: list[str], value: str) -> None:
        if value and value not in target:
            target.append(value)

    for node in parsed.walk():
        if isinstance(node, exp.Column):
            qualifier = str(node.table or "")
            if qualifier.casefold() in public_tables:
                append_once(identifiers, public_tables[qualifier.casefold()])
            name = str(node.name or "")
            if name.casefold() in public_columns:
                append_once(identifiers, public_columns[name.casefold()])
        elif isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
            if not node.this.is_string:
                append_once(literals, "-" + str(node.this.this))
        elif isinstance(node, exp.Literal):
            if isinstance(node.parent, exp.Neg) and node.parent.this is node:
                continue
            append_once(literals, str(node.this))
        elif isinstance(node, exp.Boolean):
            append_once(literals, str(node.this).lower())
        elif isinstance(node, exp.Null):
            # `IS NULL` expresses absence, not a contract value. Publish NULL
            # only when it is a value, such as in a CASE arm or COALESCE.
            if isinstance(node.parent, exp.Is):
                continue
            append_once(literals, "null")

    result: dict[str, list[str]] = {}
    if identifiers:
        result["public_identifiers"] = identifiers
    if literals:
        result["literal_values"] = literals
    return result


def solver_safe_plan_requirements(task: TaskIR, mart: MartSpec) -> dict[str, object]:
    """Project one MartPlan into a closed solver-visible requirement schema.

    This is the shared information barrier for agent prompts and every public
    export.  It publishes ordered natural-language rules, actual source-table
    inputs, public source/output fields, join preservation, parsed condition
    identifiers/literals, and a small closed semantic-parameter vocabulary.
    It deliberately never publishes raw predicates, their operators,
    SELECT/GROUP/ORDER expressions, generated aliases, plan notes, reference
    SQL, populations, or gold values.
    """

    source_names = {table.name.casefold(): table.name for table in task.tables}
    public_columns = {
        column.name.casefold(): column.name
        for table in task.tables
        for column in table.columns
    }
    public_columns.update(
        {column.name.casefold(): column.name for column in mart.columns}
    )
    steps: list[dict[str, object]] = []
    for order, op in enumerate(mart.plan.ops, start=1):
        step: dict[str, object] = {
            "order": order,
            "operation": op.kind.value,
            "description": op.description,
        }
        source_inputs = list(
            dict.fromkeys(
                source_names[table.casefold()]
                for table in op.tables
                if table.casefold() in source_names
            )
        )
        if source_inputs:
            step["source_inputs"] = source_inputs
        carried_fields = list(
            dict.fromkeys(
                public_columns[column.casefold()]
                for column in op.columns
                if column.casefold() in public_columns
            )
        )
        if carried_fields:
            step["carried_fields"] = carried_fields
        if op.join_type is not None:
            step["join_preservation"] = op.join_type.value
        condition = solver_safe_condition_requirements(task, mart, op)
        if condition:
            step["condition"] = condition
        semantic_details = _solver_public_semantic_details(task, mart, op)
        if semantic_details:
            step["semantic_parameters"] = semantic_details
        steps.append(step)
    return {"steps": steps}


def solver_safe_mart_requirements(task: TaskIR, mart: MartSpec) -> dict[str, object]:
    """Full public MartSpec contract plus its solver-safe ordered rules."""

    relationships = [
        {
            "child_table": relationship.child_table,
            "child_columns": list(relationship.child_columns),
            "parent_table": relationship.parent_table,
            "parent_columns": list(relationship.parent_columns),
            "required": relationship.required,
        }
        for relationship in task.relationships
    ]
    result: dict[str, object] = {
        "name": mart.name,
        "description": mart.description,
        "grain": mart.grain,
        "key_columns": list(mart.key_columns),
        "columns": [
            {
                "name": column.name,
                "type": column.type.value,
                "description": column.description,
            }
            for column in mart.columns
        ],
        "transformation": solver_safe_plan_requirements(task, mart),
    }
    if relationships:
        result["source_relationships"] = relationships
    return result


def solver_safe_plan_summary(task: TaskIR, mart: MartSpec) -> str:
    """Deterministic prose form of :func:`solver_safe_plan_requirements`."""

    plan = solver_safe_plan_requirements(task, mart)
    steps = plan["steps"]
    assert isinstance(steps, list)  # constructed immediately above
    lines = [f"Mart {mart.name!r} has {len(steps)} declared semantic rules:"]
    for step in steps:
        assert isinstance(step, dict)
        extras: list[str] = []
        if step.get("source_inputs"):
            extras.append(
                "public source tables: " + ", ".join(step["source_inputs"])
            )
        if step.get("carried_fields"):
            extras.append(
                "public carried/output columns: "
                + ", ".join(step["carried_fields"])
            )
        if step.get("join_preservation"):
            extras.append(f"join preservation: {step['join_preservation']}")
        condition = step.get("condition")
        if isinstance(condition, dict) and condition:
            condition_parts: list[str] = []
            if condition.get("public_identifiers"):
                condition_parts.append(
                    "public identifiers: "
                    + ", ".join(condition["public_identifiers"])
                )
            if condition.get("literal_values"):
                condition_parts.append(
                    "literal specification values: "
                    + ", ".join(condition["literal_values"])
                )
            extras.append("condition " + "; ".join(condition_parts))
        parameters = step.get("semantic_parameters")
        if isinstance(parameters, dict) and parameters:
            extras.append(
                "semantic parameters: "
                + "; ".join(f"{key}={parameters[key]}" for key in sorted(parameters))
            )
        line = (
            f"{step['order']}. [{step['operation']}] "
            f"{step['description']}"
        )
        if extras:
            line += " (" + " | ".join(extras) + ")"
        lines.append(line)
    return "\n".join(lines)


def plan_summary(plan: MartPlan) -> str:
    """Legacy diagnostic rendering of a plan without TaskIR/MartSpec context.

    This can contain raw predicates, aggregate expressions, and generated
    relation aliases. It is intentionally not used by an agent prompt or a
    public export; use :func:`solver_safe_plan_summary` there.
    """
    lines = [f"Mart {plan.mart!r} is built by {len(plan.ops)} declared operations:"]
    for idx, op in enumerate(plan.ops, start=1):
        extras: list[str] = []
        if op.tables:
            extras.append(f"tables: {', '.join(op.tables)}")
        if op.columns:
            extras.append(f"columns: {', '.join(op.columns)}")
        if op.join_type is not None:
            extras.append(f"join type: {op.join_type.value}")
        if op.predicate:
            extras.append(f"predicate: {op.predicate}")
        shown = [k for k in sorted(op.details) if k not in _COMPILER_ONLY_DETAIL_KEYS]
        if shown:
            extras.append(
                "details: " + "; ".join(f"{k}={op.details[k]}" for k in shown)
            )
        line = f"{idx}. [{op.kind.value}] {op.description}"
        if extras:
            line += " (" + " | ".join(extras) + ")"
        lines.append(line)
    if plan.notes:
        lines.append(f"Notes: {plan.notes}")
    return "\n".join(lines)


def attack_surface(plan: MartPlan) -> dict[AttackKind, list[int]]:
    """Which AttackKind applies to which op (0-based indexes) — the structure
    verification/attacks.py mutates.

    A kind with no applicable op is OMITTED, which attacks.py reads as "not
    compilable for this task"; plan-level kinds map to every index. Routing is
    on `op.kind` plus the declared expressions' AST, never on prose.
    """
    surface: dict[AttackKind, list[int]] = {}

    def add(kind: AttackKind, idx: int) -> None:
        surface.setdefault(kind, []).append(idx)

    for idx, op in enumerate(plan.ops):
        text = _op_text(op)
        if (
            op.kind is MartOpKind.JOIN
            and op.join_type is not None
            and op.join_type is not JoinType.INNER
        ):
            add(AttackKind.INNER_JOIN, idx)
        if op.kind is MartOpKind.DEDUPE:
            add(AttackKind.NO_DEDUP, idx)
        if op.kind in AGGREGATE_FAMILY_KINDS:
            add(AttackKind.WRONG_GRAIN, idx)
            measures = [expr for _, expr in measure_items(op)]
            if any(is_distinct_aggregate_expr(e) for e in measures) or "distinct" in text:
                add(AttackKind.NO_DEDUP, idx)
            if any(is_filtered_aggregate_expr(e) for e in measures):
                add(AttackKind.DROPPED_FILTER, idx)
            if any(h in text for h in _DENOMINATOR_HINTS):
                add(AttackKind.WRONG_DENOMINATOR, idx)
        if op.kind is MartOpKind.FILTER:
            add(AttackKind.DROPPED_FILTER, idx)
            if plan.declares_pattern(SemanticPattern.AGGREGATE_THEN_FILTER):
                add(AttackKind.WRONG_AGG_STAGE, idx)
        if op.kind in (MartOpKind.WINDOW, MartOpKind.EXTREMA):
            add(AttackKind.WRONG_WINDOW, idx)
        if op.kind is MartOpKind.RATIO:
            add(AttackKind.WRONG_DENOMINATOR, idx)
        if op.kind in PROJECTION_KINDS:
            select = op.details.get("select", "")
            parsed = _parse_select_list(select) if select else None
            coalesces = parsed is not None and parsed.find(exp.Coalesce) is not None
            if coalesces or (
                op.kind is MartOpKind.DERIVE
                and any(h in text for h in _NULL_DEFAULT_HINTS)
            ):
                add(AttackKind.NO_NULL_DEFAULT, idx)

    every = list(range(len(plan.ops)))
    for kind in _PLAN_LEVEL_KINDS:
        surface[kind] = list(every)
    return surface


# --- Compiler-grade plan builders (the ONE definition of an adapter plan) ---

def quote(identifier: str) -> str:
    """Double-quote one SQL identifier (vendored names may hit reserved words)."""
    return quote_sql_identifier(identifier, dialect="duckdb", force=True)


def relation_identifier(identifier: str) -> str:
    """Render a relation/alias while leaving ordinary generated SQL unchanged."""

    return quote_sql_identifier(identifier, dialect="duckdb")


def _unique(name: str, taken: set[str]) -> str:
    """Deterministic collision-free relation/alias name."""
    candidate = name
    n = 1
    while candidate in taken:
        n += 1
        candidate = f"{name}_{n}"
    taken.add(candidate)
    return candidate


@dataclass(frozen=True)
class StarJoin:
    """One LEFT JOIN of a source table onto the relation built so far.

    `on_pairs` are (carried left alias, right column of `table`); `carry` are
    (column, alias) pairs projected forward — a later op can only reference
    what was carried.
    """

    table: str
    on_pairs: tuple[tuple[str, str], ...]
    carry: tuple[tuple[str, str], ...] = ()
    #: Declared relationship key columns, recorded on the op for provenance.
    rel_columns: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class Measure:
    """One aggregate measure of a star mart; `expr` is SQL over carried aliases.

    Set `null_default` ONLY for measures that can actually be NULL: a COALESCE
    around a COUNT is a dead `no_null_default` surface that makes the attack lie.
    """

    column: str
    expr: str
    null_default: str | None = None
    # -- plan-library additions; the legacy `build_star` ignores them --------
    #: Mart column type — read only when the builder mints the column contract.
    type: ColumnType = ColumnType.BIGINT
    description: str = ""
    #: What KIND of work this measure is, in the anchor's own taxonomy.
    kind: MartColumnKind = MartColumnKind.AGGREGATED
    #: False = internal scaffolding feeding a `Derived` column, not a mart
    #: column of its own; it still gets an alias so the derive can name it.
    emit: bool = True
    #: Whether a REAL input row can omit this measure's source value. ``False``
    #: lets the public contract omit an impossible all-missing-real-row branch;
    #: ``None`` preserves the conservative contract for unmeasured adapters.
    #: Empty LEFT-join groups can still need ``null_default`` either way.
    input_nullable: bool | None = None


#: Quoted identifiers inside a measure expression, used to tell a measure
#: over a parent-carried column from one over the matching rows.
_QUOTED_NAME_RE = re.compile(r'"([^"]+)"')


def _article(word: str, *, capitalized: bool = False) -> str:
    """"a" or "an" for `word`. Table names are the subject of most generated
    sentences, and "a employees row" reads as a defect to the critic that has
    to decide whether the sentence was written carefully."""
    article = "an" if str(word)[:1].lower() in "aeiou" else "a"
    return article.capitalize() if capitalized else article


@dataclass(frozen=True)
class KeyColumn:
    """One grain column of a plan-library mart."""

    column: str
    type: ColumnType
    description: str
    #: Parent column it projects (empty when `expr` supplies the value).
    source: str = ""
    #: SQL over the parent alias, for a DERIVED grain (a truncated period).
    expr: str = ""
    #: A grain can itself be computed (for example, a day truncated from a
    #: source timestamp).  Preserve that distinction in the mart contract
    #: instead of labelling every key as a passthrough.
    kind: MartColumnKind = MartColumnKind.PASSTHROUGH


@dataclass(frozen=True)
class Passthrough:
    """One NON-computed attribute carried alongside the measures.

    Joins the GROUP BY (it is functionally dependent on the grain) rather than
    taking a MAX() wrapper that would misreport it.
    """

    column: str
    type: ColumnType
    description: str
    source: str = ""
    #: Hop alias the attribute comes from ("" = the parent table).
    from_hop: str = ""
    #: Literal to COALESCE it to in the final projection (orphan buckets).
    null_default: str | None = None


@dataclass(frozen=True)
class WindowExpr:
    """One OVER() expression evaluated on the JOINED rows BEFORE aggregation.

    Two load-bearing rules: the ORDER BY must name the TIE-BREAK column, not
    just the measure, and running totals need an explicit frame (the RANGE
    default lumps equal keys together, so the total is wrong exactly on ties).
    """

    alias: str
    expr: str
    type: ColumnType = ColumnType.BIGINT
    description: str = ""


#: What an ARGMAX column becomes on an empty group, PER TYPE. The default is a
#: SQL literal inside ``COALESCE(<column>, <default>)``, so it must bind against
#: the column's type (``COALESCE(<varchar>, 0)`` is a BinderException). Types
#: without an honest zero are ABSENT ON PURPOSE (see `extremum_default`).
_EXTREMUM_DEFAULTS: dict[ColumnType, str] = {
    ColumnType.TEXT: "'(none)'",
    ColumnType.INTEGER: "0",
    ColumnType.BIGINT: "0",
    ColumnType.FLOAT: "0",
    ColumnType.DECIMAL: "0",
}


def extremum_default(column_type: ColumnType) -> str:
    """The SQL literal an ARGMAX column of this type takes on an empty group.

    FAIL CLOSED: an undeclared type raises rather than emitting no COALESCE,
    which would ship NULL against a description promising a value. There is no
    honest zero for a DATE or BOOLEAN, so the shape declines instead.
    """
    try:
        return _EXTREMUM_DEFAULTS[column_type]
    except KeyError:
        raise ShapeNotSelectable(
            f"no declared empty-group default for an ARGMAX column of type "
            f"{column_type.value!r}; declare one in _EXTREMUM_DEFAULTS or do not "
            "project this column (a column with no default ships NULL against a "
            "description that promises a value)"
        ) from None


def _default_prose(column_type: ColumnType) -> str:
    """How the shipped column DESCRIPTION spells the empty-group default.

    Read off `extremum_default`, never hand-written beside it: the prose is
    graded against the emitted column.
    """
    literal = extremum_default(column_type)
    if literal.startswith("'") and literal.endswith("'"):
        return f"the literal {literal}"
    return literal


#: How the shipped prose spells the TEXT order a tie-break relies on.
#: "Alphabetically smallest" is not a statement about stored bytes: under
#: binary collation 'Zulu' < 'alpha', so a case-insensitive solver picks a
#: different winner on exactly the ties that differ in case.
TEXT_ORDER_PROSE = (
    "under a plain case-sensitive comparison of the stored text (the warehouse "
    "default order, in which every uppercase letter sorts before every "
    "lowercase one)"
)


@dataclass(frozen=True)
class Extremum:
    """One ARGMAX column: a NON-aggregate attribute of the row at which a
    measure is extremal (compiles to QUALIFY ROW_NUMBER() ... = 1).

    `order_by` MUST carry a tie-break term, or unresolved ties make the gold a
    function of DuckDB's row order and a CORRECT solver can score below 1.0.
    `type` is the SOURCE column's type: it selects the empty-group COALESCE
    literal, and a type with no declared default is refused here.
    """

    column: str
    source: str
    type: ColumnType
    description: str

    def __post_init__(self) -> None:
        extremum_default(self.type)  # fail closed: no default, no column


@dataclass(frozen=True)
class Derived:
    """One POST-aggregate mart column: a ratio, a CASE ladder, an existence flag.

    `expr` names measures and keys by MART COLUMN NAME in braces; the builder
    substitutes its internal aliases, so the caller never guesses them.
    """

    column: str
    expr: str
    type: ColumnType
    description: str
    kind: MartColumnKind = MartColumnKind.DERIVED


#: The constructed witness rows the counterfactual population may carry. Named
#: HERE because the SHAPE declares which functional discriminations it needs
#: and populations.py adds only those plus schema-proven nullable boundaries.
#: Fail-closed rule: NO CONSTRUCT SHIPS WITHOUT ITS WITNESS.
WITNESS_CONTROL = "A_control"                  # ordinary parent, 2 distinct children
WITNESS_CHILDLESS = "B_childless"              # parent with NO bridge rows
WITNESS_DUPLICATE = "C_duplicate"              # byte-identical duplicate children
WITNESS_SAME_CHILD = "D_same_child"            # 2 bridge rows -> the SAME child
WITNESS_ALL_FAIL = "E_all_fail"                # every child fails the predicate
WITNESS_TIE = "F_tie"                          # exact measure tie, different labels
WITNESS_OUT_OF_DOMAIN = "G_out_of_domain"      # categorical value outside the domain
WITNESS_ON_THRESHOLD = "H_on_threshold"        # value EXACTLY on a declared threshold
WITNESS_BRIDGE_NO_CHILD = "I_bridge_no_child"  # bridge row whose child is missing
WITNESS_SECOND_PERIOD = "J_second_period"      # the same entity in two periods
WITNESS_BELOW_THRESHOLD = "K_below_threshold"  # exactly one below a group threshold
WITNESS_NULL_MEASURE = "L_null_measure"        # linked rows whose measures are all NULL
WITNESS_ARGMAX_CASE_ORDER = "M_argmax_case_order"  # binary text order differs from LOWER
WITNESS_ARGMAX_NULL_ORDER = "N_argmax_null_order"  # NULLS LAST differs from NULLS FIRST
WITNESS_DISTINCT_MEASURE = "O_distinct_measure"  # repeated non-NULL value in one group
#: Reverses row A/F input order so `wrong_window` mutants cannot depend on scan
#: order, including period-ordered shapes without a tie witness.
WITNESS_LATEST_FIRST = "P_latest_first"          # same entity, later period listed FIRST

#: Deterministic construction order (one parent row per witness, in this order).
WITNESS_ORDER: tuple[str, ...] = (
    WITNESS_CONTROL,
    WITNESS_CHILDLESS,
    WITNESS_DUPLICATE,
    WITNESS_SAME_CHILD,
    WITNESS_ALL_FAIL,
    WITNESS_TIE,
    WITNESS_OUT_OF_DOMAIN,
    WITNESS_ON_THRESHOLD,
    WITNESS_BRIDGE_NO_CHILD,
    WITNESS_SECOND_PERIOD,
    WITNESS_BELOW_THRESHOLD,
    WITNESS_NULL_MEASURE,
    WITNESS_ARGMAX_CASE_ORDER,
    WITNESS_ARGMAX_NULL_ORDER,
    WITNESS_DISTINCT_MEASURE,
    WITNESS_LATEST_FIRST,
)

#: The witnesses that MIRROR row A on the ordering axis (winner listed FIRST).
#: A shape claiming `wrong_window` (any variant) must construct at least one,
#: or its claim rests on the position an unordered window happens to take.
WINDOW_MIRROR_WITNESSES: frozenset[str] = frozenset({WITNESS_TIE, WITNESS_LATEST_FIRST})


def wrong_window_mirror_problem(
    attack_claims: tuple[str, ...], witnesses: tuple[str, ...]
) -> str | None:
    """Why a shape's `wrong_window` claim is unbacked, or None.

    Row A alone lists its winner LAST; a mutant that always takes the last row
    of a partition reproduces gold on it. The claim needs a mirror witness
    (row F for a measure order with a tie-break, row P for a period order)
    so that no fixed position is right on both.
    """
    claims = {str(claim).split("@", 1)[0] for claim in attack_claims}
    if "wrong_window" not in claims:
        return None
    if WINDOW_MIRROR_WITNESSES & set(witnesses):
        return None
    return (
        "claim 'wrong_window' has no mirror witness: row A lists its winner "
        "last, so the claim needs "
        f"{WITNESS_TIE} (measure order) or {WITNESS_LATEST_FIRST} (period order) "
        "as well, else an unordered window taking the last row per partition "
        "reproduces gold"
    )


#: The budget contract, in the anchor's measured minima. `build_rollup` raises
#: on it rather than reporting advice, so it is a floor the builder cannot miss.
MIN_MART_COLUMNS = 6
MIN_MART_COMPUTED = 5
MIN_MART_PASSTHROUGH = 2

@dataclass(frozen=True)
class FactRoles:
    """Which COLUMNS of the bridge/child tables the witness rows must control.

    A witness is constructible only when the shape names the column its
    discriminating value goes in; `build_rollup` refuses a shape that does not.
    """

    #: Bridge column that is COUNTed (its PK, or any always-present column).
    link_key: str = ""
    #: Bridge column holding the FK to the child — the fan-out axis (row D).
    child_key: str = ""
    #: Numeric bridge column that is SUMmed / ranked (rows F, H).
    measure: str = ""
    #: Text bridge/child column projected by an argmax (row F needs it).
    label: str = ""
    #: Categorical bridge column a filtered aggregate keys on (rows E, G, H).
    predicate_column: str = ""
    #: Values of `predicate_column` the filter ACCEPTS.
    predicate_pass: tuple[str, ...] = ()
    #: Values it REJECTS — row E fills every child with one of these.
    predicate_fail: tuple[str, ...] = ()
    #: The enumerated domain a CASE ladder maps; `out_of_domain` is a legal
    #: schema value the ladder does NOT name, so a missing ELSE is falsifiable.
    domain: tuple[str, ...] = ()
    out_of_domain: str = ""
    #: Timestamp/date bridge column a temporal grid truncates (row J).
    period_column: str = ""
    #: Child-table key column the bridge points at (row I withholds it).
    child_primary_key: str = ""
    #: PARENT-side column whose declared domain a CASE ladder maps; distinct
    #: from the bridge-side `predicate_column`. Row G writes `out_of_domain`
    #: here, so a missing ELSE is falsifiable without violating the schema enum.
    domain_column: str = ""


@dataclass(frozen=True)
class AllNullAggregateWitness:
    """A recoverable all-missing input boundary for one aggregate measure.

    ``source_table`` is the rollup's base table. ``input_columns`` are the
    published source columns whose values must all be NULL on one real row;
    ``direct_group_columns`` are source columns projected directly into the
    GROUP BY and therefore prove that row forms an isolated output group.
    Population construction activates the witness only when every input is
    nullable in the final ``TableSpec``.  Keeping this evidence per measure
    lets several compatible witnesses share one deterministic source row.
    """

    source_table: str
    input_columns: tuple[str, ...]
    measure_columns: tuple[str, ...]
    direct_group_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "source_table",
            "input_columns",
            "measure_columns",
            "direct_group_columns",
        ):
            if not getattr(self, field_name):
                raise ValueError(
                    f"all-NULL aggregate witness needs non-empty {field_name}"
                )


@dataclass(frozen=True)
class RoundBeforeSumWitness:
    """Exact source binding for a recovered per-row rounding aggregate.

    The only admitted expression shape is
    ``SUM(ROUND(input_column / divisor, decimal_places))``.  The adapter
    records that syntax-level fact; population construction later checks the
    final table constraints and plants two same-group rows only when they can
    exist without violating an identity or relationship.
    """

    source_table: str
    input_column: str
    measure_columns: tuple[str, ...]
    direct_group_columns: tuple[str, ...]
    divisor: Decimal
    decimal_places: int

    def __post_init__(self) -> None:
        for field_name in (
            "source_table",
            "input_column",
            "measure_columns",
            "direct_group_columns",
        ):
            if not getattr(self, field_name):
                raise ValueError(
                    f"round-before-sum witness needs non-empty {field_name}"
                )
        if not self.divisor.is_finite() or self.divisor <= 0:
            raise ValueError(
                "round-before-sum witness divisor must be finite and positive"
            )
        # Higher precision is technically legal SQL, but the canonical runtime
        # stores FLOAT/DECIMAL literals as doubles.  This conservative bound
        # keeps the planted value a comfortable distance from rounding ties.
        if not 0 <= self.decimal_places <= 6:
            raise ValueError(
                "round-before-sum witness decimal_places must be between 0 and 6"
            )


@dataclass(frozen=True)
class StarShape:
    """What a built plan IS — the structural facts the counterfactual population
    and the attack catalogue derive from.

    Returned by every builder so populations.py reads the shape the builder
    emitted rather than re-deriving it from op ordering.
    """

    mart: str
    parent: str
    parent_keys: tuple[str, ...]
    key_columns: tuple[str, ...]
    fact: str = ""
    fact_link_columns: tuple[str, ...] = ()
    fact_dedupe: bool = False
    #: Mart columns whose value is NULL (before COALESCE) for a parent row
    #: with no fact rows — what a dropped null-default is observable on.
    null_capable_measures: tuple[str, ...] = ()
    has_join: bool = False

    # -- plan-library additions (all default to the legacy star's answer) ----
    #: Which library shape built this plan ("star", "fan_out_rollup", ...).
    shape_name: str = "star"
    #: The SECOND hop: the child the bridge fans out onto. Empty on a one-hop
    #: star, which is why COUNT and COUNT(DISTINCT) cannot diverge there.
    child: str = ""
    #: (bridge column, child column) pairs of the second hop.
    child_link_pairs: tuple[tuple[str, str], ...] = ()
    join_hops: int = 0
    #: Exact LEFT-join edges whose unmatched left rows can certify the
    #: ``inner_join`` attack. Each item is (left table, left columns, right
    #: table, right columns). Recovered adapters populate this because their
    #: base table is commonly the CHILD side of a relationship, the reverse of
    #: the plan-library witness orientation.
    join_edges: tuple[
        tuple[str, tuple[str, ...], str, tuple[str, ...]], ...
    ] = ()
    #: Undefaulted aggregate inputs for which a real, isolated all-NULL group
    #: can be constructed. Recovered adapters state the exact source bindings;
    #: populations.py activates only the witnesses whose final source columns
    #: are nullable and then verifies the literal rows before advertising them.
    all_null_aggregate_witnesses: tuple[AllNullAggregateWitness, ...] = ()
    #: Exact recovered ``SUM(ROUND(source / literal, places))`` expressions.
    #: populations.py admits only key-free numeric source tables, constructs
    #: two real same-group rows, and advertises a condition only after the final
    #: literals prove that per-row rounding differs from sum-then-round.
    round_before_sum_witnesses: tuple[RoundBeforeSumWitness, ...] = ()
    #: Mart columns by operator family — what `derive_attack_cases` reads to
    #: decide which cases it may HONESTLY declare.
    distinct_measures: tuple[str, ...] = ()
    filtered_measures: tuple[str, ...] = ()
    ratio_measures: tuple[str, ...] = ()
    #: Measures dividing by a LITERAL constant (unit conversions); disjoint from
    #: `ratio_measures` by construction. The Div the mutant flattens to 1.
    constant_divisor_measures: tuple[str, ...] = ()
    ranked_measures: tuple[str, ...] = ()
    conditional_columns: tuple[str, ...] = ()
    window_measures: tuple[str, ...] = ()
    fanout_capable_measures: tuple[str, ...] = ()
    #: Does the counterfactual CONSTRUCT the duplicate-child witness that makes
    #: `no_dedup` observable? An adapter whose plans are RECOVERED from vendor
    #: SQL must set False — with no witness the mutant scores 1.0 and the
    #: counterfactual claim is a lie the gate later rejects.
    dedupe_counterfactual_witness: bool = True
    #: Same question for the JOIN witness (the childless parent that makes
    #: `inner_join` observable); without it every FK resolves and LEFT is INNER.
    join_counterfactual_witness: bool = True
    #: Witness rows this shape REQUIRES the counterfactual to contain.
    witnesses: tuple[str, ...] = ()
    #: Attack cases those witnesses justify ('<kind>' or '<kind>@<variant>').
    attack_claims: tuple[str, ...] = ()
    #: Where those rows put their discriminating values.
    roles: FactRoles = field(default_factory=FactRoles)
    #: Numeric boundaries a CASE ladder tests, so row H can land ON one.
    thresholds: tuple[float, ...] = ()
    #: WHERE the witness rows go: (anchor table, anchor key, bridge table,
    #: bridge FK). Its own field because shapes that build their base FROM the
    #: fact table invert `parent`/`fact`, so deriving the anchor from those two
    #: would put every witness row in the wrong table.
    witness_anchor_table: str = ""
    witness_anchor_key: str = ""
    witness_bridge_table: str = ""
    witness_bridge_fk: str = ""

    def witness_anchor(self) -> tuple[str, str, str, str]:
        """(anchor table, anchor key, bridge table, bridge FK), with the
        legacy star's answer as the fallback."""
        return (
            self.witness_anchor_table or self.parent,
            self.witness_anchor_key or (self.parent_keys[0] if self.parent_keys else ""),
            self.witness_bridge_table or self.fact,
            self.witness_bridge_fk
            or (self.fact_link_columns[0] if self.fact_link_columns else ""),
        )

    @property
    def computed_columns(self) -> tuple[str, ...]:
        """Mart columns this shape declares as COMPUTED (anchor taxonomy)."""
        seen: list[str] = []
        for group in (
            self.distinct_measures,
            self.filtered_measures,
            self.ratio_measures,
            self.ranked_measures,
            self.conditional_columns,
            self.window_measures,
            self.fanout_capable_measures,
        ):
            for c in group:
                if c not in seen:
                    seen.append(c)
        return tuple(seen)


@dataclass(frozen=True)
class BuiltPlan:
    plan: MartPlan
    shape: StarShape
    #: The mart's COLUMN CONTRACT when the builder knows it. Legacy builders
    #: leave it empty (their adapters mint MartColumns); every plan-library
    #: shape fills it, which is what makes the budget a contract, not a hope.
    columns: tuple[MartColumn, ...] = ()

    @property
    def computed_count(self) -> int:
        return sum(1 for c in self.columns if c.classified and c.computed)

    @property
    def passthrough_count(self) -> int:
        return sum(
            1 for c in self.columns if c.kind is MartColumnKind.PASSTHROUGH
        )


def _outcome_sentence(description: str) -> str:
    """The sentence of a computed column's description that states what it
    IS ("It is date truncated to the start of its calendar day ..."), or the
    whole description when it has no such sentence."""
    text = " ".join(str(description or "").split())
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if sentence.startswith("It is ") or sentence.startswith("it is "):
            return sentence if sentence.endswith(".") else sentence + "."
    return text if text.endswith(".") or not text else text + "."


def _source_op_description(table: str, *, used: bool) -> str:
    """The SOURCE rule for `table`: read for extraction either way, and when
    no later rule of the mart matches or reads its rows, say so.

    Every closure table gets a SOURCE op so extraction is graded on all of
    them, but "Read source table campaign_history." beside three joins that
    never name it left the critic two readings — joined along the declared
    relationship (fan-out, dropped rows) or not joined at all (twitter_ads,
    batch10 2026-09-11). The transform's use of the table is stated.
    """
    if used:
        return f"Read source table {table}."
    return (
        f"Read source table {table} for extraction only: no rule of this mart "
        "matches or reads its rows, and it adds no rows and no columns to "
        "the mart."
    )


def build_star(
    *,
    mart: str,
    parent: str,
    parent_keys: tuple[str, ...],
    key_columns: tuple[str, ...],
    key_exprs: tuple[str, ...] = (),
    parent_carry: tuple[tuple[str, str], ...] = (),
    joins: tuple[StarJoin, ...] = (),
    measures: tuple[Measure, ...] = (),
    dedupe: tuple[str, tuple[str, ...]] = (),
    extra_sources: tuple[str, ...] = (),
    #: The columns of `parent` that are KEYS of it. When supplied, the grain is
    #: CHECKED against it; `None` is the only reason that check may be skipped.
    parent_key_columns: frozenset[str] | None = None,
    notes: str = "",
    grain_description: str = "",
) -> BuiltPlan:
    """Build the compiler-grade plan for a key + measures roll-up mart.

    `parent_keys` and `key_columns` are positionally aligned source and mart
    names. FAIL CLOSED ON A NON-KEY GRAIN: a `parent_keys` entry outside a
    supplied `parent_key_columns` RAISES, because the grain is projected without
    DISTINCT, so it fans out under the LEFT JOIN while GROUP BY hides the
    inflation.
    """
    if len(parent_keys) != len(key_columns):
        raise ValueError("parent_keys and key_columns must be positionally aligned")
    if key_exprs and len(key_exprs) != len(key_columns):
        raise ValueError("key_exprs must be positionally aligned with key_columns")
    if not parent_keys:
        raise ValueError(f"mart {mart!r}: a star plan needs at least one key column")
    if not measures:
        raise ValueError(
            f"mart {mart!r}: a star plan needs at least one measure "
            "(a keys-only mart is SELECT DISTINCT, not a task)"
        )
    if parent_key_columns is not None and not key_exprs:
        # Defence in depth behind the ingest-side refusal. RAISE rather than
        # DISTINCT the grain: DISTINCT fixes the arithmetic but silently changes
        # the mart's meaning from "one row per parent row" to "per distinct
        # label", and the inflated measures stay invisible in the row count.
        non_key = [c for c in parent_keys if c not in parent_key_columns]
        if non_key:
            fanned = ", ".join(
                f"{j.table} on ({', '.join(l for l, _ in j.on_pairs)})" for j in joins
            ) or "no joined table"
            raise ValueError(
                f"mart {mart!r}: grain column(s) {non_key} are not a key of "
                f"{parent!r} (keys: {sorted(parent_key_columns)}), so the grain "
                f"projection duplicates and every measure over [{fanned}] is "
                "inflated. Refusing: a star mart grains on a key, and DISTINCTing "
                "the grain would silently change the mart from 'one row per "
                f"{parent} row' to 'one row per distinct value'."
            )

    taken = {parent, *(j.table for j in joins), *extra_sources}
    base_rel = _unique("mart_base", taken)
    agg_rel = _unique("mart_grouped", taken)
    out_rel = _unique("mart_final", taken)

    # SOURCE ops name EVERY table in scope, even ones no op reads: extraction is
    # graded on all of them, and an unnamed table is one a solver skips for free.
    in_scope: list[str] = [parent] + [j.table for j in joins]
    used: set[str] = set(in_scope)
    in_scope += [t for t in extra_sources if t not in in_scope]
    ops: list[MartOp] = [
        MartOp(
            kind=MartOpKind.SOURCE,
            description=_source_op_description(t, used=t in used),
            tables=(t,),
        )
        for t in in_scope
    ]
    if dedupe:
        dedupe_table, dedupe_columns = dedupe
        # Descriptions are checkable contract text: the author must restate them
        # and operator vocabulary is banned there, so state the OUTCOME only.
        # (Governs every `description=` in this module.)
        ops.append(
            MartOp(
                kind=MartOpKind.DEDUPE,
                description=(
                    f"{dedupe_table} declares no primary key upstream, so "
                    "byte-identical duplicate rows can occur; every such row "
                    "counts ONCE, however many copies arrive."
                ),
                tables=(dedupe_table,),
                columns=tuple(dedupe_columns),
            )
        )

    # base: the grain, already wearing its mart column names.
    key_projection = [
        (key_exprs[i] if key_exprs else f"{relation_identifier(parent)}.{quote(src)}")
        for i, src in enumerate(parent_keys)
    ]
    base_select = ", ".join(
        [
            f"{expr} AS {quote(dst)}"
            for expr, dst in zip(key_projection, key_columns)
        ]
        + [
            f"{relation_identifier(parent)}.{quote(src)} AS {quote(alias)}"
            for src, alias in parent_carry
        ]
    )
    star_grain = grain_description or (
        f"Form the mart key columns {', '.join(key_columns)} from "
        f"source table {parent}."
    )
    if not grain_description and not key_exprs:
        renamed = [
            (src, dst) for src, dst in zip(parent_keys, key_columns) if src != dst
        ]
        if renamed:
            star_grain += " " + " ".join(
                f"{dst} is the value of the {src} column of {parent}."
                for src, dst in renamed
            )
    ops.append(
        MartOp(
            kind=MartOpKind.DERIVE,
            description=star_grain,
            tables=(parent,),
            columns=tuple(key_columns),
            details={"select": base_select, "name": base_rel},
        )
    )

    carried: list[tuple[str, str]] = [(base_rel, k) for k in key_columns] + [
        (base_rel, alias) for _, alias in parent_carry
    ]
    current = base_rel
    for i, join in enumerate(joins, start=1):
        step = _unique(f"mart_joined_{i}", taken)
        predicate = " AND ".join(
            f"{relation_identifier(join.table)}.{quote(right)} = "
            f"{relation_identifier(current)}.{quote(left)}"
            for left, right in join.on_pairs
        )
        select = ", ".join(
            [
                f"{relation_identifier(rel)}.{quote(alias)} AS {quote(alias)}"
                for rel, alias in carried
            ]
            + [
                f"{relation_identifier(join.table)}.{quote(col)} AS {quote(alias)}"
                for col, alias in join.carry
            ]
        )
        ops.append(
            MartOp(
                kind=MartOpKind.JOIN,
                # Stated as an OUTCOME ("RETAINED"): naming 'inner' here would
                # put both halves of a mutually-exclusive term pair in the rule.
                # The caution — an INNER join drops unmatched parent rows — lives
                # in this comment; join_type/predicate carry the mechanics.
                description=(
                    join.description
                    or (
                        f"Bring in {join.table} against the grain: {parent} rows "
                        f"with no matching {join.table} rows are RETAINED and "
                        "report the declared defaults."
                    )
                ),
                tables=(current, join.table),
                columns=tuple(join.rel_columns),
                join_type=JoinType.LEFT,
                predicate=predicate,
                details={"select": select, "name": step},
            )
        )
        carried = [(step, alias) for _, alias in carried] + [
            (step, alias) for _, alias in join.carry
        ]
        current = step

    group_by = ", ".join(quote(k) for k in key_columns)
    agg_details: dict[str, str] = {"group_by": group_by, "name": agg_rel}
    internal: list[tuple[str, Measure]] = []
    for i, measure in enumerate(measures):
        alias = f"m_{i}"
        agg_details[alias] = measure.expr
        internal.append((alias, measure))
    # Keep parent-carried values separate from child summaries: join repetition
    # must not make an unmatched parent's own value look missing.
    _parent_alias_source = {alias: src for src, alias in parent_carry}
    _join_aliases = {alias for j in joins for _, alias in j.carry}
    group_measures: list[Measure] = []
    carried_measures: list[tuple[Measure, str]] = []
    for _, _m in internal:
        _refs = set(_QUOTED_NAME_RE.findall(_m.expr))
        _parent_refs = _refs & set(_parent_alias_source)
        if _parent_refs and not (_refs & _join_aliases):
            carried_measures.append(
                (_m, _parent_alias_source[sorted(_parent_refs)[0]])
            )
        else:
            group_measures.append(_m)
    _grain = ", ".join(key_columns)
    _agg_sentences = [
        (
            f"One output row per {_grain}, reporting "
            + ", ".join(m.column for m in group_measures)
            + " for that row's matching rows."
        )
        if group_measures
        else f"One output row per {_grain}."
    ]
    _an = _article(parent, capitalized=True)
    for _m, _src in carried_measures:
        _agg_sentences.append(
            f"{_m.column} is taken from the {parent} side only: its value "
            f"comes from {_src} on the {parent} rows, never from the matching "
            f"rows. {_an} {parent} row with no matching rows still reports "
            f"whatever {_src} holds on the {parent} side."
        )
    # Measure expressions stay in `agg_details`; the description uses mart-column
    # names only, or the checkable rule would demand internal aliases verbatim.
    ops.append(
        MartOp(
            kind=MartOpKind.AGGREGATE,
            description=" ".join(_agg_sentences),
            tables=(current,),
            columns=tuple(key_columns) + tuple(m.column for _, m in internal),
            details=agg_details,
        )
    )

    null_capable = tuple(m.column for _, m in internal if m.null_default is not None)
    final_select = ", ".join(
        [f"{quote(k)} AS {quote(k)}" for k in key_columns]
        + [
            (
                f"COALESCE({alias}, {m.null_default}) AS {quote(m.column)}"
                if m.null_default is not None
                else f"{alias} AS {quote(m.column)}"
            )
            for alias, m in internal
        ]
    )
    # The COALESCE lives in `final_select` (what attack_surface reads for
    # no_null_default); the description states its outcome only.
    ops.append(
        MartOp(
            kind=MartOpKind.DERIVE,
            description=(
                "Name the mart columns"
                + (
                    f"; {', '.join(null_capable)} report a default value — never "
                    f"NULL — for a {parent} row with no matching rows and for a "
                    f"{parent} row none of whose matching rows carries a value."
                    if null_capable
                    else "."
                )
            ),
            tables=(agg_rel,),
            columns=tuple(key_columns) + tuple(m.column for _, m in internal),
            details={"select": final_select, "name": out_rel},
        )
    )
    ops.append(
        MartOp(
            kind=MartOpKind.TIE_BREAK,
            # "total order BY <col>" is a by-clause the declarative gate bans.
            description=f"Deterministic output order: sort by {', '.join(key_columns)}.",
            columns=tuple(key_columns),
        )
    )

    first_join = joins[0] if joins else None
    shape = StarShape(
        mart=mart,
        parent=parent,
        parent_keys=tuple(parent_keys),
        key_columns=tuple(key_columns),
        fact=first_join.table if first_join is not None else "",
        fact_link_columns=(
            tuple(right for _, right in first_join.on_pairs)
            if first_join is not None
            else ()
        ),
        fact_dedupe=bool(dedupe),
        null_capable_measures=null_capable,
        has_join=bool(joins),
    )
    return BuiltPlan(plan=MartPlan(mart=mart, ops=tuple(ops), notes=notes), shape=shape)


def build_projection(
    *,
    mart: str,
    table: str,
    select_map: tuple[tuple[str, str], ...],
    key_columns: tuple[str, ...],
    extra_sources: tuple[str, ...] = (),
    notes: str = "",
    description: str = "",
) -> BuiltPlan:
    """Build the compiler-grade plan for a one-row-per-source-row mart.

    `select_map` is (source column, mart column) in mart column order. Used by
    pools whose mart IS a rename/cast projection of one extracted table (the
    dlt entity dimensions, the dbt staging-grain marts).
    """
    if not select_map:
        raise ValueError(f"mart {mart!r}: a projection needs at least one column")
    taken = {table, *extra_sources}
    out_rel = _unique("mart_final", taken)
    select = ", ".join(
        f"{relation_identifier(table)}.{quote(src)} AS {quote(dst)}"
        for src, dst in select_map
    )
    mart_columns = tuple(dst for _, dst in select_map)
    in_scope = [table] + [t for t in extra_sources if t != table]
    ops = tuple(
        MartOp(
            kind=MartOpKind.SOURCE,
            description=f"Read source table {t}.",
            tables=(t,),
        )
        for t in in_scope
    ) + (
        MartOp(
            kind=MartOpKind.DERIVE,
            description=(
                description
                or f"Project {table} to the mart contract: {', '.join(mart_columns)}."
            ),
            tables=(table,),
            columns=mart_columns,
            details={"select": select, "name": out_rel},
        ),
        MartOp(
            kind=MartOpKind.TIE_BREAK,
            description=f"Deterministic output order: sort by {', '.join(key_columns)}.",
            columns=tuple(key_columns),
        ),
    )
    shape = StarShape(
        mart=mart,
        parent=table,
        parent_keys=tuple(
            src for src, dst in select_map if dst in set(key_columns)
        ),
        key_columns=tuple(key_columns),
    )
    return BuiltPlan(plan=MartPlan(mart=mart, ops=ops, notes=notes), shape=shape)


# --- Op constructors: the vocabulary plan shapes are assembled from ---
# Constructors, not raw MartOp(...): each construct has exactly ONE determinizing
# spelling, and these re-run `op_problems` on their own output so a malformed op
# cannot leave this module.

def filtered_count_expr(alias: str, predicate: str, *, distinct: bool = False) -> str:
    """``COUNT([DISTINCT] CASE WHEN <pred> THEN <alias> END)``.

    Predicate lives INSIDE the aggregate: a WHERE drops the zero row.
    """
    inner = f"CASE WHEN {predicate} THEN {quote(alias)} END"
    return f"COUNT(DISTINCT {inner})" if distinct else f"COUNT({inner})"


def filtered_sum_expr(alias: str, predicate: str, *, zero: str = "0") -> str:
    """``SUM(CASE WHEN <pred> THEN <alias> ELSE <zero> END)`` — ELSE mandatory."""
    return f"SUM(CASE WHEN {predicate} THEN {quote(alias)} ELSE {zero} END)"


def distinct_count_expr(alias: str) -> str:
    """``COUNT(DISTINCT <alias>)`` — the fan-out-sensitive count.

    Over a two-hop join it is the only count that disagrees with COUNT(x), which
    is what makes `no_dedup` killable where every source table declares a PK.
    """
    return f"COUNT(DISTINCT {quote(alias)})"


def ratio_expr(
    numerator: str,
    denominator: str,
    *,
    units: str = "fraction",
    digits: int = 4,
    null_result: str = "0.0",
) -> str:
    """Guarded division: CAST, NULLIF, explicit scale, explicit rounding.

    Every guard is load-bearing: no CAST truncates integer operands to 0, no
    NULLIF divides by zero on a childless parent, no ROUND leaves precision to
    the engine, and an unstated scale puts gold and solver exactly 100x apart.
    """
    if units not in ("fraction", "percent"):
        raise ValueError("ratio units must be 'fraction' or 'percent'")
    scaled = "" if units == "fraction" else " * 100"
    quotient = (
        f"CAST({quote(numerator)} AS DOUBLE) / NULLIF({quote(denominator)}, 0){scaled}"
    )
    return f"ROUND(COALESCE({quotient}, {null_result}), {digits})"


def case_map_expr(
    column: str, mapping: tuple[tuple[str, str], ...], *, otherwise: str
) -> str:
    """Categorical CASE over a DECLARED domain, with a mandatory ELSE.

    `otherwise` is not optional: without it an out-of-domain value silently
    becomes NULL, observable only against counterfactual row G.
    """
    if not mapping:
        raise ValueError("a categorical mapping needs at least one branch")
    branches = " ".join(
        f"WHEN {_sql_literal(value)} THEN {_sql_literal(label)}" for value, label in mapping
    )
    return f"CASE {quote(column)} {branches} ELSE {_sql_literal(otherwise)} END"


#: A comparison of a ladder input against a numeric literal: the only place a
#: band boundary lives. Equality tests (`= 0 THEN 'none'`) name a single value,
#: not a boundary between two bands, and are not matched as thresholds.
_LADDER_BOUNDARY_RE = re.compile(r"(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)(?![\d.])")
_LADDER_ARM_RE = re.compile(
    r"WHEN\s+(.+?)\s+THEN\s+'((?:[^']|'')*)'", re.IGNORECASE | re.DOTALL
)
_LADDER_ELSE_RE = re.compile(r"ELSE\s+'((?:[^']|'')*)'\s*END", re.IGNORECASE)
_LADDER_TEST_RE = re.compile(r"(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?)(?![\d.])")


def _ladder_arm_at(condition: str, value: float) -> bool | None:
    """Does this arm's condition hold for a ladder input EXACTLY at `value`?
    None when the arm compares against something other than one numeric
    literal (a column, a string), which no boundary value decides."""
    tests = _LADDER_TEST_RE.findall(condition)
    if len(tests) != 1:
        return None
    op, literal = tests[0]
    other = float(literal)
    return {
        ">=": value >= other, "<=": value <= other, ">": value > other,
        "<": value < other, "=": value == other,
    }[op]


def _ladder_boundary_statement(expression: str) -> str:
    """Describe the boundary behavior of a SQL CASE ladder.

    Evaluate arms in order at every numeric threshold and name the selected
    band. This states inclusive and exclusive boundaries without ambiguous
    lower/upper wording. A ladder without numeric literals is categorical and
    has no boundary statement.
    """
    text = str(expression)
    arms = [(cond, label.replace("''", "'")) for cond, label in _LADDER_ARM_RE.findall(text)]
    default = _LADDER_ELSE_RE.search(text)
    else_label = default.group(1).replace("''", "'") if default else None
    seen: dict[str, str] = {}
    for _comparison, literal in _LADDER_BOUNDARY_RE.findall(text):
        value = float(literal)
        key = f"{value:g}"
        if key in seen:
            continue
        band = else_label
        undecidable = False
        for condition, label in arms:
            holds = _ladder_arm_at(condition, value)
            if holds is None:
                undecidable = True
                break
            if holds:
                band = label
                break
        if undecidable or band is None:
            continue
        seen[key] = band
    if not seen:
        return "categorical mapping; no numeric boundary"
    return "; ".join(
        f"a value exactly at {value} is '{band}'" for value, band in seen.items()
    )


def threshold_ladder_expr(
    expression: str, thresholds: tuple[tuple[str, str], ...], *, otherwise: str
) -> str:
    """Ordered threshold ladder with a mandatory ELSE.

    `thresholds` are (comparison, label) in evaluation order. The comparison is
    written out because inclusive-vs-exclusive IS the boundary attack: `>` and
    `>=` differ on exactly one row (counterfactual row H).
    """
    if not thresholds:
        raise ValueError("a threshold ladder needs at least one branch")
    branches = " ".join(
        f"WHEN {expression} {comparison} THEN {_sql_literal(label)}"
        for comparison, label in thresholds
    )
    return f"CASE {branches} ELSE {_sql_literal(otherwise)} END"


def running_total_expr(
    measure: str, partition_by: tuple[str, ...], order_by: tuple[str, ...]
) -> str:
    """Running total with an EXPLICIT frame.

    Mandatory: the RANGE default lumps together every row with an equal ORDER BY
    key, so the total is wrong exactly on ties and only on ties.
    """
    if not order_by:
        raise ValueError("a running total needs an ORDER BY (a total order)")
    partition = (
        f"PARTITION BY {', '.join(quote(c) for c in partition_by)} " if partition_by else ""
    )
    order = ", ".join(f"{quote(c)} ASC" for c in order_by)
    return (
        f"SUM({quote(measure)}) OVER ({partition}ORDER BY {order} "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
    )


def lag_delta_expr(
    measure: str,
    partition_by: tuple[str, ...],
    order_by: tuple[str, ...],
    *,
    first_row_value: str = "0",
) -> str:
    """Period-over-period delta via LAG, with the first row's value pinned.

    No frame clause: DuckDB rejects one on LAG. The ORDER BY is still required
    — a LAG without one is a coin flip.
    """
    if not order_by:
        raise ValueError("a LAG delta needs an ORDER BY (a total order)")
    partition = (
        f"PARTITION BY {', '.join(quote(c) for c in partition_by)} " if partition_by else ""
    )
    order = ", ".join(f"{quote(c)} ASC" for c in order_by)
    lag = f"LAG({quote(measure)}) OVER ({partition}ORDER BY {order})"
    return f"{quote(measure)} - COALESCE({lag}, {first_row_value})"


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _select_list(projections: tuple[tuple[str, str], ...]) -> str:
    """``<expr> AS "<alias>"`` list, in the order given (deterministic).

    An EMPTY alias emits the expression verbatim (a carried fragment already
    wears its aliases); such columns stay out of the op's `columns`, so the op
    claims only what it computes.
    """
    return ", ".join(
        (f"{expr} AS {quote(alias)}" if alias else expr) for expr, alias in projections
    )


def _computed_aliases(projections: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    return tuple(alias for _, alias in projections if alias)


def _certified(op: MartOp) -> MartOp:
    """Fail closed at CONSTRUCTION: the compiler's contract PLUS an honest label."""
    problems = op_problems(op) + label_problems(op)
    if problems:
        raise ValueError(
            f"{op.kind.value} op does not satisfy its contract: " + "; ".join(problems)
        )
    return op


def infer_aggregate_kind(measure_exprs: tuple[str, ...]) -> MartOpKind:
    """The honest aggregate-family label for a set of measure expressions
    (precedence filtered > distinct > plain).

    Exposed so every builder labels its aggregate op without duplicating the AST
    rules; `structural_features` dispatches on op.kind, so a wrong label hides
    the construct entirely.
    """
    if any(is_filtered_aggregate_expr(e) for e in measure_exprs):
        return MartOpKind.FILTERED_AGGREGATE
    if any(is_distinct_aggregate_expr(e) for e in measure_exprs):
        return MartOpKind.DISTINCT
    return MartOpKind.AGGREGATE


def group_by_op(
    *,
    source: str,
    name: str,
    group_by: tuple[str, ...],
    measures: tuple[tuple[str, str, str], ...],
    predicate: str = "",
    description: str = "",
) -> MartOp:
    """One GROUP BY op whose KIND is INFERRED from its measures.

    `measures` are (internal alias, mart column, expression). Inferred, not
    passed, because `attack_surface` routes on the kind: a hand-written label
    can lie about whether a mutant has anything to bite on.
    """
    if not group_by:
        raise ValueError("a group-by op needs at least one grouping column")
    if not measures:
        raise ValueError("a group-by op needs at least one measure")
    kind = infer_aggregate_kind(tuple(expr for _, _, expr in measures))
    details = {"group_by": ", ".join(quote(c) for c in group_by), "name": name}
    for alias, _, expr in measures:
        details[alias] = expr
    if kind is MartOpKind.FILTERED_AGGREGATE:
        if not predicate:
            raise ValueError(
                "a filtered aggregate must state its predicate: the solver has to be "
                "told which rows count, and the certifier quotes it in the prose"
            )
        # The EXACT scope rule (op_problems can only enforce the necessary
        # condition): with the alias -> mart-column map in hand, a predicate
        # governing SOME measures must name EVERY one it governs.
        guarded = [
            column for _, column, expr in measures if is_filtered_aggregate_expr(expr)
        ]
        unguarded = [
            column
            for _, column, expr in measures
            if not is_filtered_aggregate_expr(expr)
        ]
        unnamed = [c for c in guarded if not predicate_governs(predicate, c)]
        if unguarded and unnamed:
            raise ValueError(
                f"the filtered aggregate leaves {unguarded} unguarded, so its "
                f"predicate governs only some of its measures, but the stated "
                f"predicate {predicate!r} does not name {unnamed}. State the SCOPE "
                "(which measures count only qualifying rows) or guard every measure "
                "— an unscoped predicate is documentation the compiled SQL does not "
                "implement."
            )
    return _certified(
        MartOp(
            kind=kind,
            # Default description is an OUTCOME in mart-column names; the
            # expressions live in `details` (see build_rollup's aggregate op).
            description=(
                description
                or (
                    f"One output row per {', '.join(group_by)}, reporting "
                    + ", ".join(column for _, column, _ in measures)
                    + " for that row's matching rows."
                )
            ),
            tables=(source,),
            columns=tuple(group_by) + tuple(column for _, column, _ in measures),
            predicate=predicate,
            details=details,
        )
    )


def extrema_op(
    *,
    source: str,
    name: str,
    partition_by: tuple[str, ...],
    measure: str,
    tie_break: str,
    projections: tuple[tuple[str, str], ...],
    direction: str = "DESC",
    tie_break_direction: str = "ASC",
    description: str = "",
    tie_break_is_text: bool = False,
) -> MartOp:
    """ARGMAX: project attributes OF the row at which `measure` is extremal.

    QUALIFY ROW_NUMBER(), never ``WHERE rn=1``: the DROPPED_FILTER mutant strips
    every Where, which would conflate the two attacks into a false kill.
    `tie_break_is_text` makes the description spell the collation, since
    "smallest" over text is session-dependent otherwise.
    """
    if not partition_by:
        raise ValueError("an extremum needs a PARTITION BY, or it is global")
    if not tie_break:
        raise ValueError("an extremum needs a declared tie-break column")
    # NULLS LAST written out, never inherited from `default_null_order`: one
    # policy in both directions (no value ranks last), so max and min agree.
    order_by = (
        f"{quote(measure)} {direction.upper()} NULLS LAST, "
        f"{quote(tie_break)} {tie_break_direction.upper()} NULLS LAST"
    )
    return _certified(
        MartOp(
            kind=MartOpKind.EXTREMA,
            description=(
                description
                or (
                    f"Keep, per {', '.join(partition_by)}, the single row with the "
                    f"{'largest' if direction.upper() == 'DESC' else 'smallest'} "
                    f"{measure}, breaking ties by {tie_break} "
                    f"{'ascending' if tie_break_direction.upper() == 'ASC' else 'descending'}"
                    + (f" ({TEXT_ORDER_PROSE})" if tie_break_is_text else "")
                    + f", and project {', '.join(alias for _, alias in projections)}."
                )
            ),
            tables=(source,),
            columns=_computed_aliases(projections),
            details={
                "select": _select_list(projections),
                "partition_by": ", ".join(quote(c) for c in partition_by),
                "order_by": order_by,
                "tie_break": tie_break,
                "name": name,
            },
        )
    )


def window_op(
    *,
    source: str,
    name: str,
    projections: tuple[tuple[str, str], ...],
    description: str,
) -> MartOp:
    """A projection carrying at least one OVER() expression.

    Build the expressions with `running_total_expr` / `lag_delta_expr`: they
    carry the explicit frame and ORDER BY this op's contract requires.
    """
    return _certified(
        MartOp(
            kind=MartOpKind.WINDOW,
            description=description,
            tables=(source,),
            columns=_computed_aliases(projections),
            details={"select": _select_list(projections), "name": name},
        )
    )


def conditional_op(
    *,
    source: str,
    name: str,
    projections: tuple[tuple[str, str], ...],
    domain: str,
    description: str,
) -> MartOp:
    """A projection carrying at least one CASE ladder, every one with an ELSE.

    `domain` records the DECLARED value domain the ladder was built from. Fail
    closed: an invented domain has no out-of-domain witness, so its ELSE is
    unfalsifiable — no declared domain, no ladder.
    """
    if not domain.strip():
        raise ValueError(
            "a conditional op must record the declared domain it maps; an "
            "invented domain has no out-of-domain witness"
        )
    return _certified(
        MartOp(
            kind=MartOpKind.CONDITIONAL,
            description=description,
            tables=(source,),
            columns=_computed_aliases(projections),
            details={
                "select": _select_list(projections),
                "domain": domain,
                "name": name,
            },
        )
    )


def ratio_op(
    *,
    source: str,
    name: str,
    projections: tuple[tuple[str, str], ...],
    units: str,
    null_result: str,
    rounding: str,
    description: str,
) -> MartOp:
    """A projection carrying at least one guarded ratio.

    `units`, `null_result` and `rounding` are REQUIRED — the certifier re-derives
    the prose from them, and an unstated scale manufactures answer-key bugs.
    """
    return _certified(
        MartOp(
            kind=MartOpKind.RATIO,
            description=description,
            tables=(source,),
            columns=_computed_aliases(projections),
            details={
                "select": _select_list(projections),
                "units": units,
                "null_result": null_result,
                "rounding": rounding,
                "name": name,
            },
        )
    )


# Plan-library shapes require witness rows for every wrong implementation.
# A fan-out on the second join hop makes wrong-grain variants observable.

#: Deterministic internal alias for the i-th aggregate measure.
def _measure_alias(index: int) -> str:
    return f"m_{index}"


def _fmt(expr: str, names: dict[str, str], *, where: str) -> str:
    """Substitute ``{mart_column}`` placeholders with quoted identifiers.

    An unknown placeholder is a hard error: a surviving ``{typo}`` would compile
    to invalid SQL at gold time, long after the plan validated.
    """
    out: list[str] = []
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "{":
            end = expr.find("}", i)
            if end < 0:
                raise ValueError(f"{where}: unterminated '{{' in expression {expr!r}")
            key = expr[i + 1:end]
            if key not in names:
                raise ValueError(
                    f"{where}: expression {expr!r} names {key!r}, which is not a "
                    f"column of this mart (known: {sorted(names)})"
                )
            out.append(names[key])
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


#: Which FactRoles fields each witness row needs before it can be constructed.
#: Fail closed: `build_rollup` refuses a shape declaring a witness whose
#: construction columns it never named; populations.py applies the same
#: requirements before activating the schema-conditional row L.
_WITNESS_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    WITNESS_CONTROL: ("link_key",),
    WITNESS_CHILDLESS: (),
    WITNESS_DUPLICATE: ("link_key",),
    WITNESS_SAME_CHILD: ("child_key", "child_primary_key"),
    WITNESS_ALL_FAIL: ("predicate_column", "predicate_pass", "predicate_fail"),
    WITNESS_TIE: ("measure", "label"),
    WITNESS_OUT_OF_DOMAIN: ("domain_column", "domain", "out_of_domain"),
    # Built by giving one parent EXACTLY the boundary number of bridge rows, so
    # only the link column is needed; the boundary rides on StarShape.thresholds.
    WITNESS_ON_THRESHOLD: ("link_key",),
    WITNESS_BRIDGE_NO_CHILD: ("child_key", "child_primary_key"),
    WITNESS_SECOND_PERIOD: ("period_column",),
    # Built with one fewer bridge row than the first declared threshold. This
    # is the discriminator for a HAVING-style aggregate filter moved onto raw
    # input rows: presence is true, but the grouped threshold is false.
    WITNESS_BELOW_THRESHOLD: ("link_key",),
    # Schema-conditional: populations.py appends this witness only when the
    # bridge measure column is actually nullable.  A real link key distinguishes
    # the all-NULL group from row B's LEFT-join placeholder.
    WITNESS_NULL_MEASURE: ("link_key", "measure"),
    # Schema-conditional argmax boundaries. populations.py activates these
    # only for an exact bridge-side TEXT label (and, for the NULL-order arm,
    # only when a real NULL is legal under the table/relationship contract).
    WITNESS_ARGMAX_CASE_ORDER: ("link_key", "measure", "label"),
    WITNESS_ARGMAX_NULL_ORDER: ("link_key", "measure", "label"),
    # Two real rows in one group carry the same non-NULL measure while their
    # link keys remain distinct. This is the direct COUNT(DISTINCT measure)
    # versus COUNT(measure) discriminator for one-hop shapes.
    WITNESS_DISTINCT_MEASURE: ("link_key", "measure"),
    # The period-ordered mirror of row A (see WITNESS_LATEST_FIRST).
    WITNESS_LATEST_FIRST: ("period_column",),
}


def distinct_source_problems(
    *,
    keys: tuple[KeyColumn, ...],
    passthrough: tuple[Passthrough, ...],
    hops: tuple[StarJoin, ...],
) -> list[str]:
    """Every MART-VISIBLE column a rollup would draw twice from one source.

    Each source column may feed at most one mart column, or the mart clears its
    budget by telling the solver nothing twice. INTERNAL aliases (`parent_carry`,
    a hop's `carry`) and derived keys are exempt: they never become mart columns.
    """
    problems: list[str] = []
    base: dict[str, list[str]] = {}
    for k in keys:
        if k.source and not k.expr:
            base.setdefault(k.source, []).append(k.column)
    for p in passthrough:
        if not p.from_hop:
            base.setdefault(p.source or p.column, []).append(p.column)
    for src, names in base.items():
        if len(names) > 1:
            problems.append(f"parent column {src!r} projected as {names}")
    hop_carry = {hop.table: dict((alias, src) for src, alias in hop.carry) for hop in hops}
    hop_side: dict[tuple[str, str], list[str]] = {}
    for p in passthrough:
        if p.from_hop:
            src = hop_carry.get(p.from_hop, {}).get(p.column, p.source or p.column)
            hop_side.setdefault((p.from_hop, src), []).append(p.column)
    for (table, src), names in hop_side.items():
        if len(names) > 1:
            problems.append(f"hop {table!r} column {src!r} projected as {names}")
    return problems


def _witness_problems(
    witnesses: tuple[str, ...], roles: FactRoles, *, fact: str, child: str
) -> list[str]:
    problems: list[str] = []
    for witness in witnesses:
        if witness not in _WITNESS_REQUIREMENTS:
            problems.append(f"unknown witness {witness!r}")
            continue
        if witness not in (WITNESS_CHILDLESS, WITNESS_OUT_OF_DOMAIN) and not fact:
            problems.append(f"witness {witness} needs a bridge table, but none is joined")
        for attr in _WITNESS_REQUIREMENTS[witness]:
            if not getattr(roles, attr):
                problems.append(
                    f"witness {witness} needs FactRoles.{attr}, which the shape "
                    "never named — the row would be unconstructible"
                )
        if witness in (WITNESS_SAME_CHILD, WITNESS_BRIDGE_NO_CHILD) and not child:
            problems.append(
                f"witness {witness} needs a SECOND hop (the fan-out child); this "
                "shape joins only one table, so COUNT and COUNT(DISTINCT) agree"
            )
    return problems


def build_rollup(
    *,
    mart: str,
    shape_name: str,
    parent: str,
    keys: tuple[KeyColumn, ...],
    passthrough: tuple[Passthrough, ...] = (),
    #: Every passthrough column takes ONE value per key (an attribute of the
    #: single source row a key value identifies), so the grouping never splits
    #: a key. The plan-library shapes say True; a compiler-derived rollup
    #: keeps False and states the literal GROUP BY semantics instead.
    carried_per_key: bool = False,
    #: (parent column, alias) pairs carried into the joined relation for the
    #: MEASURES to reference. Not mart columns and not part of the grain — the
    #: row-level values a shape whose base table IS the fact table needs.
    parent_carry: tuple[tuple[str, str], ...] = (),
    hops: tuple[StarJoin, ...] = (),
    windows: tuple[WindowExpr, ...] = (),
    measures: tuple[Measure, ...] = (),
    #: ARGMAX columns, evaluated on the JOINED rows and LEFT JOINed back onto the
    #: grouped relation — the one branch making the plan a DAG, because an argmax
    #: needs the rows the GROUP BY collapsed.
    extrema: tuple[Extremum, ...] = (),
    extrema_order_by: str = "",
    extrema_tie_break: str = "",
    #: How the description names the tie-break, in the task's REAL column
    #: vocabulary. `extrema_tie_break` is a plan-internal alias the compiler
    #: needs, and prose must never quote it — it names nothing the solver sees.
    extrema_tie_break_prose: str = "",
    #: Whether the leading extrema ordering value can be missing on a REAL row.
    #: False specializes prose only; SQL retains explicit NULL placement and
    #: empty LEFT-join groups retain their defaults.
    extrema_order_nullable: bool | None = None,
    post_windows: tuple[WindowExpr, ...] = (),
    derived: tuple[Derived, ...] = (),
    dedupe: tuple = (),
    extra_sources: tuple[str, ...] = (),
    roles: FactRoles = FactRoles(),
    #: Attack cases this shape's WITNESSES justify (``<kind>[@<variant>]``).
    #: `derive_attack_cases` declares exactly these — a claim the witnesses
    #: cannot support is a surface nothing can realize.
    attack_claims: tuple[str, ...] = (),
    #: Explicit prose/SQL predicate for a FILTERED_AGGREGATE op. Defaults to
    #: the ``<predicate_column> IN (<predicate_pass>)`` the roles describe.
    aggregate_predicate: str = "",
    witnesses: tuple[str, ...] = (),
    thresholds: tuple[float, ...] = (),
    child: str = "",
    child_link_pairs: tuple[tuple[str, str], ...] = (),
    #: (anchor table, anchor key, bridge table, bridge FK) for witness rows.
    #: Defaults to the parent/first-hop pair, which is right for every shape
    #: whose base table IS the dimension.
    witness_anchor: tuple[str, str, str, str] | None = None,
    notes: str = "",
    grain_description: str = "",
    enforce_budget: bool = True,
) -> BuiltPlan:
    """The ONE core every plan-library shape is built from; every op it emits
    passes `op_problems`, so a built plan always compiles.

    Three fail-closed refusals: an unconstructible witness or an uncertified
    column kind raises ValueError (a builder bug, which must PROPAGATE rather
    than silently shrink the mart list); a mart under the column minima raises
    `MartBudgetError`; a duplicated source column raises `ShapeNotSelectable`.
    """
    if not keys:
        raise ValueError(f"mart {mart!r}: a rollup needs at least one grain column")
    if not measures:
        raise ValueError(
            f"mart {mart!r}: a rollup needs at least one measure "
            "(a keys-only mart is SELECT DISTINCT, not a task)"
        )

    fact = hops[0].table if hops else ""
    witness_problems = _witness_problems(witnesses, roles, fact=fact, child=child)
    if witness_problems:
        raise ValueError(
            f"mart {mart!r}: unconstructible witnesses — " + "; ".join(witness_problems)
        )
    # NO CONSTRUCT SHIPS WITHOUT ITS WITNESS, applied to the claim itself: a
    # wrong_window claim needs a winner-first mirror of row A.
    mirror_problem = wrong_window_mirror_problem(attack_claims, witnesses)
    if mirror_problem:
        raise ValueError(f"mart {mart!r}: unbacked attack claim — {mirror_problem}")
    collisions = distinct_source_problems(keys=keys, passthrough=passthrough, hops=hops)
    if collisions:
        raise ShapeNotSelectable(
            f"mart {mart!r}: one source column under two names — "
            + "; ".join(collisions)
            + " (not selectable: the chain's roles coincide)"
        )

    taken = {parent, *(h.table for h in hops), *extra_sources}
    base_rel = _unique("mart_base", taken)
    ranked_rel = _unique("mart_ranked", taken)
    agg_rel = _unique("mart_grouped", taken)
    post_rel = _unique("mart_windowed", taken)
    named_rel = _unique("mart_named", taken)
    ratio_rel = _unique("mart_ratio", taken)
    final_rel = _unique("mart_final", taken)

    key_columns = tuple(k.column for k in keys)
    passthrough_columns = tuple(p.column for p in passthrough)
    grain_columns = key_columns + passthrough_columns

    # -- ops ---------------------------------------------------------------
    in_scope: list[str] = [parent] + [h.table for h in hops]
    used: set[str] = set(in_scope)
    in_scope += [t for t in extra_sources if t not in in_scope]
    ops: list[MartOp] = [
        MartOp(
            kind=MartOpKind.SOURCE,
            description=_source_op_description(t, used=t in used),
            tables=(t,),
        )
        for t in in_scope
    ]
    if dedupe:
        dedupe_table, dedupe_columns = dedupe
        # OUTCOME wording ("counts ONCE"), not the SELECT DISTINCT mechanics:
        # operator words are banned in checkable description text. The op KIND
        # is what routes the no_dedup mutant.
        ops.append(
            MartOp(
                kind=MartOpKind.DEDUPE,
                description=(
                    f"{dedupe_table} declares no primary key upstream, so "
                    "byte-identical duplicate rows can occur; every such row "
                    "counts ONCE, however many copies arrive."
                ),
                tables=(dedupe_table,),
                columns=tuple(dedupe_columns),
            )
        )

    base_projection = [
        (
            f"{k.expr} AS {quote(k.column)}"
            if k.expr
            else f"{relation_identifier(parent)}.{quote(k.source or k.column)} "
            f"AS {quote(k.column)}"
        )
        for k in keys
    ] + [
        f"{relation_identifier(parent)}.{quote(p.source or p.column)} AS {quote(p.column)}"
        for p in passthrough
        if not p.from_hop
    ] + [
        f"{relation_identifier(parent)}.{quote(src)} AS {quote(alias)}"
        for src, alias in parent_carry
    ]
    # A COMPUTED key is not "formed from the source table's own values": the
    # feasibility reviewer read "date_day ... from source table
    # promoted_tweet_report" against a schema that publishes only `date`
    # and found no rule deriving one from the other (twitter_ads, batch10
    # 2026-09-11). The key's own outcome sentence is stated in the rule.
    computed_keys = [k for k in keys if k.expr]
    grain_text = grain_description or (
        f"Form the mart key columns {', '.join(key_columns)} from "
        f"source table {parent}."
    )
    if computed_keys and not grain_description:
        grain_text += " " + " ".join(
            f"{k.column} is not copied from a {parent} column but computed "
            f"from {parent}: {_outcome_sentence(k.description)}"
            for k in computed_keys
        )
    # A key copied under ANOTHER NAME ("segment" published as `keyword`) is
    # named with its source column: "formed from source table X" alone left
    # the critic two groupings (twitter_ads keyword_report, batch10 run E).
    renamed_keys = [k for k in keys if not k.expr and k.source and k.source != k.column]
    if renamed_keys and not grain_description:
        grain_text += " " + " ".join(
            f"{k.column} is the value of the {k.source} column of {parent}."
            for k in renamed_keys
        )
    ops.append(
        MartOp(
            kind=MartOpKind.DERIVE,
            description=grain_text,
            tables=(parent,),
            columns=key_columns
            + tuple(p.column for p in passthrough if not p.from_hop),
            details={"select": ", ".join(base_projection), "name": base_rel},
        )
    )

    carried: list[str] = (
        list(key_columns)
        + [p.column for p in passthrough if not p.from_hop]
        + [alias for _, alias in parent_carry]
    )
    current = base_rel
    for i, hop in enumerate(hops, start=1):
        step = _unique(f"mart_joined_{i}", taken)
        predicate = " AND ".join(
            f"{relation_identifier(hop.table)}.{quote(right)} = "
            f"{relation_identifier(current)}.{quote(left)}"
            for left, right in hop.on_pairs
        )
        select = ", ".join(
            [
                f"{relation_identifier(current)}.{quote(a)} AS {quote(a)}"
                for a in carried
            ]
            + [
                f"{relation_identifier(hop.table)}.{quote(col)} AS {quote(alias)}"
                for col, alias in hop.carry
            ]
        )
        ops.append(
            MartOp(
                kind=MartOpKind.JOIN,
                # Comment-only warning, since a description naming both 'left'
                # retention and 'inner' is a rule no prose can satisfy: AN INNER
                # JOIN AT THIS HOP SILENTLY DROPS UNMATCHED PARENT ROWS.
                description=(
                    hop.description
                    or (
                        f"Bring in {hop.table} (hop {i} of {len(hops)}): rows "
                        f"with no matching {hop.table} row are RETAINED and "
                        "report the declared defaults."
                    )
                ),
                tables=(current, hop.table),
                columns=tuple(hop.rel_columns),
                join_type=JoinType.LEFT,
                predicate=predicate,
                details={"select": select, "name": step},
            )
        )
        carried = carried + [alias for _, alias in hop.carry]
        current = step

    ranked_columns: tuple[str, ...] = ()
    if windows:
        select = ", ".join(
            [f"{quote(a)} AS {quote(a)}" for a in carried]
            + [f"{w.expr} AS {quote(w.alias)}" for w in windows]
        )
        ranked_columns = tuple(
            w.alias for w in windows if w.alias in {m.column for m in measures}
        )
        ops.append(
            MartOp(
                kind=MartOpKind.WINDOW,
                description=(
                    "Rank the joined rows within each group under an explicit total "
                    "order (measure first, then the declared tie-break), so the "
                    "extremal row is a function of the input and not of row order."
                ),
                tables=(current,),
                columns=key_columns + ranked_columns,
                details={"select": select, "name": ranked_rel},
            )
        )
        carried = carried + [w.alias for w in windows]
        current = ranked_rel

    group_by = ", ".join(quote(c) for c in grain_columns)
    agg_details: dict[str, str] = {"group_by": group_by, "name": agg_rel}
    aliases: dict[str, str] = {}
    for index, measure in enumerate(measures):
        alias = _measure_alias(index)
        agg_details[alias] = measure.expr
        aliases[measure.column] = alias

    filtered = [m for m in measures if is_filtered_aggregate_expr(m.expr)]
    distinct = [m for m in measures if is_distinct_aggregate_expr(m.expr)]
    if filtered:
        agg_kind = MartOpKind.FILTERED_AGGREGATE
    elif distinct:
        agg_kind = MartOpKind.DISTINCT
    else:
        agg_kind = MartOpKind.AGGREGATE
    predicate = aggregate_predicate
    if agg_kind is MartOpKind.FILTERED_AGGREGATE:
        # Whose rows does the predicate count? No WHERE is emitted here (the
        # filter is the CASE inside each guarded measure), so on MIXED measures
        # an unscoped predicate reads as op-wide and is wrong about every
        # unguarded one. Fail closed here, where both are in hand.
        unguarded = [m.column for m in measures if m not in filtered]
        if not predicate:
            if not (roles.predicate_column and roles.predicate_pass):
                raise ValueError(
                    f"mart {mart!r}: a filtered aggregate must name its predicate column "
                    "and the values it accepts (FactRoles.predicate_column/"
                    "predicate_pass) — an unstated predicate is an unfair key"
                )
            # The roles state ONE condition, so this fallback may only speak for
            # measures sharing ONE guard; a shape with two (passing AND failing
            # counts in one op) must state its own scope or the prose lies.
            distinct_guards = {
                tuple(guard_conditions(m.expr)) for m in filtered
            }
            if len(distinct_guards) > 1:
                raise ValueError(
                    f"mart {mart!r}: its measures filter on {len(distinct_guards)} "
                    "DIFFERENT conditions, which FactRoles (one predicate_column, "
                    "one predicate_pass) cannot state; pass an explicit "
                    "aggregate_predicate naming each measure and the rows it counts"
                )
            condition = (
                f"{roles.predicate_column} IN ("
                + ", ".join(f"'{v}'" for v in roles.predicate_pass)
                + ")"
            )
            predicate = (
                condition
                if not unguarded
                else (
                    ", ".join(m.column for m in filtered)
                    + f" count only rows where {condition}; every other measure "
                    "counts every row of the group"
                )
            )
        elif unguarded:
            unnamed = [m.column for m in filtered if not predicate_governs(predicate, m.column)]
            if unnamed:
                raise ValueError(
                    f"mart {mart!r}: the filtered aggregate leaves {unguarded} "
                    f"unguarded, so its predicate governs only some of its measures, "
                    f"but the stated predicate {predicate!r} does not name {unnamed}. "
                    "State the SCOPE (which measures count only qualifying rows) or "
                    "guard every measure — an unscoped predicate is documentation the "
                    "compiled SQL does not implement."
                )
    pre_agg_rel = current
    # A possibly-NULL aggregate with no effective final default must preserve
    # that result.  Do not infer non-nullability merely from CASE's ELSE arm:
    # when every row qualifies, a nullable THEN value is still every value the
    # aggregate sees. Name the exposed measures explicitly.
    undefaulted = [
        m.column
        for m in measures
        if m.emit and _aggregate_can_be_null(m)
    ]
    # Describe zero substitution for every non-null aggregate output; otherwise
    # NULL-versus-zero behavior remains unspecified.
    zero_substituted = [
        m.column
        for m in measures
        if m.emit and not _aggregate_can_be_null(m) and _zero_substituted_inputs(m)
    ]
    ops.append(
        MartOp(
            kind=agg_kind,
            # Builder rule, comment-only because the prose gates ban the
            # vocabulary: EVERY PREDICATE LIVES INSIDE ITS AGGREGATE, NEVER IN A
            # WHERE — a WHERE drops the zero row.
            description=(
                # The GROUP BY carries the attribute columns beside the keys;
                # listing them all as "one output row per ..." beside a grain
                # sentence naming only the keys left the critic two readings
                # (twitter_ads, batch10 run J, 2026-09-11). Say what the
                # grouping is and what it means for rows that share the keys.
                (
                    # A carried attribute is determined by its key and must not
                    # be described as a second grain.
                    f"One output row per {', '.join(key_columns)}, carrying "
                    f"{', '.join(passthrough_columns)} beside the keys: a key "
                    "value identifies one source row for the carried columns, "
                    "so they take one value per key and never split a group"
                    if passthrough_columns and carried_per_key
                    else f"One output row per {', '.join(key_columns)} together with "
                    f"the carried {', '.join(passthrough_columns)}, which are part of "
                    "the grain: source rows that agree on the key columns but differ "
                    "in a carried column fall in different output rows"
                    if passthrough_columns
                    else f"One output row per {', '.join(key_columns)}"
                )
                + ", reporting "
                + ", ".join(m.column for m in measures if m.emit)
                + " for that row's matching rows."
                + (
                    # Short and author-sayable, so the fidelity gate's coverage
                    # is reachable in one sentence.
                    " A group with no qualifying rows still appears, "
                    "reporting 0; a retained row with no matching rows has "
                    "nothing to count, so its counts are 0, never 1."
                    if filtered
                    else ""
                )
                + (
                    " These aggregate outputs have no declared replacement for "
                    "an empty result: "
                    + ", ".join(undefaulted)
                    # Say the all-missing case for EVERY listed output, not
                    # only a conditional total: naming the conditional case
                    # alone left "spend" (a plain total of converted values)
                    # with a NULL-versus-0 fork (twitter_ads, batch10 run K,
                    # 2026-09-11).
                    + ". Preserve an empty result as empty, not 0: a missing input "
                    "value contributes nothing, and an output whose matching input "
                    "values are all missing is empty, whether it reads every "
                    "matching row or only the rows that qualify for its condition. "
                    "Every other output follows its own declared column rule."
                    if undefaulted
                    else ""
                )
                + (
                    " These aggregate outputs count a missing input value as 0, "
                    "so a row whose matching values are all missing reports 0, "
                    "never empty: "
                    + ", ".join(zero_substituted)
                    + "."
                    if zero_substituted
                    else ""
                )
            ),
            tables=(current,),
            columns=grain_columns + tuple(m.column for m in measures if m.emit),
            predicate=predicate,
            details=agg_details,
        )
    )
    current = agg_rel
    carried = list(grain_columns) + [m.column for m in measures]

    #: From here on, everything is named by its MART column name.
    names: dict[str, str] = {c: quote(c) for c in grain_columns}
    for measure in measures:
        names[measure.column] = aliases[measure.column]

    if extrema:
        if not (extrema_order_by and extrema_tie_break):
            raise ValueError(
                f"mart {mart!r}: an extremum needs an explicit order (measure plus "
                "tie-break) and must NAME its tie-break column"
            )
        top_rel = _unique("mart_top", taken)
        joined_rel = _unique("mart_with_top", taken)
        partition_by = ", ".join(quote(k) for k in key_columns)
        # ARGMAX, not MAX: the payload is an attribute OF the extremal row, not
        # the extremal value; op_problems keeps the projection aggregate-free.
        ops.append(
            MartOp(
                kind=MartOpKind.EXTREMA,
                description=(
                    "Keep the single row per "
                    f"{', '.join(key_columns)} at which the ordering measure is "
                    f"largest, ties broken by "
                    f"{extrema_tie_break_prose or extrema_tie_break}, and take "
                    + ", ".join(e.column for e in extrema)
                    + " from that winning row. "
                    + (
                        "EVERY row of the group ranks, including a row whose "
                        "ordering measure has no value — such a row sorts after "
                        "every row that has one — so a group with at least one "
                        "row always has a winning row, and the declared defaults "
                        "belong to a group with NO rows."
                        if extrema_order_nullable is not False
                        else (
                            "The ordering measure is required on every real input "
                            "row, so a non-empty group always has a winning row; "
                            "the declared defaults belong only to a group with "
                            "NO rows."
                        )
                    )
                ),
                tables=(pre_agg_rel,),
                columns=key_columns + tuple(e.column for e in extrema),
                details={
                    "select": ", ".join(
                        [f"{quote(k)} AS {quote(k)}" for k in key_columns]
                        + [
                            f"{quote(e.source)} AS {quote(e.column)}" for e in extrema
                        ]
                    ),
                    "partition_by": partition_by,
                    "order_by": extrema_order_by,
                    "tie_break": extrema_tie_break,
                    "name": top_rel,
                },
            )
        )
        ops.append(
            MartOp(
                kind=MartOpKind.JOIN,
                description=(
                    "Attach the extremal row's attributes to the grouped measures. "
                    "LEFT, so a group with no rows at all keeps its measures."
                ),
                tables=(agg_rel, top_rel),
                columns=key_columns,
                join_type=JoinType.LEFT,
                predicate=" AND ".join(
                    f"{relation_identifier(top_rel)}.{quote(k)} = "
                    f"{relation_identifier(agg_rel)}.{quote(k)}"
                    for k in key_columns
                ),
                details={
                    "select": ", ".join(
                        [
                            f"{relation_identifier(agg_rel)}.{quote(c)} AS {quote(c)}"
                            for c in grain_columns
                        ]
                        + [
                            f"{relation_identifier(agg_rel)}.{aliases[m.column]} "
                            f"AS {aliases[m.column]}"
                            for m in measures
                        ]
                        + [
                            f"{relation_identifier(top_rel)}.{quote(e.column)} "
                            f"AS {quote(e.column)}"
                            for e in extrema
                        ]
                    ),
                    "name": joined_rel,
                },
            )
        )
        for e in extrema:
            names[e.column] = quote(e.column)
        carried = carried + [e.column for e in extrema]
        current = joined_rel

    named_select = ", ".join(
        [f"{quote(k.column)} AS {quote(k.column)}" for k in keys]
        + [
            (
                f"COALESCE({quote(p.column)}, {p.null_default}) AS {quote(p.column)}"
                if p.null_default is not None
                else f"{quote(p.column)} AS {quote(p.column)}"
            )
            for p in passthrough
        ]
        + [
            (
                f"COALESCE({names[m.column]}, {m.null_default}) AS {quote(m.column)}"
                if m.null_default is not None
                else f"{names[m.column]} AS {quote(m.column)}"
            )
            for m in measures
        ]
        + [
            (
                # No `else` branch, deliberately: `extremum_default` raises for a
                # type with no declared default rather than shipping a NULL
                # column under a description promising a value.
                f"COALESCE({quote(e.column)}, {extremum_default(e.type)}) "
                f"AS {quote(e.column)}"
            )
            for e in extrema
        ]
    )
    null_capable = tuple(m.column for m in measures if m.null_default is not None)
    all_missing_capable = tuple(
        m.column
        for m in measures
        if m.null_default is not None and m.input_nullable is not False
    )
    # The COALESCEs live in `named_select` (what attack_surface reads for
    # no_null_default). Every default covers an empty LEFT-join group. The
    # all-missing REAL-row arm is stated only when source nullability permits it.
    ops.append(
        MartOp(
            kind=MartOpKind.DERIVE,
            description=(
                "Name the mart columns"
                + (
                    (
                        f"; {', '.join(null_capable)} "
                        + ("reports its declared default" if len(null_capable) == 1
                           else "report their declared defaults")
                        + " — never NULL — for a group with no matching rows."
                        + (
                            f" For {', '.join(all_missing_capable)}, the default "
                            "also applies to a group none of whose real rows "
                            "carries an input value."
                            if all_missing_capable
                            else ""
                        )
                    )
                    if null_capable
                    else "."
                )
            ),
            tables=(current,),
            columns=grain_columns
            + tuple(m.column for m in measures if m.emit)
            + tuple(e.column for e in extrema),
            details={"select": named_select, "name": named_rel},
        )
    )
    current = named_rel
    names = {c: quote(c) for c in names}
    carried = (
        list(grain_columns)
        + [m.column for m in measures]
        + [e.column for e in extrema]
    )

    if post_windows:
        # AFTER the named DERIVE: a window must read the mart's DECLARED,
        # already-COALESCEd values, never the raw ``m_<i>`` aggregate, or one
        # NULL cell propagates through the running total and the next LAG and
        # the gold contradicts the shipped prose. The check below enforces it.
        raw_aliases = set(aliases.values())
        leaked = sorted(k for k, v in names.items() if v in raw_aliases)
        if leaked:
            raise ValueError(
                f"mart {mart!r}: post-aggregate windows would read raw aggregate "
                f"aliases for {leaked}; windows must read the named, defaulted "
                "mart columns"
            )
        select = ", ".join(
            [f"{quote(c)} AS {quote(c)}" for c in carried]
            + [
                f"{_fmt(w.expr, names, where=f'mart {mart!r} window {w.alias!r}')} "
                f"AS {quote(w.alias)}"
                for w in post_windows
            ]
        )
        ops.append(
            MartOp(
                kind=MartOpKind.WINDOW,
                # Comment-only builder rule: every expression in `select` carries
                # an EXPLICIT frame (the RANGE default is wrong on ties).
                description=(
                    "Compute "
                    + ", ".join(w.alias for w in post_windows)
                    + " over the grouped rows in a stated total order, so rows "
                    "that tie on the ordering value still get one deterministic "
                    "result each."
                ),
                tables=(current,),
                columns=key_columns + tuple(w.alias for w in post_windows),
                details={"select": select, "name": post_rel},
            )
        )
        for w in post_windows:
            names[w.alias] = quote(w.alias)
        carried = carried + [w.alias for w in post_windows]
        current = post_rel

    ratios = [d for d in derived if d.kind is MartColumnKind.DERIVED and "/" in d.expr]
    ladders = [d for d in derived if d.kind is MartColumnKind.CATEGORICAL]
    plain = [d for d in derived if d not in ratios and d not in ladders]
    if plain:
        raise ValueError(
            f"mart {mart!r}: derived columns "
            f"{[d.column for d in plain]} are neither a guarded division nor a CASE "
            "ladder; fold plain arithmetic into a Measure expression instead"
        )

    if ratios:
        select = ", ".join(
            [f"{quote(c)} AS {quote(c)}" for c in carried]
            + [
                f"{_fmt(d.expr, names, where=f'mart {mart!r} ratio {d.column!r}')} "
                f"AS {quote(d.column)}"
                for d in ratios
            ]
        )
        ops.append(
            MartOp(
                kind=MartOpKind.RATIO,
                description=(
                    "Guarded ratios: "
                    + "; ".join(f"{d.column} — {d.description}" for d in ratios)
                ),
                tables=(current,),
                columns=tuple(carried) + tuple(d.column for d in ratios),
                details={
                    "select": select,
                    "name": ratio_rel,
                    "units": "fraction",
                    "null_result": (
                        "0.0 when the denominator is 0 or NULL (the NULLIF guard "
                        "makes the division NULL and the COALESCE substitutes 0.0)"
                    ),
                    "rounding": "ROUND to 4 decimal places",
                },
            )
        )
        for d in ratios:
            names[d.column] = quote(d.column)
        carried = carried + [d.column for d in ratios]
        current = ratio_rel

    if ladders:
        # ONE op PER LADDER: the fidelity gate needs each rule's identifiers in
        # one short passage, which a glued op makes unsatisfiable. The compiled
        # SQL is unchanged — each select carries the prior columns forward.
        domain = ", ".join(roles.domain) if roles.domain else ""
        for index, d in enumerate(ladders):
            # Derive boundary inclusivity from this ladder's comparison; do not
            # attach a boundary rule to non-threshold ladders.
            boundary = _ladder_boundary_statement(d.expr)
            last = index == len(ladders) - 1
            rel = final_rel if last else f"{final_rel}_l{index + 1}"
            select = ", ".join(
                [f"{quote(c)} AS {quote(c)}" for c in carried]
                + [
                    f"{_fmt(d.expr, names, where=f'mart {mart!r} ladder {d.column!r}')} "
                    f"AS {quote(d.column)}"
                ]
            )
            ops.append(
                MartOp(
                    kind=MartOpKind.CONDITIONAL,
                    # No appended generalities: d.description already states
                    # every band and arm, and extra sentences add coverage terms
                    # the author has no reason to echo.
                    description=f"{d.column} — {d.description}",
                    tables=(current,),
                    columns=tuple(carried) + (d.column,),
                    details={
                        "select": select,
                        "name": rel,
                        "domain": domain,
                        "boundary": boundary,
                    },
                )
            )
            names[d.column] = quote(d.column)
            carried = carried + [d.column]
            current = rel

    ops.append(
        MartOp(
            kind=MartOpKind.TIE_BREAK,
            description=f"Deterministic output order: sort by {', '.join(key_columns)}.",
            columns=key_columns,
        )
    )

    # -- the column contract ------------------------------------------------
    columns: list[MartColumn] = [
        MartColumn(
            name=k.column,
            type=k.type,
            description=k.description,
            kind=k.kind,
        )
        for k in keys
    ] + [
        MartColumn(
            name=p.column,
            type=p.type,
            description=p.description,
            kind=MartColumnKind.PASSTHROUGH,
        )
        for p in passthrough
    ]
    ranked_set = {w.alias for w in windows}
    for measure in measures:
        if not measure.emit:
            continue
        columns.append(
            MartColumn(
                name=measure.column,
                type=measure.type,
                description=measure.description,
                kind=(
                    MartColumnKind.RANKED
                    if measure.column in ranked_set
                    else measure.kind
                ),
            )
        )
    for e in extrema:
        columns.append(
            MartColumn(
                name=e.column,
                type=e.type,
                description=e.description,
                kind=MartColumnKind.RANKED,
            )
        )
    for w in post_windows:
        columns.append(
            MartColumn(
                name=w.alias,
                type=w.type,
                description=w.description or f"Windowed measure {w.alias}.",
                kind=MartColumnKind.RANKED,
            )
        )
    for d in ratios + ladders:
        columns.append(
            MartColumn(
                name=d.column, type=d.type, description=d.description, kind=d.kind
            )
        )

    seen: set[str] = set()
    for column in columns:
        if column.name in seen:
            raise ValueError(f"mart {mart!r}: duplicate mart column {column.name!r}")
        seen.add(column.name)

    plan = MartPlan(mart=mart, ops=tuple(ops), notes=notes)
    op_faults = [
        problem
        for index, op in enumerate(plan.ops)
        for problem in op_problems(op, loc=f"mart {mart!r} op[{index}] ({op.kind.value})")
    ]
    if op_faults:
        raise ValueError(f"mart {mart!r}: ill-formed ops — " + "; ".join(op_faults))

    built = BuiltPlan(
        plan=plan,
        columns=tuple(columns),
        shape=StarShape(
            mart=mart,
            parent=parent,
            parent_keys=tuple(k.source for k in keys if k.source),
            key_columns=key_columns,
            fact=fact,
            fact_link_columns=(
                tuple(right for _, right in hops[0].on_pairs) if hops else ()
            ),
            fact_dedupe=bool(dedupe),
            null_capable_measures=null_capable,
            has_join=bool(hops),
            shape_name=shape_name,
            child=child,
            child_link_pairs=child_link_pairs,
            join_hops=len(hops),
            distinct_measures=tuple(m.column for m in distinct if m.emit),
            filtered_measures=tuple(m.column for m in filtered if m.emit),
            ratio_measures=tuple(d.column for d in ratios),
            constant_divisor_measures=tuple(
                m.column
                for m in measures
                if m.emit and is_constant_divisor_expr(m.expr)
            ),
            ranked_measures=tuple(
                c.name for c in columns if c.kind is MartColumnKind.RANKED
            ),
            conditional_columns=tuple(d.column for d in ladders),
            window_measures=tuple(w.alias for w in post_windows),
            fanout_capable_measures=tuple(
                m.column for m in measures if m.emit and len(hops) >= 2
            ),
            witnesses=witnesses,
            attack_claims=attack_claims,
            roles=roles,
            thresholds=thresholds,
            witness_anchor_table=witness_anchor[0] if witness_anchor else "",
            witness_anchor_key=witness_anchor[1] if witness_anchor else "",
            witness_bridge_table=witness_anchor[2] if witness_anchor else "",
            witness_bridge_fk=witness_anchor[3] if witness_anchor else "",
        ),
    )
    # Kind certification BEFORE counting, so a lie can never clear the
    # computed-column budget. A wrong kind is a BUILDER BUG: plain ValueError,
    # which PROPAGATES through the trial protocol rather than skipping silently.
    kind_faults = kind_certification_problems(built)
    if kind_faults:
        raise ValueError(f"mart {mart!r}: uncertified column kinds — " + "; ".join(kind_faults))
    if enforce_budget:
        budget = budget_problems(built)
        if budget:
            raise MartBudgetError(f"mart {mart!r}: " + "; ".join(budget))
    return built


def kind_certification_problems(built: BuiltPlan) -> list[str]:
    """Declared kinds the plan's ops do not certify, plus every UNDECLARED kind
    on a BuiltPlan carrying a column contract.

    Legacy plans carry no columns and are not judged here at all.
    """
    if not built.columns:
        return []
    problems = column_kind_problems_for(
        built.plan, built.columns, mart_name=built.plan.mart
    )
    unclassified = [c.name for c in built.columns if c.kind is None]
    if unclassified:
        problems.append(
            f"mart {built.plan.mart!r}: columns {unclassified} declare no kind; a "
            "plan-library mart declares every column"
        )
    return problems


def budget_problems(built: BuiltPlan) -> list[str]:
    """The per-mart column BUDGET CONTRACT, checked against the emitted plan.

    The count is CERTIFIED first: kind problems are reported ahead of the
    totals, so a passthrough falsely declared 'ranked' can never be what clears
    the computed budget.
    """
    problems: list[str] = list(kind_certification_problems(built))
    total = len(built.columns)
    if total < MIN_MART_COLUMNS:
        problems.append(
            f"{total} target columns, under the anchor's measured minimum of "
            f"{MIN_MART_COLUMNS}"
        )
    if built.computed_count < MIN_MART_COMPUTED:
        problems.append(
            f"{built.computed_count} computed columns, under the shape budget of "
            f"{MIN_MART_COMPUTED}"
        )
    if built.passthrough_count < MIN_MART_PASSTHROUGH:
        problems.append(
            f"{built.passthrough_count} passthrough columns, under the shape budget "
            f"of {MIN_MART_PASSTHROUGH} (the anchor's marts are ~45% passthrough; a "
            "100%-computed mart is as unlike the benchmark as a 0%-computed one)"
        )
    return problems


def plan_template_signature(plan: MartPlan) -> str:
    """Ordered (op kind + join type) tuple plus the grain — the SHAPE identity,
    used to count how far a corpus collapses to a handful of templates."""
    parts: list[str] = []
    for op in plan.ops:
        if op.kind is MartOpKind.JOIN and op.join_type is not None:
            parts.append(f"{op.kind.value}:{op.join_type.value}")
        elif op.kind is MartOpKind.AGGREGATE or op.kind in AGGREGATE_FAMILY_KINDS:
            group = op.details.get("group_by", "")
            parts.append(f"{op.kind.value}/{group.count(',') + 1 if group else 0}")
        else:
            parts.append(op.kind.value)
    return " -> ".join(parts)


# ---------------------------------------------------------------------------
# The registered shapes, and the schema EVIDENCE that selects each one
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChainEvidence:
    """The schema facts a plan-library shape needs, and nothing else.

    Every field is READ OFF THE SCHEMA, never off an answer or a published
    query — matching a mart grain to a pool's published questions is
    contamination no string grep can see. Fail closed: no evidence, no shape,
    because invented evidence has no witness to falsify it.
    """

    parent: str
    parent_key: str
    parent_key_type: ColumnType = ColumnType.BIGINT
    #: A text attribute of the parent — the mart's passthrough column.
    parent_attr: str = ""
    parent_attr_type: ColumnType = ColumnType.TEXT
    #: Parent column with a DECLARED domain (synsql DDL comment, schemapile
    #: ENUM/CHECK, fivetran compiled CASE body). Drives the categorical ladder.
    parent_domain_column: str = ""
    domain: tuple[str, ...] = ()
    #: A legal schema value the ladder deliberately does NOT name (row G).
    out_of_domain: str = ""

    #: Hop 1 — the bridge. Fan-out lives here.
    bridge: str = ""
    bridge_key: str = ""
    #: TYPE of `bridge_key`, load-bearing: `argmax_profile` projects the
    #: extremal row's key, whose empty-group default must bind against this type
    #: (a TEXT key under BIGINT is a DuckDB bind error). The key is not
    #: guaranteed numeric, so the type travels with it.
    bridge_key_type: ColumnType = ColumnType.BIGINT
    bridge_parent_fk: str = ""
    bridge_child_fk: str = ""
    bridge_status: str = ""
    bridge_status_pass: tuple[str, ...] = ()
    bridge_status_fail: tuple[str, ...] = ()
    bridge_amount: str = ""
    bridge_amount_type: ColumnType = ColumnType.INTEGER
    bridge_label: str = ""
    bridge_timestamp: str = ""

    #: Hop 2 — the child the bridge fans out onto. Its ABSENCE is why a
    #: one-hop star can never make COUNT and COUNT(DISTINCT) disagree.
    child: str = ""
    child_key: str = ""
    child_label: str = ""

    #: The fact table declares no upstream primary key.
    bridge_needs_dedupe: bool = False
    #: bridge -> child link is OPTIONAL. Only then can a bridge row point at a
    #: missing child, so only then may a shape declare witness I and the
    #: second-hop inner-join attack — otherwise the attack has no witness.
    child_link_optional: bool = False
    #: bridge -> parent link is OPTIONAL, i.e. an activity row can genuinely be
    #: orphaned. `orphan_coverage` REQUIRES this; it is the shape's premise.
    owner_link_optional: bool = False

    # -- Nullability of the grain sources (adapters/evidence.py sets these) --
    #: The bridge -> parent fk is optional AND nullable, so NULL fks get
    #: injected. A shape whose GRAIN rests on that column declines rather than
    #: freeze a NULL grain key (`reference/gold.py` raises NullGrainKeyError).
    owner_key_nullable: bool = False
    #: ``bridge_key == bridge_parent_fk``: the countable-key fallback landed on
    #: the hop-1 link, so the key names the PARENT, never WHICH bridge row.
    bridge_key_is_parent_fk: bool = False
    #: Whether ``bridge_key`` identifies exactly one bridge row.  A numeric,
    #: non-null fallback is countable but is not automatically unique; argmax
    #: may use it to count rows while it must not publish it as ``top_row_id``
    #: or use it as a supposedly total tie-break unless this fact is true.
    bridge_key_is_unique: bool = False
    #: Whether `bridge_timestamp` / `bridge_amount` / `bridge_label` /
    #: `parent_attr` are declared nullable; ``None`` = unreported by the
    #: adapter, so shapes stay tolerant.
    #: `temporal_grid` declines a nullable timestamp (a NULL month names no row).
    bridge_timestamp_nullable: bool | None = None
    bridge_amount_nullable: bool | None = None
    bridge_label_nullable: bool | None = None
    parent_attr_nullable: bool | None = None


#: ChainEvidence roles grouped by the TABLE they name a column of. Two required
#: roles of one group naming the same column is a chain no shape can honestly
#: build (one source column under two mart names, or an argmax whose measure is
#: the row's own id). Grouped by table side, since names repeat across tables.
_ROLE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("parent_key", "parent_attr", "parent_domain_column"),
    (
        "bridge_key", "bridge_parent_fk", "bridge_child_fk", "bridge_status",
        "bridge_amount", "bridge_label", "bridge_timestamp",
    ),
    ("child_key", "child_label"),
)


def _require(
    evidence: ChainEvidence,
    shape: str,
    *fields: str,
    allow_shared: tuple[tuple[str, str], ...] = (),
) -> None:
    """Fail closed unless the shape's evidence is PRESENT and ROLE-DISTINCT.

    No two required roles of one table side may name the same column, except
    pairs passed in `allow_shared`. Both refusals raise `ShapeNotSelectable`, so
    trial selection SKIPS the chain — a colliding chain is never repaired here.
    """
    missing = [f for f in fields if not getattr(evidence, f)]
    if missing:
        raise ShapeNotSelectable(
            f"shape {shape!r}: schema evidence is missing {missing} — the shape is "
            "not selectable on this schema (fail closed: do not invent it)"
        )
    required = set(fields)
    shared = {frozenset(pair) for pair in allow_shared}
    for group in _ROLE_GROUPS:
        roles = [f for f in group if f in required]
        for i, a in enumerate(roles):
            for b in roles[i + 1:]:
                if frozenset((a, b)) in shared:
                    continue
                if getattr(evidence, a) == getattr(evidence, b):
                    raise ShapeNotSelectable(
                        f"shape {shape!r}: roles {a} and {b} both name column "
                        f"{getattr(evidence, a)!r} — not selectable (fail closed: "
                        "one source column cannot honestly play two roles)"
                    )


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{quote(column)} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


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
    distinct_ = "DISTINCT " if e.bridge_needs_dedupe else ""
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
                    f"Number of {distinct_}{e.bridge} rows linked to this "
                    f"{e.parent} row. 0 when there are none."
                    + (
                        f" {e.bridge} declares no primary key upstream and "
                        "byte-identical duplicate rows occur in the source; they "
                        "count ONCE."
                        if e.bridge_needs_dedupe
                        else ""
                    )
                ),
            ),
            Measure(
                column="distinct_child_count",
                expr='COUNT(DISTINCT "dim_key")',
                type=ColumnType.BIGINT,
                description=(
                    f"Number of DISTINCT {e.child} rows reached through those links. "
                    "Two links pointing at the same child count ONCE. 0 when there "
                    "are no links."
                    + (
                        # The join op says a dangling link "still counts as a
                        # link and is RETAINED"; this count is over the CHILD
                        # rows, so such a link reaches none. Left unstated, the
                        # ambiguity critic read the two rules as a fork on
                        # synsql__3d_object_positioning (batch10 2026-09-11).
                        f" A link whose {e.child} row is missing reaches no "
                        f"{e.child} row and adds nothing to this count."
                        if e.child_link_optional
                        else ""
                    )
                ),
            ),
            Measure(
                column="active_link_count",
                expr=f'COUNT(CASE WHEN {passes} THEN "link_key" END)',
                type=ColumnType.BIGINT,
                description=(
                    f"Number of {distinct_}linked {e.bridge} rows whose "
                    f"{e.bridge_status} is one of {list(e.bridge_status_pass)}. A "
                    "parent whose links ALL fail that test reports 0, not a "
                    "missing row."
                ),
            ),
            Measure(
                column="total_amount",
                expr='SUM("link_amount")',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Sum of {e.bridge_amount} over every {distinct_}linked row; "
                    "0 when there are no links, and 0 when none of the linked "
                    f"rows carries a {e.bridge_amount} value."
                ),
            ),
            Measure(
                column="active_amount",
                expr=f'SUM(CASE WHEN {passes} THEN "link_amount" ELSE 0 END)',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Sum of {e.bridge_amount} over {distinct_}links whose "
                    f"{e.bridge_status} is one of {list(e.bridge_status_pass)}; "
                    "0 when none qualify, and 0 when every qualifying row lacks a "
                    f"{e.bridge_amount} value."
                ),
            ),
            Measure(
                column="max_amount",
                expr='MAX("link_amount")',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Largest {e.bridge_amount} among the linked rows; 0 when there "
                    "are no links, and 0 when none of the linked rows carries a "
                    f"{e.bridge_amount} value."
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
                    "active_amount divided by total_amount, expressed as a FRACTION "
                    "between 0 and 1 (not a percentage), rounded to 4 decimal "
                    "places, and reported as 0.0 when total_amount is 0."
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
                    "Size band of link_count: 'none' at exactly 0, 'small' for 1-2 "
                    "INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above "
                    "5. Every value falls in exactly one band."
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
    body.append(
        MartOp(
            kind=MartOpKind.FILTER,
            description=(
                f"Retain a grouped {evidence.parent} row only when link_count is "
                f"at least {threshold}, inclusive. Apply this rule after the "
                "per-parent measures are computed: a parent with one linked row "
                "does not appear, while a parent with exactly two does."
            ),
            tables=(current,),
            columns=mart_columns,
            predicate=f'{quote("link_count")} >= {threshold}',
            details={"name": filtered_rel},
        )
    )
    body.append(base.plan.ops[-1])

    threshold_note = (
        " This mart emits the column only for parent groups whose link_count is "
        f"at least {threshold}; lower-count groups produce no output row."
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
    built = BuiltPlan(plan=plan, shape=shape, columns=columns)
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


def argmax_profile(evidence: ChainEvidence, *, mart: str, notes: str = "") -> BuiltPlan:
    """ARGMAX, not MAX: the LABEL of the row at which a measure is extremal.

    The ORDER BY must carry the tie-break and the shipped description must NAME
    it, or the gold is a function of DuckDB's row order and a correct solver can
    score below 1.0. `top_row_id` ships only when the bridge key is a GENUINE row
    identifier: on a PK-less bridge it is constant per parent, so it would name
    no row and is dropped along with its order term.
    """
    e = evidence
    amount_may_be_missing = e.bridge_amount_nullable is not False
    label_may_be_missing = e.bridge_label_nullable is not False
    key_is_row_id = (
        bool(e.bridge_key)
        and e.bridge_key != e.bridge_parent_fk
        and not e.bridge_key_is_parent_fk
        and e.bridge_key_is_unique
    )
    row_id_tie_break_prose = f"the smallest {e.bridge_key}" + (
        f" {TEXT_ORDER_PROSE}"
        if e.bridge_key_type is ColumnType.TEXT
        else ""
    )
    _require(
        e, "argmax_profile",
        "parent", "parent_key", "parent_attr",
        "bridge", "bridge_key", "bridge_parent_fk", "bridge_amount", "bridge_label",
        allow_shared=(("bridge_key", "bridge_parent_fk"),),
    )
    # "f_id" makes the order TOTAL (two rows can share measure AND label);
    # dropped when it is the parent fk, since it then names no row. NULLS LAST is
    # written out on EVERY term, never inherited: null placement is a session
    # setting, so an unstated order is not a deterministic one.
    order = '"f_measure" DESC NULLS LAST, "f_label" ASC NULLS LAST' + (
        ', "f_id" ASC NULLS LAST' if key_is_row_id else ""
    )
    frame = "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"
    row_id_extremum = (
        Extremum(
            column="top_row_id",
            source="f_id",
            # The bridge key's OWN type, read off the schema: a hard-coded
            # BIGINT emits `0` for a TEXT key, which is a bind error. The
            # description derives from the same value, so it cannot drift.
            type=e.bridge_key_type,
            description=(
                f"The {e.bridge_key} of that same extremal row — the winner "
                "under the SAME total order, so it is the identifier of a "
                f"real {e.bridge} row whenever the parent has any. "
                + (
                    f"This includes when none of them carries a "
                    f"{e.bridge_amount} value. "
                    if amount_may_be_missing
                    else ""
                )
                + "It is "
                f"{_default_prose(e.bridge_key_type)} when there are "
                "no rows, and only then. It identifies WHICH row won, so a tie resolved the "
                "wrong way is visible even when two rows share a label."
            ),
        ),
    ) if key_is_row_id else ()
    return build_rollup(
        mart=mart,
        shape_name="argmax_profile",
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
                    (e.bridge_key, "f_id"),
                    (e.bridge_amount, "f_measure"),
                    (e.bridge_label, "f_label"),
                ),
                rel_columns=(e.bridge_parent_fk, e.parent_key),
                description=(
                    f"Bring in {e.bridge}: a {e.parent} row with no {e.bridge} rows "
                    "still appears, with the declared defaults."
                ),
            ),
        ),
        windows=(
            WindowExpr(
                alias="part_max",
                expr=(
                    f'MAX("f_measure") OVER (PARTITION BY "parent_key" '
                    f'ORDER BY "f_measure" DESC {frame})'
                ),
            ),
        ),
        extrema=(
            Extremum(
                column="top_label",
                source="f_label",
                type=ColumnType.TEXT,
                # State every schema-permitted NULL outcome. Omit impossible
                # real-row cases for required sources, but keep empty defaults.
                description=(
                    f"The {e.bridge_label} of the {e.bridge} row with the LARGEST "
                    f"{e.bridge_amount} for this {e.parent} row. Ties in "
                    f"{e.bridge_amount} are broken by taking the SMALLEST "
                    f"{e.bridge_label} {TEXT_ORDER_PROSE}"
                    + (
                        f" — a row with no {e.bridge_label} value sorts after "
                        "every labelled row"
                        if label_may_be_missing
                        else ""
                    )
                    + (
                        f"; rows tied on both are resolved by "
                        f"{row_id_tie_break_prose}. "
                        if key_is_row_id
                        else ". "
                    )
                    + (
                        f"A row with no {e.bridge_amount} value still ranks, after "
                        f"every row that has one, so a parent holding at least one "
                        f"{e.bridge} row always has a winning row — when NONE of "
                        f"its rows carries a {e.bridge_amount} value the winner is "
                        "the one the tie-break alone selects, not the no-rows "
                        "default. "
                        if amount_may_be_missing
                        else ""
                    )
                    + f"The literal '(none)' when the parent has no {e.bridge} "
                    "rows at all"
                    + (
                        f", and '(none)' when the winning row has no "
                        f"{e.bridge_label} value"
                        if label_may_be_missing
                        else ""
                    )
                    + "."
                ),
            ),
        )
        + row_id_extremum,
        extrema_order_by=order,
        extrema_tie_break="f_label",
        extrema_order_nullable=e.bridge_amount_nullable,
        # The description names the REAL columns; 'f_label'/'f_id' are the
        # compiler's internal aliases and appear only in details.
        extrema_tie_break_prose=(
            f"the smallest {e.bridge_label} {TEXT_ORDER_PROSE}"
            # The winner-selection rule is the one the critic reads for the
            # tie-break; leaving the missing-label placement to the column
            # description alone left it "silent on NULL placement"
            # (lavestima, batch10 run K, 2026-09-11).
            + (
                f" (a row with no {e.bridge_label} value sorts after every "
                "row that has one)"
                if label_may_be_missing
                else ""
            )
            + (f", then {row_id_tie_break_prose}" if key_is_row_id else "")
        ),
        measures=(
            # State the all-null arm when the source permits it: "0 when there
            # are no rows" is silent about a parent whose rows all lack a value,
            # and gold coalesces those to 0 where a literal reading keeps NULL.
            Measure(
                column="top_measure",
                expr='MAX("f_measure")',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"The largest {e.bridge_amount} itself; 0 when the parent "
                    f"has no {e.bridge} rows"
                    + (
                        f", and 0 when none of its rows carries a "
                        f"{e.bridge_amount} value"
                        if amount_may_be_missing
                        else ""
                    )
                    + "."
                ),
                input_nullable=e.bridge_amount_nullable,
            ),
            Measure(
                column="tied_count",
                expr='COUNT(CASE WHEN "f_measure" = "part_max" THEN "f_id" END)',
                type=ColumnType.BIGINT,
                description=(
                    f"How many {e.bridge} rows are tied at that largest "
                    f"{e.bridge_amount}. 1 when exactly one row carries that "
                    f"largest {e.bridge_amount}; 0 when there are no rows"
                    + (
                        f" or when none of the rows carries a "
                        f"{e.bridge_amount} value; a row with no "
                        f"{e.bridge_amount} value never ties: only a row whose "
                        f"{e.bridge_amount} value equals the largest value among "
                        "the parent's rows holds the maximum, so the winning row "
                        "of a parent whose rows all lack a value — the row the "
                        "tie-break alone selects — is not counted here"
                        if amount_may_be_missing
                        else ""
                    )
                    + "."
                ),
            ),
            Measure(
                column="child_count",
                expr='COUNT("f_id")',
                type=ColumnType.BIGINT,
                description=(
                    # The independent implementer counted the LEFT-join
                    # placeholder itself (COUNT(*) = 1 for a childless jobs
                    # row; dlt__workable, batch10 run K, 2026-09-11).
                    f"Number of {e.bridge} rows for this {e.parent} row; "
                    "0 when there are none. "
                    # With reasoning the witness moved from COUNT(*) to
                    # COUNT(*) FILTER (WHERE measure IS NOT NULL): a linked
                    # row with no measure value is still a row (dlt__workable
                    # probe, 2026-09-11).
                    + (
                        f"Every linked {e.bridge} row counts, whether or not it "
                        f"carries {_article(e.bridge_amount)} {e.bridge_amount} value. "
                        if amount_may_be_missing
                        else ""
                    )
                    + f"{_article(e.parent, capitalized=True)} {e.parent} row kept with no "
                    f"{e.bridge} row reports 0 here, never 1: its placeholder "
                    f"holds no {e.bridge} row to count."
                ),
            ),
            Measure(
                column="total_measure",
                expr='SUM("f_measure")',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Sum of {e.bridge_amount} over all of them; 0 when the "
                    f"parent has no {e.bridge} rows"
                    + (
                        f", and 0 when none of its rows carries a "
                        f"{e.bridge_amount} value (rows with no "
                        f"{e.bridge_amount} value add nothing)"
                        if amount_may_be_missing
                        else ""
                    )
                    + "."
                ),
                input_nullable=e.bridge_amount_nullable,
            ),
        ),
        aggregate_predicate=(
            "a row counts toward tied_count when its measure equals the per-parent "
            "maximum measure; when no row carries a measure there is no maximum "
            "and tied_count is 0"
            if amount_may_be_missing
            else "a row counts toward tied_count when its measure equals the "
            "per-parent maximum measure"
        ),
        derived=(
            Derived(
                column="top_measure_share",
                expr=(
                    "COALESCE(ROUND(CAST({top_measure} AS DOUBLE) / "
                    "NULLIF({total_measure}, 0), 4), 0.0)"
                ),
                type=ColumnType.FLOAT,
                description=(
                    "top_measure divided by total_measure as a FRACTION between 0 "
                    "and 1 (not a percentage), rounded to 4 decimals, 0.0 when "
                    "total_measure is 0."
                ),
            ),
            Derived(
                column="tie_state",
                expr=(
                    "CASE WHEN {tied_count} = 0 THEN 'empty' "
                    "WHEN {tied_count} = 1 THEN 'unique' ELSE 'tied' END"
                ),
                type=ColumnType.TEXT,
                # 'empty' is derived from tied_count, so it must be described in
                # tied_count's terms: a parent whose rows all lack a value has
                # tied_count 0 and reports 'empty' while child_count is nonzero.
                description=(
                    "'empty' when no row holds a maximum at all — the parent has "
                    f"no {e.bridge} rows"
                    + (
                        f", or none of its rows carries a {e.bridge_amount} value"
                        if amount_may_be_missing
                        else ""
                    )
                    + " — 'unique' when exactly one row "
                    "holds the maximum, 'tied' when two or more do. Equivalently, "
                    "tie_state follows tied_count: 'empty' when tied_count is 0, "
                    "'unique' when it is 1, 'tied' when it is 2 or more."
                    + (
                        # `empty` describes the measure maximum, not whether
                        # ranking finds a winner. Name only emitted columns and
                        # define the maximum over non-NULL values.
                        f" A row holds the maximum only when it carries a "
                        f"{e.bridge_amount} value equal to the largest "
                        f"{e.bridge_amount} value among the parent's rows; a row "
                        f"with no {e.bridge_amount} value never holds the maximum. "
                        f"So a parent whose {e.bridge} rows all lack a "
                        f"{e.bridge_amount} value has no row holding the maximum "
                        "and is 'empty', with tied_count 0, even though the "
                        "ranking still selects a winning row for it by the "
                        "tie-break alone, which "
                        + ("top_label and top_row_id name" if key_is_row_id else "top_label names")
                        + "."
                        if amount_may_be_missing
                        else ""
                    )
                ),
                kind=MartColumnKind.CATEGORICAL,
            ),
        ),
        attack_claims=(
            "inner_join", "no_null_default",
            "dropped_filter", "dropped_filter@filter_to_where",
            "wrong_denominator", "wrong_grain", "wrong_window",
            "custom@wrong_boundary_else",
        ),
        roles=FactRoles(
            link_key=e.bridge_key,
            measure=e.bridge_amount,
            label=e.bridge_label,
        ),
        witnesses=(WITNESS_CONTROL, WITNESS_CHILDLESS, WITNESS_TIE),
        grain_description=f"One row per {e.parent} row, keyed by {e.parent_key}.",
        notes=notes,
    )


def latest_snapshot(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan:
    """One deterministic as-of row per parent, plus lifetime measures.

    This is intentionally not another numeric argmax.  The winning row is the
    latest row by a schema-declared timestamp, with a genuine unique row key as
    the total-order tie-break.  A keyless source is deduplicated byte-for-byte
    before ranking, but only when the schema says exact duplicates are possible.
    """
    e = evidence
    required = [
        "parent",
        "parent_key",
        "parent_attr",
        "bridge",
        "bridge_key",
        "bridge_key_is_unique",
        "bridge_parent_fk",
        "bridge_amount",
        "bridge_label",
        "bridge_timestamp",
    ]
    # Status is a useful payload when the schema declares one, but it is not
    # part of the as-of ordering. Keep it role-distinct when present without
    # inventing it as an admission requirement when absent.
    if e.bridge_status:
        required.append("bridge_status")
    _require(
        e,
        "latest_snapshot",
        *required,
    )
    amount_may_be_missing = e.bridge_amount_nullable is not False
    label_may_be_missing = e.bridge_label_nullable is not False
    order = (
        '"snapshot_at" DESC NULLS LAST, '
        '"snapshot_row_id" ASC NULLS LAST'
    )
    tie_break_prose = f"the smallest {e.bridge_key}" + (
        f" {TEXT_ORDER_PROSE}"
        if e.bridge_key_type is ColumnType.TEXT
        else ""
    )
    dedupe = (e.bridge, ()) if e.bridge_needs_dedupe else ()
    status_carry = (
        ((e.bridge_status, "snapshot_status"),) if e.bridge_status else ()
    )
    status_extrema = (
        (
            Extremum(
                column="latest_status",
                source="snapshot_status",
                type=ColumnType.TEXT,
                description=(
                    f"{e.bridge_status} from that same latest row; '(none)' when "
                    "there are no rows or when the winning value is missing."
                ),
            ),
        )
        if e.bridge_status
        else ()
    )
    return build_rollup(
        mart=mart,
        shape_name="latest_snapshot",
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
                    (e.bridge_key, "snapshot_row_id"),
                    (e.bridge_timestamp, "snapshot_at"),
                )
                + status_carry
                + (
                    (e.bridge_amount, "snapshot_amount"),
                    (e.bridge_label, "snapshot_label"),
                ),
                rel_columns=(e.bridge_parent_fk, e.parent_key),
                description=(
                    f"Bring in {e.bridge}; a {e.parent} row with no matching "
                    f"{e.bridge} row is retained and receives the stated empty "
                    "snapshot values."
                ),
            ),
        ),
        measures=(
            Measure(
                column="event_count",
                expr='COUNT("snapshot_row_id")',
                type=ColumnType.BIGINT,
                description=(
                    # The independent implementer counted the LEFT-join
                    # placeholder itself (COUNT(*) = 1 for a childless jobs
                    # row; dlt__workable, batch10 run K, 2026-09-11).
                    f"Number of {e.bridge} rows for this {e.parent} row; "
                    "0 when there are none. "
                    # With reasoning the witness moved from COUNT(*) to
                    # COUNT(*) FILTER (WHERE measure IS NOT NULL): a linked
                    # row with no measure value is still a row (dlt__workable
                    # probe, 2026-09-11).
                    + (
                        f"Every linked {e.bridge} row counts, whether or not it "
                        f"carries {_article(e.bridge_amount)} {e.bridge_amount} value. "
                        if amount_may_be_missing
                        else ""
                    )
                    + f"{_article(e.parent, capitalized=True)} {e.parent} row kept with no "
                    f"{e.bridge} row reports 0 here, never 1: its placeholder "
                    f"holds no {e.bridge} row to count."
                ),
            ),
            Measure(
                column="lifetime_amount",
                expr='SUM("snapshot_amount")',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Sum of {e.bridge_amount} over all matching {e.bridge} rows; "
                    "0 when there are no rows"
                    + (
                        # A MIXED group was unstated: "over all matching rows"
                        # read as NULL propagating to the default (lefty02w,
                        # batch10 run G, 2026-09-11). The sibling argmax
                        # already says rows with no value add nothing.
                        f" and when none of those rows carries an "
                        f"{e.bridge_amount} value; a row with no "
                        f"{e.bridge_amount} value adds nothing, so a group "
                        "with some values sums the values it has"
                        if amount_may_be_missing
                        else ""
                    )
                    + "."
                ),
                input_nullable=e.bridge_amount_nullable,
            ),
        ),
        extrema=(
            Extremum(
                column="latest_row_id",
                source="snapshot_row_id",
                type=e.bridge_key_type,
                description=(
                    f"{e.bridge_key} of the row with the latest {e.bridge_timestamp}; "
                    f"ties take {tie_break_prose}. It is "
                    f"{_default_prose(e.bridge_key_type)} when there are no rows."
                    + (
                        # Rank every row by timestamp; the amount does not filter
                        # candidates for the latest-row calculation.
                        f" Every {e.bridge} row of the {e.parent} row ranks, "
                        f"whether or not it carries a {e.bridge_amount} value: "
                        f"the latest {e.bridge_timestamp} wins even when that "
                        f"row's {e.bridge_amount} is missing."
                        if amount_may_be_missing
                        else ""
                    )
                ),
            ),
        )
        + status_extrema
        + (
            Extremum(
                column="latest_amount",
                source="snapshot_amount",
                type=e.bridge_amount_type,
                description=(
                    f"{e.bridge_amount} from that same latest row; 0 when there "
                    "are no rows"
                    + (
                        " or when the winning value is missing"
                        if amount_may_be_missing
                        else ""
                    )
                    + "."
                ),
            ),
            Extremum(
                column="latest_label",
                source="snapshot_label",
                type=ColumnType.TEXT,
                description=(
                    f"{e.bridge_label} from that same latest row; '(none)' when "
                    "there are no rows"
                    + (
                        " or when the winning value is missing"
                        if label_may_be_missing
                        else ""
                    )
                    + "."
                ),
            ),
        ),
        extrema_order_by=order,
        extrema_tie_break="snapshot_row_id",
        extrema_tie_break_prose=tie_break_prose,
        extrema_order_nullable=e.bridge_timestamp_nullable,
        derived=(
            Derived(
                column="latest_amount_share",
                expr=(
                    "COALESCE(ROUND(CAST({latest_amount} AS DOUBLE) / "
                    "NULLIF({lifetime_amount}, 0), 4), 0.0)"
                ),
                type=ColumnType.FLOAT,
                description=(
                    "latest_amount divided by lifetime_amount as a fraction, "
                    "rounded to 4 decimal places; 0.0 when lifetime_amount is 0."
                    + (
                        # The numerator is the REPORTED latest_amount, after
                        # its default: a winning row with no value gives 0
                        # over a positive lifetime_amount, hence 0.0 — not a
                        # missing share (lefty02w, batch10 run E).
                        " The division uses latest_amount as this mart reports "
                        "it, after its default of 0 for a winning row whose "
                        f"{e.bridge_amount} is missing, so such a row gives 0.0."
                        if amount_may_be_missing
                        else ""
                    )
                ),
            ),
        ),
        dedupe=dedupe,
        attack_claims=(
            "inner_join",
            "no_null_default",
            "wrong_denominator",
            "wrong_window",
        )
        + (("no_dedup",) if e.bridge_needs_dedupe else ()),
        roles=FactRoles(
            link_key=e.bridge_key,
            measure=e.bridge_amount,
            label=e.bridge_label,
            predicate_column=e.bridge_status,
            predicate_pass=e.bridge_status_pass if e.bridge_status else (),
            predicate_fail=e.bridge_status_fail if e.bridge_status else (),
            period_column=e.bridge_timestamp,
        ),
        # Row P mirrors row A on the period axis: the latest row listed FIRST
        # for one parent and LAST for another, so the wrong_window claim does
        # not rest on which row an unordered window happens to emit first.
        witnesses=(
            WITNESS_CONTROL,
            WITNESS_CHILDLESS,
            WITNESS_SECOND_PERIOD,
            WITNESS_LATEST_FIRST,
        )
        + ((WITNESS_DUPLICATE,) if e.bridge_needs_dedupe else ()),
        grain_description=f"One row per {e.parent} row, keyed by {e.parent_key}.",
        notes=notes,
    )


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


ShapeBuilder = Callable[..., BuiltPlan]


@dataclass(frozen=True)
class ShapeRegistration:
    """One authoritative plan-library shape registration.

    ``shape_name`` is the builder's stable template identity;
    ``adapter_suffix`` is the schema adapter's mart-name suffix.
    ``adapter_auto_select`` controls policy, not capability. Registered shapes
    such as ``orphan_coverage`` can remain excluded from automatic selection to
    preserve existing mart rosters and task hashes.
    """

    shape_name: str
    builder: ShapeBuilder
    adapter_suffix: str
    adapter_auto_select: bool = True


#: The single authoritative plan-library registry.  Order is semantic policy:
#: richest evidence first, distinct relational programs before the generic
#: numeric argmax, with orphan coverage retained as an explicit final shape.
SHAPE_REGISTRY: tuple[ShapeRegistration, ...] = (
    ShapeRegistration("fan_out_rollup", fan_out_rollup, "rollup"),
    ShapeRegistration(
        "aggregate_then_filter",
        aggregate_then_filter,
        "having",
        adapter_auto_select=False,
    ),
    ShapeRegistration("status_cohort_union", status_cohort_union, "cohorts"),
    ShapeRegistration("temporal_grid", temporal_grid, "by_period"),
    ShapeRegistration("latest_snapshot", latest_snapshot, "snapshot"),
    ShapeRegistration("categorical_ladder", categorical_ladder, "bands"),
    ShapeRegistration(
        "measure_state_distribution", measure_state_distribution, "distribution"
    ),
    ShapeRegistration("argmax_profile", argmax_profile, "top"),
    ShapeRegistration(
        "orphan_coverage",
        orphan_coverage,
        "orphan_coverage",
        adapter_auto_select=False,
    ),
)


def registered_shape_builders(
    *, adapter_auto_select: bool = False
) -> tuple[tuple[str, ShapeBuilder], ...]:
    """Return a stable view of the registry for a concrete consumer.

    The default view uses canonical shape names and includes every registered
    shape (tests, proofs, and direct plan-library selection).  The adapter view
    uses mart suffixes and includes only shapes whose selection policy is
    enabled.  Returning tuples keeps callers deterministic and prevents them
    from mutating registry state.
    """

    if adapter_auto_select:
        return tuple(
            (registration.adapter_suffix, registration.builder)
            for registration in SHAPE_REGISTRY
            if registration.adapter_auto_select
        )
    return tuple(
        (registration.shape_name, registration.builder)
        for registration in SHAPE_REGISTRY
    )


#: Backward-compatible view used by ``select_shapes`` and by callers that
#: temporarily inject a builder to test the trial protocol.  It is derived
#: from ``SHAPE_REGISTRY``; do not add shapes directly to this tuple.
SHAPE_BUILDERS: tuple[tuple[str, ShapeBuilder], ...] = registered_shape_builders()


def select_shapes(
    evidence: ChainEvidence, *, mart_prefix: str, budget: int = 2
) -> tuple[BuiltPlan, ...]:
    """Every shape this schema's EVIDENCE supports, up to the mart budget, in
    deterministic SHAPE_BUILDERS order.

    Raises when the evidence supports nothing — no falling back to a narrow
    projection. Only `ShapeNotSelectable` and `MartBudgetError` mean "not on this
    schema"; any other ValueError from a builder is a bug and propagates.
    """
    built: list[BuiltPlan] = []
    for name, builder in SHAPE_BUILDERS:
        if len(built) >= budget:
            break
        try:
            built.append(builder(evidence, mart=f"{mart_prefix}_{name}"))  # type: ignore[operator]
        except (ShapeNotSelectable, MartBudgetError):
            # The two declines the trial protocol exists for; any OTHER
            # ValueError is a builder bug and propagates.
            continue
    if not built:
        raise ValueError(
            f"no plan-library shape is selectable on this schema (mart_prefix="
            f"{mart_prefix!r}); do not fall back to a narrow projection"
        )
    return tuple(built)

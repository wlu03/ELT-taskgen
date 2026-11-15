"""Shared plan vocabulary, typed operations, and source-grounded naming."""

from __future__ import annotations
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

from sqlglot import exp

from elt_taskgen.generation.witness import (
    WINDOW_MIRROR_WITNESSES,
    WITNESS_ALL_FAIL,
    WITNESS_ARGMAX_CASE_ORDER,
    WITNESS_ARGMAX_NULL_ORDER,
    WITNESS_BELOW_THRESHOLD,
    WITNESS_BRIDGE_NO_CHILD,
    WITNESS_CHILDLESS,
    WITNESS_CONTROL,
    WITNESS_DISTINCT_MEASURE,
    WITNESS_DUPLICATE,
    WITNESS_LATEST_FIRST,
    WITNESS_NULL_MEASURE,
    WITNESS_ON_THRESHOLD,
    WITNESS_OUT_OF_DOMAIN,
    WITNESS_SAME_CHILD,
    WITNESS_SECOND_PERIOD,
    WITNESS_TIE,
)
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
from elt_taskgen.generation.plan_validation import (
    AGGREGATE_FAMILY_KINDS,
    PROJECTION_KINDS,
    ShapeNotSelectable,
    _DENOMINATOR_HINTS,
    _NULL_DEFAULT_HINTS,
    _PLAN_LEVEL_KINDS,
    _op_text,
    _parse_expression,
    _parse_select_list,
    column_kind_problems_for,
    is_distinct_aggregate_expr,
    is_filtered_aggregate_expr,
    label_problems,
    measure_items,
    op_problems,
    predicate_governs,
)

_COMPILER_ONLY_DETAIL_KEYS = frozenset({"select", "sql", "name"})

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
class FactBranch:
    """One fact's complete FK binding and optional additive measure."""

    table: str
    child_columns: tuple[str, ...]
    parent_columns: tuple[str, ...]
    measure: str = ""
    measure_type: ColumnType = ColumnType.INTEGER


@dataclass(frozen=True)
class BranchedFactCandidate:
    """A proven parent grain with two independently counted fact sources."""

    grain_table: str
    grain_key: tuple[str, ...]
    key_types: tuple[ColumnType, ...]
    branches: tuple[FactBranch, ...]
    attributes: tuple[tuple[str, ColumnType], ...] = ()
    extra_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class BranchedFactShape(StarShape):
    """Small extension for the two-branch builder and its row constructor."""

    branches: tuple[FactBranch, ...] = ()


@dataclass(frozen=True)
class BuiltPlan:
    plan: MartPlan
    shape: StarShape
    #: The mart's COLUMN CONTRACT when the builder knows it. Legacy builders
    #: leave it empty (their adapters mint MartColumns); every plan-library
    #: shape fills it, which is what makes the budget a contract, not a hope.
    columns: tuple[MartColumn, ...] = ()
    #: template column name -> the dataset-specific name the mart gave it.
    #: A wrapper shape (`aggregate_then_filter`) names one of the base mart's
    #: columns after the base plan is built, and the template name it knows is
    #: no longer the column's name.
    renamed_columns: tuple[tuple[str, str], ...] = ()

    def column_named(self, template: str) -> str:
        """This mart's actual name for a template column name."""
        return dict(self.renamed_columns).get(template, template)

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

MAX_DATASET_COLUMN_CHARS = 52


def _slug(value: str) -> str:
    """A snake_case identifier fragment, or '' when nothing usable is left."""
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return re.sub(r"_+", "_", text).strip("_")


def _dataset_name(template: str, parts: Sequence[str]) -> str:
    """One dataset-specific name from `parts`, or '' when a part is missing.

    A missing part means the shape does not carry that role for this chain, and
    the template name stands: a half-named column reads worse than a plain one.
    """
    slugs = [_slug(part) for part in parts]
    if not all(slugs):
        return ""
    name = "_".join(slugs)
    if not name or name[0].isdigit() or len(name) > MAX_DATASET_COLUMN_CHARS:
        return ""
    return name if name != template else ""

_GENERIC_KEY_COLUMNS = frozenset(
    {"id", "key", "code", "uuid", "guid", "pk", "no", "num", "number", "index"}
)

_GENERIC_ATTR_COLUMNS = frozenset({"name", "title", "label", "description", "text"})


def _singular(table: str) -> str:
    """`employees` -> `employee`, for qualifying a generic column name."""
    text = _slug(table)
    if text.endswith("ies") and len(text) > 4:
        return text[:-3] + "y"
    if any(text.endswith(s) for s in ("ses", "xes", "zes", "ches", "shes")):
        return text[:-2]
    if text.endswith("s") and not text.endswith("ss") and len(text) > 3:
        return text[:-1]
    return text


def _qualified(source: str, table: str, generic: frozenset[str]) -> str:
    """`source`, or `<table-singular>_<source>` when `source` says nothing."""
    column = _slug(source)
    if not column or column not in generic:
        return column
    prefix = _singular(table)
    return f"{prefix}_{column}" if prefix and not column.startswith(prefix) else column


@dataclass(frozen=True)
class _NameEvidence:
    """The chain nouns a dataset-specific mart column name is built from."""

    parent: str = ""
    bridge: str = ""
    child: str = ""
    measure: str = ""
    label: str = ""
    status: str = ""
    link_key: str = ""
    key_source: str = ""
    attr_source: str = ""

_DATASET_COLUMN_RULES: dict[str, Callable[[_NameEvidence], tuple[str, ...]]] = {
    # grain and carried attributes: the source column the mart projects
    "parent_key": lambda e: (_qualified(e.key_source, e.parent, _GENERIC_KEY_COLUMNS),),
    "entity_key": lambda e: (_qualified(e.key_source, e.parent, _GENERIC_KEY_COLUMNS),),
    "parent_name": lambda e: (
        _qualified(e.attr_source, e.parent, _GENERIC_ATTR_COLUMNS),
    ),
    "entity_name": lambda e: (
        _qualified(e.attr_source, e.parent, _GENERIC_ATTR_COLUMNS),
    ),
    "owner_name": lambda e: (
        _qualified(e.attr_source, e.parent, _GENERIC_ATTR_COLUMNS),
    ),
    # counts over the linked rows
    "link_count": lambda e: (e.bridge, "count"),
    "event_count": lambda e: (e.bridge, "count"),
    "child_count": lambda e: (e.bridge, "count"),
    "matched_count": lambda e: ("matched", e.bridge, "count"),
    "orphan_count": lambda e: ("orphan", e.bridge, "count"),
    "distinct_child_count": lambda e: ("distinct", e.child, "count"),
    "distinct_status_count": lambda e: ("distinct", e.status, "count"),
    "distinct_amount_count": lambda e: ("distinct", e.measure, "count"),
    "row_count": lambda e: (e.bridge, "count"),
    "active_link_count": lambda e: (e.status, e.bridge, "count"),
    "active_event_count": lambda e: (e.status, e.bridge, "count"),
    "passing_count": lambda e: (e.status, "pass", "count"),
    "failing_count": lambda e: (e.status, "fail", "count"),
    "tied_count": lambda e: ("top", e.measure, "tie_count"),
    # measure totals and extremes
    "total_amount": lambda e: ("total", e.measure),
    "total_measure": lambda e: ("total", e.measure),
    "lifetime_amount": lambda e: ("lifetime", e.measure),
    "period_amount": lambda e: ("period", e.measure),
    "max_amount": lambda e: ("max", e.measure),
    "active_amount": lambda e: (e.status, e.measure),
    "matched_amount": lambda e: ("matched", e.measure),
    "latest_amount": lambda e: ("latest", e.measure),
    "top_measure": lambda e: ("top", e.measure),
    "running_amount": lambda e: ("running", e.measure),
    "prev_period_amount": lambda e: ("prev_period", e.measure),
    # labels, ids and states carried from the winning row
    "top_label": lambda e: (
        "top", _qualified(e.label, e.bridge, _GENERIC_ATTR_COLUMNS),
    ),
    "top_row_id": lambda e: (
        "top", _qualified(e.link_key, e.bridge, _GENERIC_KEY_COLUMNS),
    ),
    "latest_label": lambda e: (
        "latest", _qualified(e.label, e.bridge, _GENERIC_ATTR_COLUMNS),
    ),
    "latest_row_id": lambda e: (
        "latest", _qualified(e.link_key, e.bridge, _GENERIC_KEY_COLUMNS),
    ),
    "latest_status": lambda e: ("latest", e.status),
    "parent_status": lambda e: (e.parent, e.status),
    "status_group": lambda e: (e.status, "group"),
    "cohort": lambda e: (e.status, "cohort"),
    "measure_state": lambda e: (e.measure, "state"),
    "has_links": lambda e: ("has", e.bridge),
    "tie_state": lambda e: ("top", e.measure, "tie_state"),
    # ratios and bands
    "active_amount_ratio": lambda e: (e.status, e.measure, "share"),
    "top_measure_share": lambda e: ("top", e.measure, "share"),
    "max_amount_share": lambda e: ("max", e.measure, "share"),
    "latest_amount_share": lambda e: ("latest", e.measure, "share"),
    "period_share": lambda e: ("period", e.measure, "share"),
    "passing_ratio": lambda e: (e.status, "pass", "share"),
    "match_rate": lambda e: ("matched", e.bridge, "share"),
    "size_band": lambda e: (e.bridge, "count_band"),
    "adoption_band": lambda e: (e.status, "pass", "band"),
    "coverage_band": lambda e: ("matched", e.bridge, "band"),
    "trend": lambda e: (e.measure, "trend"),
}


def _dataset_column_names(
    templates: Sequence[str], evidence: _NameEvidence, *, reserved: Iterable[str] = ()
) -> dict[str, str]:
    """template name -> dataset-specific name, for the names that can have one.

    Skips a proposal that is unusable, that collides with another mart column,
    or that a source column already answers to under a different role.
    """
    taken = {name.lower() for name in reserved} | set(templates)
    out: dict[str, str] = {}
    for template in templates:
        rule = _DATASET_COLUMN_RULES.get(template)
        if rule is None:
            continue
        proposed = _dataset_name(template, rule(evidence))
        if not proposed or proposed in taken:
            continue
        out[template] = proposed
        taken.add(proposed)
    return out

_PROSE_AMBIGUOUS_NAMES = frozenset({"cohort"})


def _renamed_text(text: str, names: Mapping[str, str]) -> str:
    """`text` with every whole-word template column name replaced.

    This is the SQL-safe rewrite: a projection that aliases a column must move
    with the column, so every name is replaced here.
    """
    if not text or not names:
        return text
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
    return pattern.sub(lambda m: names[m.group(1)], text)


def _renamed_prose(text: str, names: Mapping[str, str]) -> str:
    """`text` with the column names replaced, except the ordinary words.

    Prose is where "cohort" means a cohort rather than the column; the SQL
    that aliases the column still goes through `_renamed_text`.
    """
    return _renamed_text(
        text,
        {
            template: name
            for template, name in names.items()
            if template not in _PROSE_AMBIGUOUS_NAMES
        },
    )


def _dataset_rename_map(
    *,
    keys: tuple[KeyColumn, ...],
    passthrough: tuple[Passthrough, ...],
    measures: tuple[Measure, ...],
    extrema: tuple[Extremum, ...],
    post_windows: tuple[WindowExpr, ...],
    derived: tuple[Derived, ...],
    parent: str,
    hops: tuple[StarJoin, ...],
    windows: tuple[WindowExpr, ...],
    roles: FactRoles,
    child: str,
) -> dict[str, str]:
    """template column name -> the dataset-specific name this rollup gives it.

    Empty when the chain names nothing usable, and every name the map leaves
    out keeps its template spelling.
    """
    templates = [
        *(k.column for k in keys),
        *(p.column for p in passthrough),
        *(m.column for m in measures),
        *(x.column for x in extrema),
        # POST-aggregate windows are mart columns. The pre-aggregate ones are
        # plan-internal aliases, renamed only where a mart column shares the
        # spelling, which the same map handles.
        *(w.alias for w in post_windows),
        *(d.column for d in derived),
    ]
    # A carried attribute states its source column on the hop that carries it.
    hop_carry = {
        hop.table: {alias: source for source, alias in hop.carry} for hop in hops
    }
    carried_source = ""
    if passthrough:
        first = passthrough[0]
        carried_source = first.source or hop_carry.get(first.from_hop or "", {}).get(
            first.column, ""
        )
    evidence = _NameEvidence(
        parent=parent,
        bridge=hops[0].table if hops else "",
        child=child or (hops[1].table if len(hops) > 1 else ""),
        measure=roles.measure,
        label=roles.label,
        status=roles.predicate_column,
        link_key=roles.link_key,
        key_source=keys[0].source if keys and not keys[0].expr else "",
        attr_source=carried_source,
    )
    source_columns = {
        *(k.source for k in keys if k.source),
        *(p.source for p in passthrough if p.source),
        *(x.source for x in extrema if x.source),
        *(source for carry in hop_carry.values() for source in carry.values()),
        *(right for hop in hops for _, right in hop.on_pairs),
    }
    # A source column that happens to be spelled like a template name shares
    # the plan's string namespace, so renaming that name would rewrite the
    # source reference too. Leave those names alone.
    templates = [t for t in templates if t not in source_columns]
    # What a new name must not shadow: the relation aliases the ops carry
    # values under. A SOURCE column of a joined table is always referenced
    # through its own table, so a mart column may share its spelling — that is
    # how a key named `id` on `employees` becomes `employee_id`.
    reserved = {
        parent,
        child,
        *(h.table for h in hops),
        *(alias for hop in hops for _, alias in hop.carry),
        *(w.alias for w in windows),
    }
    return _dataset_column_names(templates, evidence, reserved=reserved)


def _rename_built_plan(built: BuiltPlan, names: Mapping[str, str]) -> BuiltPlan:
    """`built` with its template column names replaced throughout.

    `build_rollup` renames its INPUTS, before it assembles anything. The two
    union shapes assemble their ops directly, so the same map is applied to the
    assembled plan: the ops carry the name through every intermediate relation,
    and the final projection is what the mart column contract states. Relation
    names are NOT columns and keep their spelling (`tables`, `details['name']`).
    """
    if not names:
        return built

    def named(column: str) -> str:
        return names.get(column, column)

    def says(text: str) -> str:
        return _renamed_text(text, names)

    def reads(text: str) -> str:
        return _renamed_prose(text, names)

    ops = tuple(
        op.model_copy(
            update={
                "description": reads(op.description),
                "columns": tuple(named(column) for column in op.columns),
                "predicate": says(op.predicate),
                # A GROUP BY op keys its details by the measure ALIAS, so the
                # key is a column name too; `name` is the relation's.
                "details": {
                    named(key): value if key == "name" else says(value)
                    for key, value in op.details.items()
                },
            }
        )
        for op in built.plan.ops
    )
    plan = built.plan.model_copy(
        update={"ops": ops, "notes": reads(built.plan.notes)}
    )
    columns = tuple(
        column.model_copy(
            update={
                "name": named(column.name),
                "description": reads(column.description),
            }
        )
        for column in built.columns
    )
    shape = replace(
        built.shape,
        **{
            field: tuple(named(column) for column in getattr(built.shape, field))
            for field in (
                "key_columns",
                "null_capable_measures",
                "distinct_measures",
                "filtered_measures",
                "ratio_measures",
                "constant_divisor_measures",
                "ranked_measures",
                "conditional_columns",
                "window_measures",
                "fanout_capable_measures",
                "all_null_aggregate_witnesses",
                "round_before_sum_witnesses",
            )
        },
    )
    return BuiltPlan(
        plan=plan,
        shape=shape,
        columns=columns,
        renamed_columns=tuple(sorted(names.items())),
    )


def _union_shape_names(
    evidence: ChainEvidence, templates: Sequence[str]
) -> dict[str, str]:
    """The rename map for a shape assembled outside `build_rollup`."""
    name_evidence = _NameEvidence(
        parent=evidence.parent,
        bridge=evidence.bridge,
        child=evidence.child,
        measure=evidence.bridge_amount,
        label=evidence.bridge_label,
        status=evidence.bridge_status,
        link_key=evidence.bridge_key,
        key_source=evidence.parent_key,
        attr_source=evidence.parent_attr,
    )
    source_columns = {
        evidence.parent_key,
        evidence.parent_attr,
        evidence.bridge_key,
        evidence.bridge_parent_fk,
        evidence.bridge_child_fk,
        evidence.bridge_status,
        evidence.bridge_amount,
        evidence.bridge_label,
        evidence.bridge_timestamp,
        evidence.child_key,
        evidence.child_label,
    } - {""}
    return _dataset_column_names(
        [t for t in templates if t not in source_columns],
        name_evidence,
        # The aliases these shapes carry their joined values under.
        reserved={evidence.parent, evidence.bridge, evidence.child}
        | {"link_key", "link_status", "link_amount"},
    )


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

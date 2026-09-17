"""Compile findings and catalogue entries into executable SQL or load mutants.

Every mutant runs across populations and is scored through `upstream_eval`. Inert
mutations raise because they provide no evidence.
"""

from __future__ import annotations

import csv
import datetime
import decimal
import io
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from elt_taskgen.models import (
    AttackCase,
    AttackKind,
    Backend,
    ColumnType,
    Finding,
    MartOpKind,
    MartSpec,
    PopulationName,
    ProposedAttackCase,
    RLVR_TASK_VARIANTS,
    Row,
    Severity,
    SemanticPattern,
    TableSpec,
    TaskIR,
    TaskVariant,
    canonical_json,
)
from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection
from elt_taskgen.sql_identifiers import quote_sql_identifier

#: Sandbox limits for untrusted mutant SQL, pinned to `SemanticLimits` by tests.
ATTACK_MEMORY_LIMIT_MB = 512
ATTACK_THREADS = 1
#: Row/byte caps on one mutant mart's materialized output; exceeding either
#: is recorded as an execution error (a crash, never a kill).
ATTACK_MAX_RESULT_ROWS_PER_MART = 100_000
ATTACK_MAX_RESULT_BYTES_PER_MART = 16 * 1024 * 1024

HARDCODE_DIRECTIVE_PREFIX = "directive:hardcode-population-outputs:"
KIND_DIRECTIVE_PREFIX = "directive:kind:"
#: EL mutation of the RENDERED ARTIFACTS: ``directive:load:<name>[:<arg>]``.
LOAD_DIRECTIVE_PREFIX = "directive:load:"
#: Exact critic-to-compiler handoff.  The suffix is canonical JSON describing
#: one closed operation; unlike a coarse kind enum it preserves the mart,
#: tables, backend, and semantic alternative the critic actually named.
STRUCTURED_DIRECTIVE_PREFIX = "directive:structured:"

#: Case-name prefix for a promoter-compiled proposal (one per finding).
PROPOSAL_CASE_PREFIX = "proposed__"

#: Params key by which a proposal asks for the output-emission directive.
HARDCODE_PARAM = "hardcode_population"
#: Params key selecting one named member of ``KIND_VARIANTS``.  A named
#: variant is never inferred merely because it is the only registered member:
#: the critic must state the exact wrong implementation it means to test.
VARIANT_PARAM = "variant"

#: Written next to the case's rewards.json when a proposal is REJECTED.
REJECTED_PROPOSAL_FILENAME = "rejected_proposal.json"
MUTATION_FIDELITY_FILENAME = "mutation_fidelity.json"

#: Closed executable parameter grammar.  Unknown keys are rejected before any
#: reward is measured; silently ignoring one would recreate the audit defect.
PROPOSAL_PARAM_KEYS: frozenset[str] = frozenset(
    {
        HARDCODE_PARAM,
        VARIANT_PARAM,
        "copy_mart",
        "target_mart",
        "skip_backend",
        "skip_tables",
        "zero_is_missing",
        "add_dedup",
        "remove_dedup",
        "dedup_table",
    }
)

_DUCK_TYPES: dict[ColumnType, str] = {
    ColumnType.INTEGER: "INTEGER",
    ColumnType.BIGINT: "BIGINT",
    ColumnType.FLOAT: "DOUBLE",
    ColumnType.DECIMAL: "DOUBLE",
    ColumnType.TEXT: "VARCHAR",
    ColumnType.BOOLEAN: "BOOLEAN",
    ColumnType.DATE: "DATE",
    ColumnType.TIMESTAMP: "TIMESTAMP",
    ColumnType.JSON: "JSON",
}


# Compilation: standing catalogue + findings -> AttackCases

#: Finding-text markers for OUTPUT EMISSION: compile to hardcode, not constants.
_OUTPUT_EMISSION_MARKERS = ("verbatim", "hardcod", "hard-cod", "hard cod")


def _emission_directive(finding: Finding) -> str | None:
    """'directive:hardcode-population-outputs:<pop>' when the finding proposes
    emitting a population's outputs, else None. The population is the first one
    named (enum order, deterministic), defaulting to DEVELOPMENT."""
    if finding.suggested_attack not in (AttackKind.CONSTANTS, AttackKind.CUSTOM):
        return None
    text = " ".join(f"{finding.summary} {finding.detail}".lower().split())
    if "output" not in text:
        return None
    if not any(marker in text for marker in _OUTPUT_EMISSION_MARKERS):
        return None
    population = PopulationName.DEVELOPMENT
    for pop in PopulationName:
        if pop.value in text:
            population = pop
            break
    return f"{HARDCODE_DIRECTIVE_PREFIX}{population.value}"


def _default_attack_directive(kind: AttackKind) -> str | None:
    """Return the kind's explicitly registered default directive, if any.

    Load mutations have their own closed grammar.  AST kinds compile without a
    named variant only when the registry contains ``""``.  In particular,
    ``wrong_agg_stage`` and ``custom`` have no implicit/default realization.
    """
    if kind.value in LOAD_MUTATIONS:
        return f"{LOAD_DIRECTIVE_PREFIX}{kind.value}"
    if kind in DIRECTIVE_ONLY_KIND_DEFAULTS:
        return f"{KIND_DIRECTIVE_PREFIX}{kind.value}"
    if "" not in KIND_VARIANTS.get(kind, frozenset()):
        return None
    return f"{KIND_DIRECTIVE_PREFIX}{kind.value}"


def compile_attacks(task: TaskIR, findings: list[Finding]) -> tuple[AttackCase, ...]:
    """Standing catalogue plus one informational case per finding naming a
    `suggested_attack`; catalogue first, then findings by id, dupes collapsed.

    An output-emission finding compiles to the hardcode directive: real frozen
    gold rows test a shortcut a lossy constants mutation does not.
    """
    cases: list[AttackCase] = list(task.attack_cases)
    seen = {c.name for c in cases}
    for finding in sorted(findings, key=lambda f: f.finding_id):
        if finding.proposed_case is not None:
            # The structured proposal is authoritative; recompiling only its attack
            # kind would discard parameters and test a different claim.
            continue
        if finding.suggested_attack is None:
            continue
        if finding.severity == Severity.INFO:
            continue
        name = f"finding__{finding.finding_id}"
        if name in seen:
            continue
        mutation = _emission_directive(finding)
        if mutation is None:
            mutation = _default_attack_directive(finding.suggested_attack)
        if mutation is None:
            # Never infer a variant from prose. Major claims without a case stay
            # unresolved; lower-severity suggestions may have no executable probe.
            continue
        seen.add(name)
        description = (
            f"Compiled from {finding.role.value} finding "
            f"{finding.finding_id}: {finding.summary}"
        )
        if finding.detail:
            description += f" — {finding.detail}"
        cases.append(
            AttackCase(
                name=name,
                kind=finding.suggested_attack,
                description=description,
                mutation=mutation,
                expected_pass={},
                required=False,
                source_finding=finding.finding_id,
            )
        )
    return tuple(cases)


# Materialization: AttackCase -> mutated SQL per mart

def _reference_sql(task: TaskIR) -> dict[str, str]:
    if task.reference is None:
        raise ValueError(f"task {task.task_id}: no reference solution to mutate")
    missing = [m.name for m in task.marts if m.name not in task.reference.sql_by_mart]
    if missing:
        raise ValueError(f"task {task.task_id}: reference SQL missing for marts {missing}")
    return {m.name: task.reference.sql_by_mart[m.name] for m in task.marts}


def _gold_stage2_csv(gold: object, population_value: str, mart: str) -> str:
    stage2 = getattr(gold, "stage2_csv", None)
    if not isinstance(stage2, dict):
        raise ValueError("gold bundle has no stage2_csv — refusing to materialize (fail closed)")
    try:
        return stage2[population_value][mart]
    except KeyError as e:
        raise ValueError(
            f"gold bundle missing stage-2 CSV for population {population_value!r} "
            f"mart {mart!r} (fail closed)"
        ) from e


def _sql_literal(raw: str, ctype: ColumnType) -> str:
    duck = _DUCK_TYPES[ctype]
    if raw == "":
        return f"CAST(NULL AS {duck})"
    if ctype in (ColumnType.INTEGER, ColumnType.BIGINT):
        return f"CAST({int(float(raw))} AS {duck})"
    if ctype in (ColumnType.FLOAT, ColumnType.DECIMAL):
        return f"CAST({float(raw)!r} AS {duck})"
    if ctype is ColumnType.BOOLEAN:
        return "TRUE" if raw.strip().lower() in ("true", "1", "t") else "FALSE"
    escaped = raw.replace("'", "''")
    return f"CAST('{escaped}' AS {duck})"


def _literal_select(mart: MartSpec, csv_text: str) -> str:
    """A SELECT that emits the given canonical gold CSV rows as literals."""
    reader = csv.reader(io.StringIO(csv_text))
    table = list(reader)
    if not table:
        raise ValueError(f"mart {mart.name}: empty gold CSV — cannot hardcode")
    header, data = table[0], table[1:]
    types = {c.name: c.type for c in mart.columns}
    unknown = [h for h in header if h not in types]
    if unknown:
        raise ValueError(f"mart {mart.name}: gold CSV columns {unknown} not in mart spec")
    cols = ", ".join(
        quote_sql_identifier(h, dialect="duckdb", force=True) for h in header
    )
    if not data:
        nulls = ", ".join(
            f"CAST(NULL AS {_DUCK_TYPES[types[h]]}) AS "
            f"{quote_sql_identifier(h, dialect='duckdb', force=True)}"
            for h in header
        )
        return f"SELECT {nulls} WHERE FALSE"
    values = ",\n".join(
        "(" + ", ".join(_sql_literal(v, types[h]) for h, v in zip(header, row)) + ")"
        for row in data
    )
    return f"SELECT {cols} FROM (VALUES\n{values}\n) AS __hardcoded({cols})"


def _outer_select(ast: exp.Expression) -> exp.Select:
    if isinstance(ast, exp.Select):
        return ast
    raise ValueError("mutation requires a plain SELECT statement at the top level")


def _constant_for(ctype: ColumnType) -> exp.Expression:
    duck = _DUCK_TYPES[ctype]
    if ctype in (
        ColumnType.INTEGER,
        ColumnType.BIGINT,
        ColumnType.FLOAT,
        ColumnType.DECIMAL,
    ):
        return exp.cast(exp.Literal.number("0"), duck)
    if ctype is ColumnType.BOOLEAN:
        return exp.false()
    return exp.cast(exp.Literal.string(""), duck)


#: Separates kind from variant in ``directive:kind:<kind>[@<variant>]``; a
#: variant is a DIFFERENT wrong implementation of the same kind.
VARIANT_SEPARATOR = "@"

#: Closed (kind, variant) catalogue: an unknown variant RAISES, never defaults.
KIND_VARIANTS: dict[AttackKind, frozenset[str]] = {
    AttackKind.INNER_JOIN: frozenset({"", "second_hop"}),
    AttackKind.NO_DEDUP: frozenset({""}),
    AttackKind.NO_NULL_DEFAULT: frozenset({"", "zero_is_missing"}),
    AttackKind.DROPPED_FILTER: frozenset({"", "filter_to_where"}),
    AttackKind.CONSTANTS: frozenset({""}),
    AttackKind.KEYS_ONLY: frozenset({""}),
    AttackKind.WRONG_GRAIN: frozenset({""}),
    AttackKind.WRONG_AGG_STAGE: frozenset({"filter_before_aggregate"}),
    AttackKind.WRONG_DENOMINATOR: frozenset({"", "filtered_denominator"}),
    AttackKind.WRONG_WINDOW: frozenset({"", "drop_frame"}),
    AttackKind.CUSTOM: frozenset(
        {
            "add_dedup",
            "argmax_as_max",
            "wrong_boundary_else",
            "wrong_boundary_inclusive",
        }
    ),
}

#: Kind directives handled outside the AST variant catalogue.  Listing these
#: explicitly preserves the closed grammar without pretending they have an
#: ``_apply_kind`` rule.
DIRECTIVE_ONLY_KIND_DEFAULTS: frozenset[AttackKind] = frozenset(
    {AttackKind.NO_OP, AttackKind.SKIP_EXTRACTION}
)

#: Whole-submission kinds need no mart list. Every other kind on a multi-mart
#: task must name every mart it touches.
TASK_WIDE_KINDS: frozenset[AttackKind] = DIRECTIVE_ONLY_KIND_DEFAULTS | frozenset(
    {AttackKind.CONSTANTS, AttackKind.KEYS_ONLY}
)


def allowed_kind_variants(kind: AttackKind) -> tuple[str, ...]:
    """Return sorted named variants accepted for an attack kind.

    This public list excludes the unnamed default. Directive-only and load-mutation
    kinds may return no named variants.
    """
    return tuple(sorted(v for v in KIND_VARIANTS.get(kind, frozenset()) if v))


def all_kind_variant_names() -> tuple[str, ...]:
    """Every named variant of every kind, sorted: the closed public vocabulary
    of ``params.variant`` (what the prompt contract publishes)."""
    return tuple(
        sorted({v for variants in KIND_VARIANTS.values() for v in variants if v})
    )


def kind_variant_contract() -> str:
    """Return the public projection of registered attack variants.

    Prompts and tool schemas share this output. `default` denotes the unnamed registry
    member; `required(...)` means the kind has no executable default.
    """
    entries: list[str] = []
    for kind, variants in sorted(
        KIND_VARIANTS.items(), key=lambda item: item[0].value
    ):
        named = sorted(value for value in variants if value)
        if "" in variants:
            realization = "default"
            if named:
                realization += "|" + "|".join(named)
        else:
            realization = "required(" + "|".join(named) + ")"
        entries.append(f"{kind.value}->{realization}")
    return (
        "Exact kind/variant registry (default means omit params.variant): "
        + "; ".join(entries)
        + "."
    )


def _structured_directive(payload: dict[str, object]) -> str:
    return STRUCTURED_DIRECTIVE_PREFIX + canonical_json(payload)


def parse_structured_directive(text: str) -> dict[str, object]:
    """Decode one exact proposal directive and reject loose JSON shapes."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"structured mutation is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("operation"), str):
        raise ValueError(
            "structured mutation must be an object with a string 'operation'"
        )
    return {str(k): v for k, v in payload.items()}


def split_kind_directive(text: str) -> tuple[AttackKind, str]:
    """'<kind>' or '<kind>@<variant>' -> (kind, variant); raises on unknown."""
    kind_text, _, variant = text.partition(VARIANT_SEPARATOR)
    kind = AttackKind(kind_text)
    allowed = KIND_VARIANTS.get(
        kind,
        frozenset({""}) if kind in DIRECTIVE_ONLY_KIND_DEFAULTS else frozenset(),
    )
    if variant not in allowed:
        raise ValueError(
            f"attack kind {kind.value!r} has no variant {variant!r} "
            f"(known: {sorted(allowed)})"
        )
    return kind, variant


def _filtered_aggregate_cases(ast: exp.Expression) -> list[exp.Case]:
    """CASE nodes INSIDE an aggregate — filtered aggregates — in document order.

    NOT a conditional LADDER (``CASE WHEN COUNT(x)=0 THEN ... END``), where the
    aggregate sits inside the CASE and walking aggregates downward never sees it.
    """
    found: list[exp.Case] = []
    seen: set[int] = set()
    for agg in ast.find_all(exp.AggFunc):
        for case in agg.find_all(exp.Case):
            if id(case) not in seen:
                seen.add(id(case))
                found.append(case)
    return found


def _unwrap_case(case: exp.Case) -> bool:
    """Replace ``CASE WHEN p THEN e [ELSE d] END`` with ``e``. False if empty."""
    ifs = case.args.get("ifs") or []
    if not ifs:
        return False
    case.replace(ifs[0].args["true"].copy())
    return True


_STRICTER: dict[type[exp.Expression], type[exp.Expression]] = {
    exp.GT: exp.GTE,
    exp.GTE: exp.GT,
    exp.LT: exp.LTE,
    exp.LTE: exp.LT,
}


def _dedupe_source_scans(
    ast: exp.Expression,
    target_table: str | None = None,
    allowed_tables: frozenset[str] | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Wrap matching base-table scans in ``SELECT DISTINCT *`` subqueries.

    This represents the concrete alternative "deduplicate physical source rows
    before applying the mart logic".  Adding DISTINCT to the final aggregate
    output would be inert on one-row-per-key marts and would not test that claim.
    """
    changed: list[str] = []
    for table in list(ast.find_all(exp.Table)):
        name = table.name
        if allowed_tables is not None and name not in allowed_tables:
            continue
        if target_table is not None and name != target_table:
            continue
        alias_name = table.alias or name
        inner = table.copy()
        inner.set("alias", None)
        query = exp.select("*").distinct().from_(inner)
        table.replace(
            exp.Subquery(
                this=query,
                alias=exp.TableAlias(this=exp.to_identifier(alias_name)),
            )
        )
        changed.append(name)
    return bool(changed), tuple(sorted(set(changed)))


def _apply_kind(
    kind: AttackKind, sql: str, mart: MartSpec, variant: str = ""
) -> str | None:
    """Apply one wrong-logic AST mutation; None when it does not apply here."""
    ast = sqlglot.parse_one(sql, read="duckdb")
    changed = False

    if kind is AttackKind.INNER_JOIN:
        # Only joins to source tables count as hops. Reattaching an extremal-row
        # CTE is internal and flipping it cannot test a second hop.
        attach_ctes = {
            cte.alias_or_name
            for cte in ast.find_all(exp.CTE)
            if isinstance(cte.this, exp.Select) and cte.this.args.get("qualify") is not None
        }
        joins = [
            j
            for j in ast.find_all(exp.Join)
            if j.side in ("LEFT", "RIGHT", "FULL")
            and not (isinstance(j.this, exp.Table) and j.this.name in attach_ctes)
        ]
        if variant == "second_hop":
            # Only the LAST hop flips, separating "parent has no bridge row"
            # from "bridge row has no child row"; the default flips all joins.
            joins = joins[1:]
        for join in joins:
            join.set("side", None)
            join.set("kind", "INNER")
            changed = True

    elif kind is AttackKind.NO_DEDUP:
        for cnt in ast.find_all(exp.Count):
            if isinstance(cnt.this, exp.Distinct) and cnt.this.expressions:
                cnt.set("this", cnt.this.expressions[0].copy())
                changed = True
        for select in ast.find_all(exp.Select):
            if select.args.get("distinct"):
                select.set("distinct", None)
                changed = True

    elif kind is AttackKind.NO_NULL_DEFAULT:
        if variant == "zero_is_missing":
            for coalesce in list(ast.find_all(exp.Coalesce)):
                first = coalesce.this
                if isinstance(first, exp.Nullif):
                    continue
                if any(
                    isinstance(default, exp.Literal) and default.is_string
                    for default in coalesce.expressions
                ):
                    # Text defaults are not zero-for-missing substitutions;
                    # `NULLIF(text, 0)` would fail binding instead.
                    continue
                coalesce.set(
                    "this",
                    exp.Nullif(
                        this=first.copy(), expression=exp.Literal.number("0")
                    ),
                )
                changed = True
        else:
            for coalesce in list(ast.find_all(exp.Coalesce)):
                coalesce.replace(coalesce.this.copy())
                changed = True

    elif kind is AttackKind.DROPPED_FILTER:
        if variant == "filter_to_where":
            # Hoist the predicate into a WHERE: parents whose children ALL
            # fail it vanish instead of reporting 0 — a row-count error.
            for case in _filtered_aggregate_cases(ast):
                select = case.find_ancestor(exp.Select)
                ifs = case.args.get("ifs") or []
                if select is None or not ifs:
                    continue
                predicate = ifs[0].this.copy()
                if not _unwrap_case(case):
                    continue
                select.set(
                    "where",
                    exp.Where(this=predicate)
                    if select.args.get("where") is None
                    else exp.Where(
                        this=exp.and_(select.args["where"].this.copy(), predicate)
                    ),
                )
                changed = True
        else:
            for select in ast.find_all(exp.Select):
                if select.args.get("where"):
                    select.set("where", None)
                    changed = True
            # A filtered aggregate carries no WHERE at all: its predicate is a
            # CASE inside the aggregate. Dropping THAT is the same defect.
            for case in _filtered_aggregate_cases(ast):
                if _unwrap_case(case):
                    changed = True

    elif kind is AttackKind.CUSTOM:
        if variant == "add_dedup":
            changed, _ = _dedupe_source_scans(ast)
        elif variant == "argmax_as_max":
            # Both argmax spellings (FIRST_VALUE OVER, ARG_MAX) need handling
            # or the mutant is inert; keep the window, else it is wrong_window.
            for window in ast.find_all(exp.Window):
                inner = window.this
                if isinstance(inner, (exp.FirstValue, exp.LastValue, exp.AnyValue)):
                    window.set("this", exp.Max(this=inner.this.copy()))
                    changed = True
            for arg in list(ast.find_all(exp.ArgMax)) + list(ast.find_all(exp.ArgMin)):
                arg.replace(exp.Max(this=arg.this.copy()))
                changed = True
        elif variant == "wrong_boundary_else":
            # Drop the mandatory ELSE: out-of-domain input yields NULL where
            # the specification demands a value.
            for case in ast.find_all(exp.Case):
                if case.args.get("default") is not None:
                    case.set("default", None)
                    changed = True
        elif variant == "wrong_boundary_inclusive":
            # Flip inclusivity on EVERY threshold: flipping only the first in
            # document order ties observability to the compiler's sort order.
            for case in ast.find_all(exp.Case):
                for branch in case.args.get("ifs") or []:
                    cond = branch.this
                    replacement = _STRICTER.get(type(cond))
                    if replacement is not None:
                        branch.set(
                            "this",
                            replacement(
                                this=cond.this.copy(), expression=cond.expression.copy()
                            ),
                        )
                        changed = True
        else:  # pragma: no cover - split_kind_directive rejects these first
            raise ValueError(f"custom attack variant {variant!r} has no AST rule")

    elif kind in (AttackKind.CONSTANTS, AttackKind.KEYS_ONLY):
        outer = _outer_select(ast)
        types = {c.name: c.type for c in mart.columns}
        keys = set(mart.key_columns)
        new_exprs: list[exp.Expression] = []
        for e in outer.expressions:
            name = e.alias_or_name
            keep = kind is AttackKind.KEYS_ONLY and name in keys
            if keep or name not in types:
                new_exprs.append(e)
            else:
                new_exprs.append(exp.alias_(_constant_for(types[name]), name))
                changed = True
        outer.set("expressions", new_exprs)

    elif kind is AttackKind.WRONG_GRAIN:
        # Walk EVERY select: build_star puts the GROUP BY in a CTE, so an
        # outer-projection-only rule is inert on every generated plan.
        for select in ast.find_all(exp.Select):
            group = select.args.get("group")
            if group is None:
                continue
            grouped = {g.sql(dialect="duckdb") for g in group.expressions}
            for agg in select.find_all(exp.AggFunc):
                for col in agg.find_all(exp.Column):
                    if col.sql(dialect="duckdb") not in grouped:
                        group.append("expressions", col.copy())
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break

    elif kind is AttackKind.WRONG_AGG_STAGE:
        # Move an aggregate-stage filter to raw rows without changing other filters.
        if not mart.plan.declares_pattern(SemanticPattern.AGGREGATE_THEN_FILTER):
            return None
        if variant != "filter_before_aggregate":  # pragma: no cover - catalogue guards
            raise ValueError(
                f"wrong_agg_stage variant {variant!r} has no AST rule"
            )

        filter_ops = [
            op for op in mart.plan.ops
            if op.kind is MartOpKind.FILTER and op.predicate
        ]
        if not filter_ops:
            return None
        expected_predicate = sqlglot.parse_one(
            filter_ops[-1].predicate, read="duckdb"
        ).sql(dialect="duckdb")
        post_aggregate_filter: exp.Select | None = None
        for select in ast.find_all(exp.Select):
            where = select.args.get("where")
            if (
                where is not None
                and where.this.sql(dialect="duckdb") == expected_predicate
            ):
                post_aggregate_filter = select
                break

        aggregate_select: exp.Select | None = None
        presence_column: exp.Column | None = None
        for select in ast.find_all(exp.Select):
            if select.args.get("group") is None:
                continue
            for count in select.find_all(exp.Count):
                columns = list(count.find_all(exp.Column))
                if columns:
                    aggregate_select = select
                    presence_column = columns[0]
                    break
            if aggregate_select is not None:
                break

        if (
            post_aggregate_filter is None
            or aggregate_select is None
            or presence_column is None
        ):
            return None

        post_aggregate_filter.set("where", None)
        aggregate_select.set(
            "where",
            exp.Where(
                this=exp.not_(
                    exp.Is(this=presence_column.copy(), expression=exp.Null())
                )
            ),
        )
        changed = True

    elif kind is AttackKind.WRONG_DENOMINATOR:
        if variant == "filtered_denominator":
            # The `filtered / total` misreading: filtered on both sides.
            # Substituted on the NUMERATOR — ratios are over alias references.
            for div in ast.find_all(exp.Div):
                numerator = div.this
                inner = numerator.this if isinstance(numerator, exp.Cast) else numerator
                denominator = div.expression
                # Guarded form is `NULLIF(<denominator>, 0)`: compare against
                # the NULLIF's first argument, not the NULLIF node.
                target = (
                    denominator.this
                    if isinstance(denominator, exp.Nullif)
                    else denominator
                )
                if inner.sql(dialect="duckdb") == target.sql(dialect="duckdb"):
                    continue  # already numerator/numerator: nothing to misread
                if isinstance(denominator, exp.Nullif):
                    denominator.set("this", inner.copy())
                else:
                    div.set(
                        "expression",
                        exp.func("NULLIF", inner.copy(), exp.Literal.number("0")),
                    )
                changed = True
        else:
            for div in ast.find_all(exp.Div):
                div.set("expression", exp.Literal.number("1"))
                changed = True

    elif kind is AttackKind.WRONG_WINDOW:
        for window in ast.find_all(exp.Window):
            if variant == "drop_frame":
                # The RANGE default lumps together every row with an equal
                # ORDER BY key, so a running total is wrong exactly on ties.
                if window.args.get("spec") is not None:
                    window.set("spec", None)
                    changed = True
                continue
            if window.args.get("order"):
                window.set("order", None)
                changed = True
            elif window.args.get("partition_by"):
                window.set("partition_by", None)
                changed = True

    else:
        raise ValueError(f"attack kind {kind.value!r} has no AST mutation rule")

    if not changed:
        return None
    return ast.sql(dialect="duckdb", pretty=True)


class InertAstMutationError(ValueError):
    """An AST mutation kind that changed NOTHING on any mart.

    A DECLARED case fails closed (the catalogue lied); a FINDING-compiled probe
    only falsifies the critic's hypothesis and is recorded as a rejected
    proposal, so a sound task is never rejected over a critic's bad guess.
    """


def _materialize_structured_mutation(
    task: TaskIR, case: AttackCase, payload: dict[str, object]
) -> dict[str, str]:
    """Materialize the exact operation encoded by a proposal directive."""
    operation = str(payload["operation"])
    reference = _reference_sql(task)

    if operation in {"skip_backend", "skip_tables"}:
        # Its wrong implementation lives in the rendered source artifacts.
        return reference

    if operation == "copy_mart":
        source = str(payload.get("source_mart") or "")
        target = str(payload.get("target_mart") or "")
        marts = {mart.name for mart in task.marts}
        if not source or not target or source not in marts or target not in marts:
            raise ValueError(
                f"attack {case.name}: copy_mart needs two declared marts; "
                f"source={source!r}, target={target!r}, known={sorted(marts)}"
            )
        if source == target:
            raise ValueError(f"attack {case.name}: copy_mart source equals target")
        # The runner materializes `source` as a temporary view before querying
        # this target.  SELECT * is intentional: it tests copying the mart, not
        # re-projecting a hand-picked shape until it happens to fit.
        out = dict(reference)
        out[target] = exp.select("*").from_(
            quote_sql_identifier(source, dialect="duckdb")
        ).sql(
            dialect="duckdb", pretty=True
        )
        return out

    if operation == "zero_is_missing":
        out: dict[str, str] = {}
        changed = False
        for mart in task.marts:
            sql = _apply_kind(
                AttackKind.NO_NULL_DEFAULT,
                reference[mart.name],
                mart,
                "zero_is_missing",
            )
            out[mart.name] = sql or reference[mart.name]
            changed = changed or sql is not None
        if not changed:
            raise InertAstMutationError(
                f"attack {case.name}: zero_is_missing found no COALESCE fallback"
            )
        return out

    if operation == "remove_dedup":
        out = {}
        changed = False
        for mart in task.marts:
            sql = _apply_kind(
                AttackKind.NO_DEDUP, reference[mart.name], mart
            )
            out[mart.name] = sql or reference[mart.name]
            changed = changed or sql is not None
        if not changed:
            raise InertAstMutationError(
                f"attack {case.name}: remove_dedup found no DISTINCT/dedup surface"
            )
        return out

    if operation == "add_dedup":
        target_table_raw = payload.get("dedup_table")
        target_table = str(target_table_raw) if target_table_raw else None
        known_tables = {table.name for table in task.tables}
        if target_table is not None and target_table not in known_tables:
            raise ValueError(
                f"attack {case.name}: unknown dedup_table {target_table!r}"
            )
        out = {}
        changed = False
        for mart in task.marts:
            ast = sqlglot.parse_one(reference[mart.name], read="duckdb")
            mart_changed, _ = _dedupe_source_scans(
                ast, target_table, frozenset(known_tables)
            )
            out[mart.name] = (
                ast.sql(dialect="duckdb", pretty=True)
                if mart_changed
                else reference[mart.name]
            )
            changed = changed or mart_changed
        if not changed:
            suffix = f" for table {target_table!r}" if target_table else ""
            raise InertAstMutationError(
                f"attack {case.name}: add_dedup found no source scan{suffix}"
            )
        return out

    raise ValueError(
        f"attack {case.name}: unknown structured operation {operation!r}"
    )


def materialize_mutation(task: TaskIR, case: AttackCase, gold: object) -> dict[str, str]:
    """Return mutated SQL by mart for one attack case.

    Dispatch by mutation form; load directives retain reference SQL because their
    mutation applies to loading. Raise when no mart changes.
    """
    mutation = case.mutation.strip()

    if mutation.startswith(STRUCTURED_DIRECTIVE_PREFIX):
        payload = parse_structured_directive(
            mutation[len(STRUCTURED_DIRECTIVE_PREFIX):]
        )
        return _materialize_structured_mutation(task, case, payload)

    if mutation.startswith(HARDCODE_DIRECTIVE_PREFIX):
        pop_value = mutation[len(HARDCODE_DIRECTIVE_PREFIX):]
        if pop_value not in {p.value for p in PopulationName}:
            raise ValueError(f"attack {case.name}: unknown population {pop_value!r}")
        return {
            m.name: _literal_select(m, _gold_stage2_csv(gold, pop_value, m.name))
            for m in task.marts
        }

    if mutation.startswith(LOAD_DIRECTIVE_PREFIX):
        split_load_directive(mutation[len(LOAD_DIRECTIVE_PREFIX):])  # validate now
        return _reference_sql(task)

    if case.kind is AttackKind.NO_OP:
        return {}

    if case.kind is AttackKind.SKIP_EXTRACTION:
        return _reference_sql(task)

    kind, variant = case.kind, ""
    if mutation.startswith(KIND_DIRECTIVE_PREFIX):
        kind, variant = split_kind_directive(mutation[len(KIND_DIRECTIVE_PREFIX):])
    elif mutation:
        if len(task.marts) != 1:
            raise ValueError(
                f"attack {case.name}: literal-SQL mutation is ambiguous on a "
                f"multi-mart task; use a directive form"
            )
        return {task.marts[0].name: mutation}

    reference = _reference_sql(task)
    out: dict[str, str] = {}
    any_changed = False
    for mart in task.marts:
        mutated = _apply_kind(kind, reference[mart.name], mart, variant)
        if mutated is None:
            out[mart.name] = reference[mart.name]
        else:
            out[mart.name] = mutated
            any_changed = True
    if not any_changed:
        label = kind.value + (f"{VARIANT_SEPARATOR}{variant}" if variant else "")
        raise InertAstMutationError(
            f"attack {case.name}: mutation kind {label!r} is inert on every "
            f"mart of task {task.task_id} (fail closed)"
        )
    return out


# EL: load-side mutations against the RENDERED SOURCE ARTIFACTS

#: The ONLY edits an EL mutant may make: each changes how MANY records load,
#: all `compare_stage1` grades. A value-corrupting edit would score 1.0.
OP_DUPLICATE = "duplicate"                 # every record written twice
OP_EMPTY = "empty"                         # the artifact serves no records
OP_TRUNCATE_FIRST_UNIT = "truncate_first_unit"  # only page 1 / part 0 / INSERT 1
OP_DROP_NULL_ROWS = "drop_null_rows"       # records containing any NULL removed
OP_HEADER_AS_ROW = "header_as_row"         # CSV header repeated as a data row

#: Multi-unit backends: the only ones where an unfollowed cursor is a real bug.
PAGINATED_BACKENDS: frozenset[Backend] = frozenset(
    {Backend.REST, Backend.S3, Backend.POSTGRES}
)

#: Records per unit (REST page, postgres batch, S3 part); a table fitting in
#: one unit makes `truncate_table` inert, decidable before the mutant runs.
UNIT_ROWS: dict[Backend, int] = {
    Backend.REST: 100,
    Backend.POSTGRES: 500,
    Backend.S3: 5_000,
}


def _unit_rows(task: TaskIR, table: str) -> int:
    assignment = task.backend_for(table)
    if assignment.backend is Backend.REST:
        return int(assignment.options.get("page_size", UNIT_ROWS[Backend.REST]))
    return UNIT_ROWS[assignment.backend]

#: Closed catalogue: an unknown load-mutation name RAISES, never defaults.
LOAD_MUTATIONS: frozenset[str] = frozenset(
    {
        "skip_backend",
        "skip_tables",
        "partial_backend",
        "duplicate_on_load",
        "truncate_table",
        "wrong_source_file",
        "stale_snapshot",
        "header_as_row",
        "null_row_drop",
        "fabricate_counts",
    }
)


class InertLoadMutationError(ValueError):
    """A load mutant that kept FULL extract-load reward on every population.

    Same house rule as the inert-AST check: a mutant indistinguishable from the
    trusted load proves nothing and must not be allowed to 'pass' quietly.
    """


class InapplicableLoadMutationError(ValueError):
    """This task/data offers NO surface for the requested load mutation.

    Decided at RESOLUTION time, unlike `InertLoadMutationError` (measured after
    a run). A REQUIRED case re-raises (fail closed); a probe is recorded in
    ``inapplicable.json``, never in the rewards shape a gate would then grade.
    """


@dataclass(frozen=True)
class LoadMutationPlan:
    """A resolved EL mutation: which artifacts are edited, and how.

    Every field derives DETERMINISTICALLY from the TaskIR and the frozen gold
    counts — never from wall clock, iteration order, or a random choice.
    """

    name: str
    #: graded population -> population actually read (differs only for
    #: `stale_snapshot`).
    source_population: dict[PopulationName, PopulationName]
    #: tables never loaded at all (absent from the warehouse and from counts).
    omit_tables: tuple[str, ...] = ()
    #: table -> artifact edit applied to the cloned rendered tree.
    ops: dict[str, str] = field(default_factory=dict)
    #: table pairs whose rendered artifacts are exchanged wholesale.
    swaps: tuple[tuple[str, str], ...] = ()
    #: `fabricate_counts` only: the stage-1 answer submitted with NO artifact
    #: opened and no warehouse built.
    fabricated_counts: dict[PopulationName, dict[str, int]] | None = None
    #: Human-readable statement of what was targeted and why — audit material.
    detail: str = ""


class MutationFidelityError(ValueError):
    """The compiler did not realize the operation the proposal encoded."""


def _fidelity_record_path(workspace: Path, task: TaskIR, case: AttackCase) -> Path:
    return (
        Path(workspace)
        / "tasks"
        / task.task_id
        / "attacks"
        / case.name
        / MUTATION_FIDELITY_FILENAME
    )


def _write_fidelity_record(
    workspace: Path,
    task: TaskIR,
    case: AttackCase,
    *,
    passed: bool,
    requested: dict[str, object],
    realized: dict[str, object],
    checks: tuple[str, ...],
    errors: tuple[str, ...] = (),
) -> dict[str, object]:
    record: dict[str, object] = {
        "case": case.name,
        "source_finding": case.source_finding,
        "task_content_hash": task.content_hash(),
        "passed": passed,
        "requested": requested,
        "realized": realized,
        "checks": list(checks),
        "errors": list(errors),
    }
    path = _fidelity_record_path(workspace, task, case)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(record), encoding="utf-8")
    return record


def _kind_directive_target_marts(
    task: TaskIR, kind: AttackKind, variant: str
) -> tuple[str, ...]:
    """Return the exact marts a kind directive would mutate.

    This static preflight applies the closed AST rewrite to trusted reference SQL
    without loading data. Execution verifies the same target scope again.
    """

    if kind in DIRECTIVE_ONLY_KIND_DEFAULTS:
        # ``no_op`` and ``skip_extraction`` target the whole task and bypass the
        # AST rewriter; every mart is therefore a static target.
        return tuple(sorted(mart.name for mart in task.marts))
    reference = _reference_sql(task)
    targets: list[str] = []
    for mart in task.marts:
        if _apply_kind(kind, reference[mart.name], mart, variant) is not None:
            targets.append(mart.name)
    return tuple(sorted(targets))


#: One claim token: a maximal run of letters/digits after case folding.  ``_``
#: and ``-`` inside a word are one and the same separator, so the identifier
#: ``second_hop`` and the prose word ``second-hop`` are the same token
#: sequence.
_CLAIM_TOKEN_RE = re.compile(r"[0-9a-z]+")
#: Identifier-like prose word. Underscores and hyphens stay inside a word;
#: dots split qualified names so only exact components match.
_CLAIM_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[_\-]+[A-Za-z0-9]+)*")
#: What may separate two words of ONE space-joined label: whitespace and the
#: quoting marks prose wraps identifiers in.  Any other character between two
#: words (``.``, ``,``, ``;``, ``:``, a bracket, a slash, a dash) is a hard
#: boundary a multi-word label never spans.
_CLAIM_SOFT_GAP_RE = re.compile(r"[\s`*'\"‘’“”]*")


def claim_tokens(text: str) -> tuple[str, ...]:
    """The normalized token sequence of one identifier or prose fragment."""
    return tuple(_CLAIM_TOKEN_RE.findall(str(text).casefold()))


def _claim_phrases(text: str) -> list[list[tuple[str, ...]]]:
    """The prose as PHRASES of whole words: each word is its normalized
    token tuple, and a phrase breaks wherever two words are separated by
    anything but a soft gap (`_CLAIM_SOFT_GAP_RE`)."""
    text = str(text)
    phrases: list[list[tuple[str, ...]]] = []
    current: list[tuple[str, ...]] = []
    previous_end: int | None = None
    for match in _CLAIM_WORD_RE.finditer(text):
        if previous_end is not None:
            gap = text[previous_end:match.start()]
            if _CLAIM_SOFT_GAP_RE.fullmatch(gap) is None:
                if current:
                    phrases.append(current)
                current = []
        tokens = claim_tokens(match.group(0))
        if tokens:
            current.append(tokens)
        previous_end = match.end()
    if current:
        phrases.append(current)
    return phrases


def claim_names_identifier(text: str, identifier: str) -> bool:
    """Return whether claim text names an identifier as complete normalized words.

    Case, underscores, and hyphens are normalized, but matches cannot cross punctuation
    or occur as token sub-runs of another identifier. Structured parameters remain
    authoritative for the requested target.
    """
    needle = claim_tokens(identifier)
    if not needle:
        return False
    width = len(needle)
    for phrase in _claim_phrases(text):
        for start in range(len(phrase)):
            joined: tuple[str, ...] = ()
            for word in phrase[start:]:
                joined += word
                if len(joined) >= width:
                    break
            if joined == needle:
                return True
    return False


def missing_claim_identifiers(text: str, identifiers) -> tuple[str, ...]:
    """The identifiers (in order, deduplicated) the claim text does not name."""
    return tuple(
        dict.fromkeys(
            str(identifier)
            for identifier in identifiers
            if not claim_names_identifier(text, str(identifier))
        )
    )


def validate_proposal_claim_fidelity(
    task: TaskIR, finding: Finding, proposal: ProposedAttackCase, case: AttackCase
) -> dict[str, object]:
    """Check that finding text names every identifier-bearing proposal parameter.

    The check proves only target retention, using whole normalized words. Structured
    parameters remain authoritative and semantic equivalence is not inferred.
    """
    text = f"{finding.summary} {finding.detail} {proposal.rationale}"
    mutation = case.mutation.strip()
    requested: dict[str, object]
    identifiers: tuple[str, ...] = ()
    if mutation.startswith(STRUCTURED_DIRECTIVE_PREFIX):
        requested = parse_structured_directive(
            mutation[len(STRUCTURED_DIRECTIVE_PREFIX):]
        )
        identifiers = tuple(
            str(value)
            for key, value in requested.items()
            if key in {"source_mart", "target_mart", "backend", "dedup_table"}
            and value
        )
        tables = requested.get("tables")
        if isinstance(tables, list):
            identifiers += tuple(str(value) for value in tables)
    elif mutation.startswith(HARDCODE_DIRECTIVE_PREFIX):
        population = mutation[len(HARDCODE_DIRECTIVE_PREFIX):]
        requested = {"operation": "hardcode_population", "population": population}
        identifiers = (population,)
    elif mutation.startswith(KIND_DIRECTIVE_PREFIX):
        kind, variant = split_kind_directive(mutation[len(KIND_DIRECTIVE_PREFIX):])
        target_marts = _kind_directive_target_marts(task, kind, variant)
        requested = {
            "operation": "kind",
            "kind": kind.value,
            "variant": variant,
            "target_marts": list(target_marts),
        }
        # A single-mart target is implicit; other directives name every target
        # unless task-wide. A named variant is always part of the claim.
        identifiers = (
            (
                target_marts
                if len(task.marts) > 1 and kind not in TASK_WIDE_KINDS
                else ()
            )
            + ((variant,) if variant else ())
        )
    elif mutation.startswith(LOAD_DIRECTIVE_PREFIX):
        name, arg = split_load_directive(mutation[len(LOAD_DIRECTIVE_PREFIX):])
        requested = {"operation": name, "argument": arg}
        if arg:
            identifiers = (arg,)
    else:
        requested = {"operation": "literal_sql"}

    missing = missing_claim_identifiers(text, identifiers)
    errors: list[str] = []
    if mutation.startswith(KIND_DIRECTIVE_PREFIX) and not requested.get(
        "target_marts"
    ):
        errors.append("kind/variant has no statically applicable mart target")
    if missing:
        errors.append(
            "finding text does not name structured target(s): " + ", ".join(missing)
        )
    if proposal.kind is not case.kind:
        errors.append(
            f"proposal kind {proposal.kind.value!r} became case kind {case.kind.value!r}"
        )
    if finding.suggested_attack is not None and finding.suggested_attack is not case.kind:
        errors.append(
            f"suggested attack {finding.suggested_attack.value!r} became "
            f"case kind {case.kind.value!r}"
        )
    return {
        "passed": not errors,
        "requested": requested,
        "checks": [
            "proposal kind preserved",
            "every identifier-bearing parameter, named variant, and realized "
            "mart target appears in the finding text",
        ],
        "errors": errors,
    }


def _validate_realized_fidelity(
    task: TaskIR,
    case: AttackCase,
    sql_by_mart: dict[str, str],
    load_plan: LoadMutationPlan | None,
) -> tuple[dict[str, object], dict[str, object], tuple[str, ...]]:
    """Return (requested, realized, checks), or raise before reward execution."""
    mutation = case.mutation.strip()
    reference = _reference_sql(task)
    checks: list[str] = []
    if mutation.startswith(KIND_DIRECTIVE_PREFIX):
        kind, variant = split_kind_directive(
            mutation[len(KIND_DIRECTIVE_PREFIX):]
        )
        expected_targets = _kind_directive_target_marts(task, kind, variant)
        if kind in DIRECTIVE_ONLY_KIND_DEFAULTS:
            # ``materialize_mutation`` answers these kinds before any AST
            # rewrite (``no_op`` emits nothing; ``skip_extraction`` keeps the
            # reference SQL and cuts the load), so the SQL diff is empty by
            # construction and cannot be the realization check.  The closed
            # materializer's dispatch on ``case.kind`` is the realization.
            if case.kind is not kind:
                raise MutationFidelityError(
                    f"kind directive {kind.value!r} realized case kind "
                    f"{case.kind.value!r}"
                )
            record = {
                "operation": "kind",
                "kind": kind.value,
                "variant": variant,
                "target_marts": list(expected_targets),
            }
            return (
                dict(record),
                dict(record),
                (
                    "directive-only kind realized by its closed materializer "
                    "over every mart (no AST rewrite)",
                ),
            )
        actual_targets = tuple(
            sorted(
                mart
                for mart, sql in sql_by_mart.items()
                if sql != reference[mart]
            )
        )
        if not expected_targets or actual_targets != expected_targets:
            raise MutationFidelityError(
                "kind directive target mismatch: "
                f"expected {list(expected_targets)}, realized "
                f"{list(actual_targets)}"
            )
        requested = {
            "operation": "kind",
            "kind": kind.value,
            "variant": variant,
            "target_marts": list(expected_targets),
        }
        realized = {
            "operation": "kind",
            "kind": kind.value,
            "variant": variant,
            "target_marts": list(actual_targets),
        }
        return (
            requested,
            realized,
            (
                "exact named kind and variant realized only their statically "
                "applicable mart targets",
            ),
        )
    if not mutation.startswith(STRUCTURED_DIRECTIVE_PREFIX):
        return (
            {"operation": "legacy_directive", "mutation": mutation},
            {"kind": case.kind.value},
            ("legacy directive parsed by its closed compiler",),
        )

    requested = parse_structured_directive(
        mutation[len(STRUCTURED_DIRECTIVE_PREFIX):]
    )
    operation = str(requested["operation"])
    realized: dict[str, object] = {"operation": operation}
    if operation == "skip_backend":
        if load_plan is None:
            raise MutationFidelityError("skip_backend realized no load plan")
        backend = Backend(str(requested["backend"]))
        expected = tuple(
            sorted(a.table for a in task.backends if a.backend is backend)
        )
        actual = tuple(sorted(load_plan.ops))
        if actual != expected or any(load_plan.ops[t] != OP_EMPTY for t in actual):
            raise MutationFidelityError(
                f"skip_backend requested {expected}, realized {actual}"
            )
        realized["backend"] = backend.value
        realized["tables"] = list(actual)
        checks.append("exact requested backend tables served empty")
    elif operation == "skip_tables":
        if load_plan is None:
            raise MutationFidelityError("skip_tables realized no load plan")
        expected = tuple(sorted(str(t) for t in requested.get("tables", [])))
        actual = tuple(sorted(load_plan.omit_tables))
        if actual != expected:
            raise MutationFidelityError(
                f"skip_tables requested {expected}, realized {actual}"
            )
        realized["tables"] = list(actual)
        checks.append("only the exact requested tables are omitted")
    elif operation == "copy_mart":
        source = str(requested["source_mart"])
        target = str(requested["target_mart"])
        target_ast = sqlglot.parse_one(sql_by_mart[target], read="duckdb")
        refs = sorted(table.name for table in target_ast.find_all(exp.Table))
        if refs != [source]:
            raise MutationFidelityError(
                f"copy_mart target references {refs}, expected only {source!r}"
            )
        changed_elsewhere = sorted(
            mart for mart, sql in sql_by_mart.items()
            if mart != target and sql != reference[mart]
        )
        if changed_elsewhere:
            raise MutationFidelityError(
                f"copy_mart also changed unrelated marts {changed_elsewhere}"
            )
        realized.update(source_mart=source, target_mart=target)
        checks.append("target selects the requested source mart; others unchanged")
    elif operation == "zero_is_missing":
        changed = [
            mart for mart, sql in sql_by_mart.items() if sql != reference[mart]
        ]
        nullifs = sum(
            1
            for mart in changed
            for node in sqlglot.parse_one(sql_by_mart[mart], read="duckdb").find_all(exp.Nullif)
            if isinstance(node.expression, exp.Literal) and node.expression.this == "0"
        )
        if not changed or not nullifs:
            raise MutationFidelityError(
                "zero_is_missing did not realize NULLIF(first_value, 0)"
            )
        realized.update(marts=sorted(changed), zero_nullif_count=nullifs)
        checks.append("fallback first values are converted with NULLIF(value, 0)")
    elif operation == "add_dedup":
        changed = [
            mart for mart, sql in sql_by_mart.items() if sql != reference[mart]
        ]
        distincts = sum(
            1
            for mart in changed
            for select in sqlglot.parse_one(sql_by_mart[mart], read="duckdb").find_all(exp.Select)
            if select.args.get("distinct") is not None
        )
        if not changed or not distincts:
            raise MutationFidelityError("add_dedup realized no DISTINCT source scan")
        realized.update(marts=sorted(changed), distinct_source_scans=distincts)
        if requested.get("dedup_table"):
            realized["dedup_table"] = requested["dedup_table"]
        checks.append("dedup is applied before mart logic, not to final output")
    elif operation == "remove_dedup":
        changed = [
            mart for mart, sql in sql_by_mart.items() if sql != reference[mart]
        ]
        if not changed:
            raise MutationFidelityError("remove_dedup changed no mart")
        realized["marts"] = sorted(changed)
        checks.append("reference DISTINCT/dedup nodes were removed")
    else:  # pragma: no cover - materializer rejects first
        raise MutationFidelityError(f"unknown operation {operation!r}")
    return requested, realized, tuple(checks)


def split_load_directive(text: str) -> tuple[str, str]:
    """'<name>' or '<name>:<arg>' -> (name, arg); unknown name raises."""
    name, _, arg = text.partition(":")
    if name not in LOAD_MUTATIONS:
        raise ValueError(
            f"unknown load mutation {name!r} (known: {sorted(LOAD_MUTATIONS)})"
        )
    return name, arg


def _gold_counts(gold: object, population: PopulationName) -> dict[str, int]:
    stage1 = getattr(gold, "stage1", None)
    if not isinstance(stage1, dict):
        raise ValueError(
            "gold bundle has no stage1 counts — refusing to plan a load "
            "mutation against nothing (fail closed)"
        )
    counts = stage1.get(population.value)
    if not isinstance(counts, dict):
        raise ValueError(
            f"gold bundle has no stage-1 counts for population "
            f"{population.value!r} (fail closed)"
        )
    return {str(k): int(v) for k, v in counts.items()}


def _graded_populations(task: TaskIR) -> tuple[PopulationName, ...]:
    present = {p.name for p in task.populations}
    return tuple(p for p in PopulationName if p in present)


def _tables_by_backend(task: TaskIR) -> dict[Backend, tuple[str, ...]]:
    out: dict[Backend, list[str]] = {}
    for assignment in task.backends:
        out.setdefault(assignment.backend, []).append(assignment.table)
    return {b: tuple(sorted(names)) for b, names in sorted(out.items(), key=lambda kv: kv[0].value)}


def _largest_table(candidates: tuple[str, ...], counts: dict[str, int]) -> str:
    """The candidate with the most PRIMARY rows; ties broken by name.

    Not cosmetic: `truncate_table` and `header_as_row` are only observable on a
    table that HAS rows, and the biggest likeliest spans several units.
    """
    return sorted(candidates, key=lambda t: (-counts.get(t, 0), t))[0]


def _swap_is_readable(task: TaskIR, a: str, b: str) -> bool:
    """True when each table's records can be READ through the other's schema.

    Nullable columns may go missing, but a missing or uncoercible NOT NULL
    column kills the load instead of the count vector.
    """
    columns = {t.name: {c.name: c for c in t.columns} for t in task.tables}
    for target, source in ((a, b), (b, a)):
        for name, column in columns.get(target, {}).items():
            other = columns.get(source, {}).get(name)
            if other is None:
                if not column.nullable:
                    return False
                continue
            if other.type is not column.type:
                return False
    return True


def resolve_load_mutation(
    task: TaskIR, name: str, arg: str, gold: object
) -> LoadMutationPlan:
    """Compile one ``directive:load:<name>[:<arg>]`` against THIS task.

    Fails closed on every surface the task does not offer: an EL attack
    declared where its surface is missing would otherwise "pass" by being
    inapplicable.
    """
    graded = _graded_populations(task)
    if not graded:
        raise ValueError(f"task {task.task_id}: no populations to attack")
    primary_counts = _gold_counts(
        gold, PopulationName.PRIMARY if PopulationName.PRIMARY in graded else graded[0]
    )
    same_source = {p: p for p in graded}
    by_backend = _tables_by_backend(task)

    if name == "skip_backend":
        if arg:
            try:
                skipped = Backend(arg)
            except ValueError as exc:
                raise ValueError(
                    f"task {task.task_id}: unknown backend {arg!r}"
                ) from exc
            if skipped not in by_backend:
                raise InapplicableLoadMutationError(
                    f"task {task.task_id}: requested backend {skipped.value!r} "
                    "has no assigned tables"
                )
        elif len(by_backend) < 2:
            raise InapplicableLoadMutationError(
                f"task {task.task_id}: skip_backend needs more than one source "
                f"backend (has {len(by_backend)}) — nothing to skip (fail closed)"
            )
        else:
            skipped = task.backends[0].backend  # legacy deterministic default
        tables = by_backend[skipped]
        return LoadMutationPlan(
            name=name,
            source_population=same_source,
            ops={t: OP_EMPTY for t in tables},
            detail=(
                f"backend {skipped.value!r} served empty: tables "
                f"{', '.join(tables)} load 0 rows"
            ),
        )

    if name == "skip_tables":
        try:
            requested = json.loads(arg)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"task {task.task_id}: skip_tables argument is not JSON: {exc}"
            ) from exc
        if not isinstance(requested, list) or not requested or not all(
            isinstance(value, str) and value for value in requested
        ):
            raise ValueError(
                f"task {task.task_id}: skip_tables needs a non-empty JSON list "
                "of table names"
            )
        tables = tuple(sorted(set(requested)))
        known = {table.name for table in task.tables}
        unknown = sorted(set(tables) - known)
        if unknown:
            raise ValueError(
                f"task {task.task_id}: skip_tables names unknown tables {unknown}"
            )
        return LoadMutationPlan(
            name=name,
            source_population=same_source,
            omit_tables=tables,
            detail=f"exact requested tables never loaded: {', '.join(tables)}",
        )

    if name == "partial_backend":
        # `_tables_by_backend` sorts and `max` keeps the first maximal element,
        # so the chosen table is reproducible, never left to iteration order.
        biggest = max(by_backend.items(), key=lambda kv: len(kv[1]))
        table = arg or biggest[1][0]
        if table not in {t.name for t in task.tables}:
            raise ValueError(f"task {task.task_id}: unknown table {table!r}")
        return LoadMutationPlan(
            name=name,
            source_population=same_source,
            omit_tables=(table,),
            detail=(
                f"table {table!r} (backend {task.backend_for(table).backend.value}) "
                "never loaded — the connector nobody configured"
            ),
        )

    if name == "duplicate_on_load":
        tables = tuple(sorted(t.name for t in task.tables))
        return LoadMutationPlan(
            name=name,
            source_population=same_source,
            ops={t: OP_DUPLICATE for t in tables},
            detail="every source artifact loaded twice without truncating first",
        )

    if name == "truncate_table":
        # Only a table SPANNING more than one unit is truncatable; the peak
        # over graded populations is used, since primary alone can miss a spill.
        peak = {
            table.name: max(
                _gold_counts(gold, pop).get(table.name, 0) for pop in graded
            )
            for table in task.tables
        }
        candidates = tuple(
            sorted(
                t.name
                for t in task.tables
                if task.backend_for(t.name).backend in PAGINATED_BACKENDS
                and peak.get(t.name, 0) > _unit_rows(task, t.name)
            )
        )
        if not candidates:
            raise InapplicableLoadMutationError(
                f"task {task.task_id}: no table SPANS more than one unit on any "
                "population (REST pages hold "
                f"{UNIT_ROWS[Backend.REST]}, postgres batches "
                f"{UNIT_ROWS[Backend.POSTGRES]}, S3 parts "
                f"{UNIT_ROWS[Backend.S3]}) — an unfollowed pagination cursor "
                "changes nothing here, so truncate_table is inert (fail closed)"
            )
        table = arg or _largest_table(candidates, peak)
        backend = task.backend_for(table).backend
        return LoadMutationPlan(
            name=name,
            source_population=same_source,
            ops={table: OP_TRUNCATE_FIRST_UNIT},
            detail=(
                f"only the first unit of {table!r} ({backend.value}) read: "
                "the pagination cursor is never followed"
            ),
        )

    if name == "wrong_source_file":
        # Prefer a READABLE swap over a bigger count gap: it is killed by the
        # COUNT comparator under test, not by a `coerce_value` type error.
        best: tuple[int, int, str, str] | None = None
        for _backend, tables in by_backend.items():
            for i, a in enumerate(tables):
                for b in tables[i + 1:]:
                    delta = abs(primary_counts.get(a, 0) - primary_counts.get(b, 0))
                    if delta == 0:
                        continue  # equal counts: compare_stage1 cannot see the swap
                    rank = (1 if _swap_is_readable(task, a, b) else 0, delta)
                    if best is None or rank > (best[0], best[1]):
                        best = (rank[0], delta, a, b)
        if best is None:
            raise InapplicableLoadMutationError(
                f"task {task.task_id}: no two tables share a backend AND differ in "
                "row count — swapping them would be INVISIBLE to compare_stage1 "
                "(count-only), so the mutation is inert (fail closed)"
            )
        readable, _delta, a, b = best
        return LoadMutationPlan(
            name=name,
            source_population=same_source,
            swaps=((a, b),),
            detail=(
                f"load steps for {a!r} ({primary_counts.get(a, 0)} rows) and {b!r} "
                f"({primary_counts.get(b, 0)} rows) point at each other's artifact; "
                + (
                    "the records still parse as the other schema, so the kill is a "
                    "COUNT mismatch"
                    if readable
                    else "the schemas are incompatible, so the load dies in "
                    "coercion — reward 0, but not by the count comparator"
                )
            ),
        )

    if name == "stale_snapshot":
        sources: dict[PopulationName, PopulationName] = {}
        for pop in graded:
            want = _gold_counts(gold, pop)
            for other in graded:
                if other is pop:
                    continue
                if _gold_counts(gold, other) != want:
                    sources[pop] = other
                    break
        if not sources:
            raise InapplicableLoadMutationError(
                f"task {task.task_id}: every population has the SAME stage-1 count "
                "vector, so a stale extract is undetectable (fail closed)"
            )
        return LoadMutationPlan(
            name=name,
            source_population={p: sources.get(p, p) for p in graded},
            detail=(
                "graded against a cached extract of another population: "
                + ", ".join(
                    f"{p.value}<-{sources[p].value}" for p in graded if p in sources
                )
            ),
        )

    if name == "header_as_row":
        candidates = tuple(
            sorted(
                t.name
                for t in task.tables
                if task.backend_for(t.name).backend is Backend.FILES
            )
        )
        if not candidates:
            raise InapplicableLoadMutationError(
                f"task {task.task_id}: no FILES-backed table — header_as_row has "
                "no surface here (fail closed)"
            )
        # ONLY an all-TEXT table is a surface: the header row loads and the
        # kill is N+1 by count. A typed column crashes in coercion — no kill.
        textual = tuple(
            t
            for t in candidates
            if all(
                c.type is ColumnType.TEXT
                for spec in task.tables
                if spec.name == t
                for c in spec.columns
            )
        )
        if not textual or (arg and arg not in textual):
            raise InapplicableLoadMutationError(
                f"task {task.task_id}: no all-TEXT FILES table"
                + (f" named {arg!r}" if arg else "")
                + " — the header row cannot LOAD through the trusted loader "
                "(a typed column rejects the header text in coercion), so the "
                "N+1 count kill has no surface (a coercion crash is not a "
                "count kill; fail closed)"
            )
        table = arg or _largest_table(textual, primary_counts)
        return LoadMutationPlan(
            name=name,
            source_population=same_source,
            ops={table: OP_HEADER_AS_ROW},
            detail=(
                f"the CSV header of {table!r} ingested as a data row; every "
                "column is TEXT, so the row loads and the table is N+1 — a "
                "COUNT kill"
            ),
        )

    if name == "null_row_drop":
        tables = tuple(sorted(t.name for t in task.tables))
        return LoadMutationPlan(
            name=name,
            source_population=same_source,
            ops={t: OP_DROP_NULL_ROWS for t in tables},
            detail="every record containing a NULL dropped during load (dropna)",
        )

    if name == "fabricate_counts":
        # The stage-1 answer is SUBMITTED, never measured: 'hint' is what the
        # realized-count divergence kills, 'primary' leaks on a shared scale.
        source = arg or "hint"
        if source == "hint":
            hints: dict[str, dict[str, int]] = {
                p.name.value: dict(p.scale) for p in task.populations if p.scale
            }
            if not hints:
                raise InapplicableLoadMutationError(
                    f"task {task.task_id}: no population declares a scale hint "
                    "— there is no documented number to fabricate from "
                    "(fail closed)"
                )
            fallback = hints.get(
                PopulationName.PRIMARY.value, hints[sorted(hints)[0]]
            )
            fabricated = {
                pop: dict(hints.get(pop.value, fallback)) for pop in graded
            }
            return LoadMutationPlan(
                name=name,
                source_population=same_source,
                fabricated_counts=fabricated,
                detail=(
                    "stage-1 counts submitted straight from the DECLARED scale "
                    "hint (populations without a hint echo the primary hint); "
                    "no artifact was read"
                ),
            )
        if source == "primary":
            if PopulationName.PRIMARY not in graded:
                raise InapplicableLoadMutationError(
                    f"task {task.task_id}: no primary population — nothing to "
                    "echo (fail closed)"
                )
            if len(graded) < 2:
                raise InapplicableLoadMutationError(
                    f"task {task.task_id}: primary is the ONLY graded "
                    "population, so echoing its counts is the correct answer "
                    "everywhere — inapplicable (fail closed)"
                )
            if all(
                _gold_counts(gold, pop) == primary_counts for pop in graded
            ):
                raise InapplicableLoadMutationError(
                    f"task {task.task_id}: every population's frozen count "
                    "vector EQUALS primary's, so echoing primary is the "
                    "correct answer everywhere — the same no-surface rule as "
                    "stale_snapshot (fail closed)"
                )
            fabricated = {pop: dict(primary_counts) for pop in graded}
            return LoadMutationPlan(
                name=name,
                source_population=same_source,
                fabricated_counts=fabricated,
                detail=(
                    "the frozen PRIMARY count vector submitted for every "
                    "population; expected to leak on primary and resampled "
                    "(shared scale) and to die wherever the data differs"
                ),
            )
        raise ValueError(
            f"fabricate_counts: unknown source {source!r} (use '', 'hint' or "
            "'primary')"
        )

    raise ValueError(f"unknown load mutation {name!r}")  # pragma: no cover


# Artifact surgery (per rendered format)

def _write_text(path: Path, text: str) -> None:
    """Replace a file's CONTENT by replacing the file.

    The clone is made of hardlinks, so writing in place would edit the
    workspace's real artifact; unlinking first confines the mutation.
    """
    if path.exists():
        path.unlink()
    path.write_text(text, encoding="utf-8")


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:  # cross-device, or a filesystem without hardlinks
        shutil.copy2(src, dst)


def _clone_rendered(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, copy_function=_link_or_copy)


def _jsonl_lines(path: Path) -> list[str]:
    return [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _has_null(record: object) -> bool:
    return isinstance(record, dict) and any(v is None for v in record.values())


def _op_jsonl_file(path: Path, op: str) -> None:
    lines = _jsonl_lines(path)
    if op == OP_DUPLICATE:
        out = lines + lines
    elif op == OP_EMPTY:
        out = []
    elif op == OP_DROP_NULL_ROWS:
        out = [line for line in lines if not _has_null(json.loads(line))]
    else:
        raise ValueError(f"load op {op!r} does not apply to a jsonl artifact")
    _write_text(path, "".join(line + "\n" for line in out))


def _op_s3_dir(path: Path, op: str) -> None:
    parts = sorted(path.rglob("*.jsonl"))
    if not parts:
        raise FileNotFoundError(f"{path}: S3 layout contains no .jsonl objects")
    if op == OP_TRUNCATE_FIRST_UNIT:
        for extra in parts[1:]:
            extra.unlink()
        return
    for part in parts:
        _op_jsonl_file(part, op)


def _op_rest_dir(path: Path, op: str) -> None:
    index_path = path / "index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"{path}: REST fixture dir has no index.json")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    pages = list(index.get("pages") or [])
    if op == OP_TRUNCATE_FIRST_UNIT:
        # The CLIENT-side bug: later pages stay on disk untouched — the server
        # still serves them, the loader just never follows the cursor.
        kept = pages[:1]
        index["pages"] = kept
        rows = 0
        for name in kept:
            payload = json.loads((path / name).read_text(encoding="utf-8"))
            rows += len(payload.get("data") or [])
        index["row_count"] = rows
        _write_text(index_path, canonical_json(index) + "\n")
        return
    total = 0
    for name in pages:
        page_path = path / name
        payload = json.loads(page_path.read_text(encoding="utf-8"))
        data = list(payload.get("data") or [])
        if op == OP_DUPLICATE:
            data = data + data
        elif op == OP_EMPTY:
            data = []
        elif op == OP_DROP_NULL_ROWS:
            data = [row for row in data if not _has_null(row)]
        else:
            raise ValueError(f"load op {op!r} does not apply to a REST artifact")
        payload["data"] = data
        total += len(data)
        _write_text(page_path, canonical_json(payload) + "\n")
    index["row_count"] = total
    _write_text(index_path, canonical_json(index) + "\n")


def _op_csv_file(path: Path, op: str) -> None:
    with path.open("r", encoding="utf-8", newline="") as fh:
        table = list(csv.reader(fh))
    if not table:
        raise ValueError(f"{path}: CSV artifact has no header row")
    header, data = table[0], table[1:]
    if op == OP_DUPLICATE:
        rows = data + data
    elif op == OP_EMPTY:
        rows = []
    elif op == OP_DROP_NULL_ROWS:
        rows = [r for r in data if all(cell != "" for cell in r)]
    elif op == OP_HEADER_AS_ROW:
        rows = [list(header)] + data
    else:
        raise ValueError(f"load op {op!r} does not apply to a CSV artifact")
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    _write_text(path, buf.getvalue())


def _tuple_has_null(node: exp.Expression) -> bool:
    return any(isinstance(e, exp.Null) for e in node.find_all(exp.Null))


def _op_postgres_sql(path: Path, op: str) -> None:
    """Statement-level surgery on a rendered postgres load script.

    Parsed with sqlglot, the same parser the loader reads it with, so a script
    this module can edit is a script the loader can execute.
    """
    statements = [s for s in sqlglot.parse(path.read_text(encoding="utf-8"), read="postgres") if s]
    inserts = [s for s in statements if isinstance(s, exp.Insert)]
    others = [s for s in statements if not isinstance(s, exp.Insert)]
    if op == OP_DUPLICATE:
        out = statements + [s.copy() for s in inserts]
    elif op == OP_EMPTY:
        out = others
    elif op == OP_TRUNCATE_FIRST_UNIT:
        out = others + inserts[:1]
    elif op == OP_DROP_NULL_ROWS:
        out = list(others)
        for insert in inserts:
            values = insert.expression
            if not isinstance(values, exp.Values):
                raise ValueError(f"{path}: INSERT without a VALUES clause")
            kept = [t for t in values.expressions if not _tuple_has_null(t)]
            if not kept:
                continue
            clone = insert.copy()
            clone.expression.set("expressions", kept)
            out.append(clone)
    else:
        raise ValueError(f"load op {op!r} does not apply to a postgres artifact")
    text = "\n".join(s.sql(dialect="postgres") + ";" for s in out) + "\n"
    _write_text(path, text)


def _apply_artifact_op(task: TaskIR, rendered_dir: Path, table: str, op: str) -> None:
    from elt_taskgen.reference.solution import find_rendered_artifact

    backend = task.backend_for(table).backend
    path = find_rendered_artifact(task, rendered_dir, table)
    if backend is Backend.POSTGRES:
        _op_postgres_sql(path, op)
    elif backend is Backend.MONGODB:
        _op_jsonl_file(path, op)
    elif backend is Backend.S3:
        if path.is_dir():
            _op_s3_dir(path, op)
        else:
            _op_jsonl_file(path, op)
    elif backend is Backend.REST:
        if path.is_dir():
            _op_rest_dir(path, op)
        else:
            raise ValueError(
                f"{path}: single-file REST artifact has no pagination surface"
            )
    elif backend is Backend.FILES:
        _op_csv_file(path, op)
    else:  # pragma: no cover — Backend is a closed enum
        raise ValueError(f"unknown backend {backend!r}")


def _swap_artifacts(task: TaskIR, rendered_dir: Path, a: str, b: str) -> None:
    """Exchange two tables' rendered artifacts (files or whole dirs)."""
    from elt_taskgen.reference.solution import find_rendered_artifact

    path_a = find_rendered_artifact(task, rendered_dir, a)
    path_b = find_rendered_artifact(task, rendered_dir, b)
    spare = path_a.parent / f"{path_a.name}.__swap__"
    os.rename(path_a, spare)
    os.rename(path_b, path_a)
    os.rename(spare, path_b)


def apply_load_mutation(
    task: TaskIR,
    plan: LoadMutationPlan,
    population: PopulationName,
    workspace: Path,
    con: duckdb.DuckDBPyConnection,
) -> dict[str, int]:
    """Load ONE population through the trusted loader over a MUTATED clone of
    the rendered tree; returns the per-table counts that load produced.

    The clone is thrown away: the workspace's own rendered tree is never edited.
    """
    from elt_taskgen.reference.solution import load_sources_duckdb

    source = plan.source_population.get(population, population)
    rendered = (
        Path(workspace) / "tasks" / task.task_id / "populations" / source.value / "rendered"
    )
    if not rendered.is_dir():
        raise FileNotFoundError(
            f"load mutation {plan.name!r} needs the RENDERED artifacts of "
            f"population {source.value!r} ({rendered}) — the rows/*.jsonl "
            "fallback bypasses the extraction surface entirely, which is the "
            "very thing an EL mutant must exercise (fail closed)"
        )
    tmp_root = Path(tempfile.mkdtemp(prefix=".el_attack_", dir=str(workspace)))
    try:
        clone = tmp_root / "rendered"
        _clone_rendered(rendered, clone)
        for table in sorted(plan.ops):
            _apply_artifact_op(task, clone, table, plan.ops[table])
        for a, b in plan.swaps:
            _swap_artifacts(task, clone, a, b)
        loaded = load_sources_duckdb(task, clone, con)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    counts = dict(getattr(loaded, "counts", None) or {})
    for table in plan.omit_tables:
        relation = quote_sql_identifier(table, dialect="duckdb", force=True)
        con.execute(f"DROP TABLE IF EXISTS {relation}")
        counts.pop(table, None)
    return counts


# Execution: mutant -> per-population reward via THE reward implementation

def _resolve_evaluate():
    """The single reward implementation — never a private comparator."""
    from elt_taskgen.verification import upstream_eval

    fn = getattr(upstream_eval, "evaluate", None)
    if fn is None:
        raise RuntimeError(
            "verification.upstream_eval.evaluate is unavailable; attacks refuse "
            "to score with any other comparator (fail closed)"
        )
    return fn


def _resolve_evaluate_variant():
    """The single reward implementation's VARIANT dispatch; absent => raise,
    because a per-variant number from anything else is a second reward."""
    from elt_taskgen.verification import upstream_eval

    fn = getattr(upstream_eval, "evaluate_variant", None)
    if fn is None:
        raise RuntimeError(
            "verification.upstream_eval.evaluate_variant is unavailable; attacks "
            "refuse to score a variant with any other comparator (fail closed)"
        )
    return fn


def _create_and_fill(
    con: duckdb.DuckDBPyConnection, table: TableSpec, rows: list[Row]
) -> None:
    cols_ddl = ", ".join(
        f"{quote_sql_identifier(c.name, dialect='duckdb', force=True)} "
        f"{_DUCK_TYPES[c.type]}"
        for c in table.columns
    )
    relation = quote_sql_identifier(table.name, dialect="duckdb", force=True)
    con.execute(f"CREATE TABLE {relation} ({cols_ddl})")
    if rows:
        placeholders = ", ".join("?" for _ in table.columns)
        data = [[row.get(c.name) for c in table.columns] for row in rows]
        con.executemany(
            f"INSERT INTO {relation} VALUES ({placeholders})", data
        )


def _load_rows_jsonl(rows_dir: Path, table: TableSpec) -> list[Row]:
    path = rows_dir / f"{table.name}.jsonl"
    if not path.is_file():
        raise FileNotFoundError(
            f"canonical rows file {path} missing — cannot execute attack (fail closed)"
        )
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _load_population_sources(
    task: TaskIR,
    population: PopulationName,
    workspace: Path,
    con: duckdb.DuckDBPyConnection,
    skip_tables: frozenset[str],
) -> dict[str, int]:
    """E+L one population into `con`; returns per-table loaded row counts.

    Prefers the trusted reference loader over populations/<pop>/rendered/,
    falling back to the canonical rows/*.jsonl; missing both => raise.
    """
    pop_dir = Path(workspace) / "tasks" / task.task_id / "populations" / population.value
    rendered_dir = pop_dir / "rendered"
    rows_dir = pop_dir / "rows"

    if not skip_tables and rendered_dir.is_dir():
        try:
            from elt_taskgen.reference import solution as _ref_solution
        except ImportError:
            _ref_solution = None
        loader = getattr(_ref_solution, "load_sources_duckdb", None)
        if loader is not None:
            loaded = loader(task, rendered_dir, con)
            counts = getattr(loaded, "counts", None)
            if not isinstance(counts, dict):
                raise RuntimeError(
                    "reference loader returned no counts — fail closed"
                )
            return dict(counts)

    if not rows_dir.is_dir():
        raise FileNotFoundError(
            f"no loadable sources for population {population.value!r}: neither a "
            f"usable {rendered_dir} nor {rows_dir} exists (fail closed)"
        )
    counts: dict[str, int] = {}
    for table in task.tables:
        if table.name in skip_tables:
            _create_and_fill(con, table, [])
            counts[table.name] = 0
            continue
        rows = _load_rows_jsonl(rows_dir, table)
        _create_and_fill(con, table, rows)
        counts[table.name] = len(rows)
    return counts


class AttackOutputLimitError(ValueError):
    """A mutant mart's materialized output exceeded the row or byte cap."""


#: Stable code for unscorable mutant output that exceeded a harness cap.
ATTACK_OUTPUT_LIMIT_CODE = "output_limit"

#: Stable code for mutant SQL refused by the external-access sandbox.
ATTACK_EXTERNAL_ACCESS_CODE = "external_access"


def _fetch_rows(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> list[Row]:
    """Fetch one mart under optional row and byte caps.

    Capped reads stream up to one row beyond the limit and raise
    `AttackOutputLimitError` immediately. Omitting both caps preserves unbounded
    behavior.
    """
    if max_rows is not None and max_rows < 1:
        raise ValueError("max_rows must be positive")
    if max_bytes is not None and max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    cur = con.execute(sql)
    columns = [d[0] for d in cur.description]
    out: list[Row] = []
    total_bytes = 0
    while True:
        record = cur.fetchone()
        if record is None:
            break
        if max_rows is not None and len(out) >= max_rows:
            raise AttackOutputLimitError(
                f"query produced more than the allowed {max_rows} rows"
            )
        row: Row = {}
        for name, value in zip(columns, record):
            if isinstance(value, decimal.Decimal):
                value = float(value)
            elif isinstance(value, (datetime.datetime, datetime.date)):
                value = value.isoformat()
            row[name] = value
        if max_bytes is not None:
            total_bytes += sum(
                len(str(value).encode("utf-8")) for value in row.values()
            )
            if total_bytes > max_bytes:
                raise AttackOutputLimitError(
                    f"query output exceeded the allowed {max_bytes} bytes"
                )
        out.append(row)
    return out


def _record_attack(
    workspace: Path,
    task: TaskIR,
    case: AttackCase,
    sql_by_mart: dict[str, str],
    rewards: dict[PopulationName, float],
    errors: dict[str, str],
    rewards_by_variant: dict[str, dict[str, float]] | None = None,
    stage1_breaks: dict[str, dict[str, str]] | None = None,
    load_plan: LoadMutationPlan | None = None,
) -> None:
    """Persist the mutated SQL and the measured record (no wall-clock content).

    `rewards` is the PARENT reward; `rewards_by_variant` holds the per-variant
    numbers from the SAME execution, and `stage1_breaks` names the tables whose
    counts broke — evidence the strict-binary EL reward collapses to one 0.0.
    """
    attack_dir = Path(workspace) / "tasks" / task.task_id / "attacks" / case.name
    attack_dir.mkdir(parents=True, exist_ok=True)
    for mart in sorted(sql_by_mart):
        (attack_dir / f"mutation_{mart}.sql").write_text(
            sql_by_mart[mart], encoding="utf-8"
        )
    record = {
        "case": case.name,
        "kind": case.kind.value,
        "required": case.required,
        "source_finding": case.source_finding,
        "rewards": {p.value: rewards[p] for p in rewards},
        "expected_pass": {p.value: v for p, v in case.expected_pass.items()},
        "errors": errors,
        "task_content_hash": task.content_hash(),
    }
    if rewards_by_variant is not None:
        record["rewards_by_variant"] = rewards_by_variant
    if stage1_breaks:
        record["stage1_breaks"] = stage1_breaks
    if load_plan is not None:
        record["load_mutation"] = {
            "name": load_plan.name,
            "detail": load_plan.detail,
            "omit_tables": list(load_plan.omit_tables),
            "ops": dict(load_plan.ops),
            "swaps": [list(pair) for pair in load_plan.swaps],
            "source_population": {
                p.value: q.value for p, q in sorted(
                    load_plan.source_population.items(), key=lambda kv: kv[0].value
                )
            },
        }
        if load_plan.fabricated_counts is not None:
            record["load_mutation"]["fabricated_counts"] = {
                p.value: {t: int(n) for t, n in sorted(counts_map.items())}
                for p, counts_map in sorted(
                    load_plan.fabricated_counts.items(), key=lambda kv: kv[0].value
                )
            }
    (attack_dir / "rewards.json").write_text(canonical_json(record), encoding="utf-8")


def _record_inapplicable_attack(
    workspace: Path, task: TaskIR, case: AttackCase, reason: str
) -> None:
    """Record an informational probe whose surface this task does not offer.

    Written as ``inapplicable.json``, NEVER ``rewards.json``: the gate reads a
    rewards record as "compiled" and would then demand measured rewards. The
    reason still lands on disk — an invisible exclusion looks like a hole.
    """
    attack_dir = Path(workspace) / "tasks" / task.task_id / "attacks" / case.name
    attack_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "case": case.name,
        "kind": case.kind.value,
        "required": case.required,
        "inapplicable": reason,
        "task_content_hash": task.content_hash(),
    }
    (attack_dir / "inapplicable.json").write_text(
        canonical_json(record), encoding="utf-8"
    )


def run_attack(
    task: TaskIR, case: AttackCase, gold: object, workspace: Path
) -> dict[PopulationName, float]:
    """Execute one mutant across populations and return rewards by population.

    Score only through the shared evaluator. Record crashes separately because they are
    not evidence, and raise when an EL mutant retains full reward everywhere.
    """
    try:
        sql_by_mart = materialize_mutation(task, case, gold)
    except InertAstMutationError as exc:
        if case.required:
            # A REQUIRED case with no mutation surface is a catalogue lie —
            # its expected_pass matrix claims kills the SQL cannot express.
            raise
        # An informational probe whose kind has no surface in this task's SQL:
        # the code just certified the critic's hypothesis WRONG, so record it.
        _record_inapplicable_attack(workspace, task, case, str(exc))
        return {}
    evaluate = _resolve_evaluate()
    evaluate_variant = _resolve_evaluate_variant()

    mutation = case.mutation.strip()
    load_plan: LoadMutationPlan | None = None
    structured: dict[str, object] | None = None
    if mutation.startswith(STRUCTURED_DIRECTIVE_PREFIX):
        structured = parse_structured_directive(
            mutation[len(STRUCTURED_DIRECTIVE_PREFIX):]
        )
        operation = str(structured["operation"])
        try:
            if operation == "skip_backend":
                load_plan = resolve_load_mutation(
                    task, "skip_backend", str(structured["backend"]), gold
                )
            elif operation == "skip_tables":
                load_plan = resolve_load_mutation(
                    task,
                    "skip_tables",
                    canonical_json(structured.get("tables", [])),
                    gold,
                )
        except InapplicableLoadMutationError as exc:
            if case.required:
                raise
            _record_inapplicable_attack(workspace, task, case, str(exc))
            return {}
    if mutation.startswith(LOAD_DIRECTIVE_PREFIX):
        name, arg = split_load_directive(mutation[len(LOAD_DIRECTIVE_PREFIX):])
        try:
            load_plan = resolve_load_mutation(task, name, arg, gold)
        except InapplicableLoadMutationError as exc:
            if case.required:
                # A REQUIRED mutant with no surface is a bug in the case, not
                # a waivable measurement: it claims kills the data cannot make.
                raise
            _record_inapplicable_attack(workspace, task, case, str(exc))
            return {}

    try:
        requested, realized, fidelity_checks = _validate_realized_fidelity(
            task, case, sql_by_mart, load_plan
        )
    except MutationFidelityError as exc:
        _write_fidelity_record(
            workspace,
            task,
            case,
            passed=False,
            requested=(structured or {"mutation": mutation}),
            realized={},
            checks=(),
            errors=(str(exc),),
        )
        raise
    _write_fidelity_record(
        workspace,
        task,
        case,
        passed=True,
        requested=requested,
        realized=realized,
        checks=fidelity_checks,
    )

    skip_tables: frozenset[str] = frozenset()
    if case.kind is AttackKind.SKIP_EXTRACTION and load_plan is None:
        if not task.backends:
            raise ValueError("skip_extraction attack on a task with no backends")
        skipped_backend = task.backends[0].backend
        skip_tables = frozenset(
            b.table for b in task.backends if b.backend == skipped_backend
        )
        # PREFER the artifact-level form: a non-empty `skip_tables` forces the
        # rows/*.jsonl fallback, which never touches the extraction surface.
        rendered_root = Path(workspace) / "tasks" / task.task_id / "populations"
        if all(
            (rendered_root / pop.value / "rendered").is_dir()
            for pop in _graded_populations(task)
        ):
            load_plan = resolve_load_mutation(task, "skip_backend", "", gold)
            skip_tables = frozenset()

    rewards: dict[PopulationName, float] = {}
    variant_rewards: dict[str, dict[str, float]] = {
        v.value: {} for v in RLVR_TASK_VARIANTS
    }
    stage1_breaks: dict[str, dict[str, str]] = {}
    errors: dict[str, str] = {}
    present = {p.name for p in task.populations}
    for pop in PopulationName:
        if pop not in present:
            continue
        # The mutant is UNTRUSTED SQL: a sandboxed connection (external access
        # off, config locked) under the semantic scorer's resource envelope,
        # so a mutant can neither read the gold nor exhaust the host.
        con = sandboxed_memory_connection(
            memory_limit_mb=ATTACK_MEMORY_LIMIT_MB,
            threads=ATTACK_THREADS,
            disable_temp_spill=True,
            deterministic_settings=True,
        )
        try:
            if load_plan is not None and load_plan.fabricated_counts is not None:
                # The answer is submitted, not measured: nothing loads and no
                # mart SQL runs. Empty marts ON PURPOSE, not crashed marts.
                counts = dict(load_plan.fabricated_counts[pop])
            elif load_plan is not None:
                try:
                    counts = apply_load_mutation(task, load_plan, pop, workspace, con)
                except (duckdb.Error, ValueError, OSError) as e:
                    # The wrong load produced no warehouse: still a kill, but
                    # not a count mismatch, so it is recorded as a crash.
                    counts = {}
                    errors[f"{pop.value}/__load__"] = f"{type(e).__name__}: {e}"
            else:
                counts = _load_population_sources(
                    task, pop, workspace, con, skip_tables
                )
            if case.kind is AttackKind.NO_OP:
                actual_stage1: dict[str, int] = {}
                actual_marts: dict[str, list[Row]] = {m.name: [] for m in task.marts}
            elif load_plan is not None and load_plan.fabricated_counts is not None:
                actual_stage1 = counts
                actual_marts = {m.name: [] for m in task.marts}
            else:
                actual_stage1 = counts
                actual_marts = {}
                if structured is not None and structured.get("operation") == "copy_mart":
                    source_mart = str(structured["source_mart"])
                    source_sql = sql_by_mart[source_mart]
                    quoted = quote_sql_identifier(
                        source_mart, dialect="duckdb", force=True
                    )
                    con.execute(
                        f"CREATE OR REPLACE TEMP VIEW {quoted} AS {source_sql}"
                    )
                for mart in task.marts:
                    sql = sql_by_mart.get(mart.name)
                    if sql is None:
                        actual_marts[mart.name] = []
                        continue
                    try:
                        actual_marts[mart.name] = _fetch_rows(
                            con,
                            sql,
                            max_rows=ATTACK_MAX_RESULT_ROWS_PER_MART,
                            max_bytes=ATTACK_MAX_RESULT_BYTES_PER_MART,
                        )
                    except AttackOutputLimitError:
                        actual_marts[mart.name] = []
                        errors[f"{pop.value}/{mart.name}"] = ATTACK_OUTPUT_LIMIT_CODE
                    except duckdb.PermissionException:
                        # The sandbox's refusal (file system / external
                        # access off), never the path it named.
                        actual_marts[mart.name] = []
                        errors[f"{pop.value}/{mart.name}"] = ATTACK_EXTERNAL_ACCESS_CODE
                    except duckdb.Error as e:
                        actual_marts[mart.name] = []
                        errors[f"{pop.value}/{mart.name}"] = str(e)
            result = evaluate(task, gold, pop, actual_stage1, actual_marts)
            rewards[pop] = float(result.reward)
            # `rewards` above retains the internal composite diagnostic. The
            # graded reward map contains exactly the two RLVR task units.
            for variant in RLVR_TASK_VARIANTS:
                measured = evaluate_variant(
                    variant, task, gold, pop, actual_stage1, actual_marts
                )
                variant_rewards[variant.value][pop.value] = float(measured.reward)
                if variant is TaskVariant.EXTRACT_LOAD and measured.reward < 1.0:
                    broken = {
                        table: why
                        for table, why in sorted(measured.stage1_detail.items())
                        if not why.startswith("ok (")
                    }
                    if broken:
                        stage1_breaks[pop.value] = broken
        finally:
            con.close()

    _record_attack(
        workspace,
        task,
        case,
        sql_by_mart,
        rewards,
        errors,
        rewards_by_variant=variant_rewards,
        stage1_breaks=stage1_breaks,
        load_plan=load_plan,
    )

    if load_plan is not None:
        el = variant_rewards[TaskVariant.EXTRACT_LOAD.value]
        if el and all(value >= 1.0 for value in el.values()):
            raise InertLoadMutationError(
                f"attack {case.name}: load mutation {load_plan.name!r} keeps FULL "
                f"extract-load reward on every population of task "
                f"{task.task_id} ({load_plan.detail}) — inert, fail closed"
            )
    return rewards


# Promotion: ProposedAttackCase -> measured -> required AttackCase (or record)

class PromotionOutcome(BaseModel):
    """One proposal's fate, recorded whether it was promoted or not.

    Both matrices are present whenever the case ran: `predicted` is the
    council's bet, `measured`/`measured_pass` the result, and `mismatches`
    every population where the two disagree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str
    case_name: str
    kind: str
    promoted: bool
    reason: str
    predicted: dict[str, bool] = Field(default_factory=dict)
    measured: dict[str, float] = Field(default_factory=dict)
    measured_pass: dict[str, bool] = Field(default_factory=dict)
    predicted_by_stage: dict[str, dict[str, bool]] = Field(default_factory=dict)
    measured_by_stage: dict[str, dict[str, float]] = Field(default_factory=dict)
    measured_pass_by_stage: dict[str, dict[str, bool]] = Field(default_factory=dict)
    fidelity: dict[str, object] = Field(default_factory=dict)
    mismatches: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromotionResult:
    """What the promoter did, with the (possibly extended) task.

    `task` is the SAME object when nothing was promoted, else a copy carrying
    the promoted cases — a SEMANTIC edit the caller must persist so every stage
    re-attests at the new content hash.
    """

    task: TaskIR
    promoted: tuple[AttackCase, ...]
    rejected: tuple[PromotionOutcome, ...]
    outcomes: tuple[PromotionOutcome, ...]
    #: case name -> rewards for every executed proposal; unmerged into the
    #: attack payload reads as deleted evidence and fails the gate.
    rewards: dict[str, dict[PopulationName, float]]

    @property
    def task_changed(self) -> bool:
        return bool(self.promoted)


def _proposal_case_name(finding: Finding) -> str:
    return f"{PROPOSAL_CASE_PREFIX}{finding.finding_id}"


def _string_tuple(value: object, *, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (tuple, list)):
        values = tuple(value)
    else:
        raise ValueError(f"proposal param {name!r} must be a string or string list")
    if not values or not all(isinstance(item, str) and item for item in values):
        raise ValueError(f"proposal param {name!r} contains an empty/non-string value")
    return tuple(str(item) for item in values)


def _copy_mart_pair(params: dict[str, object]) -> tuple[str, str]:
    raw = params["copy_mart"]
    if isinstance(raw, str):
        if "->" in raw:
            source, target = (part.strip() for part in raw.split("->", 1))
        else:
            source = raw
            target = str(params.get("target_mart") or "")
    else:
        pair = _string_tuple(raw, name="copy_mart")
        if len(pair) != 2:
            raise ValueError(
                "proposal param 'copy_mart' must contain [source_mart,target_mart]"
            )
        source, target = pair
    if not source or not target:
        raise ValueError(
            "proposal copy_mart needs both source and target mart identifiers"
        )
    return source, target


def _proposal_structured_payload(
    proposal: ProposedAttackCase,
) -> dict[str, object] | None:
    """Compile proposal params to ONE exact operation, or None for legacy kind.

    No key is ever ignored.  Multiple operation families are ambiguous and
    rejected instead of executing whichever happens to be checked first.
    """
    params: dict[str, object] = dict(proposal.params)
    unknown = sorted(set(params) - PROPOSAL_PARAM_KEYS)
    if unknown:
        raise ValueError(
            f"unsupported proposal parameter(s) {unknown}; supported: "
            f"{sorted(PROPOSAL_PARAM_KEYS)}"
        )
    if VARIANT_PARAM in params:
        variant = params[VARIANT_PARAM]
        if not isinstance(variant, str) or not variant:
            raise ValueError("proposal param 'variant' must be a non-empty string")
        if params.get(HARDCODE_PARAM) is not None:
            raise ValueError(
                f"proposal param 'variant' cannot be combined with "
                f"{HARDCODE_PARAM!r}"
            )
    operation_keys = [
        key
        for key in (
            "copy_mart",
            "skip_tables",
            "skip_backend",
            "zero_is_missing",
            "add_dedup",
            "remove_dedup",
        )
        if key in params and params[key] not in (None, False, (), [])
    ]
    if len(operation_keys) > 1:
        raise ValueError(
            "one proposed_case may describe exactly one executable operation; "
            f"got {operation_keys}"
        )
    if operation_keys and VARIANT_PARAM in params:
        raise ValueError(
            f"proposal param 'variant' cannot be combined with "
            f"{operation_keys[0]!r}"
        )
    if operation_keys and params.get(HARDCODE_PARAM) is not None:
        raise ValueError(
            f"{HARDCODE_PARAM!r} cannot be combined with {operation_keys[0]!r}"
        )
    if not operation_keys:
        extras = set(params) - {HARDCODE_PARAM, VARIANT_PARAM}
        if extras:
            raise ValueError(
                f"proposal parameters {sorted(extras)} do not select an operation"
            )
        return None

    operation = operation_keys[0]
    auxiliaries = {
        "copy_mart": {"target_mart"},
        "add_dedup": {"dedup_table"},
    }.get(operation, set())
    stray = set(params) - {operation} - auxiliaries
    if stray:
        raise ValueError(
            f"proposal operation {operation!r} has unrelated parameters "
            f"{sorted(stray)}"
        )
    if operation == "copy_mart":
        if proposal.kind is not AttackKind.CUSTOM:
            raise ValueError("copy_mart requires proposed kind 'custom'")
        source, target = _copy_mart_pair(params)
        return {
            "operation": operation,
            "source_mart": source,
            "target_mart": target,
        }
    if operation == "skip_tables":
        if proposal.kind not in (
            AttackKind.SKIP_EXTRACTION,
            AttackKind.PARTIAL_BACKEND,
        ):
            raise ValueError(
                "skip_tables requires kind 'skip_extraction' or 'partial_backend'"
            )
        return {
            "operation": operation,
            "tables": list(_string_tuple(params[operation], name=operation)),
        }
    if operation == "skip_backend":
        if proposal.kind is not AttackKind.SKIP_EXTRACTION:
            raise ValueError("skip_backend requires kind 'skip_extraction'")
        backend = str(params[operation])
        try:
            Backend(backend)
        except ValueError as exc:
            raise ValueError(f"unknown skip_backend {backend!r}") from exc
        return {"operation": operation, "backend": backend}
    if operation == "zero_is_missing":
        if params[operation] is not True:
            raise ValueError("zero_is_missing must be true when specified")
        if proposal.kind not in (AttackKind.NO_NULL_DEFAULT, AttackKind.CUSTOM):
            raise ValueError(
                "zero_is_missing requires kind 'no_null_default' or 'custom'"
            )
        return {"operation": operation}
    if operation == "add_dedup":
        if params[operation] is not True:
            raise ValueError("add_dedup must be true when specified")
        if proposal.kind is not AttackKind.CUSTOM:
            raise ValueError("add_dedup requires proposed kind 'custom'")
        payload: dict[str, object] = {"operation": operation}
        if params.get("dedup_table"):
            payload["dedup_table"] = str(params["dedup_table"])
        return payload
    if operation == "remove_dedup":
        if params[operation] is not True:
            raise ValueError("remove_dedup must be true when specified")
        if proposal.kind is not AttackKind.NO_DEDUP:
            raise ValueError("remove_dedup requires proposed kind 'no_dedup'")
        return {"operation": operation}
    raise AssertionError(operation)  # pragma: no cover - closed list above


def _proposal_mutation(finding: Finding, proposal: ProposedAttackCase) -> str:
    """Compile a structured proposal into an executable mutation directive.

    Parameters select explicit targets; otherwise the kind must have a registered
    default. Finding prose never supplies implicit parameters. Unknown populations,
    kinds without defaults, and unregistered variants raise instead of being
    approximated.
    """
    structured = _proposal_structured_payload(proposal)
    if structured is not None:
        return _structured_directive(structured)

    requested = proposal.params.get(HARDCODE_PARAM)
    if requested is not None:
        if proposal.kind is not AttackKind.CONSTANTS:
            raise ValueError(
                f"{HARDCODE_PARAM!r} requires proposed kind 'constants'"
            )
        value = str(requested)
        if value not in {p.value for p in PopulationName}:
            raise ValueError(
                f"proposed case names unknown {HARDCODE_PARAM} {value!r} "
                f"(known: {sorted(p.value for p in PopulationName)})"
            )
        return f"{HARDCODE_DIRECTIVE_PREFIX}{value}"
    requested_variant = proposal.params.get(VARIANT_PARAM)
    if requested_variant is not None:
        if proposal.kind.value in LOAD_MUTATIONS:
            raise ValueError(
                f"load attack kind {proposal.kind.value!r} does not accept a "
                "kind variant"
            )
        value = str(requested_variant)
        # Parse the exact directive now.  This verifies both kind and variant
        # before a candidate can reach materialization or execute any SQL.
        split_kind_directive(
            f"{proposal.kind.value}{VARIANT_SEPARATOR}{value}"
        )
        return (
            f"{KIND_DIRECTIVE_PREFIX}{proposal.kind.value}"
            f"{VARIANT_SEPARATOR}{value}"
        )
    mutation = _default_attack_directive(proposal.kind)
    if mutation is None:
        allowed = sorted(KIND_VARIANTS.get(proposal.kind, frozenset()))
        raise ValueError(
            f"proposal kind {proposal.kind.value!r} has no default variant; "
            f"params.variant must name one of {allowed}"
        )
    return mutation


def _proposal_description(finding: Finding, proposal: ProposedAttackCase) -> str:
    """Full-fidelity description: the finding's text AND the proposal's own
    rationale/params both survive — this is what a human reads at audit time."""
    parts = [
        f"Promoted from {finding.role.value} proposal on finding "
        f"{finding.finding_id}: {finding.summary}"
    ]
    if finding.detail:
        parts.append(finding.detail)
    parts.append(f"Proposer rationale: {proposal.rationale}")
    if proposal.params:
        params = ", ".join(
            f"{k}={proposal.params[k]!r}" for k in sorted(proposal.params)
        )
        parts.append(f"Proposal params: {params}")
    return " — ".join(parts)


def _candidate_case(
    finding: Finding, proposal: ProposedAttackCase, *, required: bool
) -> AttackCase:
    """The proposal as an AttackCase.

    Measured as required=False (an unproven proposal must assert nothing);
    rebuilt required=True only once the measurement matched, which is what puts
    it under the required-mutants gate.
    """
    return AttackCase(
        name=_proposal_case_name(finding),
        kind=proposal.kind,
        description=_proposal_description(finding, proposal),
        mutation=_proposal_mutation(finding, proposal),
        expected_pass=dict(proposal.expected_pass),
        required=required,
        source_finding=finding.finding_id,
    )


def _validate_proposal_targets(task: TaskIR, case: AttackCase) -> None:
    """Validate task-bound proposal identifiers before execution.

    Reject unknown marts, tables, backends, or populations as compile failures;
    materializers repeat their checks as a second boundary.
    """
    mutation = case.mutation.strip()
    mart_names = {mart.name for mart in task.marts}
    table_names = {table.name for table in task.tables}
    task_backends = {assignment.backend for assignment in task.backends}
    populations = {population.name for population in task.populations}

    if mutation.startswith(STRUCTURED_DIRECTIVE_PREFIX):
        payload = parse_structured_directive(
            mutation[len(STRUCTURED_DIRECTIVE_PREFIX):]
        )
        operation = str(payload["operation"])
        if operation == "copy_mart":
            source = str(payload.get("source_mart") or "")
            target = str(payload.get("target_mart") or "")
            unknown = sorted({source, target} - mart_names - {""})
            if unknown:
                raise ValueError(
                    f"proposal copy_mart names unknown mart(s) {unknown}; "
                    f"known: {sorted(mart_names)}"
                )
            if not source or not target or source == target:
                raise ValueError(
                    "proposal copy_mart requires two different declared marts"
                )
        elif operation == "skip_tables":
            tables = tuple(str(value) for value in payload.get("tables", []))
            unknown = sorted(set(tables) - table_names)
            if unknown:
                raise ValueError(
                    f"proposal skip_tables names unknown table(s) {unknown}; "
                    f"known: {sorted(table_names)}"
                )
        elif operation == "skip_backend":
            backend = Backend(str(payload.get("backend") or ""))
            if backend not in task_backends:
                raise ValueError(
                    f"proposal skip_backend names unassigned backend "
                    f"{backend.value!r}; assigned: "
                    f"{sorted(value.value for value in task_backends)}"
                )
        elif operation == "add_dedup" and payload.get("dedup_table"):
            table = str(payload["dedup_table"])
            if table not in table_names:
                raise ValueError(
                    f"proposal add_dedup names unknown table {table!r}; "
                    f"known: {sorted(table_names)}"
                )
        return

    if mutation.startswith(HARDCODE_DIRECTIVE_PREFIX):
        population = PopulationName(
            mutation[len(HARDCODE_DIRECTIVE_PREFIX):]
        )
        if population not in populations:
            raise ValueError(
                f"proposal hardcode_population names absent population "
                f"{population.value!r}"
            )
        return

    if mutation.startswith(LOAD_DIRECTIVE_PREFIX):
        name, argument = split_load_directive(
            mutation[len(LOAD_DIRECTIVE_PREFIX):]
        )
        if name != case.kind.value:
            raise ValueError(
                f"proposal kind {case.kind.value!r} became load mutation {name!r}"
            )
        if argument:
            raise ValueError(
                f"proposal load mutation {name!r} carries an unsupported "
                "unvalidated argument"
            )
        return

    if mutation.startswith(KIND_DIRECTIVE_PREFIX):
        kind, _variant = split_kind_directive(
            mutation[len(KIND_DIRECTIVE_PREFIX):]
        )
        if kind is not case.kind:
            raise ValueError(
                f"proposal kind {case.kind.value!r} became kind {kind.value!r}"
            )
        return

    raise ValueError("a proposed attack case must compile to a closed directive")


def validate_proposed_case(
    task: TaskIR,
    finding: Finding,
    proposal: ProposedAttackCase,
    *,
    required: bool = False,
) -> tuple[AttackCase, dict[str, object]]:
    """Compile and statically validate one proposal without executing it."""
    if set(proposal.expected_pass_by_stage) != set(RLVR_TASK_VARIANTS):
        raise ValueError(
            "active proposed_case requires complete extract_load and transform "
            "prediction matrices; legacy combined-only evidence cannot execute"
        )
    case = _candidate_case(finding, proposal, required=required)
    _validate_proposal_targets(task, case)
    fidelity = validate_proposal_claim_fidelity(task, finding, proposal, case)
    return case, fidelity


def _compare_matrices(
    predicted: dict[str, bool], measured: dict[PopulationName, float]
) -> tuple[dict[str, bool], list[str]]:
    """(measured_pass, mismatches) over ALL FIVE populations.

    A population with no measured reward is a MISMATCH, not a pass: unmeasured
    means the prediction covering it stays unverified (fail closed).
    """
    measured_pass: dict[str, bool] = {}
    mismatches: list[str] = []
    for pop in PopulationName:
        got = measured.get(pop)
        if got is None:
            mismatches.append(
                f"{pop.value}: predicted "
                f"{'FULL' if predicted[pop.value] else 'LOST'}, but no reward "
                "was measured on that population"
            )
            continue
        measured_pass[pop.value] = got == 1.0
        if measured_pass[pop.value] is not predicted[pop.value]:
            mismatches.append(
                f"{pop.value}: predicted "
                f"{'FULL' if predicted[pop.value] else 'LOST'}, measured "
                f"reward {got}"
            )
    return measured_pass, mismatches


def _recorded_stage_rewards(
    workspace: Path, task: TaskIR, case_name: str
) -> dict[str, dict[str, float]]:
    path = (
        Path(workspace)
        / "tasks"
        / task.task_id
        / "attacks"
        / case_name
        / "rewards.json"
    )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"cannot read stage reward matrix for {case_name!r}: {exc}"
        ) from exc
    if record.get("task_content_hash") != task.content_hash():
        raise ValueError(
            f"stage reward matrix for {case_name!r} is stale at "
            f"{record.get('task_content_hash')!r}"
        )
    raw = record.get("rewards_by_variant")
    if not isinstance(raw, dict):
        raise ValueError(f"attack {case_name!r} has no rewards_by_variant")
    out: dict[str, dict[str, float]] = {}
    for stage in RLVR_TASK_VARIANTS:
        per_population = raw.get(stage.value)
        if not isinstance(per_population, dict):
            raise ValueError(
                f"attack {case_name!r} has no {stage.value!r} reward matrix"
            )
        out[stage.value] = {
            str(pop): float(value) for pop, value in per_population.items()
        }
    errors = record.get("errors")
    if isinstance(errors, dict) and errors:
        raise ValueError(
            f"attack {case_name!r} crashed instead of realizing a wrong "
            f"implementation: {sorted(str(key) for key in errors)[:5]}"
        )
    return out


def _compare_stage_matrices(
    predicted: dict[str, dict[str, bool]],
    measured: dict[str, dict[str, float]],
) -> tuple[dict[str, dict[str, bool]], list[str]]:
    measured_pass: dict[str, dict[str, bool]] = {}
    mismatches: list[str] = []
    for stage in RLVR_TASK_VARIANTS:
        stage_name = stage.value
        expected_stage = predicted.get(stage_name)
        measured_stage = measured.get(stage_name)
        if expected_stage is None:
            mismatches.append(f"{stage_name}: no prediction supplied")
            continue
        if measured_stage is None:
            mismatches.append(f"{stage_name}: no rewards measured")
            continue
        passed: dict[str, bool] = {}
        for population in PopulationName:
            got = measured_stage.get(population.value)
            if got is None:
                mismatches.append(
                    f"{stage_name}/{population.value}: no reward measured"
                )
                continue
            passed[population.value] = got == 1.0
            expected = expected_stage.get(population.value)
            if expected is None:
                mismatches.append(
                    f"{stage_name}/{population.value}: no prediction supplied"
                )
            elif passed[population.value] is not expected:
                mismatches.append(
                    f"{stage_name}/{population.value}: predicted "
                    f"{'FULL' if expected else 'LOST'}, measured reward {got}"
                )
        measured_pass[stage_name] = passed
    return measured_pass, mismatches


def _record_rejected_proposal(
    workspace: Path, task: TaskIR, outcome: PromotionOutcome
) -> Path:
    """Persist a rejected proposal with BOTH matrices (no wall clock)."""
    attack_dir = (
        Path(workspace) / "tasks" / task.task_id / "attacks" / outcome.case_name
    )
    attack_dir.mkdir(parents=True, exist_ok=True)
    path = attack_dir / REJECTED_PROPOSAL_FILENAME
    # Import lazily to avoid a review↔verification cycle. The projection is the
    # value-free, booleans-and-codes view consumed by `audit list`.
    from elt_taskgen.review.tools.critic_validators import project_proposal_matrix
    from elt_taskgen.review.tools.projection import project_promotion

    record = {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "promoted": False,
        **outcome.model_dump(mode="json"),
        "projection": project_promotion(outcome),
        # Post-session audit matrix: booleans only, never rewards or a session tool.
        "projection_matrix": project_proposal_matrix((outcome,)),
    }
    path.write_text(canonical_json(record), encoding="utf-8")
    return path


def promote_proposed_cases(
    task: TaskIR,
    findings: list[Finding],
    workspace: Path,
    gold: object = None,
) -> PromotionResult:
    """Execute proposed cases and promote only exact reward predictions.

    Process findings in id order across all populations. Any compile error or prediction
    mismatch records rejection and promotes nothing; partial promotion is forbidden.
    """
    if gold is None:
        raise ValueError(
            "promote_proposed_cases requires the frozen gold bundle to score "
            "proposals against (fail closed — a proposal is only ever decided "
            "by measurement)"
        )

    existing = {c.name for c in task.attack_cases}
    outcomes: list[PromotionOutcome] = []
    promoted: list[AttackCase] = []
    rewards: dict[str, dict[PopulationName, float]] = {}

    for finding in sorted(findings, key=lambda f: f.finding_id):
        proposal = finding.proposed_case
        if proposal is None:
            continue
        name = _proposal_case_name(finding)
        predicted = {p.value: bool(proposal.expected_pass[p]) for p in PopulationName}
        predicted_by_stage = {
            stage.value: {
                population.value: bool(
                    proposal.expected_pass_by_stage[stage][population]
                )
                for population in PopulationName
            }
            for stage in RLVR_TASK_VARIANTS
            if stage in proposal.expected_pass_by_stage
        }

        if name in existing:
            # Already promoted at this identity: re-promoting would duplicate
            # the case name (TaskIR rejects that). Idempotent by construction.
            outcomes.append(
                PromotionOutcome(
                    finding_id=finding.finding_id,
                    case_name=name,
                    kind=proposal.kind.value,
                    promoted=True,
                    reason=(
                        "already in the task's attack set at this content "
                        "hash; measured as a standing case"
                    ),
                    predicted=predicted,
                    predicted_by_stage=predicted_by_stage,
                )
            )
            continue

        claim_fidelity: dict[str, object] = {}
        fidelity: dict[str, object] = {}
        try:
            candidate, claim_fidelity = validate_proposed_case(
                task, finding, proposal, required=False
            )
            if not bool(claim_fidelity["passed"]):
                _write_fidelity_record(
                    workspace,
                    task,
                    candidate,
                    passed=False,
                    requested=dict(claim_fidelity["requested"]),
                    realized={},
                    checks=tuple(str(v) for v in claim_fidelity["checks"]),
                    errors=tuple(str(v) for v in claim_fidelity["errors"]),
                )
                raise MutationFidelityError(
                    "; ".join(str(v) for v in claim_fidelity["errors"])
                )
            measured = run_attack(task, candidate, gold, workspace)
            compiler_fidelity = json.loads(
                _fidelity_record_path(workspace, task, candidate).read_text(
                    encoding="utf-8"
                )
            )
            fidelity = {
                "passed": bool(claim_fidelity.get("passed"))
                and bool(compiler_fidelity.get("passed")),
                "claim": claim_fidelity,
                "compiler": compiler_fidelity,
            }
        except Exception as exc:  # noqa: BLE001 - any failure REJECTS, loudly
            outcome = PromotionOutcome(
                finding_id=finding.finding_id,
                case_name=name,
                kind=proposal.kind.value,
                promoted=False,
                reason=f"proposal could not be executed: {type(exc).__name__}: {exc}",
                predicted=predicted,
                predicted_by_stage=predicted_by_stage,
                fidelity=fidelity or claim_fidelity,
            )
            outcomes.append(outcome)
            _record_rejected_proposal(workspace, task, outcome)
            continue

        rewards[name] = measured
        measured_pass, mismatches = _compare_matrices(predicted, measured)
        measured_by_stage: dict[str, dict[str, float]] = {}
        measured_pass_by_stage: dict[str, dict[str, bool]] = {}
        if measured:
            try:
                measured_by_stage = _recorded_stage_rewards(
                    workspace, task, name
                )
            except ValueError as exc:
                mismatches.append(str(exc))
            else:
                if predicted_by_stage:
                    measured_pass_by_stage, stage_mismatches = (
                        _compare_stage_matrices(
                            predicted_by_stage, measured_by_stage
                        )
                    )
                    mismatches.extend(stage_mismatches)
        if mismatches:
            outcome = PromotionOutcome(
                finding_id=finding.finding_id,
                case_name=name,
                kind=proposal.kind.value,
                promoted=False,
                reason=(
                    "measured reward matrix does not match the proposed "
                    f"expectation on {len(mismatches)} population(s)"
                ),
                predicted=predicted,
                measured={p.value: r for p, r in measured.items()},
                measured_pass=measured_pass,
                predicted_by_stage=predicted_by_stage,
                measured_by_stage=measured_by_stage,
                measured_pass_by_stage=measured_pass_by_stage,
                fidelity=fidelity,
                mismatches=tuple(mismatches),
            )
            outcomes.append(outcome)
            _record_rejected_proposal(workspace, task, outcome)
            continue

        if all(predicted.values()):
            outcome = PromotionOutcome(
                finding_id=finding.finding_id,
                case_name=name,
                kind=proposal.kind.value,
                promoted=False,
                reason=(
                    "proposal was confirmed to keep FULL combined reward on "
                    "all five populations — live uncaught exploit; block and "
                    "repair rather than promoting a mutant that asserts no kill"
                ),
                predicted=predicted,
                measured={p.value: r for p, r in measured.items()},
                measured_pass=measured_pass,
                predicted_by_stage=predicted_by_stage,
                measured_by_stage=measured_by_stage,
                measured_pass_by_stage=measured_pass_by_stage,
                fidelity=fidelity,
            )
            outcomes.append(outcome)
            _record_rejected_proposal(workspace, task, outcome)
            continue

        promoted.append(_candidate_case(finding, proposal, required=True))
        existing.add(name)
        outcomes.append(
            PromotionOutcome(
                finding_id=finding.finding_id,
                case_name=name,
                kind=proposal.kind.value,
                promoted=True,
                reason=(
                    "measured reward matrix matches the proposed expectation "
                    "on all five populations"
                ),
                predicted=predicted,
                measured={p.value: r for p, r in measured.items()},
                measured_pass=measured_pass,
                predicted_by_stage=predicted_by_stage,
                measured_by_stage=measured_by_stage,
                measured_pass_by_stage=measured_pass_by_stage,
                fidelity=fidelity,
            )
        )

    new_task = task
    if promoted:
        new_task = task.model_copy(
            update={"attack_cases": tuple(task.attack_cases) + tuple(promoted)}
        )
    return PromotionResult(
        task=new_task,
        promoted=tuple(promoted),
        rejected=tuple(o for o in outcomes if not o.promoted),
        outcomes=tuple(outcomes),
        rewards=rewards,
    )

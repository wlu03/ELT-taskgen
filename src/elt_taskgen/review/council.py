"""Run council roles through a pluggable provider and return typed findings.

`Finding` cannot express acceptance. `render_view` enforces each role's information
boundary, and view changes invalidate transcript and admission identities.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Any, Protocol

import sqlglot
import yaml
from sqlglot import exp as sqlexp
from sqlglot.optimizer.eliminate_subqueries import eliminate_subqueries

from elt_taskgen.generation.mart_plan import (
    solver_safe_mart_requirements,
    solver_safe_plan_summary,
)

from elt_taskgen.models import (
    AttackKind,
    CouncilRole,
    Finding,
    FindingProvenance,
    FindingScreen,
    FindingScreenStatus,
    MartSpec,
    PopulationName,
    ProposedAttackCase,
    RepairRoute,
    Severity,
    TaskIR,
    sha256_hex,
)

__all__ = [
    "SCREEN_SIGNALS",
    "Provider",
    "ProviderProtocolError",
    "ast_leak_findings",
    "author_prose",
    "is_executable_probe",
    "leak_findings",
    "render_view",
    "run_council",
    "screen_findings",
    "screen_report",
    "semantic_leak_findings",
    "task_vocabulary",
]

#: Fixed critic execution order (deterministic council output ordering).
CRITIC_ROLES: tuple[CouncilRole, ...] = (
    CouncilRole.AMBIGUITY_CRITIC,
    CouncilRole.POPULATION_ADVERSARY,
    CouncilRole.SHORTCUT_ATTACKER,
    CouncilRole.FEASIBILITY_REVIEWER,
)

_DATA_MODEL_BEGIN = "=== BEGIN DATA MODEL ==="
_DATA_MODEL_END = "=== END DATA MODEL ==="

#: Minimum normalized-fragment length considered evidence of a private-SQL leak.
_LEAK_FRAGMENT_MIN_LEN = 15


def _extract_block(text: str, begin: str, end: str) -> str | None:
    """Return the text between two marker lines, or None if absent."""
    start = text.find(begin)
    if start == -1:
        return None
    start += len(begin)
    stop = text.find(end, start)
    if stop == -1:
        return None
    return text[start:stop]


# Provider protocol (implementations live in review/providers.py)

class Provider(Protocol):
    """Pluggable completion backend for one council role."""

    def complete(self, role: CouncilRole, prompt: str) -> str: ...


class ProviderProtocolError(RuntimeError):
    """A provider response violated the council protocol (not valid JSON, no
    'findings' list, or an unparseable finding object).

    Raised, never degraded: a synthetic finding would let a broken provider
    masquerade as a diligent reviewer. The review stage fails closed."""


# View builders (role-specific information barriers)

def _table_lines(task: TaskIR) -> list[str]:
    lines: list[str] = []
    for t in task.tables:
        backend = task.backend_for(t.name).backend.value
        lines.append(f"- table {t.name} (backend: {backend}): {t.description}")
        for c in t.columns:
            bits = [c.type.value]
            if c.nullable:
                bits.append("nullable")
            if c.enum_values:
                bits.append("one of " + ", ".join(c.enum_values))
            lines.append(f"    - {c.name} ({'; '.join(bits)}): {c.description}")
        if t.primary_key:
            lines.append(f"    primary key: {', '.join(t.primary_key)}")
        if t.business_key:
            lines.append(f"    business key: {', '.join(t.business_key)}")
    for rel in task.relationships:
        req = "required" if rel.required else "optional (may be NULL or dangling)"
        lines.append(
            f"- relationship: {rel.child_table}({', '.join(rel.child_columns)}) -> "
            f"{rel.parent_table}({', '.join(rel.parent_columns)}), {req}"
        )
    return lines


def _author_plan_summary(task: TaskIR, mart: MartSpec) -> str:
    """The shared solver-safe projection used by prompts and public exports."""

    return solver_safe_plan_summary(task, mart)


def mart_data_model_prose(task: TaskIR, mart: MartSpec) -> str:
    """data_model.yaml-style prose description of one mart.

    Derived ONLY from MartSpec fields, never from reference SQL.
    """
    requirements = solver_safe_mart_requirements(task, mart)
    doc = {
        mart.name: {
            key: value
            for key, value in requirements.items()
            if key not in {"name", "source_relationships"}
        }
    }
    return yaml.safe_dump(doc, sort_keys=True, default_flow_style=False, width=88)


def _author_view(task: TaskIR) -> str:
    """What the semantic author sees: IR + plan summaries. No reference SQL,
    no attack mutations, no gold, no counterfactual literal rows."""
    lines = [
        f"TASK: {task.title or task.task_id}",
        "",
        "SOURCE SCHEMA:",
        *_table_lines(task),
        "",
        "MART PLAN SUMMARIES:",
    ]
    for mart in task.marts:
        lines.append(_author_plan_summary(task, mart))
    lines += [
        "",
        _DATA_MODEL_BEGIN,
        *[mart_data_model_prose(task, m).rstrip("\n") for m in task.marts],
        _DATA_MODEL_END,
        "",
        "Write solver-visible prose describing the project and each mart. "
        "Do not include any SQL implementation or any output values.",
    ]
    return "\n".join(lines)


def _population_summary(
    task: TaskIR,
    *,
    with_conditions: bool,
    condition_text: Callable[[int, int, str], str] | None = None,
) -> list[str]:
    """Render population names and scales, optionally including condition prose.

    Literal rows are always withheld. `condition_text` may replace individual condition
    lines; by default conditions are rendered verbatim.
    """
    lines: list[str] = []
    for index, pop in enumerate(task.populations):
        scale = (
            ", ".join(f"{t}~{n}" for t, n in sorted(pop.scale.items()))
            if pop.scale
            else "literal constructed rows"
        )
        lines.append(f"- population {pop.name.value}: scale [{scale}]")
        if with_conditions:
            for slot, cond in enumerate(pop.conditions):
                shown = cond if condition_text is None else condition_text(index, slot, cond)
                lines.append(f"    condition: {shown}")
            if pop.literal_rows:
                lines.append(
                    "    (this population is built from literal constructed "
                    "rows; the row arrays themselves are not shown, but the "
                    "conditions above are reproduced verbatim and may name "
                    "concrete values)"
                )
    return lines


def _mart_output_lines(task: TaskIR) -> list[str]:
    lines: list[str] = []
    for m in task.marts:
        lines.append(f"- mart {m.name} (grain: {m.grain}; keys: {', '.join(m.key_columns)})")
        for c in m.columns:
            lines.append(f"    - {c.name} ({c.type.value}): {c.description}")
    return lines


def _public_source_schema_lines(task: TaskIR) -> list[str]:
    """Exactly the source-schema surface the SOLVER'S bundle publishes.

    PARITY RULE, BOTH DIRECTIONS: the two public files are reproduced VERBATIM
    by calling the exporter's own functions, never re-derived, so the view
    cannot drift from the shipped bytes. A POORER VIEW REJECTS SOUND TASKS —
    critics fatally report facts that are shipped to the solver but absent from
    their screen. Never here: reference SQL, plans, mutations, gold, literal rows.
    """
    from elt_taskgen.export.eltbench import (
        _source_schema_markdown,  # the exact typed block documentation.md ships
        schema_csv,
    )

    lines: list[str] = [
        "  [documentation.md — the '## Source tables' block, shipped verbatim "
        "in every variant's bundle]",
    ]
    lines += [f"  {line}".rstrip() for line in _source_schema_markdown(task)]
    lines.append(
        "  [schemas/<table>.csv — shipped verbatim; header is the pinned "
        "upstream two-column header]"
    )
    for table in task.tables:
        lines.append(f"  schemas/{table.name}.csv:")
        lines += [
            f"    {line}" for line in schema_csv(table).rstrip("\n").splitlines()
        ]
    return lines


def _critic_view(task: TaskIR) -> str:
    """What ambiguity critic / shortcut attacker / feasibility reviewer see.

    PARITY: exactly the shipped public bundle. No more (a barrier leak) and no
    less (that rejects sound tasks)."""
    return "\n".join(
        [
            "PUBLIC SOLVER PROSE:",
            task.solver_prompt or "(no prose authored yet)",
            "",
            "PUBLIC SOURCE SCHEMAS (exactly what the solver's bundle publishes):",
            *_public_source_schema_lines(task),
            "",
            "MART OUTPUT SCHEMAS:",
            *_mart_output_lines(task),
            "",
            "POPULATIONS (names and scales only):",
            # Stated so it is never re-discovered as a defect: primary and
            # resampled SHARE a scale BY DESIGN, and the data-sensitivity gate
            # requires that.
            "  (primary and resampled share a scale by design: resampled is a "
            "fresh draw from the same distribution — the memorization check.)",
            *_population_summary(task, with_conditions=False),
        ]
    )


def _population_adversary_view(task: TaskIR) -> str:
    """Critic view plus per-population data conditions (never literal values)."""
    return "\n".join(
        [
            _critic_view(task),
            "",
            "POPULATION CONDITIONS:",
            *_population_summary(task, with_conditions=True),
        ]
    )


def render_view(role: CouncilRole, task: TaskIR) -> str:
    """THE stimulus one role is sent for one task — the single renderer.

    ONE RENDERER ON PURPOSE: `metrology.view_digest()` must hash exactly what
    the critics read, and a digest taken from a second rendering path would
    drift silently. Pure function of `(role, task)` — no paths, clock,
    environment or unordered iteration.
    """
    if role == CouncilRole.SEMANTIC_AUTHOR:
        return _author_view(task)
    if role == CouncilRole.POPULATION_ADVERSARY:
        return _population_adversary_view(task)
    return _critic_view(task)


#: Historical private alias; `render_view` is the name to use.
_view_for = render_view


# Leak detection (prose vs private SQL)

def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


#: Token width of the whitespace-independent shingles added to the literal path.
#: Line fragments alone are defeated by REFORMATTING the private SQL; shingles
#: run over the whole normalized statement, so line breaks hide nothing.
_LEAK_SHINGLE_TOKENS = 7
_LEAK_SHINGLE_STRIDE = 3
#: Longer than line fragments on purpose: a 7-token window of real SQL that also
#: clears this bound cannot be ordinary English.
_LEAK_SHINGLE_MIN_LEN = 30


#: Tokens that make a fragment SQL rather than a bare name enumeration. A
#: fragment with none of these (and no operator punctuation) is a run of
#: identifiers the PUBLIC mart spec already publishes by design.
_SQL_STRUCTURE_TOKENS = frozenset(
    """select from where join left right inner outer cross on as with group
    order by having case when then else end over partition rows between
    union intersect except distinct count sum avg min max coalesce nullif
    cast row_number first_value limit offset""".split()
)


def _carries_sql_structure(fragment: str) -> bool:
    """Does this normalized fragment contain anything beyond public names?"""
    if any(ch in fragment for ch in "()=<>*/+"):
        return True
    return any(tok in _SQL_STRUCTURE_TOKENS for tok in fragment.split())


def _sql_leak_fragments(sql: str) -> list[str]:
    """Normalized fragments of one private statement that must never appear
    in prose: its long lines PLUS whitespace-independent token shingles.
    Keyword-free identifier runs are excluded — see _SQL_STRUCTURE_TOKENS."""
    frags = {
        _normalize(line)
        for line in sql.splitlines()
        if len(_normalize(line)) >= _LEAK_FRAGMENT_MIN_LEN
    }
    tokens = _normalize(sql).split()
    for start in range(0, max(len(tokens) - _LEAK_SHINGLE_TOKENS + 1, 0),
                       _LEAK_SHINGLE_STRIDE):
        shingle = " ".join(tokens[start:start + _LEAK_SHINGLE_TOKENS])
        if len(shingle) >= _LEAK_SHINGLE_MIN_LEN:
            frags.add(shingle)
    return sorted(f for f in frags if _carries_sql_structure(f))


def _private_sql_fragments(task: TaskIR) -> dict[str, list[str]]:
    """source label -> normalized fragments that must never appear in prose."""
    private: dict[str, list[str]] = {}
    if task.reference is not None:
        for mart, sql in sorted(task.reference.sql_by_mart.items()):
            frags = _sql_leak_fragments(sql)
            if frags:
                private[f"reference:{mart}"] = frags
    for case in task.attack_cases:
        if case.mutation.startswith("directive:"):
            frags = [_normalize(case.mutation)]
        else:
            frags = _sql_leak_fragments(case.mutation)
        if frags:
            private[f"attack:{case.name}"] = frags
    return private


#: Dialect of the private reference SQL; prose spans parse with the same one.
_SQL_DIALECT = "duckdb"

#: Keyword shapes that make a prose span worth parsing as SQL. Deliberately
#: generous: precision comes from fingerprint-set membership, not span extraction.
_SQL_SPAN_RE = re.compile(
    r"\bselect\b|\bwith\s+[a-z0-9_]+\s+as\s*\(", re.IGNORECASE
)

_FENCED_BLOCK_RE = re.compile(r"```[a-z]*\s*(.*?)```", re.IGNORECASE | re.DOTALL)


#: Commutative operators whose operand order carries no meaning; sorting them
#: kills the `a.x = b.y` / `b.y = a.x` rewrites a transcriber gets for free.
_COMMUTATIVE_OPS: tuple[type, ...] = (
    sqlexp.EQ,
    sqlexp.NEQ,
    sqlexp.And,
    sqlexp.Or,
    sqlexp.Add,
    sqlexp.Mul,
)


def _anonymize_identifiers(tree: sqlexp.Expression) -> sqlexp.Expression:
    """Canonicalize a parsed statement while preserving structure and literals.

    Replace identifier spellings, remove qualifiers and aliases, normalize equivalent
    join syntax while preserving join side, and sort commutative operands. Function
    names and literals remain.
    """
    for column in tree.find_all(sqlexp.Column):
        for part in ("table", "db", "catalog"):
            if column.args.get(part) is not None:
                column.set(part, None)
    for table in tree.find_all(sqlexp.Table):
        for part in ("db", "catalog", "alias"):
            if table.args.get(part) is not None:
                table.set(part, None)
    # Deepest-first so replacing a child never invalidates an outer rewrite.
    for alias in sorted(
        tree.find_all(sqlexp.Alias), key=lambda n: n.depth, reverse=True
    ):
        if alias.this is not None and alias.parent is not None:
            alias.replace(alias.this)
    for join in tree.find_all(sqlexp.Join):
        if join.args.get("side") and join.args.get("kind"):
            join.set("kind", None)
    for ident in tree.find_all(sqlexp.Identifier):
        ident.set("this", "x")
        ident.set("quoted", False)
    for node in sorted(
        tree.find_all(*_COMMUTATIVE_OPS), key=lambda n: n.depth, reverse=True
    ):
        left, right = node.args.get("this"), node.args.get("expression")
        if left is None or right is None:
            continue
        if left.sql(dialect=_SQL_DIALECT) > right.sql(dialect=_SQL_DIALECT):
            node.set("this", right)
            node.set("expression", left)
    return tree


def _select_arg(select: sqlexp.Select, name: str):
    """Select arg lookup robust to the sqlglot arg-key rename (sqlglot >= 30
    uses 'from_' / 'with_'; older versions used 'from' / 'with')."""
    value = select.args.get(name)
    if value is None:
        value = select.args.get(name + "_")
    return value


def _fingerprintable(select: sqlexp.Select) -> bool:
    """Only structurally substantial SELECTs are fingerprinted: anonymization
    collapses trivial one-column reads to a universal shape, which would make
    the scan fire on noise instead of leaks."""
    if _select_arg(select, "from") is None:
        return False
    return bool(
        _select_arg(select, "where")
        or _select_arg(select, "group")
        or _select_arg(select, "joins")
        or _select_arg(select, "with")
        or len(select.expressions) >= 2
    )


def _parse_statements(sql: str) -> list[sqlexp.Expression]:
    """Parse `sql` under the reference dialect; [] when it is not SQL."""
    try:
        statements = sqlglot.parse(sql, read=_SQL_DIALECT)
    except sqlglot.errors.ParseError:
        return []
    except Exception:  # sqlglot tokenizer errors on pathological text
        return []
    return [s for s in statements if s is not None]


def _normalize_inlined_subqueries(statement: sqlexp.Expression) -> sqlexp.Expression:
    """Rewrite derived tables (inline subqueries in FROM/JOIN) back into CTEs.

    `WITH x AS (...) SELECT ... FROM x` and `SELECT ... FROM (...)` are
    different trees for the same query, so without this an inlined copy shares
    only part of its shapes. Best-effort: an unrewritable statement is returned
    unchanged and the caller fingerprints the raw tree too, so this can only ADD
    detections.
    """
    try:
        return eliminate_subqueries(statement)
    except Exception:  # optimizer rules assume well-formed, qualified trees
        return statement


def _shape_fingerprints(statement: sqlexp.Expression) -> set[str]:
    """Anonymized-AST fingerprints of every substantial SELECT in one tree.

    The tree is mutated in place by the anonymizer, so callers pass a copy.
    """
    _anonymize_identifiers(statement)
    return {
        sha256_hex(select.sql(dialect=_SQL_DIALECT))
        for select in statement.find_all(sqlexp.Select)
        if _fingerprintable(select)
    }


@lru_cache(maxsize=512)
def _sql_ast_fingerprints(sql: str) -> frozenset[str]:
    """Canonical anonymized-AST fingerprints of every substantial SELECT in
    `sql`, or the empty set when the text does not parse as SQL.

    Each statement is fingerprinted TWICE — as written, and after inline derived
    tables fold into CTEs — so CTE-inlining in either direction lands on a
    shared shape.
    """
    fingerprints: set[str] = set()
    for statement in _parse_statements(sql):
        fingerprints |= _shape_fingerprints(statement.copy())
        fingerprints |= _shape_fingerprints(
            _normalize_inlined_subqueries(statement.copy())
        )
    return frozenset(fingerprints)


def _prose_sql_spans(prose: str) -> list[str]:
    """Candidate SQL-like spans of the prose: fenced code blocks, plus every
    paragraph containing a SELECT/WITH...AS( shape (and its tail from the first
    such keyword, for SQL embedded mid-paragraph)."""
    spans: list[str] = []
    for match in _FENCED_BLOCK_RE.finditer(prose):
        spans.append(match.group(1))
    for paragraph in re.split(r"\n\s*\n", prose):
        keyword = _SQL_SPAN_RE.search(paragraph)
        if keyword is None:
            continue
        spans.append(paragraph)
        if keyword.start() > 0:
            spans.append(paragraph[keyword.start():])
    return spans


def _private_ast_fingerprints(task: TaskIR) -> dict[str, frozenset[str]]:
    """source label -> anonymized AST fingerprints of the private SQL."""
    private: dict[str, frozenset[str]] = {}
    if task.reference is not None:
        for mart, sql in sorted(task.reference.sql_by_mart.items()):
            fps = _sql_ast_fingerprints(sql)
            if fps:
                private[f"reference:{mart}"] = fps
    for case in task.attack_cases:
        if case.mutation.startswith("directive:"):
            continue
        fps = _sql_ast_fingerprints(case.mutation)
        if fps:
            private[f"attack:{case.name}"] = fps
    return private


# Back shape signatures with exact equality of base tables, source columns,
# functions, and literals plus a non-triviality floor; this survives restructuring.

#: Non-triviality floor for a signature to be usable as leak evidence.
_SIGNATURE_MIN_TABLES = 2
_SIGNATURE_MIN_FUNCTIONS = 2
_SIGNATURE_MIN_COLUMNS = 2


def _locally_bound_names(statement: sqlexp.Expression) -> set[str]:
    """Lowercase names the statement itself INTRODUCES: CTE names, table
    aliases, projection aliases.

    An inlining rewrite is free to create or destroy these, so the signature
    excludes them on both sides.
    """
    bound: set[str] = set()
    for cte in statement.find_all(sqlexp.CTE):
        if cte.alias_or_name:
            bound.add(cte.alias_or_name.lower())
    for table_alias in statement.find_all(sqlexp.TableAlias):
        if table_alias.name:
            bound.add(table_alias.name.lower())
    for alias in statement.find_all(sqlexp.Alias):
        if alias.alias:
            bound.add(alias.alias.lower())
    return bound


def _statement_semantic_signature(statement: sqlexp.Expression) -> str | None:
    """Inlining-invariant signature of one parsed statement, or None when the
    statement is not a structurally substantial query."""
    if not any(_fingerprintable(s) for s in statement.find_all(sqlexp.Select)):
        return None
    bound = _locally_bound_names(statement)
    tables = {
        t.name.lower()
        for t in statement.find_all(sqlexp.Table)
        if t.name and t.name.lower() not in bound
    }
    columns = {
        c.name.lower() for c in statement.find_all(sqlexp.Column) if c.name
    } - bound
    functions: set[str] = set()
    for func in statement.find_all(sqlexp.Func):
        name = func.sql_name()
        if isinstance(func.args.get("this"), sqlexp.Distinct) or func.args.get(
            "distinct"
        ):
            name += " DISTINCT"
        functions.add(name)
    literals = {
        lit.sql(dialect=_SQL_DIALECT) for lit in statement.find_all(sqlexp.Literal)
    }
    if (
        len(tables) < _SIGNATURE_MIN_TABLES
        or len(functions) < _SIGNATURE_MIN_FUNCTIONS
        or len(columns) < _SIGNATURE_MIN_COLUMNS
    ):
        return None
    payload = "|".join(
        part + "=" + ",".join(sorted(values))
        for part, values in (
            ("tables", tables),
            ("columns", columns),
            ("functions", functions),
            ("literals", literals),
        )
    )
    return sha256_hex(payload)


@lru_cache(maxsize=512)
def _sql_semantic_signatures(sql: str) -> frozenset[str]:
    """Inlining-invariant signatures of every substantial statement in `sql`."""
    signatures = {
        _statement_semantic_signature(statement)
        for statement in _parse_statements(sql)
    }
    return frozenset(s for s in signatures if s is not None)


def _private_semantic_signatures(task: TaskIR) -> dict[str, frozenset[str]]:
    """source label -> inlining-invariant signatures of the private SQL."""
    private: dict[str, frozenset[str]] = {}
    if task.reference is not None:
        for mart, sql in sorted(task.reference.sql_by_mart.items()):
            sigs = _sql_semantic_signatures(sql)
            if sigs:
                private[f"reference:{mart}"] = sigs
    for case in task.attack_cases:
        if case.mutation.startswith("directive:"):
            continue
        sigs = _sql_semantic_signatures(case.mutation)
        if sigs:
            private[f"attack:{case.name}"] = sigs
    return private


def semantic_leak_findings_by_source(task: TaskIR) -> list[tuple[str, Finding]]:
    """(source label, FATAL finding) for prose SQL whose source-access signature
    is IDENTICAL to private SQL's even though no query shape matched — the
    restructured-copy residual."""
    prose = task.solver_prompt
    if not prose.strip():
        return []
    private = _private_semantic_signatures(task)
    if not private:
        return []
    span_sigs: set[str] = set()
    for span in _prose_sql_spans(prose):
        span_sigs |= _sql_semantic_signatures(span)
    if not span_sigs:
        return []
    findings: list[tuple[str, Finding]] = []
    for source, sigs in sorted(private.items()):
        hits = sorted(sigs & span_sigs)
        if not hits:
            continue
        findings.append((
            source,
            Finding(
                finding_id=f"leak-sem-{sha256_hex(source + '|' + hits[0])[:8]}",
                role=CouncilRole.SHORTCUT_ATTACKER,
                provenance=FindingProvenance.CODE,
                severity=Severity.FATAL,
                summary=(
                    f"Solver prose contains SQL that reads exactly what private "
                    f"SQL from {source} reads (identical source-access "
                    "signature): a restructured copy, not an independent query."
                ),
                detail=(
                    "Inlining-invariant signature match "
                    f"({hits[0][:12]}): same base tables, same source columns, "
                    "same function set and same literals as the private SQL, "
                    "with no shape in common. Rewriting a CTE as a subquery, "
                    "DISTINCT as GROUP BY, or a grouped CTE as a correlated "
                    "scalar subquery changes the shape but not what the query "
                    "reads."
                ),
                route_hint=RepairRoute.SPECIFICATION,
                suggested_attack=None,
            ),
        ))
    return findings


def semantic_leak_findings(task: TaskIR) -> list[Finding]:
    """Semantic-signature findings only (see semantic_leak_findings_by_source)."""
    return [finding for _, finding in semantic_leak_findings_by_source(task)]


@lru_cache(maxsize=512)
def _sql_parses(sql: str) -> bool:
    """Does sqlglot understand this statement under the reference dialect?"""
    try:
        return any(s is not None for s in sqlglot.parse(sql, read=_SQL_DIALECT))
    except Exception:  # ParseError, TokenError, anything the tokenizer raises
        return False


def _unscannable_private_sources(task: TaskIR) -> list[str]:
    """Private SQL sources the AST detector CANNOT see (they do not parse).

    A parse failure yields an empty fingerprint set, so an unparseable reference
    silently disables the AST half of the scan — indistinguishable from a clean
    pass. Naming those sources is what lets `leak_findings` refuse instead.
    """
    blind: list[str] = []
    sources: list[tuple[str, str]] = []
    if task.reference is not None:
        sources += [
            (f"reference:{mart}", sql)
            for mart, sql in sorted(task.reference.sql_by_mart.items())
        ]
    sources += [
        (f"attack:{case.name}", case.mutation)
        for case in task.attack_cases
        if not case.mutation.startswith("directive:")
    ]
    for label, sql in sources:
        if not sql.strip():
            continue
        if "select" not in _normalize(sql):
            continue  # not a query: the literal path is the whole contract
        if not _sql_parses(sql):
            blind.append(label)
    return blind


def leak_scan_coverage_findings(task: TaskIR) -> list[Finding]:
    """FATAL findings when the AST leak detector is BLIND to a private source
    while the prose does contain SQL-shaped spans to compare against.

    FAIL CLOSED ON THE DETECTOR, not just its output: if the factory cannot
    parse its own private SQL, "the scan found nothing" is a statement about the
    scan, not the prose. Prose with no SQL-shaped span is deliberately exempt.
    """
    prose = task.solver_prompt
    if not prose.strip():
        return []
    blind = _unscannable_private_sources(task)
    if not blind:
        return []
    if not _prose_sql_spans(prose):
        return []
    return [
        Finding(
            finding_id=f"leak-blind-{sha256_hex(source)[:8]}",
            role=CouncilRole.SHORTCUT_ATTACKER,
            provenance=FindingProvenance.CODE,
            severity=Severity.FATAL,
            summary=(
                f"Leak scan cannot certify the prose: private SQL {source} "
                "does not parse under the reference dialect, so the AST "
                "detector is blind to it while the prose contains SQL."
            ),
            detail=(
                "The anonymized-AST path silently returns no fingerprints for "
                "unparseable SQL. Prose carrying SQL-shaped spans therefore "
                "cannot be cleared against this source. Fix the private SQL "
                "so it parses, or remove the SQL from the prose."
            ),
            route_hint=RepairRoute.REFERENCE,
            suggested_attack=None,
        )
        for source in blind
    ]


def ast_leak_findings_by_source(task: TaskIR) -> list[tuple[str, Finding]]:
    """(source label, FATAL finding) for every prose SQL span whose canonical,
    identifier-anonymized AST matches private reference/attack SQL.

    Catches what literal containment misses: paraphrased copies with renamed
    aliases, reflowed whitespace, changed keyword case.
    """
    prose = task.solver_prompt
    if not prose.strip():
        return []
    private = _private_ast_fingerprints(task)
    if not private:
        return []
    span_fps: set[str] = set()
    for span in _prose_sql_spans(prose):
        span_fps |= _sql_ast_fingerprints(span)
    if not span_fps:
        return []
    findings: list[tuple[str, Finding]] = []
    for source, fps in sorted(private.items()):
        hits = sorted(fps & span_fps)
        if not hits:
            continue
        findings.append((
            source,
            Finding(
                finding_id=f"leak-ast-{sha256_hex(source + '|' + hits[0])[:8]}",
                role=CouncilRole.SHORTCUT_ATTACKER,
                provenance=FindingProvenance.CODE,
                severity=Severity.FATAL,
                summary=(
                    f"Solver prose contains SQL that is a disguised copy of "
                    f"private SQL from {source} (canonical anonymized AST "
                    "match): the task is solvable by transcription."
                ),
                detail=(
                    "Anonymized-AST fingerprint match "
                    f"({len(hits)} shared shape(s): "
                    + ", ".join(h[:12] for h in hits[:3])
                    + "). Alias/CTE/column renames and reformatting do not "
                    "hide a copied query."
                ),
                route_hint=RepairRoute.SPECIFICATION,
                suggested_attack=None,
            ),
        ))
    return findings


def ast_leak_findings(task: TaskIR) -> list[Finding]:
    """AST-path findings only (see ast_leak_findings_by_source)."""
    return [finding for _, finding in ast_leak_findings_by_source(task)]


def leak_findings(task: TaskIR) -> list[Finding]:
    """FATAL findings for private SQL present in the prose.

    A prose leak hands the solver the answer key. Three fail-closed, code-only
    detectors, each source reported once by the strongest that fired: literal
    fragment containment; anonymized AST fingerprints (catching paraphrases and
    CTE-inlining either direction); and the semantic signature — base tables,
    source columns, functions and literals — which catches copies sharing no
    query SHAPE at all.
    """
    prose = _normalize(task.solver_prompt)
    if not prose:
        return []
    findings: list[Finding] = []
    literal_sources: set[str] = set()
    for source, frags in sorted(_private_sql_fragments(task).items()):
        hits = sorted({f for f in frags if f in prose})
        if not hits:
            continue
        literal_sources.add(source)
        findings.append(
            Finding(
                finding_id=f"leak-{sha256_hex(source + '|' + hits[0])[:8]}",
                role=CouncilRole.SHORTCUT_ATTACKER,
                provenance=FindingProvenance.CODE,
                severity=Severity.FATAL,
                summary=(
                    f"Solver prose leaks private SQL from {source}: the task "
                    "is solvable by transcription, not by reasoning."
                ),
                detail="Leaked fragment(s): " + "; ".join(repr(h) for h in hits[:3]),
                route_hint=RepairRoute.SPECIFICATION,
                suggested_attack=None,
            )
        )
    reported = set(literal_sources)
    for source, finding in ast_leak_findings_by_source(task):
        if source not in reported:
            reported.add(source)
            findings.append(finding)
    for source, finding in semantic_leak_findings_by_source(task):
        if source not in reported:
            reported.add(source)
            findings.append(finding)
    findings.extend(leak_scan_coverage_findings(task))
    return findings


# Provider-output parsing (findings only; no acceptance vocabulary exists)

def _parse_findings(role: CouncilRole, raw: str, id_suffix: str) -> list[Finding]:
    """Reduce a provider response to Finding objects.

    Only Finding fields survive, so acceptance-flavored keys have nowhere to
    land. Unusable output RAISES ProviderProtocolError rather than proceeding on
    a synthetic finding. A malformed or PARTIAL `proposed_case` is a schema
    violation that fails the stage — never dropped so the finding can proceed
    unproposed. The proposal certifies nothing; attacks.py executes it.
    """

    def protocol_error(reason: str) -> ProviderProtocolError:
        return ProviderProtocolError(
            f"provider output for role {role.value} violated the council "
            f"protocol: {reason}; first 500 chars: {raw[:500]!r}"
        )

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise protocol_error("not valid JSON") from None
    # Independently enforce the same closed response contract at the consumer.
    # RoutedProvider already checks it at the transport boundary, but council
    # also accepts the Provider protocol and replayed text directly; neither is
    # allowed to bypass required fields or smuggle acceptance/private metadata
    # through keys the JSON Schema forbids.
    from elt_taskgen.review.providers import _normalized_proposal, validate_payload_for

    wire_data = dict(data) if isinstance(data, dict) else data
    if isinstance(wire_data, dict) and "role" in wire_data:
        envelope_role = wire_data.pop("role")
        if envelope_role != role.value:
            raise protocol_error(
                f"normalized envelope role {envelope_role!r} does not match "
                f"requested role {role.value!r}"
            )
    problem = validate_payload_for(role.value, wire_data)
    if problem is not None:
        raise protocol_error(problem)

    findings: list[Finding] = []
    for i, item in enumerate(wire_data["findings"]):
        if not isinstance(item, dict):
            raise protocol_error(f"finding #{i} is not an object")
        if role is not CouncilRole.SEMANTIC_AUTHOR and "proposed_case" not in item:
            raise protocol_error(
                f"finding #{i} is missing required proposed_case; send null "
                "for a non-actionable finding or a complete executable case"
            )
        try:
            route_hint = item.get("route_hint")
            suggested = item.get("suggested_attack")
            proposed = item.get("proposed_case")
            parsed_proposal = (
                ProposedAttackCase.model_validate(_normalized_proposal(item))
                if proposed is not None
                else None
            )
            parsed_suggested = AttackKind(suggested) if suggested else None
            if (
                parsed_proposal is not None
                and parsed_suggested is not None
                and parsed_proposal.kind is not parsed_suggested
            ):
                raise ValueError(
                    f"proposed_case.kind {parsed_proposal.kind.value!r} does not "
                    f"match suggested_attack {parsed_suggested.value!r}"
                )
            findings.append(
                Finding(
                    finding_id=f"{role.value}-{i:02d}-{id_suffix}",
                    role=role,  # the requested role, never the provider's claim
                    # Stamped by the PARSER, so no provider field can alter it.
                    # `severity` below is the model's claim; this is the fact.
                    provenance=FindingProvenance.PROVIDER,
                    severity=Severity(item.get("severity", Severity.INFO.value)),
                    summary=str(item.get("summary", "")),
                    detail=str(item.get("detail", "")),
                    route_hint=RepairRoute(route_hint) if route_hint else None,
                    suggested_attack=parsed_suggested,
                    proposed_case=parsed_proposal,
                )
            )
        except (ValueError, TypeError) as exc:
            raise protocol_error(f"finding #{i} invalid: {exc}") from exc
    return findings


# Screen evidence strength without judging substantive truth: nullifying signals
# always void, while weak signals void only without an executable consequence.
# Preserve finding text and attach `FindingScreen` with signals and withheld content.

#: Whole-DETAIL values that assert nothing (compared after normalization).
_NULL_DETAIL_SENTINELS: frozenset[str] = frozenset({
    "", "n/a", "n.a", "n.a.", "na", "none", "null", "nil", "-", "--", "...",
    "tbd", "todo", "to be determined", "see above", "see below",
    "see summary", "as above", "as stated above", "same as above",
    "no detail", "no details", "not applicable",
})

#: Whole-field, self-identifying non-content.  These are grammars rather than
#: substrings: a fatal report ABOUT a placeholder is substantive evidence, not
#: itself placeholder text.
_PLACEHOLDER_CLAUSES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:this\s+is\s+(?:only\s+)?a\s+)?placeholder"
        r"(?:\s*[-—–:;]\s*(?:please\s+)?see\s+(?:the\s+)?"
        r"(?:specific\s+)?findings?\s+(?:below|above))?"
    ),
    re.compile(r"(?:please\s+)?see\s+(?:the\s+)?(?:specific\s+)?findings?\s+(?:below|above)"),
    re.compile(r"example\s+finding"),
    re.compile(r"fill\s+(?:this|it)\s+in(?:\s+later)?"),
    re.compile(r"lorem\s+ipsum"),
)

#: Whole terminal-clause grammars for withdrawing the finding's own claim.
#: Quoted markers do not match; only the final sentence or list item is checked.
_WITHDRAWAL_NOUN = r"(?:finding|claim|defect|issue|concern|ambiguity|problem)"
_GRADED_FORK_WITHDRAWAL = (
    r"(?:(?:[^.!?;\r\n]{1,256},\s*)?"
    r"(?:so|thus|therefore|accordingly)\s+)?"
    r"(?:i|we)\s+(?:hereby\s+)?(?:retract|withdraw)\s+"
    r"(?:this|it)\s+as\s+a\s+graded\s+fork"
)
_TERMINAL_WITHDRAWAL_CLAUSES: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?:this|the)\s+{_WITHDRAWAL_NOUN}\s+"
        r"(?:is|was|has been)\s+(?:hereby\s+)?(?:withdrawn|retracted)"
        r"(?:\s+(?:upon|after)\s+[a-z0-9][a-z0-9 _-]{0,80})?"
    ),
    re.compile(
        rf"(?:i|we)\s+(?:hereby\s+)?(?:retract|withdraw)"
        rf"(?:\s+(?:this|the|my|our)\s+{_WITHDRAWAL_NOUN})?"
    ),
    re.compile(
        rf"(?:(?:i|we)\s+(?:am|are)\s+)?(?:retracting|withdrawing)"
        rf"(?:\s+(?:this|the|my|our)(?:\s+{_WITHDRAWAL_NOUN})?)?"
    ),
    # Require an active withdrawal and no-defect conclusion in the same clause.
    re.compile(
        r"(?:(?:i|we)\s+(?:am|are)\s+)?withdrawing\s+this\s+as\s+a\s+fork"
        r"\s*[-—–:]\s*no\s+defect\s+is\s+claimed"
    ),
    # Match only a terminal first-person graded-fork withdrawal.
    re.compile(_GRADED_FORK_WITHDRAWAL),
    re.compile(
        r"(?:(?:on reflection|upon re-examination|after (?:review|verification)|"
        r"therefore|thus|so|accordingly|in conclusion)\s*[:,]?\s*)?"
        r"(?:(?:there is|i find|we find)\s+)?"
        rf"(?:no\s+{_WITHDRAWAL_NOUN}(?:\s+here)?|"
        rf"not\s+(?:a|an)\s+{_WITHDRAWAL_NOUN}|"
        rf"(?:this|it)\s+is\s+not\s+(?:a|an)\s+{_WITHDRAWAL_NOUN})"
        r"(?:\s*,\s*(?:(?:i|we)\s+(?:am|are)\s+)?"
        r"(?:retracting|withdrawing)"
        rf"(?:\s+(?:this|the)\s+{_WITHDRAWAL_NOUN})?)?"
    ),
    re.compile(rf"no\s+{_WITHDRAWAL_NOUN}\s+(?:is\s+)?filed"),
    re.compile(r"(?:there is\s+)?no\s+(?:action|change)\s+needed(?:\s+here)?"),
    re.compile(r"(?:this\s+(?:is|was)\s+)?(?:a\s+)?false alarm"),
    re.compile(r"disregard\s+(?:this|the)\s+finding"),
    re.compile(
        r"(?:[^,]{1,256},\s*)?"
        r"(?:(?:so|thus|therefore|accordingly)\s+)?"
        r"(?:this\s+(?:is|was)\s+(?:(?:reported|included|provided|offered)\s+as\s+)?)?"
        r"context(?:\s+for\s+coverage)?\s+only"
    ),
    re.compile(r"this\s+is\s+acceptable\s+as\s+specified"),
    re.compile(
        r"(?:(?:i|we)\s+(?:am|are)\s+)?filing\s+nothing\s+further"
        r"(?:\s+(?:on|for|about)\s+[a-z0-9_ -]{1,96})?"
    ),
)

#: Flag only an explicit mismatch between a global empty-param executable and
#: the critic's own target-specific prediction; never infer scope from mart names.
_GLOBAL_PROPOSAL_SCOPE_RE = re.compile(
    r"\bif\b[^.;]{0,96}\b(?:mutant|mutation|attack)\b[^.;]{0,64}"
    r"\bapplied globally\b"
)
_SPECIFIC_PROPOSAL_SCOPE_RE = re.compile(
    r"\b(?:this|the)\s+(?:prediction|proposal|case)\s+is\b[^.;]{0,192}"
    r"\bspecifically\b"
)

#: The populations whose reward can establish or refute a population-blindness
#: claim. Development is solver-visible debug data and the population-adversary
#: contract explicitly excludes it as a discriminator.
_HIDDEN_GRADED_POPULATIONS: frozenset[PopulationName] = frozenset({
    PopulationName.PRIMARY,
    PopulationName.RESAMPLED,
    PopulationName.COUNTERFACTUAL,
    PopulationName.STRESS,
})

#: NULLIFYING signals: the finding asserts it is not a finding. These void on
#: their own — it is the proposer's own statement that there is nothing here.
NULLIFYING_SIGNALS: tuple[str, ...] = (
    "predicted_graded_discriminator",
    "self_retracted",
    "placeholder_text",
)

#: EVIDENCE-ABSENCE signals: the finding may be real but supports nothing in
#: writing. These void ONLY when it also proposes no executable consequence — a
#: terse finding that compiles to a mutant has the mutant as its evidence.
WEAK_EVIDENCE_SIGNALS: tuple[str, ...] = ("empty_detail", "ungrounded_detail")

#: The whole catalogue, DERIVED from the two tiers rather than restated, so a
#: signal can never exist in one place and not the other.
SCREEN_SIGNALS: tuple[str, ...] = tuple(
    sorted(WEAK_EVIDENCE_SIGNALS + NULLIFYING_SIGNALS)
)

#: Minimum task-token length admitted to the grounding vocabulary; short
#: fragments ("id", "no") would ground any English sentence.
_GROUNDING_MIN_TOKEN = 4

#: Name prefix a PROMOTED proposal's case gets. Mirrors
#: attacks.PROPOSAL_CASE_PREFIX and is asserted equal to it by the tests.
PROPOSAL_CASE_NAME_PREFIX = "proposed__"

_WORD_RE = re.compile(r"[a-z0-9_]+")
_RULE_REF_RE = re.compile(r"\brule\s*#?\s*\d+\b")
_CLOSING_BOUNDARY_RE = re.compile(r"(?:\r?\n)+|(?<=[.!?;])(?:[ \t]+|$)")
_LIST_PREFIX_RE = re.compile(r"^\s*(?:(?:[-*+•‣▪])|(?:\d{1,3}[.)]))\s+")


def _norm_text(text: str) -> str:
    """Lowercase, whitespace-collapsed form used by every screen comparison."""
    return " ".join(text.lower().split())


def _closing_span(text: str) -> str:
    """The finding's last sentence OR line/list item, normalized.

    Newlines must be boundaries before whitespace is collapsed: otherwise an
    unpunctuated bullet quoting "no defect" in the middle of a real fatal turns
    the whole body into one apparent closing span.  A retraction is a conclusion;
    matching only the final structural clause keeps quoted/negated language in
    earlier evidence from being screened.
    """
    raw = text.strip()
    if not raw:
        return ""
    parts: list[str] = []
    for raw_part in _CLOSING_BOUNDARY_RE.split(raw):
        part = _LIST_PREFIX_RE.sub("", raw_part.strip())
        normalized = _norm_text(part)
        if normalized:
            parts.append(normalized)
    return parts[-1] if parts else ""


def _terminal_withdrawal_clause(text: str) -> str | None:
    """Return a genuine terminal withdrawal clause, never a marker hit.

    The terminal punctuation is irrelevant to the grammar, but quotes and
    arbitrary leading/trailing assertions are deliberately retained: neither
    ``the author wrote 'no defect'`` nor ``no defect, but the leak stands`` is
    itself a withdrawal.
    """
    # A final parenthetical that merely records the already-made filing
    # classification must not hide the preceding withdrawal.  Do not skip
    # arbitrary asides: only the exact metadata shape emitted by the critic
    # contract is ignored, and the preceding clause must independently match
    # the closed withdrawal grammar above.
    raw = text.strip()
    without_aside = re.sub(
        r"\s*\(filed\s+at\s+info\s+per\s+instruction\s+not\s+to\s+"
        r"fabricate(?:;\s*see\s+other\s+findings)?\.?\)\s*$",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    candidates = (raw,) if without_aside == raw else (raw, without_aside)
    for candidate in candidates:
        closing = _closing_span(candidate)
        clause = closing.rstrip(" .!?;").strip()
        if clause and any(
            pattern.fullmatch(clause)
            for pattern in _TERMINAL_WITHDRAWAL_CLAUSES
        ):
            return closing
    return None


def _placeholder_clause(text: str) -> str | None:
    """Return self-identifying placeholder text only when it fills a field.

    Keeping the match field-wide is the safety property: ``this is a
    placeholder`` remains junk, while ``customer_summary contains a
    placeholder instead of a total_spend rule`` remains a defect report.
    """
    normalized = _norm_text(text)
    clause = normalized.rstrip(" .!?;").strip()
    if not clause:
        return None
    if any(pattern.fullmatch(clause) for pattern in _PLACEHOLDER_CLAUSES):
        return normalized
    return None


def task_vocabulary(task: TaskIR) -> frozenset[str]:
    """Every token a finding could name to prove it is talking about THIS task.

    Table, column, mart, key-column, population and backend names, each whole
    and split on '_'. Tokens shorter than `_GROUNDING_MIN_TOKEN` are dropped.
    """
    raw: set[str] = set()
    for table in task.tables:
        raw.add(table.name)
        raw.update(c.name for c in table.columns)
    for backend in task.backends:
        raw.add(backend.backend.value)
    for mart in task.marts:
        raw.add(mart.name)
        raw.update(c.name for c in mart.columns)
        raw.update(mart.key_columns)
    for pop in task.populations:
        raw.add(pop.name.value)
    tokens: set[str] = set()
    for name in raw:
        lowered = name.strip().lower()
        for piece in [lowered, *lowered.split("_")]:
            if len(piece) >= _GROUNDING_MIN_TOKEN:
                tokens.add(piece)
    return frozenset(tokens)


def _grounding_hits(text: str, vocabulary: frozenset[str]) -> list[str]:
    """Task tokens (and 'rule N' references) this text actually names."""
    lowered = text.lower()
    hits = {w for w in _WORD_RE.findall(lowered) if w in vocabulary}
    hits.update(_RULE_REF_RE.findall(lowered))
    return sorted(hits)


def _screen_signals(
    finding: Finding, vocabulary: frozenset[str], *, multi_mart: bool = False
) -> tuple[list[str], str]:
    """Deterministic (signals, evidence) for one finding. Pure."""
    signals: list[str] = []
    evidence: list[str] = []

    detail_norm = _norm_text(finding.detail).strip(" .")
    if detail_norm in _NULL_DETAIL_SENTINELS:
        signals.append("empty_detail")
        evidence.append(f"detail={finding.detail.strip()!r}")
    elif not _grounding_hits(finding.detail, vocabulary):
        signals.append("ungrounded_detail")
        evidence.append(
            "detail names no table/column/mart/population/backend/rule of "
            f"this task: {finding.detail.strip()[:160]!r}"
        )

    # A placeholder FIELD is not enough to erase proof in the other field.
    # `placeholder_text` is nullifying only when the whole finding consists of
    # explicit whole-field placeholders/null sentinels. An unfinished-looking
    # substantive summary is not safe to erase; ordinary weak-evidence policy
    # can still void a non-fatal, proposal-free slot whose detail is junk.
    summary_placeholder = _placeholder_clause(finding.summary)
    detail_placeholder = _placeholder_clause(finding.detail)
    summary_norm = _norm_text(finding.summary)
    summary_is_stub = (
        summary_placeholder is not None
        or summary_norm.strip(" .") in _NULL_DETAIL_SENTINELS
    )
    detail_is_stub = (
        detail_placeholder is not None
        or detail_norm in _NULL_DETAIL_SENTINELS
    )
    if (
        (summary_placeholder is not None or detail_placeholder is not None)
        and summary_is_stub
        and detail_is_stub
    ):
        signals.append("placeholder_text")
        evidence.append(
            "whole finding is a self-identifying placeholder scaffold: "
            f"summary={finding.summary.strip()[:80]!r}, "
            f"detail={finding.detail.strip()[:80]!r}"
        )

    for field in (finding.summary, finding.detail):
        closing = _terminal_withdrawal_clause(field)
        if closing is not None:
            signals.append("self_retracted")
            evidence.append(f"terminal clause retracts: {closing[-160:]!r}")
            break

    proposal = finding.proposed_case
    if (
        finding.provenance is FindingProvenance.PROVIDER
        and finding.role is CouncilRole.POPULATION_ADVERSARY
        and finding.severity is Severity.MAJOR
        and proposal is not None
    ):
        predicted_catches = sorted(
            population.value
            for population in _HIDDEN_GRADED_POPULATIONS
            if proposal.expected_pass.get(population) is False
        )
        if predicted_catches:
            # `expected_pass=False` is the critic's claim that this population
            # catches its case; one such discriminator refutes a MAJOR no-population
            # claim. Unscopable mart-local concerns must omit the proposal.
            signals.append("predicted_graded_discriminator")
            evidence.append(
                "population-adversary major proposed_case predicts the exact "
                "wrong logic loses FULL reward on hidden graded population(s): "
                + ", ".join(predicted_catches)
            )

    if multi_mart and proposal is not None and not proposal.params:
        claim = _norm_text(
            f"{finding.summary} {finding.detail} {proposal.rationale}"
        )
        global_scope = _GLOBAL_PROPOSAL_SCOPE_RE.search(claim)
        specific_scope = _SPECIFIC_PROPOSAL_SCOPE_RE.search(claim)
        if (
            global_scope is not None
            and specific_scope is not None
            and global_scope.end() <= specific_scope.start()
            and specific_scope.start() - global_scope.end() <= 256
        ):
            signals.append("self_retracted")
            evidence.append(
                "proposal retracts its own executable scope: "
                f"{claim[max(0, global_scope.start() - 24):specific_scope.end()][:320]!r}"
            )

    return sorted(set(signals)), "; ".join(evidence)


def _void(finding: Finding, signals: list[str], evidence: str) -> Finding:
    """Demote a self-nullifying finding WITHOUT deleting or rewording it.

    Severity drops to INFO because every consumer already reads that field; the
    claimed severity and executable content move into the screen record.
    """
    return finding.model_copy(
        update={
            "severity": Severity.INFO,
            "suggested_attack": None,
            "proposed_case": None,
            "screen": FindingScreen(
                status=FindingScreenStatus.VOID,
                signals=tuple(signals),
                evidence=evidence,
                claimed_severity=finding.severity,
                withheld_attack=finding.suggested_attack,
                withheld_proposal=finding.proposed_case,
            ),
        }
    )


def _note(finding: Finding, signals: list[str], evidence: str) -> Finding:
    """Record a weak-evidence signal WITHOUT changing anything else.

    Severity and executable content are untouched; only the record is added.
    """
    return finding.model_copy(
        update={
            "screen": FindingScreen(
                status=FindingScreenStatus.NOTED,
                signals=tuple(signals),
                evidence=evidence,
                claimed_severity=finding.severity,
            )
        }
    )


def _compiled_mutant_key(task: TaskIR, finding: Finding) -> str | None:
    """The executable consequence of a finding, as an identity string.

    Computed by running the REAL compiler, never by reimplementing its directive
    rules: two findings are duplicates exactly when the compiler emits the same
    mutant. None when the finding compiles to nothing.
    """
    from elt_taskgen.verification import attacks as attacks_mod

    bare = task.model_copy(update={"attack_cases": ()})
    # `compile_attacks` intentionally does not also emit a lossy probe for a
    # structured proposal.  The screen still needs the finding's *probe-side*
    # identity so it can prove that an independent echo is covered by the
    # promoter.  Strip only the proposal for this identity calculation; the
    # proposal itself remains untouched in the returned finding.
    probe = finding.model_copy(update={"proposed_case": None})
    cases = attacks_mod.compile_attacks(bare, [probe])
    if not cases:
        return None
    case = cases[0]
    marts = ",".join(sorted(m.name for m in task.marts))
    return f"{case.kind.value}|{case.mutation}|{marts}"


def _proposal_mutant_key(task: TaskIR, finding: Finding) -> str | None:
    """The same identity for a finding's PROPOSED case (promoter side).

    When a finding both suggests an attack and proposes a case, the proposal
    path is stronger (a falsifiable five-population prediction), so the probe
    side yields. Degrades to None rather than guessing.
    """
    proposal = finding.proposed_case
    if proposal is None:
        return None
    from elt_taskgen.verification import attacks as attacks_mod

    try:
        mutation = attacks_mod._proposal_mutation(finding, proposal)
    except Exception:  # uncompilable: promote_proposed_cases records the reason
        return None
    marts = ",".join(sorted(m.name for m in task.marts))
    return f"{proposal.kind.value}|{mutation}|{marts}"


def screen_findings(task: TaskIR, findings: list[Finding]) -> list[Finding]:
    """Screen one council run without changing finding count or order.

    Self-nullifying provider findings are voided, then survivors sharing a compiled
    mutant are marked duplicates. Code-origin leak findings bypass screening. Fatal
    provider findings are voided only by an explicit nullifying signal.
    """
    vocabulary = task_vocabulary(task)
    screened: list[Finding] = []
    for finding in findings:
        if finding.provenance is FindingProvenance.CODE:
            screened.append(finding)
            continue
        signals, evidence = _screen_signals(
            finding, vocabulary, multi_mart=len(task.marts) > 1
        )
        if not signals:
            screened.append(finding)
            continue
        nullifying = [s for s in signals if s in NULLIFYING_SIGNALS]
        executable = (
            finding.suggested_attack is not None or finding.proposed_case is not None
        )
        if finding.severity is Severity.FATAL:
            # Only self-nullification can take down a rejection reason.
            screened.append(
                _void(finding, signals, evidence)
                if nullifying
                else _note(finding, signals, evidence)
            )
        elif nullifying or not executable:
            screened.append(_void(finding, signals, evidence))
        else:
            screened.append(_note(finding, signals, evidence))

    order = sorted(range(len(screened)), key=lambda i: screened[i].finding_id)

    # Which mutants the PROMOTER will execute anyway (proposal side), and for
    # which finding. First finding_id wins — deterministic.
    promoter_covers: dict[str, str] = {}
    for i in order:
        prop_key = _proposal_mutant_key(task, screened[i])
        if prop_key is not None:
            promoter_covers.setdefault(prop_key, screened[i].finding_id)

    # Which mutants the PROBE side would compile, and from which findings.
    groups: dict[str, list[int]] = {}
    for i in order:
        key = _compiled_mutant_key(task, screened[i])
        if key is not None:
            groups.setdefault(key, []).append(i)

    out = list(screened)

    def _duplicate(i: int, key: str, target: str) -> None:
        prior = screened[i].screen  # a NOTED record must not be overwritten
        out[i] = screened[i].model_copy(
            update={
                "suggested_attack": None,
                "screen": FindingScreen(
                    status=FindingScreenStatus.DUPLICATE,
                    signals=prior.signals if prior else (),
                    duplicate_of=target,
                    mutant_key=key,
                    claimed_severity=screened[i].severity,
                    withheld_attack=screened[i].suggested_attack,
                    evidence="; ".join(
                        filter(
                            None,
                            [
                                prior.evidence if prior else "",
                                f"compiles to the mutant already executed for {target}",
                            ],
                        )
                    ),
                ),
            }
        )

    for key, members in groups.items():
        covered_by = promoter_covers.get(key)
        if covered_by is not None:
            # The promoter measures this exact mutant against a full
            # five-population prediction; a probe of it proves strictly less.
            target = f"{PROPOSAL_CASE_NAME_PREFIX}{covered_by}"
            for i in members:
                _duplicate(i, key, target)
            continue
        if len(members) == 1:
            continue
        rep, others = members[0], members[1:]
        for i in others:
            _duplicate(i, key, screened[rep].finding_id)
        out[rep] = screened[rep].model_copy(
            update={
                "screen": FindingScreen(
                    status=FindingScreenStatus.REPRESENTATIVE,
                    coalesced_from=tuple(screened[i].finding_id for i in others),
                    mutant_key=key,
                    evidence=(
                        f"{len(members)} findings compile to this one mutant; "
                        "executed once"
                    ),
                )
            }
        )
    return _keep_shortcut_diligence(screened, out)


def is_executable_probe(finding: Finding) -> bool:
    """Return whether a finding counts as an executable shortcut probe.

    The finding must be above `INFO`, name a `suggested_attack`, and include a
    `proposed_case` of the same kind. Compilation is checked separately.
    """
    return (
        finding.severity is not Severity.INFO
        and finding.suggested_attack is not None
        and finding.proposed_case is not None
        and finding.proposed_case.kind is finding.suggested_attack
    )


def _keep_shortcut_diligence(
    screened: list[Finding], out: list[Finding]
) -> list[Finding]:
    """Preserve shortcut diligence through deduplication.

    If the input contained an executable probe, restore one after screening if
    deduplication removed them all.
    """
    from elt_taskgen.models import CouncilRole

    probe = is_executable_probe

    # Measured on `screened`, not the raw input: resurrecting a probe the VOID
    # pass rejected would launder the very thing the screen just rejected.
    had = [
        i
        for i, f in enumerate(screened)
        if f.role is CouncilRole.SHORTCUT_ATTACKER and probe(f)
    ]
    if not had:
        return out
    if any(
        out[i].role is CouncilRole.SHORTCUT_ATTACKER and probe(out[i])
        for i in range(len(out))
    ):
        return out
    keep = min(had, key=lambda i: screened[i].finding_id)
    prior = out[keep].screen
    out[keep] = out[keep].model_copy(
        update={
            "suggested_attack": screened[keep].suggested_attack,
            "screen": FindingScreen(
                status=FindingScreenStatus.REPRESENTATIVE,
                coalesced_from=(prior.duplicate_of,) if prior else (),
                mutant_key=prior.mutant_key if prior else "",
                signals=("shortcut_diligence_exemption",),
                evidence=(
                    "duplicate of "
                    f"{prior.duplicate_of if prior else 'another case'}, but "
                    "restored: it is the shortcut attacker's only executable "
                    "probe and the review stage reads that as diligence"
                ),
            ),
        }
    )
    return out


def screen_report(findings: list[Finding]) -> str:
    """One deterministic line summarising what the screen did (for logs/detail).

    A voided finding that CLAIMED FATAL is named: voiding a rejection reason is
    the most consequential thing this screen does.
    """
    void = [f for f in findings if _has_status(f, FindingScreenStatus.VOID)]
    dup = [f for f in findings if _has_status(f, FindingScreenStatus.DUPLICATE)]
    rep = [f for f in findings if _has_status(f, FindingScreenStatus.REPRESENTATIVE)]
    noted = [f for f in findings if _has_status(f, FindingScreenStatus.NOTED)]
    if not (void or dup or noted):
        return "screen: nothing voided, nothing coalesced"
    parts = []
    if noted:
        parts.append(
            "noted (weak evidence, left executable) "
            + ", ".join(f.finding_id for f in noted)
        )
    if void:
        parts.append(
            "voided "
            + ", ".join(
                f"{f.finding_id}[{'+'.join(f.screen.signals)}]" for f in void
            )
        )
        withdrawn_fatal = [
            f for f in void if f.screen.claimed_severity is Severity.FATAL
        ]
        if withdrawn_fatal:
            parts.append(
                "SELF-WITHDRAWN FATAL (claimed fatal, retracted by its own "
                "terminal clause; re-read before trusting) "
                + ", ".join(f.finding_id for f in withdrawn_fatal)
            )
    if dup:
        targets = sorted({f.screen.duplicate_of for f in dup})
        parts.append(
            f"coalesced {len(dup)} redundant probe(s) onto {len(targets)} "
            f"execution(s) ({len(rep)} of them compiled probes): "
            + ", ".join(f"{f.finding_id}->{f.screen.duplicate_of}" for f in dup)
        )
    return "screen: " + "; ".join(parts)


def _has_status(finding: Finding, status: FindingScreenStatus) -> bool:
    return finding.screen is not None and finding.screen.status is status


# Public entry points

def author_prose(task: TaskIR, provider: Provider) -> str:
    """Semantic authoring: IR + plan summaries -> solver-visible prose.

    The author's view carries no reference SQL, mutations, gold or literal rows,
    and `run_council` re-checks the stored prose independently. No approval
    power: this returns prose and nothing else.
    """
    prose = provider.complete(
        CouncilRole.SEMANTIC_AUTHOR, render_view(CouncilRole.SEMANTIC_AUTHOR, task)
    )
    if not isinstance(prose, str) or not prose.strip():
        raise ValueError("semantic author produced empty prose")
    return prose


#: Keys of the `record` mapping `author_prose_session` fills for its caller
#: (the author stage runner): the `SessionResult`, the revision count, the
#: number of drafts checked, the final-draft precheck and the terminal name.
AUTHOR_SESSION_RECORD_KEYS: tuple[str, ...] = (
    "result",
    "revisions",
    "drafts",
    "precheck",
    "terminal",
)


class AuthorSessionLimitStop(RuntimeError):
    """Signal an author-session limit reached before any draft exists.

    The stage maps this non-defect to a blocked salted rerun without spending a repair
    round. The exception carries terminal, limit kind, salt, and the text-free partial
    session result.
    """

    def __init__(
        self,
        terminal: str,
        *,
        limit: str,
        session_result=None,
        session_salt: int = 0,
        record: dict | None = None,
    ) -> None:
        self.terminal = str(terminal)
        self.limit = str(limit or "unknown")
        self.session_result = session_result
        self.session_salt = int(session_salt)
        self.record = dict(record or {})
        super().__init__(
            f"semantic author session ended {self.terminal} (session_limit:{self.limit}) "
            "without any draft; a harness-imposed cap, not a task defect — the stage "
            "waits on a salted re-run and spends no repair round"
        )


def author_session_limit_outcome(exc: AuthorSessionLimitStop, engine, task: TaskIR):
    """Map an author-session limit stop to the stage outcome.

    Before the salted rerun cap, return a blocked outcome with the next salt. After the
    cap, wall limits raise transient infrastructure failure; agent-attributable limits
    take one specification failure round. Engines without a rerun counter are treated as
    having zero prior reruns.
    """
    from elt_taskgen import engine as engine_mod  # lazy: the engine imports nothing from here

    stage = engine_mod.StageName.AUTHOR.value
    counter = getattr(engine, "session_limit_reruns", None)
    reruns = int(counter(task.task_id, stage)) if callable(counter) else 0
    limit = str(exc.limit or "unknown")
    result = exc.session_result
    data = {
        "stage": stage,
        "limit": limit,
        "reruns_used": str(reruns),
        "session_terminal": exc.terminal,
    }
    digest = str(getattr(result, "session_sha256", "") or "")
    if digest:
        data["session_sha256"] = digest
    if reruns >= engine_mod.MAX_SESSION_LIMIT_RERUNS:
        if limit in engine_mod.SESSION_LIMIT_HALT_KINDS:
            return engine_mod.StageOutcome(
                engine_mod.VERDICT_FAIL,
                engine_mod.StagePayload(
                    error=(
                        f"semantic author session stopped at session_limit:{limit} after "
                        f"{reruns} salted re-runs (the bound is "
                        f"{engine_mod.MAX_SESSION_LIMIT_RERUNS}); a {limit} stop is not "
                        "agent-attributable, so the stage halts as transient "
                        "infrastructure and no round is taken"
                    ),
                    infrastructure=engine_mod.SESSION_WALL_HALT_MARKER,
                    data=data,
                ),
            )
        data[engine_mod.SESSION_LIMIT_FALLBACK_KEY] = limit
        return engine_mod.StageOutcome(
            engine_mod.VERDICT_FAIL,
            engine_mod.StagePayload(
                error=(
                    f"semantic author session ended {exc.terminal} without any prose after "
                    f"{reruns} salted re-runs (the bound is "
                    f"{engine_mod.MAX_SESSION_LIMIT_RERUNS}); falling back to the "
                    "empty-output route, one SPECIFICATION round"
                ),
                data=data,
            ),
            route=RepairRoute.SPECIFICATION,
        )
    salt = reruns + 1
    data[engine_mod.BLOCKED_ON_KEY] = f"{engine_mod.SESSION_LIMIT_BLOCK_PREFIX}{limit}"
    data["session_salt"] = str(salt)
    data["rerun"] = f"{salt}/{engine_mod.MAX_SESSION_LIMIT_RERUNS}"
    return engine_mod.StageOutcome(
        engine_mod.VERDICT_BLOCKED,
        engine_mod.StagePayload(
            detail=(
                f"semantic author session ended {exc.terminal} without any draft "
                f"(session_limit:{limit}); waiting on salted re-run {salt}/"
                f"{engine_mod.MAX_SESSION_LIMIT_RERUNS} — a harness-imposed cap is not a "
                "task defect, so no repair round is spent"
            ),
            data=data,
        ),
    )


#: Abort reason codes that describe THE DRAFT rather than the task: when one
#: of these ends a session that already submitted a draft, the draft stands
#: and the stage's own gate decides. `infeasible`, `spec_conflict` and
#: `out_of_scope` are claims about the view itself and always stop.
_AUTHOR_DRAFT_ABORT_CODES: frozenset[str] = frozenset(
    {"cannot_repair", "insufficient_information"}
)


def author_prose_session(
    task: TaskIR,
    provider,
    *,
    tools: Sequence,
    policy,
    contamination_index=None,
    coverage_requirement=None,
    worker=None,
    clock=None,
    record: dict | None = None,
    session_salt: int | None = None,
) -> str:
    """Run one bounded, harness-validated prose revision session.

    Each turn submits prose or aborts. Submitted drafts run through `check_prose`; the
    final draft alone receives the contamination precheck, and leak scanning remains in
    review. Return the accepted or last submitted draft, raise `ValueError` after
    abstention, and raise `AuthorSessionLimitStop` when a limit stops the session before
    any draft. Infrastructure and protocol faults propagate.
    """
    import tempfile
    from pathlib import Path

    from elt_taskgen.review import prompts
    from elt_taskgen.review.session import TerminalState
    from elt_taskgen.review.tools import validators as author_tools
    from elt_taskgen.review.tools.projection import assert_value_free

    tools = tuple(tools)
    names = {str(getattr(t, "name", "")) for t in tools}
    if author_tools.AUTHOR_CHECK_TOOL not in names:
        raise ValueError("the author session needs the check_prose validator among its tools")
    if getattr(policy, "submit_tool", "") != author_tools.AUTHOR_SUBMIT_TOOL:
        raise ValueError("the author session's submit tool is submit_prose")
    forbidden = names & {"check_structure", "leak_findings"}
    if forbidden:
        raise ValueError(
            f"{sorted(forbidden)} are not author tools: check_structure is a proposer "
            "tool and the leak scan stays at review"
        )
    limits = policy.limits
    session = author_tools.AuthorSession(
        task=task,
        max_revisions=limits.max_revisions,
        contamination_index=contamination_index,
        coverage_requirement=coverage_requirement,
    )
    if worker is None:
        worker = author_tools.author_validator_worker()
    salt = int(getattr(policy, "session_salt", 0) or 0) if session_salt is None else int(session_salt)
    view = prompts.semantic_author_session_view(
        render_view(CouncilRole.SEMANTIC_AUTHOR, task),
        max_revisions=limits.max_revisions,
        session_salt=salt,
    )
    runner_kwargs: dict = {"worker": worker}
    if clock is not None:
        runner_kwargs["clock"] = clock
    with tempfile.TemporaryDirectory(prefix="elt-taskgen-author-session-") as tmp:
        ctx = session.context(Path(tmp))
        # Halts raise through here unchanged (the engine classifies them).
        result = provider.run_session(
            CouncilRole.SEMANTIC_AUTHOR, view, policy, ctx, **runner_kwargs
        )
        terminal = getattr(result, "terminal", None)
        final = getattr(result, "final", None)
        if terminal is TerminalState.ABSTAINED:
            reason = str(final.get("reason_code") or "") if isinstance(final, dict) else ""
            # Preserve the last draft on draft-scoped aborts and let the
            # deterministic gate judge it; task-scoped aborts still stop.
            if reason in _AUTHOR_DRAFT_ABORT_CODES and isinstance(session.draft, str) and session.draft.strip():
                prose = session.draft
            else:
                raise ValueError(
                    f"semantic author abstained ({reason or 'no reason code'}): "
                    "no prose was submitted"
                )
        else:
            prose = final.get("text") if isinstance(final, dict) else final
        if not isinstance(prose, str) or not prose.strip():
            # A limit stop without an auto-submitted draft: the last submitted
            # draft, if any — the deterministic gate decides, as for one-shot.
            prose = session.draft
        if not isinstance(prose, str) or not prose.strip():
            name = str(getattr(terminal, "name", terminal) or "")
            if bool(getattr(terminal, "is_limit_stop", False)):
                # No draft was ever submitted and a harness-imposed cap
                # ended the session: BLOCKED with a salt, never a round.
                kinds = getattr(result, "correction_kinds", None) or {}
                raise AuthorSessionLimitStop(
                    name,
                    limit=str(getattr(terminal, "limit_kind", "") or ""),
                    session_result=result,
                    session_salt=salt,
                    record={
                        "result": result,
                        "revisions": int(dict(kinds).get("compile", 0) or 0),
                        "drafts": session.check_count,
                        "precheck": None,
                        "terminal": name,
                    },
                )
            raise ValueError(f"semantic author session ended {name} without any prose")
        # contamination_precheck on the FINAL draft, once, harness-side, through
        # the worker and the D1 gatekeeper (never inside the turn loop).
        precheck_tool = author_tools.author_tool(author_tools.AUTHOR_PRECHECK_TOOL)
        deadline = float(getattr(getattr(precheck_tool, "cost", None), "wall_s", 10.0) or 10.0)
        outcome = worker.run(precheck_tool, ctx, {"text": prose}, deadline_s=deadline)
        assert_value_free(str(outcome.payload).encode("utf-8"), task=task)
        precheck = outcome.observation
    if record is not None:
        kinds = getattr(result, "correction_kinds", None) or {}
        record.update(
            {
                "result": result,
                "revisions": int(dict(kinds).get("compile", 0) or 0),
                "drafts": session.check_count,
                "precheck": precheck,
                "terminal": str(getattr(terminal, "name", terminal) or ""),
            }
        )
    return prose


def run_council(
    task: TaskIR,
    provider: Provider,
    *,
    roles: tuple[CouncilRole, ...] | None = None,
) -> list[Finding]:
    """Run selected critics and return findings only.

    Private-SQL leaks become fatal code findings before any provider call. Provider
    protocol errors raise, and provider findings pass through deterministic screening.
    An empty result expresses no objection, never acceptance.
    """
    roles = tuple(CRITIC_ROLES) if roles is None else tuple(roles)
    unknown = [r for r in roles if r not in CRITIC_ROLES]
    if unknown:
        raise ValueError(
            f"not critic roles: {[r.value for r in unknown]} "
            f"(the council seats are {[r.value for r in CRITIC_ROLES]})"
        )
    findings: list[Finding] = list(leak_findings(task))
    if any(f.severity is Severity.FATAL for f in findings):
        # SHORT-CIRCUIT: the leaking `task.solver_prompt` is embedded verbatim
        # in all four critic views, so running the loop would ship private
        # reference SQL to four external endpoints before the stage fails. The
        # outcome is unchanged; leaked material never crosses the transport.
        return findings
    content_hash8 = task.content_hash()[:8]
    proposed: list[Finding] = []
    for role in roles:
        view = render_view(role, task)
        raw = provider.complete(role, view)
        proposed.extend(_parse_findings(role, raw, content_hash8))
    findings.extend(screen_findings(task, proposed))
    return findings


# Critic sessions use the council parser, void red-at-submit proposals, and apply
# the one-shot screen. RoutedProvider projects screened findings back to the wire.

def findings_from_session(
    task: TaskIR,
    role: CouncilRole,
    result: Any,
    session: Any,
    id_suffix: str,
    *,
    allow_no_submission: bool = False,
) -> list[Finding]:
    """Parse, compile-screen, and deterministically screen one critic session result.

    Production requires final output; an explicit metrology trial may treat a missing
    submission as an empty list. Invalid final payloads raise `ProviderProtocolError`.
    """
    from elt_taskgen.review.providers import normalized_text_for, validate_payload_for
    from elt_taskgen.review.tools.critic_validators import void_uncompilable_proposals

    role = CouncilRole(getattr(role, "value", role))
    final = getattr(result, "final", None)
    if final is None:
        if not allow_no_submission:
            raise ProviderProtocolError(
                f"provider output for role {role.value} violated the council "
                "protocol: critic session ended without a schema-valid "
                "submission"
            )
        raw = normalized_text_for(role.value, {"findings": []})
    elif isinstance(final, str):
        raw = final
    else:
        data = json.loads(json.dumps(final, default=str))
        problem = validate_payload_for(role.value, data)
        if problem is not None:
            raise ProviderProtocolError(
                f"provider output for role {role.value} violated the council "
                f"protocol: the session's auto-submitted draft is not a valid "
                f"payload ({problem})"
            )
        raw = normalized_text_for(role.value, data)
    parsed = _parse_findings(role, raw, id_suffix)
    voided = list(void_uncompilable_proposals(parsed, session, result))
    return screen_findings(task, voided)

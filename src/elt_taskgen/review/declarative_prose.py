"""Check that solver prose states outcomes rather than SQL mechanics.

Rules require SQL-shaped context to avoid flagging ordinary prose.
"""

from __future__ import annotations

import re

from elt_taskgen.models import GateResult, TaskIR

__all__ = [
    "GATE_NAME",
    "check_declarative_prose",
    "declarative_prose_gate",
    "operator_problems",
]

GATE_NAME = "declarative-prose"


# Normalization

#: Quoting characters stripped before matching, so backticked SQL is one shape.
_QUOTES = str.maketrans({"`": " ", '"': " ", "‘": "'", "’": "'"})


def _normalize(text: str) -> str:
    """Lowercase, unquote, collapse whitespace; punctuation is preserved.

    Punctuation is load-bearing: `count(` is a function call and
    `count (of completed orders)` is English.
    """
    return " ".join(text.lower().translate(_QUOTES).split())


_IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*")


def _task_identifiers(task: TaskIR) -> frozenset[str]:
    """Every table, column and mart name this task publishes, lowercased.

    Adjacency to one of these turns an ambiguous verb into an operator:
    "joined the loyalty program" is English, "joined to customers" is a join.
    """
    names: set[str] = set()
    for table in task.tables:
        names.add(table.name.lower())
        for column in table.columns:
            names.add(column.name.lower())
    for mart in task.marts:
        names.add(mart.name.lower())
        for column in mart.columns:
            names.add(column.name.lower())
        names.update(k.lower() for k in mart.key_columns)
    return frozenset(n for n in names if n)


# Category patterns (context-qualified; see the module docstring on scope)

#: An argument list: what makes `count(distinct order_id)` a call, not English.
_ARGS = r"\(\s*(?:distinct\b|\*|[a-z_][a-z0-9_]*\s*(?:[,)*+/-]|\.\s*[a-z_]))"

#: Aggregate / scalar functions whose call syntax is pure implementation.
_FUNCTIONS = (
    "coalesce", "count", "sum", "avg", "mean", "min", "max", "cast", "nullif",
    "ifnull", "isnull", "nvl", "greatest", "least", "row_number", "rank",
    "dense_rank", "ntile", "lag", "lead", "first_value", "last_value",
    "string_agg", "array_agg", "group_concat", "listagg", "percentile_cont",
)

#: English parenthetical glosses require whitespace plus an SQL-invalid
#: continuation, so spaced SQL calls remain detectable.
_PARENTHETICAL_GLOSS_TAIL = re.compile(
    r"\s*(?:the\s+[a-z_][a-z0-9_]*\s+|which\s+(?:is|are|was|were|has|have|"
    r"means|denotes|reports|holds|contains|becomes|equals)\b)"
)
_PARENTHETICAL_GLOSS_FIELD = re.compile(
    r"\s*([a-z_][a-z0-9_]*)\s*,\s*$"
)


def _is_parenthetical_count_gloss(
    text: str,
    match: re.Match[str],
    identifiers: frozenset[str],
) -> bool:
    """Whether a COUNT-shaped match is English ``row count (field, gloss)``."""
    fragment = match.group(0)
    opening = fragment.find("(")
    if not (
        fragment.startswith("count")
        and opening > 0
        and fragment[opening - 1].isspace()
        and _PARENTHETICAL_GLOSS_TAIL.match(text, match.end()) is not None
    ):
        return False

    field_match = _PARENTHETICAL_GLOSS_FIELD.fullmatch(fragment[opening + 1 :])
    if field_match is None or field_match.group(1) not in identifiers:
        return False
    label = field_match.group(1).replace("_", " ")
    label_head = text[: match.start() + opening].rstrip()
    label_start = len(label_head) - len(label)
    return (
        label_start >= 0
        and label_head[label_start:] == label
        and (
            label_start == 0
            or not (label_head[label_start - 1].isalnum()
                    or label_head[label_start - 1] == "_")
        )
    )


#: Words that make a following "order by" ordinary English, not a clause.
_ORDER_BY_ENGLISH = frozenset(
    {"ascending", "descending", "alphabetical", "alphabetically", "numerical",
     "numeric", "reverse", "chronological", "sort", "sorted", "sorting"}
)

#: Words that make a following "case when" ordinary English ("in the case when").
_CASE_ENGLISH = frozenset(
    {"the", "a", "an", "any", "each", "every", "this", "that", "which",
     "in", "special", "edge", "corner", "worst", "best", "same"}
)

#: A real CASE tail; OVERRULES the English guard — prose never says "then 1 end".
_SQL_CASE_TAIL = re.compile(r"[^.;:!?]{0,80}\bthen\b[^.;:!?]{0,60}\bend\b")


def _prev_word(text: str, start: int) -> str:
    """The word immediately before offset `start` ('' at the beginning)."""
    head = text[:start].rstrip()
    match = re.search(r"[a-z0-9_]+$", head)
    return match.group(0) if match else ""


#: (category, pattern, remedy). Order is the report order; it is stable.
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "join-operator",
        re.compile(
            r"\b(?:left|right|inner|outer|full|cross|natural|anti|semi|equi|"
            r"self)[\s-]+(?:outer[\s-]+)?joins?\b"
        ),
        "state which rows survive instead: say that customers with no "
        "matching completed order still appear in the output",
    ),
    (
        "join-operator",
        re.compile(
            r"\bjoins?\s+(?:direction|type|types|key|keys|condition|conditions|"
            r"predicate|clause|semantics|side|order)\b"
            r"|\b(?:the|a|an|this|that|each|every|both|one)\s+joins?\b"
            r"|\bjoin(?:s|ed|ing)?\s+(?:\w+\s+){0,2}?on\b"
            r"|\bjoin(?:s|ed|ing)?\s+using\b"
        ),
        "state which rows survive instead: say that customers with no "
        "matching completed order still appear in the output",
    ),
    (
        "function-call",
        re.compile(r"\b(?:" + "|".join(_FUNCTIONS) + r")\s*" + _ARGS),
        "state the value the column must hold, not the function that "
        "computes it (\"0, never null\" rather than a COALESCE call)",
    ),
    (
        "coalesce",
        re.compile(r"\bcoalesc(?:e|es|ed|ing)\b"),
        "say what the reader sees — \"appears with 0, never null\" — rather "
        "than naming the operator that produces it",
    ),
    (
        "distinct-operator",
        re.compile(
            r"\b(?:count|select|sum|avg|array_agg|string_agg)\s*\(?\s*distinct\b"
            r"|\bdistinct\s*\("
        ),
        "say \"count each completed order once, even if its header row "
        "repeats\" rather than naming DISTINCT",
    ),
    (
        "by-clause",
        re.compile(
            r"\b(?:group|grouped|grouping|partition|partitioned|partitioning|"
            r"cluster|clustered|distribute|distributed)\s+by\b"
        ),
        "state the grain — \"one row per customer\" / \"each order has one "
        "item total\" — rather than the clause that produces it",
    ),
    (
        "case-expression",
        re.compile(r"\bcase\s+when\b|\bwhen\b[^.;:!?]{0,80}\bthen\b[^.;:!?]{0,60}\bend\b"),
        "state the condition and its outcome as a sentence rather than as a "
        "conditional expression",
    ),
    (
        "set-operator",
        re.compile(r"\bunion\s+all\b|\bintersect\b|\bexcept\s+(?:all|select)\b"),
        "say that the two row sets are reported together and whether "
        "duplicates survive, rather than naming the set operator",
    ),
    (
        "window-syntax",
        re.compile(
            r"\bover\s*\(|\brows\s+between\b|\bunbounded\s+(?:preceding|"
            r"following)\b|\bcurrent\s+row\b"
        ),
        "state what each row's value is relative to its group, rather than "
        "the window clause that computes it",
    ),
    (
        "join-predicate",
        re.compile(
            r"\b[a-z_][a-z0-9_]*\s*\.\s*[a-z_][a-z0-9_]*\s*=\s*"
            r"[a-z_][a-z0-9_]*\s*\.\s*[a-z_][a-z0-9_]*"
        ),
        "name the relationship in words (\"an order belongs to the customer "
        "its customer_id names\") rather than writing the equality",
    ),
    (
        "query-clause",
        re.compile(
            r"\bselect\b[^.;:!?]{0,60}\bfrom\b"
            r"|\b(?:where|having)\s+[a-z_][a-z0-9_]*\s*(?:=|<>|!=|<=|>=|<|>)"
            r"|\bwith\s+[a-z_][a-z0-9_]*\s+as\s*\(|\bas\s*\(\s*select\b"
        ),
        "describe the required rows and values; the query that produces them "
        "is the solver's job",
    ),
)

#: Patterns that bite only when a task identifier starts within this many tokens.
_ADJACENCY = 2

_IDENT_QUALIFIED: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "join-operator",
        re.compile(r"\bjoin(?:s|ed|ing)?\b"),
        "state which rows survive instead of naming the join",
    ),
    (
        "distinct-operator",
        re.compile(r"\bdistinct\b"),
        "say \"once, even if the row repeats\" rather than naming DISTINCT",
    ),
    (
        "set-operator",
        re.compile(r"\bunions?\b"),
        "say that the row sets are reported together rather than naming UNION",
    ),
)


def _identifier_follows(text: str, end: int, identifiers: frozenset[str]) -> str | None:
    """The task identifier starting within `_ADJACENCY` tokens after `end`."""
    tail = text[end : end + 80]
    for offset, token in enumerate(_IDENT_RE.findall(tail)):
        if offset > _ADJACENCY:
            return None
        if token in identifiers:
            return token
    return None


def _is_declarative_distinct_phrase(
    text: str,
    match: re.Match[str],
    identifiers: frozenset[str],
) -> bool:
    """Return whether bare `distinct` is part of a declarative output phrase.

    Only published snake-case output names rendered as words and bounded “number/how
    many distinct <public identifier> rows/values” forms qualify. Explicit SQL forms
    such as `COUNT(DISTINCT ...)` never qualify.
    """
    tail_tokens = tuple(_IDENT_RE.findall(text[match.start() : match.start() + 160]))
    for identifier in identifiers:
        parts = tuple(identifier.split("_"))
        if (
            len(parts) > 1
            and parts[0] == "distinct"
            and tail_tokens[: len(parts)] == parts
        ):
            return True

    head_tokens = _IDENT_RE.findall(text[max(0, match.start() - 48) : match.start()])
    # Both "number of distinct ... values" and "how many distinct ... values"
    # are declarative; a bare DISTINCT directive remains invalid.
    if head_tokens[-2:] not in (["number", "of"], ["how", "many"]):
        return False
    following = _IDENT_RE.findall(text[match.end() : match.end() + 120])
    return bool(
        len(following) >= 2
        and following[0] in identifiers
        and following[1] in {"row", "rows", "value", "values"}
    )


def _excerpt(text: str, start: int, end: int, width: int = 28) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    return (
        ("..." if left else "")
        + text[left:right].strip()
        + ("..." if right < len(text) else "")
    )


# The check

def operator_problems(prose: str, identifiers: frozenset[str] = frozenset()) -> list[str]:
    """Every relational-operator fragment in `prose`, named. Empty = clean.

    Deterministic pure function of (prose, identifiers).
    """
    text = _normalize(prose)
    if not text:
        return []
    problems: list[str] = []
    seen: set[tuple[int, int]] = set()

    def add(category: str, start: int, end: int, remedy: str, note: str = "") -> None:
        # One fragment, one finding: the 'join' inside an already-reported
        # 'left join' must not be reported a second time.
        if any(a <= start and end <= b for a, b in seen):
            return
        seen.add((start, end))
        problems.append(
            f"{category}: prose states the SQL mechanics "
            f"{text[start:end].strip()!r}{note} in \"{_excerpt(text, start, end)}\" "
            f"— {remedy}"
        )

    for category, pattern, remedy in _PATTERNS:
        for match in pattern.finditer(text):
            if (
                category == "function-call"
                and _is_parenthetical_count_gloss(text, match, identifiers)
            ):
                continue
            if (
                category == "case-expression"
                and _prev_word(text, match.start()) in _CASE_ENGLISH
                and not _SQL_CASE_TAIL.match(text, match.end())
            ):
                continue
            add(category, match.start(), match.end(), remedy)

    # ORDER BY needs its own guard: "in ascending order by customer_id" is a
    # sentence about the required row order, not a clause.
    for match in re.finditer(r"\border\s+by\b", text):
        if _prev_word(text, match.start()) in _ORDER_BY_ENGLISH:
            continue
        add(
            "by-clause",
            match.start(),
            match.end(),
            "state the row order as an outcome (\"rows appear in ascending "
            "customer_id order\") rather than as a clause",
        )

    for category, pattern, remedy in _IDENT_QUALIFIED:
        for match in pattern.finditer(text):
            if category == "distinct-operator" and _is_declarative_distinct_phrase(
                text, match, identifiers
            ):
                continue
            ident = _identifier_follows(text, match.end(), identifiers)
            if ident is None:
                continue
            add(category, match.start(), match.end(), remedy, f" on {ident!r}")

    # Repeated prose blocks can yield byte-identical diagnostics.  One red
    # diagnostic is sufficient to fail closed and gives a bounded author
    # correction signal without implying that the duplicate was accepted.
    return sorted(set(problems))


def check_declarative_prose(task: TaskIR) -> list[str]:
    """Operator-vocabulary problems in `task.solver_prompt`. Empty = clean.

    DOES NOT COVER VACUITY: empty prose yields no problems here, so
    prose_fidelity is what fails it (every declared item is missing).
    """
    return operator_problems(task.solver_prompt, _task_identifiers(task))


def declarative_prose_gate(task: TaskIR) -> GateResult:
    """GateResult view of the check (red iff any operator fragment is found)."""
    problems = check_declarative_prose(task)
    return GateResult(
        gate=GATE_NAME,
        passed=not problems,
        details=(
            "authored prose states outcomes, not relational operators"
            if not problems
            else "; ".join(problems)
        ),
        evidence={
            "problem_count": str(len(problems)),
            "prose_chars": str(len(task.solver_prompt)),
        },
    )

"""Compare authored prose with public TaskIR and MartSpec rules.

Matching is deterministic, token-based, and fail-closed. Declarative-prose checks run
after coverage checks.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from elt_taskgen.generation.mart_plan import (
    SOLVER_PUBLIC_DETAIL_KEYS,
    solver_safe_plan_requirements,
)
from elt_taskgen.models import GateResult, MartOpKind, MartSpec, TaskIR
from elt_taskgen.review import declarative_prose

__all__ = [
    "COLUMN_DESC_COVERAGE",
    "GATE_NAME",
    "PROSE_ITEM_KINDS",
    "ProblemLocus",
    "RULE_SENTENCE_WINDOW",
    "RULE_TERM_COVERAGE",
    "check_prose_fidelity",
    "problem_locus",
    "prose_fidelity_gate",
]

GATE_NAME = "prose-fidelity"

#: Min fraction of a rule's terms in ONE PASSAGE; below 0.75, rule deletions pass.
RULE_TERM_COVERAGE = 0.75

#: Minimum fraction of an output column description's significant terms.
COLUMN_DESC_COVERAGE = 0.5

#: Function words carrying no rule substance; adding words here loosens the gate.
_STOPWORDS = frozenset(
    """
    a an and are as at be been being both but by can could do does each
    every for from had has have how if in into is it its may might must no
    nor not of off on onto only or over per shall should so some such than
    that the their them then there these they this those to under until up
    upon was were what when where whether which while who whose will with
    would
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Only semantic detail fields shown by the author view are requirements.  The
# other common keys carry compiler aliases or literal SQL and are intentionally
# private to plan compilation (generation.mart_plan._COMPILER_ONLY_DETAIL_KEYS
# is the strict minimum; group/order/partition/select expressions are likewise
# mechanics rather than solver-facing outcomes).
_PUBLIC_DETAIL_KEYS = SOLVER_PUBLIC_DETAIL_KEYS

# Negation is order-sensitive, so normalize only the closed English negative
# contractions before `_TOKEN_RE` discards apostrophes. Generic ``n't`` is
# regular except for can't/won't; ``cannot`` is the equivalent closed form.
_NEGATIVE_CONTRACTION_RE = re.compile(r"\b([a-z]+)n['’]t\b", re.IGNORECASE)


def _negation_tokens(passage: str) -> list[str]:
    text = passage.lower()
    text = re.sub(r"\bcan['’]t\b", "can not", text)
    text = re.sub(r"\bwon['’]t\b", "will not", text)
    text = _NEGATIVE_CONTRACTION_RE.sub(r"\1 not", text)
    text = re.sub(r"\bcannot\b", "can not", text)
    return _TOKEN_RE.findall(text)


def _normalize(text: str) -> str:
    """Lowercase, horizontal whitespace collapsed — LINE STRUCTURE KEPT.

    `_SENTENCE_SPLIT_RE` reads the newlines: collapse them and an unpunctuated
    markdown list becomes one giant "sentence", silently widening the
    RULE_SENTENCE_WINDOW locality guarantee to the whole list.
    """
    lines = [" ".join(line.split()) for line in text.lower().split("\n")]
    collapsed: list[str] = []
    for line in lines:
        if not line and collapsed and not collapsed[-1]:
            continue  # one blank line stands for any run of blank lines
        collapsed.append(line)
    return "\n".join(collapsed).strip()


def _significant_terms(text: str) -> tuple[str, ...]:
    """Deterministically ordered unique content tokens of a description."""
    seen: dict[str, None] = {}
    for token in _TOKEN_RE.findall(text.lower()):
        if len(token) < 3 or token in _STOPWORDS or token.isdigit():
            continue
        seen.setdefault(token, None)
    return tuple(seen)


#: Suffixes stripped when expanding a passage's vocabulary ('orders' -> 'order').
_INFLECTIONS: tuple[str, ...] = ("s", "es", "ed", "ing", "d")

#: How many CONSECUTIVE sentences of one block may jointly represent one rule.
RULE_SENTENCE_WINDOW = 2

#: Sentence TERMINATORS only; ';'/':' would shatter a banding rule's arms.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

#: A line that STARTS A BLOCK: a list item, a heading ('#') or a table row ('|').
_BLOCK_START_RE = re.compile(r"^(?:[-*+•]\s|\d+[.)]\s|#|\|)")

#: A list ENUMERATOR ('1.', '12)', 'a.'), glued back onto the item it numbers.
_ENUMERATOR_RE = re.compile(r"^(?:\(?\d+|[a-z])[.)]$")

#: A LABEL line ('Rules:'), folded into the block it introduces as sentence 1.
_LABEL_RE = re.compile(r"^[^.!?]*:$")


def _blocks(prose: str) -> list[list[str]]:
    """Normalized prose -> blocks of sentences (the locality structure).

    A BLOCK is one list item, heading, table row or paragraph; a plain line
    starting neither extends the block it is in, so hard-wrapped paragraphs are
    never shattered. Windows must stay block-local: a window spanning two list
    items lets them ASSEMBLE a rule neither states, which went GREEN on both a
    LEFT->INNER rewrite and a single-rule deletion.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    previous_blank = True
    for line in prose.split("\n"):
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            previous_blank = True
            continue
        if _BLOCK_START_RE.match(line) or previous_blank:
            if current:
                blocks.append(current)
            current = [line]
        else:
            current.append(line)  # soft-wrapped continuation
        previous_blank = False
    if current:
        blocks.append(current)

    result: list[list[str]] = []
    pending_label: list[str] = []
    for lines in blocks:
        text = " ".join(lines)
        fragments = [f.strip() for f in _SENTENCE_SPLIT_RE.split(text) if f.strip()]
        sentences: list[str] = []
        enumerator: str | None = None
        for fragment in fragments:
            if _ENUMERATOR_RE.match(fragment):
                enumerator = fragment if enumerator is None else f"{enumerator} {fragment}"
                continue
            if enumerator is not None:
                fragment = f"{enumerator} {fragment}"
                enumerator = None
            sentences.append(fragment)
        if enumerator is not None:
            sentences.append(enumerator)
        if not sentences:
            continue
        if len(sentences) == 1 and _LABEL_RE.match(sentences[0]) and not (
            _BLOCK_START_RE.match(sentences[0])
        ):
            pending_label.append(sentences[0])
            continue
        result.append(pending_label + sentences)
        pending_label = []
    if pending_label:
        result.append(pending_label)
    return result


def _vocabulary(passage: str) -> frozenset[str]:
    """Every term a passage can legitimately be said to contain.

    TOKENS, never substrings: exact token, an underscore component ('order'
    from 'order_items'), or a de-inflection ('order' from 'orders'). Substring
    containment let 'nullable' satisfy 'null' and a deleted rule stay green.
    """
    vocab: set[str] = set()
    for token in _TOKEN_RE.findall(passage.lower()):
        pieces = {token, *token.split("_")}
        for piece in list(pieces):
            for suffix in _INFLECTIONS:
                if piece.endswith(suffix) and len(piece) - len(suffix) >= 3:
                    pieces.add(piece[: -len(suffix)])
        vocab |= pieces
    return frozenset(vocab)


#: Operator word -> words stating the same OUTCOME; either satisfies a term.
#: The gate's ONLY loosening: one direction only (the outcome word implies the
#: operator, never the reverse), never a word that could satisfy two slots of
#: one rule, and anything added here goes into prompts._SEMANTIC_AUTHOR in the
#: SAME commit or the prompt describes a stricter gate than the one that runs.
_DECLARATIVE_EQUIVALENTS: dict[str, frozenset[str]] = {
    # A preserved-side join states "these rows are still there".
    "left": frozenset({"including", "include", "included", "preserved",
                       "preserve", "preserving", "unmatched", "retained",
                       "retain", "kept", "keeps"}),
    # A join is the statement "these rows belong together".
    "join": frozenset({"match", "matched", "matching", "attributed",
                       "attribute", "belongs", "belong", "belonging"}),
    # DISTINCT is the statement "it counts once, however often it repeats".
    "distinct": frozenset({"once"}),
    # SUM is the statement "the total of".
    "sum": frozenset({"total", "totals", "totalled", "totaled"}),
    # GROUP BY is the statement "each X has one".
    "grouped": frozenset({"each", "per", "own"}),
    "group": frozenset({"each", "per", "own"}),
    # Projection is the statement that a source grain/key is carried through.
    "project": frozenset({"carried"}),
    # Enumerate irregular passive forms that the small suffix normalizer cannot
    # recover from generated plan prose.
    "carry": frozenset({"carried", "carrying"}),
    "label": frozenset({"labeled", "labelled", "labeling", "labelling"}),
    "bring": frozenset({"brought"}),
    # MAX / ARGMAX: the operator words are BANNED by declarative_prose.py, so
    # outcome words are the only way to state these rules at all.
    "max": frozenset({"largest", "highest", "greatest"}),
    "argmax": frozenset({"largest", "highest", "greatest"}),
    # Irregular verb: suffix-stripping never maps 'kept' back to 'keep'.
    # 'retained'/'retain' stay out: they pushed a deleted-filter prose green.
    "keep": frozenset({"kept", "survives", "survive"}),
    # A CASE ladder's ELSE is the statement "anything else / otherwise".
    "else": frozenset({"otherwise"}),
}

#: Direct negation uses only this closed safe subset, not the full affirmative
#: equivalence map; object-taking verbs and nouns can invert meaning. Literal keys
#: still form their own term groups.
_NEGATABLE_EQUIVALENTS: dict[str, frozenset[str]] = {
    "left": frozenset({"preserve", "preserved", "preserving"}),
    "project": frozenset({"carried"}),
    # Keep every newly admitted verb outcome under the same polarity check:
    # `not carried`, `not labelled`, and `not brought` must never satisfy the
    # corresponding affirmative plan rule.
    "carry": frozenset({"carried", "carrying"}),
    "label": frozenset({"labeled", "labelled", "labeling", "labelling"}),
    "bring": frozenset({"brought"}),
}

#: Grain wording has a separate, much narrower equivalence surface than plan
#: rules.  A relationship described as rows "attributed" to an entity states
#: the same output grain as those rows being "linked" to it.  Keep this map
#: one-way and closed: broad plan-rule equivalents would make an unrelated
#: grain pass on generic transformation vocabulary.
_GRAIN_EQUIVALENTS: dict[str, frozenset[str]] = {
    "linked": frozenset({"attributed"}),
}


def _satisfied(term: str, vocab: frozenset[str]) -> bool:
    """Is `term` present literally, or via a declarative outcome word?"""
    if term in vocab:
        return True
    return bool(_DECLARATIVE_EQUIVALENTS.get(term, frozenset()) & vocab)


#: BASE-FORM verbs naming each op kind's OPERATION; object-taking verbs ('keep',
#: 'retain') stay out because honest prose negates them all the time.
_NEGATABLE_ACTIONS: dict[MartOpKind, frozenset[str]] = {
    MartOpKind.DEDUPE: frozenset({"deduplicate", "dedupe", "collapse"}),
    MartOpKind.JOIN: frozenset({"join"}),
    MartOpKind.AGGREGATE: frozenset({"aggregate", "group"}),
    # 'default' is out: it is a noun ("no default value") more often than a verb.
    MartOpKind.DERIVE: frozenset({"coalesce", "substitute"}),
    MartOpKind.TIE_BREAK: frozenset({"sort"}),
    MartOpKind.UNION: frozenset({"union", "combine", "concatenate"}),
    MartOpKind.WINDOW: frozenset({"partition"}),
    MartOpKind.FILTER: frozenset({"filter"}),
}

#: Tokens that negate a following operation verb; 'no' is out (it negates the
#: object, not the verb, and nothing deterministic tells the two apart).
_NEGATION_CUES = frozenset(
    {"not", "never", "dont", "avoid", "avoiding", "skip", "skipping", "omit",
     "omitting", "without", "refrain"}
)

#: How many tokens back a cue may sit and still govern the verb/outcome.  Four
#: is the exact span in ``refrain from using a LEFT``; arbitrary intervening
#: predicates are still rejected by `_directly_negated`'s closed glue grammar.
_NEGATION_REACH = 4


def _action_forms(token: str) -> frozenset[str]:
    """Base forms a token could be an inflection of; over-generates on purpose
    and lets the intersection with the op kind's closed verb list decide.
    """
    forms = {token}
    for suffix in ("ing", "ed", "es", "s", "d"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            stem = token[: -len(suffix)]
            forms.add(stem)
            forms.add(stem + "e")
    return frozenset(forms)


_NON_NEGATING_NOT_COMPLEMENTS = frozenset({"just", "merely", "only"})
_ADVERBIAL_NEGATION_GLUE = frozenset(
    {"actually", "also", "deliberately", "directly", "ever", "explicitly", "fully"}
)
_NEGATION_GLUE_BY_CUE: dict[str, frozenset[str]] = {
    # Auxiliaries/adverbs may intervene, but arbitrary predicates may not.
    "not": _ADVERBIAL_NEGATION_GLUE | {"being"},
    "never": _ADVERBIAL_NEGATION_GLUE | {"being"},
    "dont": _ADVERBIAL_NEGATION_GLUE,
    # These directive cues admit a bounded determiner before a gerund/noun.
    "avoid": _ADVERBIAL_NEGATION_GLUE | {"a", "an", "any", "being", "the"},
    "avoiding": _ADVERBIAL_NEGATION_GLUE | {"a", "an", "any", "being", "the"},
    "skip": _ADVERBIAL_NEGATION_GLUE | {"a", "an", "any", "being", "the"},
    "skipping": _ADVERBIAL_NEGATION_GLUE | {"a", "an", "any", "being", "the"},
    "omit": _ADVERBIAL_NEGATION_GLUE | {"a", "an", "any", "being", "the"},
    "omitting": _ADVERBIAL_NEGATION_GLUE | {"a", "an", "any", "being", "the"},
    "without": _ADVERBIAL_NEGATION_GLUE | {"a", "an", "any", "being", "the"},
    # `refrain` uniquely selects an infinitival `from [being] <outcome>`.
    "refrain": _ADVERBIAL_NEGATION_GLUE | {"being", "from"},
}


def _closed_use_bridge(cue: str, between: list[str]) -> bool:
    """Recognize only ``<cue> [from] use/using [a] <semantic term>``.

    ``use`` is intentionally not general negation glue: admitting it globally
    would let a cue jump across an unrelated predicate.  This closed phrase is
    needed for ordinary literal wording such as ``do not use a LEFT JOIN``.
    """
    remaining = list(between)
    while remaining and remaining[0] in _ADVERBIAL_NEGATION_GLUE:
        remaining.pop(0)
    if cue == "refrain":
        if not remaining or remaining.pop(0) != "from":
            return False
        while remaining and remaining[0] in _ADVERBIAL_NEGATION_GLUE:
            remaining.pop(0)
    if not remaining or remaining.pop(0) not in {"use", "using"}:
        return False
    if remaining and remaining[0] in {"a", "an", "any", "the"}:
        remaining.pop(0)
    return not remaining


def _directly_negated(tokens: list[str], index: int) -> bool:
    """Whether a nearby cue negates the token at ``index``.

    ``not only carried`` affirms that something is carried and adds another
    fact; treating it like ``not carried`` is a deterministic false positive.
    The same applies to ``not just`` and ``not merely``.  Other cue handling
    deliberately retains the gate's short, closed reach.
    """
    start = max(0, index - _NEGATION_REACH)
    for cue_index in range(start, index):
        if tokens[cue_index] not in _NEGATION_CUES:
            continue
        between = tokens[cue_index + 1 : index]
        if (
            tokens[cue_index] == "not"
            and between
            and between[0] in _NON_NEGATING_NOT_COMPLEMENTS
        ):
            continue
        # Do not carry a negation cue across another predicate; allow only closed
        # adverbial bridges such as "not explicitly carried".
        allowed_glue = _NEGATION_GLUE_BY_CUE.get(tokens[cue_index], frozenset())
        if (
            not between
            or all(token in allowed_glue for token in between)
            or _closed_use_bridge(tokens[cue_index], between)
        ):
            return True
    return False


def _semantic_action_groups(
    kind: MartOpKind, terms: tuple[str, ...]
) -> tuple[tuple[str, frozenset[str], frozenset[str]], ...]:
    """Return closed affirmative word groups that can satisfy semantic rules.

    Groups are term-local, so an unrelated positive action cannot cancel a negated
    required outcome. Unsafe noun-only equivalents are excluded.
    """
    groups: list[tuple[str, frozenset[str], frozenset[str]]] = []

    actions = _NEGATABLE_ACTIONS.get(kind, frozenset())
    if actions:
        # Keep this vocabulary closed. In particular, FILTER deliberately does
        # not regain the object-taking `keep`/`kept` words excluded above.
        groups.append((kind.value, actions, actions))

    for term in terms:
        safe_equivalents = _NEGATABLE_EQUIVALENTS.get(term)
        if not safe_equivalents:
            continue
        # Every equivalent can supply affirmative evidence for this exact
        # semantic term. The literal term and only the explicit safe subset of
        # equivalents can themselves trigger a negation finding.
        affirmative = frozenset(
            {term, *_DECLARATIVE_EQUIVALENTS.get(term, frozenset())}
        )
        # A literal semantic term must never certify itself while directly
        # negated (``never LEFT JOIN``, ``do not project``).  Only the
        # equivalents remain allowlisted because many are unsafe predicates.
        negatable = frozenset({term, *safe_equivalents})
        groups.append((term, negatable, affirmative))
    return tuple(groups)


def _negation_problem(
    kind: MartOpKind, passage: str, terms: tuple[str, ...] = ()
) -> str | None:
    """The rule's own operation put under a directive negation, if any.

    Two soundness guards, because a false positive fails an honest task: a
    passage that ALSO states the operation positively is never flagged, and a
    flagged passage only loses ITS candidacy, never the whole stage.
    """
    tokens = _negation_tokens(passage)
    for label, negatable_actions, affirmative_actions in _semantic_action_groups(
        kind, terms
    ):
        negated: list[str] = []
        positive: list[str] = []
        for index, token in enumerate(tokens):
            forms = _action_forms(token)
            if not (forms & affirmative_actions):
                continue
            if _directly_negated(tokens, index):
                if forms & negatable_actions:
                    negated.append(token)
            else:
                positive.append(token)
        if negated and not positive:
            return (
                f"puts the rule's {label!r} operation or outcome under a "
                f"negation ({negated[0]!r}) and states it positively nowhere "
                "in the passage"
            )
    return None


def _windows(prose: str, size: int) -> tuple[tuple[str, frozenset[str]], ...]:
    """Vocabularies of every run of up to `size` consecutive sentences, never
    spanning two blocks (`_blocks`).

    LOCALITY IS THE POINT: over the whole prose the other rules re-supply a
    deleted rule's vocabulary and the gate stays GREEN. Each window carries its
    TEXT too, because `_negation_problem` reads word ORDER.
    """
    windows: list[tuple[str, frozenset[str]]] = []
    for sentences in _blocks(prose):
        vocabs = [_vocabulary(s) for s in sentences]
        for start in range(len(vocabs)):
            joined: set[str] = set()
            for end in range(start, min(start + size, len(vocabs))):
                joined |= vocabs[end]
                windows.append(
                    (" ".join(sentences[start : end + 1]), frozenset(joined))
                )
    return tuple(windows)


#: Mutually exclusive vocabulary: 'inner' does not represent a rule's 'left'.
#: Fractional coverage alone is blind to substitutions that flip semantics.
_EXCLUSIVE_TERM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"left", "right", "inner", "cross"}),         # join direction
    frozenset({"ascending", "descending"}),                 # sort direction
    frozenset({"including", "excluding"}),                  # membership
    frozenset({"distinct", "duplicated"}),                  # de-duplication
    frozenset({"first", "last"}),                           # tie-break pick
)


def _polarity_problem(
    terms: tuple[str, ...], vocab: frozenset[str]
) -> str | None:
    """The exclusive term this passage CONTRADICTS, if any.

    ORDERING MATTERS: a declarative equivalent SATISFIES the wanted term and
    ends that group's check, while a LITERAL conflicting term still beats a
    merely-absent one — which is what keeps a LEFT->INNER rewrite red.
    """
    for group in _EXCLUSIVE_TERM_GROUPS:
        wanted = sorted(t for t in terms if t in group)
        if not wanted:
            continue
        missing = [t for t in wanted if not _satisfied(t, vocab)]
        if not missing:
            continue
        conflicting = sorted((vocab & group) - set(wanted))
        if conflicting:
            return (
                f"states {conflicting} where the rule says {missing} "
                "(mutually exclusive)"
            )
        return f"never states {missing}"
    return None


def _present(term: str, prose: str) -> bool:
    """Token-level presence over normalized prose (see `_vocabulary`)."""
    return term in _vocabulary(prose)


def _grain_term_present(term: str, prose: str) -> bool:
    """Whether a grain term is stated affirmatively, under its closed map.

    Most grain words retain the historical literal-token rule.  For the one
    relationship synonym above, inspect occurrences in order so a directly
    negated ``not linked`` or ``not attributed`` cannot certify the grain.
    """
    equivalents = _GRAIN_EQUIVALENTS.get(term)
    if equivalents is None:
        return _present(term, prose)
    accepted = frozenset({term, *equivalents})
    tokens = _negation_tokens(prose)
    return any(
        bool(_action_forms(token) & accepted)
        and not _directly_negated(tokens, index)
        for index, token in enumerate(tokens)
    )


def _missing_fraction_problems(
    label: str, terms: tuple[str, ...], prose: str, threshold: float
) -> str | None:
    """None if enough of `terms` appear; else a problem naming the misses."""
    if not terms:
        return None
    hits = [t for t in terms if _present(t, prose)]
    if len(hits) / len(terms) >= threshold:
        return None
    missing = [t for t in terms if not _present(t, prose)]
    return f"{label} (missing terms: {', '.join(missing)})"


def _passages(prose: str) -> tuple[tuple[str, frozenset[str]], ...]:
    """One block-local passage per source/relationship declaration.

    Source contracts must be stated as pairs.  A table named in one paragraph
    and a backend (or relationship key) mentioned elsewhere is not an
    instruction telling the solver how that table is obtained or related.
    """
    passages: list[tuple[str, frozenset[str]]] = [
        (" ".join(sentences), _vocabulary(" ".join(sentences)))
        for sentences in _blocks(prose)
    ]
    # Treat each non-empty line as a passage so neighboring list items cannot
    # affect relationship polarity or satisfy a same-passage requirement.
    passages.extend(
        (line, _vocabulary(line))
        for line in (raw.strip() for raw in prose.splitlines())
        if line
    )
    return tuple(passages)


def _check_source_contract(task: TaskIR, prose: str) -> list[str]:
    """Check the TaskIR source contract without reading reference SQL/gold."""
    problems: list[str] = []
    passages = _passages(prose)

    for table in task.tables:
        backend = task.backend_for(table.name).backend.value
        if not any(
            _present(table.name.lower(), passage)
            and _present(backend.lower(), passage)
            for passage, _ in passages
        ):
            problems.append(
                f"source table {table.name!r}: backend {backend!r} is not "
                "stated with the table in one passage"
            )

    for index, relationship in enumerate(task.relationships, start=1):
        identifiers = (
            relationship.child_table.lower(),
            *tuple(column.lower() for column in relationship.child_columns),
            relationship.parent_table.lower(),
            *tuple(column.lower() for column in relationship.parent_columns),
        )
        required_counts = {
            identifier: identifiers.count(identifier) for identifier in set(identifiers)
        }
        wanted = "required" if relationship.required else "optional"
        opposite = "optional" if relationship.required else "required"
        candidates = [
            vocab
            for passage, vocab in passages
            # Accept structural relationship forms such as arrows, not only
            # the word "relationship"; endpoint, key, and polarity checks remain.
            if _states_a_relationship(passage, vocab)
            and all(
                _TOKEN_RE.findall(passage.lower()).count(identifier) >= count
                for identifier, count in required_counts.items()
            )
        ]
        if not candidates:
            problems.append(
                f"relationship {index}: {relationship.child_table}"
                f"({', '.join(relationship.child_columns)}) -> "
                f"{relationship.parent_table}"
                f"({', '.join(relationship.parent_columns)}) endpoints and keys "
                "are not stated together in one passage"
            )
            continue
        if not any(wanted in vocab and opposite not in vocab for vocab in candidates):
            problems.append(
                # Include keys so the correction identifies the exact
                # relationship passage, including leading-underscore names.
                f"relationship {index}: {relationship.child_table}"
                f"({', '.join(relationship.child_columns)}) -> "
                f"{relationship.parent_table}"
                f"({', '.join(relationship.parent_columns)}) must be stated "
                f"as {wanted!r} (and not {opposite!r}) in its endpoint/key "
                "passage"
            )
    return problems


def _structured_terms(text: str) -> tuple[str, ...]:
    """Semantic tokens in a predicate/detail value, including numeric values."""
    # Implementation vocabulary is translated to declarative outcomes and is
    # already checked through the operation description + closed equivalents.
    # Requiring these literal spellings would contradict the no-recipe gate.
    mechanics = frozenset(
        {
            "and", "or", "not", "null", "is", "in", "as",
            "select", "from", "where", "having", "group_by", "order_by",
            "join", "left", "right", "inner", "outer", "cross",
            "count", "sum", "avg", "min", "max", "distinct", "coalesce",
            "cast", "nullif", "case", "when", "then", "else", "end",
        }
    )
    seen: dict[str, None] = {}
    for token in _TOKEN_RE.findall(text.lower()):
        if token in _STOPWORDS or token in mechanics:
            continue
        if len(token) < 3 and not token.isdigit():
            continue
        seen.setdefault(token, None)
    return tuple(seen)


def _structured_op_requirements(
    op,
    *,
    source_tables: frozenset[str],
    public_columns: frozenset[str],
    step: Mapping[str, Any] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return public identifiers and semantic terms required by structured mart operations.

    Only solver-visible operation fields contribute requirements. Compiler-only SQL
    fragments remain private, and empty optional fields add no requirement.
    """
    exact: dict[str, None] = {}
    semantic: dict[str, None] = {}
    #: The subset of `semantic` that is a literal specification value (or a
    #: join side): exact requirements, never interchangeable wording.
    literal_terms: dict[str, None] = {}
    for value in op.tables:
        if value.lower() in source_tables:
            exact.setdefault(value.lower(), None)
    for value in op.columns:
        if value.lower() in public_columns:
            exact.setdefault(value.lower(), None)
    detail_values: list[str] = []
    quoted_values: tuple[str, ...] = ()
    if step is not None:
        condition = step.get("condition") or {}
        for value in condition.get("public_identifiers") or ():
            if str(value).lower() in source_tables | public_columns:
                exact.setdefault(str(value).lower(), None)
        # A projected literal is the whole quoted value of the predicate.
        quoted_values = tuple(str(v) for v in (condition.get("literal_values") or ()))
        parameters = step.get("semantic_parameters") or {}
        detail_values = [str(parameters[key]) for key in sorted(parameters)]
        candidates: tuple[str, ...] = (*quoted_values, *detail_values)
    else:
        for key, value in op.details.items():
            if key not in _PUBLIC_DETAIL_KEYS:
                continue
            if key == "tie_break":
                # Match the public projector: compiler aliases are neither shown
                # nor required; tie-breaks are public only when every identifier
                # is a declared source/output column or source table.
                names = {
                    name.casefold()
                    for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", value)
                }
                if not names.issubset(source_tables | public_columns):
                    continue
            detail_values.append(value)
        candidates = (op.predicate, *detail_values)
    for value in candidates:
        for term in _structured_terms(value):
            if term in source_tables or term in public_columns:
                exact.setdefault(term, None)
            elif term.isdigit():
                exact.setdefault(term, None)
            elif "_" not in term or value in quoted_values or any(
                term in literal
                for literal in re.findall(r"'([^']+)'", value.lower())
            ) or value.strip().casefold() == term:
                semantic.setdefault(term, None)
                if value in quoted_values:
                    # A LITERAL SPECIFICATION VALUE IS THE REQUIREMENT, not a
                    # phrasing of it: 'qualified' cannot be satisfied by
                    # saying 'rejected'. These stay mandatory even though the
                    # restating parameters are pooled into one coverage bar.
                    literal_terms.setdefault(term, None)
    if op.join_type is not None:
        semantic.setdefault(op.join_type.value.lower(), None)
        literal_terms.setdefault(op.join_type.value.lower(), None)
    return tuple(exact), tuple(semantic), tuple(literal_terms)


#: Words and marks that make a passage a RELATIONSHIP statement rather than
#: two table names that happen to sit together.
_RELATIONSHIP_MARKERS: frozenset[str] = frozenset(
    {"relationship", "relationships", "refers", "refer", "references",
     "reference", "referencing", "fk", "foreign"}
)


def _states_a_relationship(passage: str, vocab: frozenset[str]) -> bool:
    """Is this passage a relationship statement? Either it uses one of the
    marker words, or it draws the endpoint arrow an author's list form uses
    (``child (key) -> parent (key): required``)."""
    if _RELATIONSHIP_MARKERS & vocab:
        return True
    return "->" in passage or "→" in passage


def _mart_sections(
    marts: tuple[MartSpec, ...], prose: str
) -> tuple[dict[str, str], list[str]]:
    """Parse the labelled prose section for each mart.

    Coverage stays mart-local. Recognize supported heading forms without treating
    ordinary sentences as section headers.
    """

    names = {mart.name.casefold(): mart.name for mart in marts}
    hits: dict[str, list[int]] = {mart.name: [] for mart in marts}
    #: The subset of `hits` that reads as a LABEL rather than a sentence; a
    #: mart with at least one is judged on those alone.
    labels: dict[str, list[int]] = {mart.name: [] for mart in marts}
    lines = prose.splitlines()
    for index, line in enumerate(lines):
        folded = line.casefold()
        tokens = set(_TOKEN_RE.findall(folded))
        named = [canonical for key, canonical in names.items() if key in tokens]
        if len(named) != 1:
            continue
        stripped = folded.lstrip()
        decorated = stripped.startswith("#") or stripped.startswith("=")
        undecorated = stripped.lstrip("#=*_- ")
        starts_with_mart_label = re.match(
            r"^mart(?:\s+\d+)?(?:\s*[:\-—]|\s+[a-z0-9_])",
            undecorated,
        ) is not None
        if decorated or starts_with_mart_label:
            # Treat long, sentence-like undecorated lines as prose rather than
            # headers; decorated headings remain candidates.
            sentence_like = (
                not decorated
                and stripped.rstrip().endswith((".", "!", "?"))
                and len(_TOKEN_RE.findall(folded)) >= 8
            )
            hits[named[0]].append(index)
            if not sentence_like:
                labels[named[0]].append(index)

    problems: list[str] = []
    headers: list[tuple[int, str]] = []
    for mart in marts:
        locations = labels[mart.name] or hits[mart.name]
        if not locations:
            problems.append(
                f"mart {mart.name!r}: clearly labelled mart section not found"
            )
            continue
        if len(locations) > 1:
            problems.append(
                f"mart {mart.name!r}: multiple labelled mart sections found"
            )
        headers.append((locations[0], mart.name))

    headers.sort()
    sections: dict[str, str] = {}
    for position, (start, name) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        sections[name] = "\n".join(lines[start:end]).strip()
    return sections, problems


def _check_mart(
    mart: MartSpec,
    prose: str,
    *,
    source_tables: frozenset[str],
    source_columns: frozenset[str],
    task: TaskIR | None = None,
) -> list[str]:
    problems: list[str] = []
    mart_label = f"mart {mart.name!r}"
    # The rule-level structured requirements are read off the SHARED public
    # projection the author view renders (`solver_safe_plan_requirements`),
    # never off the raw predicate: the gate demands what the view shows.
    steps: list[Mapping[str, Any]] = []
    if task is not None:
        projected = solver_safe_plan_requirements(task, mart).get("steps")
        if isinstance(projected, list) and len(projected) == len(mart.plan.ops):
            steps = [dict(step) for step in projected]

    if not _present(mart.name.lower(), prose):
        problems.append(f"{mart_label}: mart name never mentioned in prose")
        # Everything below would cascade; still run it so ALL gaps are named.

    description_terms = _significant_terms(mart.description)
    if description_terms:
        # `_mart_sections` guarantees the first line is the mart's labelled
        # header.  Bind the description to that declaration: grain/rule prose
        # later in the same section must not accidentally re-supply its words.
        declaration = prose.splitlines()[0] if prose.splitlines() else ""
        declaration_vocab = _vocabulary(declaration)
        best_description_missing = [
            term for term in description_terms
            if not _satisfied(term, declaration_vocab)
        ]
        if (
            len(description_terms) - len(best_description_missing)
        ) / len(description_terms) < RULE_TERM_COVERAGE:
            problems.append(
                f"{mart_label}: description {mart.description!r} not represented "
                "in one passage (closest passage is missing: "
                f"{', '.join(best_description_missing)})"
            )

    grain_terms = _significant_terms(mart.grain)
    missing_grain = [t for t in grain_terms if not _grain_term_present(t, prose)]
    if missing_grain:
        problems.append(
            f"{mart_label}: grain {mart.grain!r} not represented "
            f"(missing terms: {', '.join(missing_grain)})"
        )

    for key in mart.key_columns:
        if not _present(key.lower(), prose):
            problems.append(f"{mart_label}: key column {key!r} never mentioned")

    for column in mart.columns:
        name = column.name.lower()
        if not _present(name, prose):
            problems.append(
                f"{mart_label}: output column {column.name!r} never mentioned"
            )
            continue
        name_parts = set(name.split("_")) | {name}
        desc_terms = tuple(
            t for t in _significant_terms(column.description) if t not in name_parts
        )
        problem = _missing_fraction_problems(
            f"{mart_label}: output column {column.name!r} description "
            f"substance not represented",
            desc_terms,
            prose,
            COLUMN_DESC_COVERAGE,
        )
        if problem:
            problems.append(problem)

    windows = _windows(prose, RULE_SENTENCE_WINDOW)
    for index, op in enumerate(mart.plan.ops, start=1):
        rule_label = (
            f"{mart_label}: rule {index} [{op.kind.value}] "
            f"({op.description!r})"
        )
        terms = _significant_terms(op.description)
        public_columns = source_columns | frozenset(
            column.name.lower() for column in mart.columns
        )
        structured_identifiers, structured_semantics, structured_literals = _structured_op_requirements(
            op,
            source_tables=source_tables,
            public_columns=public_columns,
            step=steps[index - 1] if steps else None,
        )
        identifiers = tuple(
            dict.fromkeys(
                (*tuple(t for t in terms if "_" in t), *structured_identifiers)
            )
        )
        general = tuple(
            dict.fromkeys(
                (*tuple(t for t in terms if "_" not in t), *structured_semantics)
            )
        )
        missing_ids = [t for t in identifiers if not _present(t, prose)]
        if missing_ids:
            problems.append(
                f"{rule_label} not represented "
                f"(missing identifiers: {', '.join(missing_ids)})"
            )
            continue
        # LOCALITY: the rule must be represented by ONE passage, never
        # assembled from vocabulary scattered across the document (`_windows`).
        best_missing: list[str] | None = None
        polarity: str | None = None
        contradiction: str | None = None
        for passage, vocab in windows:
            if not all(t in vocab for t in identifiers):
                continue
            conflict = _polarity_problem(general, vocab) or _negation_problem(
                op.kind, passage, general
            )
            if conflict is not None:
                # A passage CONTRADICTING the rule outranks one that merely
                # under-covers it, so it is the problem reported.
                if "mutually exclusive" in conflict or "negation" in conflict:
                    contradiction = contradiction or conflict
                polarity = polarity or conflict
                continue
            # Pool the description with structured semantic terms in one coverage
            # bar; structured literals and every rule identifier remain mandatory.
            missing_literals = [
                term for term in structured_literals if not _satisfied(term, vocab)
            ]
            if missing_literals:
                if best_missing is None or len(missing_literals) < len(best_missing):
                    best_missing = missing_literals
                continue
            combined = tuple(dict.fromkeys((*general, *structured_semantics)))
            missing = [t for t in combined if not _satisfied(t, vocab)]
            if not combined or (len(combined) - len(missing)) / len(combined) >= (
                RULE_TERM_COVERAGE
            ):
                best_missing = []
                polarity = None
                contradiction = None
                break
            if best_missing is None or len(missing) < len(best_missing):
                best_missing = missing
        if best_missing == []:
            continue  # represented by one passage, with no contradiction
        if contradiction is not None:
            problems.append(
                f"{rule_label} not represented: a passage naming it "
                f"{contradiction}"
            )
        elif best_missing is None and polarity is not None:
            problems.append(
                f"{rule_label} not represented: every passage naming it "
                f"{polarity}"
            )
        elif best_missing is None:
            problems.append(
                f"{rule_label} not represented: no single passage states it "
                f"(its identifiers {list(identifiers)} never co-occur)"
            )
        elif best_missing:
            problems.append(
                f"{rule_label} not represented in any single passage "
                f"(closest passage is missing: {', '.join(best_missing)})"
            )

    return problems


# Parse fixed diagnostics into a code-only public locus (mart, item or rule kind,
# and named identifiers) without reading the task or adding new text.

#: The closed vocabulary of item kinds a prose-fidelity problem can point at.
PROSE_ITEM_KINDS: tuple[str, ...] = (
    "empty",
    "source_backend",
    "relationship",
    "mart_section",
    "mart_name",
    "description",
    "grain",
    "key_column",
    "output_column",
    "column_description",
    "rule",
    "operator",
    "other",
)

_LOCUS_MART_RE = re.compile(r"^mart '([^']+)': ")
_LOCUS_SOURCE_RE = re.compile(r"^source table '([^']+)': backend '([^']+)' ")
_LOCUS_RELATIONSHIP_RE = re.compile(
    r"^relationship (\d+): ([A-Za-z0-9_.\-]+)(?:\(([^)]*)\))? -> "
    r"([A-Za-z0-9_.\-]+)(?:\(([^)]*)\))?"
)
_LOCUS_RULE_RE = re.compile(r"^rule (\d+) \[([a-z_]+)\] ")
_LOCUS_KEY_RE = re.compile(r"^key column '([^']+)' never mentioned")
_LOCUS_COLUMN_RE = re.compile(r"^output column '([^']+)' (never mentioned|description substance)")
_LOCUS_OPERATOR_RE = re.compile(
    r"^([a-z\-]+): prose states the SQL mechanics (?:'[^']*'|\"[^\"]*\")(?: on '([^']+)')?"
)
_LOCUS_MISSING_RE = re.compile(
    r"\((?:closest passage is )?missing(?: terms| identifiers)?: ([^)]*)\)"
    r"|its identifiers \[([^\]]*)\] never co-occur"
)


class ProblemLocus:
    """Identify the mart and rule component named by one prose-fidelity problem.

    Fields capture the item kind, operation and ordinal, operator category, implicated
    column, and identifier-like tokens. The projector later filters identifiers to the
    public set.
    """

    __slots__ = ("mart", "item", "op_kind", "rule", "category", "column", "identifiers")

    def __init__(
        self,
        *,
        mart: str = "",
        item: str,
        op_kind: str = "",
        rule: int | None = None,
        category: str = "",
        column: str = "",
        identifiers: tuple[str, ...] = (),
    ) -> None:
        if item not in PROSE_ITEM_KINDS:
            raise ValueError(f"unknown prose item kind {item!r}")
        self.mart = mart
        self.item = item
        self.op_kind = op_kind
        self.rule = rule
        self.category = category
        self.column = column
        self.identifiers = tuple(identifiers)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ProblemLocus(mart={self.mart!r}, item={self.item!r}, op_kind={self.op_kind!r}, "
            f"rule={self.rule!r}, category={self.category!r}, column={self.column!r}, "
            f"identifiers={self.identifiers!r})"
        )


def _listed_identifiers(sentence: str) -> tuple[str, ...]:
    """The identifier-like tokens a sentence LISTS (missing terms, missing
    identifiers, never co-occurring identifiers), in order, deduplicated."""
    out: dict[str, None] = {}
    for match in _LOCUS_MISSING_RE.finditer(sentence):
        body = match.group(1) or match.group(2) or ""
        for token in re.findall(r"[A-Za-z0-9_]+", body):
            if not token.isdigit():
                out.setdefault(token, None)
    return tuple(out)


def problem_locus(problem: str) -> ProblemLocus:
    """Parse a prose-fidelity problem into a `ProblemLocus`.

    Recognize all current operator, source, relationship, section, description, grain,
    key, output, and rule templates. Unknown future templates remain red as `other`,
    scoped to a named mart when possible.
    """
    sentence = " ".join(str(problem).split())
    operator = _LOCUS_OPERATOR_RE.match(sentence)
    if operator is not None:
        return ProblemLocus(
            item="operator",
            category=operator.group(1),
            column=operator.group(2) or "",
            identifiers=(operator.group(2),) if operator.group(2) else (),
        )
    source = _LOCUS_SOURCE_RE.match(sentence)
    if source is not None:
        return ProblemLocus(
            item="source_backend", identifiers=(source.group(1), source.group(2))
        )
    relationship = _LOCUS_RELATIONSHIP_RE.match(sentence)
    if relationship is not None:
        names: dict[str, None] = {relationship.group(2): None}
        for group in (relationship.group(3), relationship.group(5)):
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", group or ""):
                names.setdefault(token, None)
        names.setdefault(relationship.group(4), None)
        return ProblemLocus(
            item="relationship", rule=int(relationship.group(1)), identifiers=tuple(names)
        )
    mart_match = _LOCUS_MART_RE.match(sentence)
    if mart_match is None:
        return ProblemLocus(item="other", identifiers=_listed_identifiers(sentence))
    mart = mart_match.group(1)
    rest = sentence[mart_match.end():]
    listed = _listed_identifiers(rest)
    if rest.startswith("solver prose is empty"):
        return ProblemLocus(mart=mart, item="empty")
    if "labelled mart section" in rest:
        return ProblemLocus(mart=mart, item="mart_section")
    if rest.startswith("mart name never mentioned"):
        return ProblemLocus(mart=mart, item="mart_name")
    if rest.startswith("description "):
        return ProblemLocus(mart=mart, item="description", identifiers=listed)
    if rest.startswith("grain "):
        return ProblemLocus(mart=mart, item="grain", identifiers=listed)
    key = _LOCUS_KEY_RE.match(rest)
    if key is not None:
        return ProblemLocus(mart=mart, item="key_column", column=key.group(1), identifiers=(key.group(1),))
    column = _LOCUS_COLUMN_RE.match(rest)
    if column is not None:
        item = "output_column" if column.group(2) == "never mentioned" else "column_description"
        return ProblemLocus(
            mart=mart, item=item, column=column.group(1),
            identifiers=(column.group(1), *listed),
        )
    rule = _LOCUS_RULE_RE.match(rest)
    if rule is not None:
        return ProblemLocus(
            mart=mart, item="rule", op_kind=rule.group(2), rule=int(rule.group(1)),
            identifiers=listed,
        )
    return ProblemLocus(mart=mart, item="other", identifiers=listed)


def check_prose_fidelity(task: TaskIR) -> list[str]:
    """Name every unrepresented public TaskIR/MartSpec requirement.

    Empty prose fails against every item (fail closed, never a pass by
    vacuity). The two halves are ONE call: completeness alone is satisfiable
    by pasting operator vocabulary, declarativeness alone by saying nothing.
    """
    prose = _normalize(task.solver_prompt)
    if not prose:
        return [
            f"mart {m.name!r}: solver prose is empty — nothing is represented"
            for m in task.marts
        ]
    problems = _check_source_contract(task, prose)
    sections, section_problems = _mart_sections(task.marts, prose)
    problems.extend(section_problems)
    source_tables = frozenset(table.name.lower() for table in task.tables)
    source_columns = frozenset(
        column.name.lower() for table in task.tables for column in table.columns
    )
    for mart in task.marts:
        problems.extend(
            _check_mart(
                mart,
                sections.get(mart.name, ""),
                source_tables=source_tables,
                source_columns=source_columns,
                task=task,
            )
        )
    problems.extend(declarative_prose.check_declarative_prose(task))
    return problems


def prose_fidelity_gate(task: TaskIR) -> GateResult:
    """GateResult view of the check (red iff any item is missing)."""
    problems = check_prose_fidelity(task)
    return GateResult(
        gate=GATE_NAME,
        passed=not problems,
        details=(
            "authored prose represents every source table/backend pair, "
            "relationship endpoint/key/requiredness, mart description, grain, "
            "key/output column, and public structured plan requirement in "
            "declarative outcomes rather than SQL operators"
            if not problems
            else "; ".join(problems)
        ),
        evidence={
            "problem_count": str(len(problems)),
            "marts": ",".join(m.name for m in task.marts),
        },
    )

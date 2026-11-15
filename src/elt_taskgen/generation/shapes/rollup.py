"""Star, projection, and rollup builders over the shared plan vocabulary."""

from __future__ import annotations
from dataclasses import replace

from elt_taskgen.models import JoinType, MartColumn, MartColumnKind, MartOp, MartOpKind, MartPlan
from elt_taskgen.generation.plan_validation import (
    MartBudgetError,
    ShapeNotSelectable,
    _aggregate_can_be_null,
    _zero_substituted_inputs,
    guard_conditions,
    is_constant_divisor_expr,
    is_distinct_aggregate_expr,
    is_filtered_aggregate_expr,
    op_problems,
    predicate_governs,
)
from elt_taskgen.generation.plan_ops import (
    BuiltPlan,
    Derived,
    Extremum,
    FactRoles,
    KeyColumn,
    Measure,
    Passthrough,
    StarJoin,
    StarShape,
    WindowExpr,
    _QUOTED_NAME_RE,
    _article,
    _dataset_rename_map,
    _fmt,
    _ladder_boundary_statement,
    _measure_alias,
    _outcome_sentence,
    _renamed_text,
    _source_op_description,
    _unique,
    _witness_problems,
    budget_problems,
    distinct_source_problems,
    extremum_default,
    kind_certification_problems,
    quote,
    relation_identifier,
    wrong_window_mirror_problem,
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

    # Name the mart's columns after THIS chain before anything reads them, so
    # the ops, the compiled SQL, the gold header and every description agree.
    # A template name is one string in the plan's namespace: it names the mart
    # column, the relation column the ops carry it through, and the name the
    # descriptions state, so the rename rewrites all three together.
    renamed_columns = _dataset_rename_map(
        keys=keys,
        passthrough=passthrough,
        measures=measures,
        extrema=extrema,
        post_windows=post_windows,
        derived=derived,
        parent=parent,
        hops=hops,
        windows=windows,
        roles=roles,
        child=child,
    )
    if renamed_columns:

        def _named(column: str) -> str:
            return renamed_columns.get(column, column)

        def _says(text: str) -> str:
            return _renamed_text(text, renamed_columns)

        keys = tuple(
            replace(
                k,
                column=_named(k.column),
                expr=_says(k.expr),
                description=_says(k.description),
            )
            for k in keys
        )
        passthrough = tuple(
            replace(p, column=_named(p.column), description=_says(p.description))
            for p in passthrough
        )
        measures = tuple(
            replace(
                m,
                column=_named(m.column),
                expr=_says(m.expr),
                description=_says(m.description),
            )
            for m in measures
        )
        extrema = tuple(
            replace(x, column=_named(x.column), description=_says(x.description))
            for x in extrema
        )
        windows = tuple(
            replace(
                w, alias=_named(w.alias), expr=_says(w.expr),
                description=_says(w.description),
            )
            for w in windows
        )
        post_windows = tuple(
            replace(
                w, alias=_named(w.alias), expr=_says(w.expr),
                description=_says(w.description),
            )
            for w in post_windows
        )
        derived = tuple(
            replace(
                d,
                column=_named(d.column),
                # `expr` names mart columns in braces; the braces move with them.
                expr=_says(d.expr),
                description=_says(d.description),
            )
            for d in derived
        )
        # A hop joins ON the base relation's key column and carries its columns
        # in under aliases the measures read; both live in the renamed namespace,
        # while the right-hand side of a pair is the source table's own column.
        hops = tuple(
            replace(
                hop,
                on_pairs=tuple((_named(left), right) for left, right in hop.on_pairs),
                carry=tuple((source, _named(alias)) for source, alias in hop.carry),
                rel_columns=tuple(_named(column) for column in hop.rel_columns),
                description=_says(hop.description),
            )
            for hop in hops
        )
        parent_carry = tuple(
            (source, _named(alias)) for source, alias in parent_carry
        )
        extrema_order_by = _says(extrema_order_by)
        extrema_tie_break = _says(extrema_tie_break)
        extrema_tie_break_prose = _says(extrema_tie_break_prose)
        aggregate_predicate = _says(aggregate_predicate)
        grain_description = _says(grain_description)
        notes = _says(notes)

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
        renamed_columns=tuple(sorted(renamed_columns.items())),
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

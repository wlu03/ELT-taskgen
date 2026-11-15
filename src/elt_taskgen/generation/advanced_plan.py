"""Temporal alignment, cohort denominators, and conserving allocations."""
from dataclasses import asdict

from elt_taskgen.generation.advanced_evidence import ASOF, COHORT, ALLOCATION
from elt_taskgen.generation.mart_plan import column_kinds_from_plan, detect_semantic_patterns, quote as q
from elt_taskgen.generation.recipe_plan import PlanBuilder
from elt_taskgen.models import (ColumnType, JoinType, MartColumn, MartColumnKind, MartOpKind,
                                MartPlan, MartSpec, SemanticPattern, canonical_json)

PATTERNS = {ASOF: SemanticPattern.SCD_ASOF, COHORT: SemanticPattern.COHORT_RETENTION,
            ALLOCATION: SemanticPattern.WEIGHTED_ALLOCATION}


def advanced_variants(e, recipe):
    return (*{
        ASOF: ("latest_for_all", "inclusive_end", "exclusive_start", "reverse_tie", "inner_match", "equality_instead_of_asof",
               *(("ignore_explicit_end", "missing_overlap_tiebreak") if e.valid_to else ())),
        COHORT: ("count_events", "active_denominator", "thirty_day_month", "before_signup", "drop_empty_months"),
        ALLOCATION: ("global_denominator", "equal_weights", "absolute_weights", "round_share_first", "repeat_total", "drop_zero_weights"),
    }[recipe], *(f"missing_key_{i}" for i in range(len(e.keys)) if len(e.keys) > 1))


def _projection(source, columns):
    return ", ".join(f'{q(source)}.{q(c)} AS {q(c)}' for c in columns)


def _asof(b, e, variant):
    if e.valid_to:
        return _native_asof(b,e,variant)
    grain = tuple(f"g_{i}" for i in range(len(e.keys)))
    ids = tuple(f"event_key_{i}" for i in range(len(e.row_keys)))
    versions = tuple(f"version_key_{i}" for i in range(len(e.history_keys)))
    events = b.project(e.fact, ", ".join([
        *(f'{q(c)} AS {q(g)}' for c, g in zip(e.links, grain, strict=True)),
        *(f'{q(c)} AS {q(g)}' for c, g in zip(e.row_keys, ids, strict=True)),
        f'CAST({q(e.event_time)} AS TIMESTAMP) AS event_at']), "dated_events")
    history = b.project(e.history, ", ".join([
        *(f'{q(c)} AS {q(g)}' for c, g in zip(e.history_links, grain, strict=True)),
        *(f'{q(c)} AS {q(g)}' for c, g in zip(e.history_keys, versions, strict=True)),
        f'CAST({q(e.valid_from)} AS TIMESTAMP) AS effective_from', f'{q(e.attribute)} AS historical_value']), "version_records")
    known = b.filter(history, "effective_from IS NOT NULL", "dated_versions")
    direction = "ASC" if variant == "reverse_tie" else "DESC"
    partition = ", ".join((*grain, "effective_from"))
    ordering = ", ".join(f"{v} {direction} NULLS LAST" for v in versions)
    ranked = b.add(MartOpKind.WINDOW, (known,), "ordered_version_starts",
        select=f"*, ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY {ordering}) AS version_rank",
        details={"partition_by": partition, "order_by": ordering})
    unique = b.filter(ranked, "version_rank = 1", "unique_version_starts")
    intervals = b.add(MartOpKind.WINDOW, (unique,), "validity_intervals",
        select=f'*, LEAD(effective_from) OVER (PARTITION BY {", ".join(grain)} ORDER BY effective_from ASC NULLS LAST) AS effective_to',
        details={"partition_by": ", ".join(grain), "order_by": "effective_from ASC NULLS LAST"})
    if variant == "latest_for_all":
        intervals = b.add(MartOpKind.EXTREMA, (unique,), "latest_version", select="*",
            details={"partition_by": ", ".join(grain), "order_by": "effective_from DESC NULLS LAST, " + ordering, "tie_break": ", ".join(versions)})
    predicates = [f'{q(events)}.{q(g)} = {q(intervals)}.{q(g)}' for i, g in enumerate(grain) if variant != f"missing_key_{i}"]
    if variant != "latest_for_all":
        lower = ">" if variant == "exclusive_start" else "=" if variant == "equality_instead_of_asof" else ">="
        upper = "<=" if variant == "inclusive_end" else "<"
        predicates += [f'{q(events)}.event_at {lower} {q(intervals)}.effective_from',
                       f'({q(events)}.event_at {upper} {q(intervals)}.effective_to OR {q(intervals)}.effective_to IS NULL)']
    current = b.add(MartOpKind.JOIN, (events, intervals), "historically_enriched",
        select=_projection(events, ids) + f', {q(intervals)}.historical_value AS historical_value, {q(intervals)}.effective_from AS matched_from',
        join_type=JoinType.INNER if variant == "inner_match" else JoinType.LEFT, predicate=" AND ".join(predicates))
    b.project(current, ", ".join([*(f'{q(i)} AS {q(k)}' for i, k in zip(ids, e.row_keys, strict=True)),
                                  "historical_value", "matched_from"]), "mart_output")


def _native_asof(b,e,variant):
    grain=tuple(f"g_{i}" for i in range(len(e.keys)))
    ids=tuple(f"event_key_{i}" for i in range(len(e.row_keys)))
    versions=tuple(f"version_key_{i}" for i in range(len(e.history_keys)))
    events=b.project(e.fact,", ".join([
        *(f'{q(c)} AS {q(g)}' for c,g in zip(e.links,grain)),
        *(f'{q(c)} AS {q(g)}' for c,g in zip(e.row_keys,ids)),
        f'CAST({q(e.event_time)} AS TIMESTAMP) AS event_at']),"dated_events")
    history=b.project(e.history,", ".join([
        *(f'{q(c)} AS {q(g)}' for c,g in zip(e.history_links,grain)),
        *(f'{q(c)} AS {q(g)}' for c,g in zip(e.history_keys,versions)),
        f'CAST({q(e.valid_from)} AS TIMESTAMP) AS effective_from',
        f'CAST({q(e.valid_to)} AS TIMESTAMP) AS effective_to',f'{q(e.attribute)} AS historical_value']),"version_records")
    history=b.filter(history,"effective_from IS NOT NULL AND (effective_to IS NULL OR effective_to > effective_from)","valid_intervals")
    direction="ASC" if variant=="reverse_tie" else "DESC"
    ordering="effective_from DESC NULLS LAST, "+", ".join(f"{v} {direction} NULLS LAST" for v in versions)
    if variant=="latest_for_all":
        history=b.add(MartOpKind.EXTREMA,(history,),"latest_version",select="*",details={
            "partition_by":", ".join(grain),"order_by":ordering,"tie_break":", ".join(versions)})
    predicates=[f'{q(events)}.{q(g)} = {q(history)}.{q(g)}' for i,g in enumerate(grain) if variant!=f"missing_key_{i}"]
    if variant!="latest_for_all":
        lower=">" if variant=="exclusive_start" else "=" if variant=="equality_instead_of_asof" else ">="
        upper="<=" if variant=="inclusive_end" else "<"
        predicates.append(f'{q(events)}.event_at {lower} {q(history)}.effective_from')
        if variant!="ignore_explicit_end":
            predicates.append(f'({q(events)}.event_at {upper} {q(history)}.effective_to OR {q(history)}.effective_to IS NULL)')
    current=b.add(MartOpKind.JOIN,(events,history),"eligible_history",select=_projection(events,ids)+", "+
        _projection(history,(*versions,"effective_from","historical_value")),
        join_type=JoinType.INNER if variant=="inner_match" else JoinType.LEFT,predicate=" AND ".join(predicates))
    # Resolve overlaps AFTER interval filtering. A same-start row with an
    # expired end cannot suppress a still-valid row with the same start.
    if variant!="missing_overlap_tiebreak":
        current=b.add(MartOpKind.EXTREMA,(current,),"selected_history",select="*",details={
            "partition_by":", ".join(ids),"order_by":ordering,"tie_break":", ".join(versions)})
    b.project(current,", ".join([*(f'{q(i)} AS {q(k)}' for i,k in zip(ids,e.row_keys)),
        "historical_value","effective_from AS matched_from"]),"mart_output")


def _cohort(b, e, variant):
    grain = tuple(f"g_{i}" for i in range(len(e.keys)))
    parents = b.project(e.parent, ", ".join([*(f'{q(k)} AS {q(g)}' for k, g in zip(e.keys, grain, strict=True)),
        f'CAST({q(e.parent_time)} AS TIMESTAMP) AS joined_at',
        f'CAST(DATE_TRUNC(\'month\', {q(e.parent_time)}) AS DATE) AS cohort_month']), "cohort_members")
    parents = b.filter(parents, "joined_at IS NOT NULL", "dated_members")
    facts = b.project(e.fact, ", ".join([*(f'{q(k)} AS {q(g)}' for k, g in zip(e.links, grain, strict=True)),
                                        f'CAST({q(e.event_time)} AS TIMESTAMP) AS event_at']), "activity_dates")
    keys = tuple(g for i, g in enumerate(grain) if variant != f"missing_key_{i}")
    joined = b.join(parents, facts, keys, ("event_at",), "member_activity", kind=JoinType.INNER)
    valid = b.filter(joined, "event_at IS NOT NULL" if variant == "before_signup" else "event_at >= joined_at", "eligible_activity")
    offset = ("CAST(FLOOR(DATE_DIFF('day', joined_at, event_at) / 30.0) AS INTEGER)" if variant == "thirty_day_month" else
              "CAST((EXTRACT(YEAR FROM event_at) - EXTRACT(YEAR FROM joined_at)) * 12 + EXTRACT(MONTH FROM event_at) - EXTRACT(MONTH FROM joined_at) AS INTEGER)")
    indexed = b.project(valid, f"*, {offset} AS month_index", "calendar_activity")
    in_window = b.filter(indexed, "month_index BETWEEN 0 AND 2", "first_three_months")
    distinct = b.aggregate(in_window, (*grain, "cohort_month", "month_index"), {"events_count": "COUNT(*)"}, "active_member_months")
    active = b.aggregate(in_window if variant == "count_events" else distinct, ("cohort_month", "month_index"),
                         {"active_members": "COUNT(*)"}, "cohort_activity")
    scope = parents
    if variant == "active_denominator":
        scope = b.aggregate(in_window, (*grain, "cohort_month"), {"event_count": "COUNT(*)"}, "ever_active_members")
    cohorts = b.aggregate(scope, ("cohort_month",), {"cohort_size": "COUNT(*)"}, "complete_cohort_sizes")
    branches = [b.project(cohorts, f"*, {i} AS month_index", f"month_{i}") for i in range(3)]
    grid = branches[0]
    for branch in branches[1:]:
        grid = b.add(MartOpKind.UNION, (grid, branch), "cohort_grid", details={"mode": "all"})
    current = b.join(grid, active, ("cohort_month", "month_index"), ("active_members",), "retention_matrix",
                     kind=JoinType.INNER if variant == "drop_empty_months" else JoinType.LEFT)
    b.project(current, "cohort_month, month_index, cohort_size, COALESCE(active_members, 0) AS active_members, "
              "ROUND(CAST(COALESCE(active_members, 0) AS DOUBLE) / NULLIF(cohort_size, 0), 4) AS retention_fraction", "mart_output")


def _allocation(b, e, variant):
    grain = tuple(f"g_{i}" for i in range(len(e.keys)))
    ids = tuple(f"line_key_{i}" for i in range(len(e.row_keys)))
    parents = b.project(e.parent, ", ".join([*(f'{q(k)} AS {q(g)}' for k, g in zip(e.keys, grain, strict=True)),
                                            f'CAST({q(e.amount)} AS DOUBLE) AS parent_amount']), "amounts_to_allocate")
    weight = f'CASE WHEN {q(e.weight)} > 0 THEN CAST({q(e.weight)} AS DOUBLE) ELSE 0.0 END'
    if variant == "absolute_weights":
        weight = f'COALESCE(ABS(CAST({q(e.weight)} AS DOUBLE)), 0.0)'
    if variant == "equal_weights":
        weight = f'CASE WHEN {q(e.weight)} > 0 THEN 1.0 ELSE 0.0 END'
    lines = b.project(e.fact, ", ".join([*(f'{q(k)} AS {q(g)}' for k, g in zip(e.links, grain, strict=True)),
        *(f'{q(k)} AS {q(g)}' for k, g in zip(e.row_keys, ids, strict=True)), f'{weight} AS eligible_weight', "1 AS allocation_scope"]), "weighted_lines")
    totals = b.aggregate(lines, ("allocation_scope",) if variant == "global_denominator" else grain,
                         {"total_weight": "SUM(eligible_weight)"}, "weight_denominators")
    keys = tuple(g for i, g in enumerate(grain) if variant != f"missing_key_{i}")
    joined = b.join(lines, parents, keys, ("parent_amount",), "lines_with_amount")
    joined = b.join(joined, totals, ("allocation_scope",) if variant == "global_denominator" else grain,
                    ("total_weight",), "normalized_lines")
    if variant == "drop_zero_weights":
        joined = b.filter(joined, "eligible_weight > 0", "positive_lines")
    ratio = "COALESCE(eligible_weight / NULLIF(total_weight, 0), 0.0)"
    amount = f"COALESCE(parent_amount, 0.0) * {ratio}"
    if variant == "round_share_first":
        amount = f"COALESCE(parent_amount, 0.0) * ROUND({ratio}, 4)"
    if variant == "repeat_total":
        amount = "COALESCE(parent_amount, 0.0)"
    b.project(joined, ", ".join([*(f'{q(i)} AS {q(k)}' for i, k in zip(ids, e.row_keys, strict=True)),
        f'ROUND({ratio}, 4) AS weight_fraction', f'ROUND({amount}, 4) AS allocated_amount']), "mart_output")


def build_advanced_ops(task, e, recipe, variant=""):
    b = PlanBuilder(task)
    {ASOF: _asof, COHORT: _cohort, ALLOCATION: _allocation}[recipe](b, e, variant)
    return tuple(b.ops)


def advanced_plan(task, e, mart, recipe, variant=""):
    if variant and variant not in advanced_variants(e, recipe):
        raise ValueError(f"Unknown {recipe} mutation {variant}")
    keys = ("cohort_month", "month_index") if recipe == COHORT else e.row_keys
    if set(keys) & {"historical_value", "matched_from", "weight_fraction", "allocated_amount"}:
        from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
        raise SemanticDifficultyError("Source keys collide with recipe output names")
    columns = [] if recipe == COHORT else [MartColumn(name=k, type=task.table(e.fact).column(k).type,
        description=f"Unchanged {e.fact}.{k}; part of the complete source record key.") for k in keys]
    def col(name, kind, description):
        columns.append(MartColumn(name=name, type=kind, description=description))
    if recipe == ASOF:
        description = f"History of {e.parent} as recorded at each {e.fact}.{e.event_time}."
        grain = f"One row per {e.fact} ({', '.join(keys)}), including NULL dates and records with no prior history."
        validity = (f"A version applies in [{e.valid_from}, {e.valid_to}); a NULL end is open-ended. Ignore NULL starts and non-NULL ends at or before the start. Filter by validity first, then choose the latest start among matching intervals, breaking ties by the largest version key tuple. Gaps remain unmatched; overlapping intervals do not duplicate an event."
                    if e.valid_to else "A version applies from its timestamp inclusive until the next distinct timestamp exclusive; the final version has no end. This task treats history timestamps as effective starts.")
        col("historical_value", ColumnType.TEXT, f"{e.history}.{e.attribute} from the latest non-NULL {e.valid_from} at or before {e.fact}.{e.event_time}, matching all {e.links} to {e.history_links}. Equal history timestamps choose the lexicographically largest tuple {e.history_keys} under native database ordering. {validity} NULL if no match; retain NULL attributes. Use stored times without timezone conversion.")
        col("matched_from", ColumnType.TIMESTAMP, f"Timestamp of that same selected history record ({e.history}.{e.valid_from}), or NULL without a match.")
    elif recipe == COHORT:
        description = f"Calendar-month activity retention of {e.parent} cohorts through {e.fact}."
        grain = "One row per nonempty entity-creation cohort month and month_index in {0, 1, 2}, including zero-activity months."
        col("cohort_month", ColumnType.DATE, f"First calendar day of {e.parent}.{e.parent_time}'s month. Exclude entities with NULL creation dates; use stored dates without timezone conversion.")
        col("month_index", ColumnType.INTEGER, "Calendar months since cohort_month: 0, 1 and 2. December to January is 1; do not divide elapsed days by 30.")
        col("cohort_size", ColumnType.BIGINT, f"Count of ALL {e.parent} records in this cohort, including entities with no activity. The denominator is constant across the three months.")
        col("active_members", ColumnType.BIGINT, f"Distinct complete {e.parent} keys {e.keys} with at least one matching {e.fact} row in this month. Join all {e.links} to {e.keys}; ignore NULL event dates, unmatched facts, and {e.event_time} earlier than the entity's {e.parent_time}. Repeated events count once per member-month. 0 if none.")
        col("retention_fraction", ColumnType.FLOAT, "active_members divided by cohort_size as a fraction, rounded to 4 places. Inactive cohorts have 0.0 in every month.")
    else:
        description = f"Allocate each {e.parent}.{e.amount} over associated {e.fact} records in proportion to {e.weight}."
        grain = f"One row per {e.fact} ({', '.join(keys)}), including nonpositive/NULL weights and unmatched parent links."
        col("weight_fraction", ColumnType.FLOAT, f"Positive {e.weight} divided by the sum of positive {e.weight} over ALL sibling records sharing the complete {e.links} key, rounded to 4 places. NULL, zero and negative weights contribute 0. A zero denominator or any NULL parent-key component gives 0.0. Fractions are defined even for orphan keys with no {e.parent} row.")
        col("allocated_amount", ColumnType.FLOAT, f"Matching {e.parent}.{e.amount} times the UNROUNDED weight fraction, then rounded to 4 places. Join all {e.links} to {e.keys}. Missing parent or NULL amount gives 0.0; negative amounts retain their sign. With a positive denominator allocations conserve the parent amount before rounding. With no positive weights allocate 0; no equal-split fallback.")
    plan = MartPlan(mart=mart, ops=build_advanced_ops(task, e, recipe, variant), template_id=recipe,
        notes=canonical_json({"evidence": asdict(e), "source_tables": sorted(t.name for t in task.tables)}))
    plan = plan.model_copy(update={"semantic_patterns": detect_semantic_patterns(plan)})
    if not variant and PATTERNS[recipe] not in plan.semantic_patterns:
        raise ValueError(f"Uncertified advanced recipe: {recipe}")
    kinds = column_kinds_from_plan(plan)
    columns = tuple(c.model_copy(update={"kind": kinds.get(c.name, MartColumnKind.DERIVED)}) for c in columns)
    return MartSpec(name=mart, description=description, grain=grain, key_columns=keys, columns=columns, plan=plan)

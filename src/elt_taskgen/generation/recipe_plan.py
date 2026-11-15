"""Composable relational building blocks and three dependent business recipes.

All expressions pass through the existing MartOp compiler. No raw SQL plans.
"""
from dataclasses import asdict

from sqlglot import parse_one

from elt_taskgen.generation.mart_plan import column_kinds_from_plan, detect_semantic_patterns, infer_aggregate_kind, quote
from elt_taskgen.generation.recipe_evidence import AGGREGATE_RANK, ENTITY_BASELINE, CALENDAR_STREAK, RecipeEvidence
from elt_taskgen.models import (ColumnType, JoinType, MartColumn, MartColumnKind, MartOp, MartOpKind,
                                MartPlan, MartSpec, SemanticPattern, TaskIR, canonical_json)

REQUIRED_PATTERNS = {
    AGGREGATE_RANK: {SemanticPattern.AGGREGATE_THEN_FILTER, SemanticPattern.AGGREGATE_THEN_EXTREMA},
    ENTITY_BASELINE: {SemanticPattern.GROUP_GLOBAL_BASELINE},
    CALENDAR_STREAK: {SemanticPattern.AGGREGATE_THEN_FILTER, SemanticPattern.CONSECUTIVE_STREAK},
}


class PlanBuilder:
    """Name-safe, typed operations shared by recipes, with explicit grains."""
    def __init__(self, task):
        self.ops = []
        self.taken = {t.name for t in task.tables}

    def add(self, kind, sources, stem, *, select=None, columns=(), **kwargs):
        name = stem
        while name in self.taken:
            name += "_"
        self.taken.add(name)
        details = kwargs.pop("details", {})
        if select is not None:
            details["select"] = select
            columns = tuple(p.alias_or_name for p in parse_one("SELECT " + select, read="duckdb").expressions
                            if p.alias_or_name and p.alias_or_name != "*")
        self.ops.append(MartOp(kind=kind, tables=tuple(sources), columns=tuple(columns),
                              description=stem.replace("_", " "), details={"name": name, **details}, **kwargs))
        return name

    def project(self, source, select, stem, **kwargs):
        return self.add(MartOpKind.DERIVE, (source,), stem, select=select, **kwargs)

    def aggregate(self, source, grain, measures, stem):
        return self.add(infer_aggregate_kind(tuple(measures.values())), (source,), stem, columns=tuple(measures),
                        details={"group_by": ", ".join(map(quote, grain)), **measures})

    def filter(self, source, predicate, stem):
        return self.add(MartOpKind.FILTER, (source,), stem, predicate=predicate)

    def join(self, left, right, keys, carried, stem, *, kind=JoinType.LEFT):
        predicate = " AND ".join(f'{quote(left)}.{quote(k)} = {quote(right)}.{quote(k)}' for k in keys)
        select = f'{quote(left)}.*' + ''.join(f', {quote(right)}.{quote(c)} AS {quote(c)}' for c in carried)
        return self.add(MartOpKind.JOIN, (left, right), stem, select=select, join_type=kind, predicate=predicate)


def recipe_variants(e: RecipeEvidence, recipe: str) -> tuple[str, ...]:
    specific = {
        AGGREGATE_RANK: ("ignore_eligibility", "rank_by_average", "sum_distinct", "reverse_tie", "exclusive_count"),
        ENTITY_BASELINE: ("fact_mean", "exclude_inactive", "missing_zero"),
        CALENDAR_STREAK: ("observed_not_calendar", *(("count_null_measures",) if e.measure_nullable else ()), "exclusive_count", "two_year_streak"),
    }
    return (*specific[recipe], "inner_parent", *(f"missing_key_{i}" for i in range(len(e.keys)) if len(e.keys) > 1))


def recipe_plan(task: TaskIR, e: RecipeEvidence, mart: str, recipe: str, variant: str = "") -> MartSpec:
    if variant and variant not in recipe_variants(e, recipe):
        raise ValueError(f"Unknown {recipe} mutation {variant}")
    q, b = quote, PlanBuilder(task)
    grain = tuple(f"g_{i}" for i in range(len(e.keys)))
    parents = b.project(e.parent, ", ".join(f'{q(k)} AS {q(g)}' for k, g in zip(e.keys, grain, strict=True)), "entity_keys")
    expressions = [f'{q(c)} AS {q(g)}' for c, g in zip(e.links, grain, strict=True)] + [f'{q(e.measure)} AS measure_value']
    if e.group:
        expressions.append(f'{q(e.group)} AS group_value')
    if e.timestamp:
        expressions.append(f'EXTRACT(YEAR FROM {q(e.timestamp)}) AS calendar_year')
    facts = b.project(e.fact, ", ".join(expressions), "activity_records")
    join_keys = tuple(k for i, k in enumerate(grain) if variant != f"missing_key_{i}")
    join_kind = JoinType.INNER if variant == "inner_parent" else JoinType.LEFT
    outputs = []
    columns = [MartColumn(name=k, type=task.table(e.parent).column(k).type, description=f"{e.parent}.{k}; part of the complete entity key.") for k in e.keys]

    def column(name, kind, description, expression=None):
        columns.append(MartColumn(name=name, type=kind, description=description))
        outputs.append(f'{expression or q(name)} AS {q(name)}')

    if recipe == AGGREGATE_RANK:
        known = b.filter(facts, "group_value IS NOT NULL", "known_groups")
        total = "SUM(DISTINCT measure_value)" if variant == "sum_distinct" else "SUM(measure_value)"
        groups = b.aggregate(known, (*grain, "group_value"),
                             {"group_total": total, "measured_count": "COUNT(measure_value)", "group_average": "AVG(measure_value)"}, "group_totals")
        eligible = b.filter(groups, "measured_count > 3" if variant == "exclusive_count" else
                            "measured_count >= 0" if variant == "ignore_eligibility" else "measured_count >= 3", "eligible_groups")
        order = "group_average" if variant == "rank_by_average" else "group_total"
        direction = "DESC" if variant == "reverse_tie" else "ASC"
        winner = b.add(MartOpKind.EXTREMA, (eligible,), "winning_group",
                       select=", ".join((*map(q, grain), "group_value AS selected_group", "group_total AS selected_total", "measured_count AS selected_count")),
                       details={"partition_by": ", ".join(map(q, grain)),
                                "order_by": f"{order} DESC NULLS LAST, group_value {direction} NULLS LAST", "tie_break": "group_value"})
        current = b.join(parents, winner, join_keys, ("selected_group", "selected_total", "selected_count"), "entity_winner", kind=join_kind)
        column("selected_group", ColumnType.TEXT, f"{e.group} with the largest SUM({e.measure}) among this entity's groups with at least 3 non-NULL measures. Ignore NULL groups; ties use the smallest group under database text ordering. NULL if none qualify.")
        column("selected_total", ColumnType.FLOAT, f"Sum of non-NULL {e.measure} for that same selected group, counting repeated equal values separately. NULL if no group qualifies.", "CAST(selected_total AS DOUBLE)")
        column("selected_count", ColumnType.BIGINT, "Number of non-NULL measures in that same selected group. NULL if no group qualifies.")
        description = "Highest-total eligible activity group for every entity."
    elif recipe == ENTITY_BASELINE:
        totals = b.aggregate(facts, grain, {"entity_total": "SUM(measure_value)"}, "entity_totals")
        current = b.join(parents, totals, join_keys, ("entity_total",), "complete_entities", kind=join_kind)
        completed = b.project(current, ", ".join((*map(q, grain), "COALESCE(entity_total, 0) AS entity_total", "1 AS baseline_scope")), "zero_filled_entities")
        scope = completed
        if variant == "exclude_inactive":
            scope = b.project(totals, "*, 1 AS baseline_scope", "active_scope")
        if variant == "fact_mean":
            scope = b.project(facts, "*, measure_value AS entity_total, 1 AS baseline_scope", "fact_scope")
        mean = b.aggregate(scope, ("baseline_scope",), {"population_mean": "AVG(entity_total)"}, "entity_baseline")
        current = b.join(completed, mean, (), ("population_mean",), "compared_entities", kind=JoinType.CROSS)
        if variant == "missing_zero":
            # Keep the correct baseline, but expose missing totals for inactive entities.
            current = b.join(parents, totals, join_keys, ("entity_total",), "unfilled_entities")
            current = b.join(current, mean, (), ("population_mean",), "unfilled_comparison", kind=JoinType.CROSS)
        column("entity_total", ColumnType.FLOAT, f"Sum of non-NULL {e.fact}.{e.measure} for this entity; 0 for no facts or all-NULL measures. Facts without a matching entity are excluded.", "CAST(entity_total AS DOUBLE)")
        column("population_mean", ColumnType.FLOAT, f"Arithmetic mean of entity_total over ALL rows of {e.parent}, including each inactive or all-NULL entity as 0. Each entity has equal weight. No output rows when the entity table is empty.")
        column("above_population_mean", ColumnType.BOOLEAN, "True exactly when entity_total is strictly greater than population_mean. Equality is false.", "entity_total > population_mean")
        description = "Entity activity totals and their comparison with the equally weighted population mean."
    elif recipe == CALENDAR_STREAK:
        dated = b.filter(facts, "calendar_year IS NOT NULL", "dated_records")
        years = b.aggregate(dated, (*grain, "calendar_year"), {"measured_count": "COUNT(*)" if variant == "count_null_measures" else "COUNT(measure_value)"}, "year_counts")
        eligible = b.filter(years, "measured_count > 3" if variant == "exclusive_count" else "measured_count >= 3", "eligible_years")
        counts = b.aggregate(eligible, grain, {"qualified_year_count": "COUNT(*)"}, "qualified_year_counts")
        offset = 1 if variant == "two_year_streak" else 2
        lag = b.add(MartOpKind.WINDOW, (eligible,), "calendar_predecessor", select=
                    f'*, LAG(calendar_year, {offset}) OVER (PARTITION BY {", ".join(map(q, grain))} ORDER BY calendar_year ASC NULLS LAST) AS prior_year',
                    details={"partition_by": ", ".join(map(q, grain)), "order_by": "calendar_year ASC NULLS LAST"})
        predicate = "calendar_year > prior_year" if variant == "observed_not_calendar" else f"calendar_year - prior_year = {offset}"
        ends = b.filter(lag, predicate, "streak_end_years")
        summary = b.aggregate(ends, grain, {"streak_end_count": "COUNT(*)", "latest_streak_end_year": "MAX(calendar_year)"}, "streak_summary")
        current = b.join(parents, counts, join_keys, ("qualified_year_count",), "entity_years", kind=join_kind)
        current = b.join(current, summary, grain, ("streak_end_count", "latest_streak_end_year"), "entity_streaks")
        column("qualified_year_count", ColumnType.BIGINT, f"Number of calendar years containing at least 3 {e.fact} records with non-NULL {e.measure}. Use the stored date/time in {e.timestamp}, without timezone conversion. Ignore NULL dates and NULL measures.", "COALESCE(qualified_year_count, 0)")
        column("streak_end_count", ColumnType.BIGINT, "Number of years Y for which Y, Y-1, and Y-2 are all qualified years. Gaps break streaks. Overlaps count separately: 4 consecutive qualified years give 2 ends. 0 when none.", "COALESCE(streak_end_count, 0)")
        column("latest_streak_end_year", ColumnType.INTEGER, "Largest such three-year streak end year, or NULL when none exists.", "CAST(latest_streak_end_year AS INTEGER)")
        description = "Qualifying activity years and overlapping three-year calendar streaks for every entity."
    else:
        raise ValueError(recipe)
    b.project(current, ", ".join([*(f'{q(g)} AS {q(k)}' for k, g in zip(e.keys, grain, strict=True)), *outputs]), "mart_output")
    plan = MartPlan(mart=mart, ops=tuple(b.ops), template_id=recipe, notes=canonical_json(asdict(e)))
    plan = plan.model_copy(update={"semantic_patterns": detect_semantic_patterns(plan)})
    if not variant and not REQUIRED_PATTERNS[recipe] <= set(plan.semantic_patterns):
        raise ValueError(f"Recipe {recipe} did not certify its required semantic dependencies")
    kinds = column_kinds_from_plan(plan)
    columns = tuple(c.model_copy(update={"kind": kinds.get(c.name, MartColumnKind.PASSTHROUGH if c.name in e.keys else MartColumnKind.DERIVED)}) for c in columns)
    return MartSpec(name=mart, description=description,
                    grain=f"One row per {e.parent} ({', '.join(e.keys)}), including entities without qualifying activity.",
                    key_columns=e.keys, columns=columns, plan=plan)

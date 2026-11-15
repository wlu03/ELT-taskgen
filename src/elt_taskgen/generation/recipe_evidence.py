"""Typed, schema-bound inputs for dependent mart recipes.

Each candidate binds one rule to real columns. Dates are never guessed from
text, and numeric identifiers are never promoted to business measures.
"""
from dataclasses import dataclass

from elt_taskgen.generation.semantic_difficulty import MEASURE_WORDS, NUMERIC, _words
from elt_taskgen.models import ColumnType, TaskIR

AGGREGATE_RANK = "aggregate_filter_rank_v1"
ENTITY_BASELINE = "entity_total_baseline_v1"
CALENDAR_STREAK = "calendar_streak_v1"
DEPENDENT_RECIPES = (AGGREGATE_RANK, ENTITY_BASELINE, CALENDAR_STREAK)


@dataclass(frozen=True)
class RecipeEvidence:
    parent: str
    keys: tuple[str, ...]
    fact: str
    links: tuple[str, ...]
    measure: str
    measure_nullable: bool = False
    group: str = ""
    timestamp: str = ""


def recipe_candidates(task: TaskIR, recipe: str) -> tuple[RecipeEvidence, ...]:
    if recipe not in DEPENDENT_RECIPES:
        raise ValueError(f"Unknown dependent recipe {recipe!r}")
    found = set()
    for edge in task.relationships:
        parent, fact = task.table(edge.parent_table), task.table(edge.child_table)
        if parent.name == fact.name or edge.parent_columns not in (parent.primary_key, parent.business_key):
            continue
        if any(parent.column(k).enum_values or parent.column(k).type in (ColumnType.BOOLEAN, ColumnType.JSON)
               for k in edge.parent_columns):
            continue
        if any(fact.column(c).type != parent.column(k).type
               for c, k in zip(edge.child_columns, edge.parent_columns, strict=True)):
            continue
        foreign = {c for r in task.relationships if r.child_table == fact.name for c in r.child_columns}
        identity = set(fact.primary_key) | set(fact.business_key) | foreign
        measures = [c.name for c in fact.columns if c.type in NUMERIC and c.name not in identity
                    and not c.enum_values and not (_words(c.name) & {"id", "key", "code", "position", "rank", "year"})
                    and (_words(c.name) | _words(c.description)) & MEASURE_WORDS]
        groups = [c.name for c in fact.columns if c.type is ColumnType.TEXT and c.name not in identity
                  and (not c.enum_values or len(c.enum_values) >= 3)
                  and (_words(c.name) & {"category", "type", "status", "group", "segment", "label", "name", "kind", "title"}
                       or c.enum_values)]
        dates = [c.name for c in fact.columns if c.type in (ColumnType.DATE, ColumnType.TIMESTAMP)
                 and c.name not in identity]
        for measure in measures:
            for group in (groups if recipe == AGGREGATE_RANK else [""]):
                for timestamp in (dates if recipe == CALENDAR_STREAK else [""]):
                    # Witnesses need several distinct records at the parent/group/year grain.
                    if any(key and set(key) <= set(edge.child_columns) | ({group} if group else set())
                           for key in (fact.primary_key, fact.business_key)):
                        continue
                    found.add(RecipeEvidence(parent.name, edge.parent_columns, fact.name, edge.child_columns,
                                             measure, fact.column(measure).nullable, group, timestamp))
    return tuple(sorted(found, key=lambda e: (e.parent, e.fact, e.measure, e.group, e.timestamp, e.keys, e.links)))

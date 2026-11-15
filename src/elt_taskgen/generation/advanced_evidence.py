"""Conservative source capabilities for temporal and allocation recipes.

These bindings are schema evidence, not invented columns. In particular an
update timestamp is not a signup date, and a unique entity row is not history.
"""
from dataclasses import dataclass

from elt_taskgen.generation.semantic_difficulty import NUMERIC, _words
from elt_taskgen.models import ColumnType

ASOF = "historical_asof_v1"
COHORT = "cohort_retention_v1"
ALLOCATION = "weighted_allocation_v1"
ADVANCED_RECIPES = (ASOF, COHORT, ALLOCATION)
TEMPORAL = (ColumnType.DATE, ColumnType.TIMESTAMP)
CREATED = {"created", "create", "signup", "registered", "registration", "joined"}
EVENT = CREATED | {"event", "occurred", "logged", "issued", "ordered", "purchased", "paid", "date"}


@dataclass(frozen=True)
class AdvancedEvidence:
    parent: str
    keys: tuple[str, ...]
    fact: str
    links: tuple[str, ...]
    row_keys: tuple[str, ...]
    parent_time: str = ""
    event_time: str = ""
    amount: str = ""
    weight: str = ""
    history: str = ""
    history_links: tuple[str, ...] = ()
    history_keys: tuple[str, ...] = ()
    valid_from: str = ""
    attribute: str = ""
    valid_to: str = ""


def _key(table):
    key = table.primary_key or table.business_key
    return key if key and all(not table.column(k).nullable and not table.column(k).enum_values
                             and table.column(k).type in (ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.TEXT)
                             for k in key) else ()


def _identity(task, table):
    return set(table.primary_key) | set(table.business_key) | {
        c for r in task.relationships if r.child_table == table.name for c in r.child_columns}


def _dates(table, roles):
    return [c.name for c in table.columns if c.type in TEMPORAL and _words(c.name) & roles
            and not _words(c.name) & {"deleted", "expired", "birth", "updated", "modified", "end"}]


def _repeatable(table, links):
    return not any(k and set(k) <= set(links) for k in (table.primary_key, table.business_key))


def advanced_candidates(task, recipe):
    if recipe not in ADVANCED_RECIPES:
        raise ValueError(recipe)
    found = []
    for edge in task.relationships:
        p, f = task.table(edge.parent_table), task.table(edge.child_table)
        if p.name == f.name or edge.parent_columns != _key(p) or not _repeatable(f, edge.child_columns):
            continue
        if any(p.column(k).type != f.column(c).type for k, c in zip(edge.parent_columns, edge.child_columns, strict=True)):
            continue
        base = dict(parent=p.name, keys=edge.parent_columns, fact=f.name, links=edge.child_columns, row_keys=_key(f))
        if recipe == COHORT:
            # History changes are not independent activity. No numeric measure is needed.
            if _words(f.name) & {"history", "hist", "snapshot"}:
                continue
            for start in _dates(p, CREATED):
                for event in _dates(f, EVENT):
                    found.append(AdvancedEvidence(**base, parent_time=start, event_time=event))
        elif recipe == ALLOCATION and base["row_keys"]:
            amounts = [c.name for c in p.columns if c.type in NUMERIC and c.name not in _identity(task, p)
                       and not c.enum_values and _words(c.name) & {"amount", "total", "budget", "cost", "revenue"}
                       and not _words(c.name) & {"id", "rate", "unit", "percent", "count"}]
            weights = [c.name for c in f.columns if c.type in NUMERIC and c.name not in _identity(task, f)
                       and not c.enum_values and _words(c.name) & {"weight", "quantity", "qty", "units"}]
            for amount in amounts:
                for weight in weights:
                    found.append(AdvancedEvidence(**base, amount=amount, weight=weight))
        elif recipe == ASOF and base["row_keys"] and not _words(f.name) & {"history", "hist", "snapshot"}:
            for h_edge in task.relationships:
                if (h_edge.parent_table, h_edge.parent_columns) != (p.name, edge.parent_columns):
                    continue
                h = task.table(h_edge.child_table)
                if h.name in (p.name, f.name) or not _key(h) or not _repeatable(h, h_edge.child_columns):
                    continue
                if any(h.column(c).type != p.column(k).type for c, k in zip(h_edge.child_columns, edge.parent_columns, strict=True)):
                    continue
                if not _words(h.name) & {"history", "hist", "versions", "version", "snapshot"}:
                    continue
                # An FK from product history to categories does not make that
                # table category history. Bind the versioned entity itself.
                entity_words = {w.removesuffix("s") for w in _words(p.name)}
                history_words = {w.removesuffix("s") for w in _words(h.name)} - {"history", "hist", "version", "snapshot"}
                if not entity_words or not entity_words <= history_words:
                    continue
                starts = [c.name for c in h.columns if c.type in TEMPORAL and
                          (c.name in {"valid_from", "effective_from", "effective_at"}
                           or _words(c.name) & {"edited", "updated", "modified"})]
                attributes = [c.name for c in h.columns if c.type is ColumnType.TEXT
                              and c.name not in _identity(task, h)
                              and (not c.enum_values or len(c.enum_values) >= 2)
                              and _words(c.name) & {"name", "status", "tier", "segment", "category", "plan"}]
                for start in starts:
                    ends = [c for c in h.columns if c.name in {"valid_to", "effective_to", "effective_until"}]
                    if len(ends)>1 or ends and ends[0].type not in TEMPORAL:
                        continue
                    for event in _dates(f, EVENT):
                        for attribute in attributes:
                            found.append(AdvancedEvidence(**base, event_time=event, history=h.name,
                                history_links=h_edge.child_columns, history_keys=_key(h), valid_from=start, attribute=attribute,
                                valid_to=ends[0].name if ends else ""))
    return tuple(sorted(set(found), key=repr))

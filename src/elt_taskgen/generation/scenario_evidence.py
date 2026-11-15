"""Typed, relationship-backed admission for interval, event, ledger and graph tasks."""
from dataclasses import dataclass

from elt_taskgen.generation.advanced_evidence import _key, _repeatable, _dates, CREATED, EVENT, TEMPORAL
from elt_taskgen.generation.semantic_difficulty import NUMERIC, _words
from elt_taskgen.models import ColumnType

INTERVAL = "interval_coverage_v1"
SESSION = "event_sessionization_v1"
LIFECYCLE = "activity_lifecycle_v1"
DOCUMENT = "document_reconciliation_v1"
LEDGER = "ledger_reconciliation_v1"
HIERARCHY = "recursive_hierarchy_v1"
SCENARIO_RECIPES = (INTERVAL, SESSION, LIFECYCLE, DOCUMENT, LEDGER, HIERARCHY)


@dataclass(frozen=True)
class ScenarioEvidence:
    parent: str
    keys: tuple[str, ...]
    fact: str
    links: tuple[str, ...]
    row_keys: tuple[str, ...]
    start: str = ""
    end: str = ""
    parent_start: str = ""
    parent_end: str = ""
    amount: str = ""
    debit: str = ""
    credit: str = ""
    other: str = ""
    other_links: tuple[str, ...] = ()
    other_amount: str = ""
    targets: tuple[str, ...] = ()
    fractional: bool = False


def intervals(table):
    for start, end in (("logged_at", "finished_at"), ("started_at", "ended_at"),
                       ("start_date", "end_date"), ("from_time", "to_time"),
                       ("valid_from", "valid_to"), ("start_time", "end_time")):
        cols = {c.name: c for c in table.columns}
        if all(k in cols and cols[k].type in TEMPORAL for k in (start, end)):
            yield start, end


def _edge(task, edge):
    p, f = task.table(edge.parent_table), task.table(edge.child_table)
    key = _key(p)
    # Normalize permuted declarations to the complete declared parent key.
    if not key or set(key) != set(edge.parent_columns) or len(key) != len(edge.child_columns):
        return None
    mapping = dict(zip(edge.parent_columns, edge.child_columns, strict=True))
    links = tuple(mapping[k] for k in key)
    if any(p.column(k).type != f.column(c).type for k, c in zip(key, links, strict=True)):
        return None
    return p, f, key, links


def scenario_candidates(task, recipe):
    if recipe not in SCENARIO_RECIPES:
        raise ValueError(recipe)
    found = []
    edges = [(r, binding) for r in task.relationships if (binding := _edge(task, r))]
    for _, (p, f, keys, links) in edges:
        base = dict(parent=p.name, keys=keys, fact=f.name, links=links, row_keys=_key(f))
        if recipe == HIERARCHY:
            parent_role = any(_words(c) & {"parent", "manager", "supervisor", "p"} for c in links)
            if p.name == f.name and parent_role:
                found.append(ScenarioEvidence(**base, targets=keys))
            elif parent_role and {w.rstrip("s") for w in _words(p.name)} <= {w.rstrip("s") for w in _words(f.name)}:
                for _, (p2, f2, keys2, links2) in edges:
                    if (p2.name, f2.name, keys2) == (p.name, f.name, keys) and links2 != links:
                        found.append(ScenarioEvidence(**base, targets=links2))
            continue
        if p.name == f.name or not _repeatable(f, links) or not _key(f):
            continue
        if recipe == INTERVAL:
            for ps, pe in intervals(p):
                for start, end in intervals(f):
                    found.append(ScenarioEvidence(**base, start=start, end=end, parent_start=ps, parent_end=pe))
        elif recipe in (SESSION, LIFECYCLE):
            if _words(f.name) & {"history", "hist", "snapshot"}:
                continue
            for time in _dates(f, EVENT):
                if recipe == SESSION and f.column(time).type is ColumnType.TIMESTAMP:
                    found.append(ScenarioEvidence(**base, start=time))
                elif recipe == LIFECYCLE:
                    for created in _dates(p, CREATED):
                        found.append(ScenarioEvidence(**base, start=time, parent_start=created))
        elif recipe == LEDGER:
            cols = {c.name: c for c in f.columns}
            if (_words(p.name) & {"entry", "entries", "transaction", "transactions"}
                    and all(c in cols and cols[c].type in NUMERIC and not cols[c].enum_values for c in ("debit", "credit"))):
                found.append(ScenarioEvidence(**base, debit="debit", credit="credit",
                    fractional=any(cols[c].type in (ColumnType.FLOAT, ColumnType.DECIMAL) for c in ("debit", "credit"))))
        elif recipe == DOCUMENT and _words(p.name) & {"invoice", "invoices", "order", "orders"}:
            if not _words(f.name) & {"line", "lines", "item", "items"}:
                continue
            amounts = [c.name for c in p.columns if c.name in ("amount_total", "total_amount")
                       and c.type in NUMERIC and not c.enum_values]
            line_amounts = [c.name for c in f.columns if c.name in ("price_subtotal", "line_total", "amount")
                            and c.type in NUMERIC and not c.enum_values]
            for _, (p2, f2, keys2, links2) in edges:
                if p2.name != p.name or keys2 != keys or f2.name in (p.name, f.name) or not _key(f2) or not _repeatable(f2, links2):
                    continue
                if not _words(f2.name) & {"payment", "payments"}:
                    continue
                paid = [c.name for c in f2.columns if c.name in ("paid_amount", "amount")
                        and c.type in NUMERIC and not c.enum_values]
                for amount in amounts:
                    for line in line_amounts:
                        for pay in paid:
                            found.append(ScenarioEvidence(**base, amount=amount, debit=line,
                                other=f2.name, other_links=links2, other_amount=pay))
    return tuple(sorted(set(found), key=repr))

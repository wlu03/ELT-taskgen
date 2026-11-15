"""Declared relationship graphs with optional, population-scoped observations.

Unknown observations stay None. Confidence describes the evidence source, not
a calibrated probability. Integer/text normalization is directional: a text FK
referencing an integer key has decimal-integer identity; opaque text keys do not.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import re

from elt_taskgen.models import ColumnType as T


@dataclass(frozen=True)
class RelationshipEdgeEvidence:
    child_table: str
    child_columns: tuple[str, ...]
    parent_table: str
    parent_columns: tuple[str, ...]
    parent_key_proven: bool
    child_nullable: bool
    nullable_columns: tuple[str, ...]
    required: bool
    child_types: tuple[str, ...]
    parent_types: tuple[str, ...]
    normalization: tuple[str, ...]
    cross_backend: bool
    provenance: str = "TaskIR.relationships and declared table keys"
    confidence: str = "declared"
    observed_match_rate: float | None = None
    observed_orphan_count: int | None = None
    maximum_fanout: int | None = None
    observed_null_count: int | None = None


@dataclass(frozen=True)
class RelationshipPath:
    tables: tuple[str, ...]
    edges: tuple[RelationshipEdgeEvidence, ...]


@dataclass(frozen=True)
class FactBranch:
    table: str
    edge: RelationshipEdgeEvidence


@dataclass(frozen=True)
class DimensionBranch:
    table: str
    edge: RelationshipEdgeEvidence


@dataclass(frozen=True)
class RelationshipGraphEvidence:
    grain_table: str
    grain_key: tuple[str, ...]
    edges: tuple[RelationshipEdgeEvidence, ...]
    fact_branches: tuple[FactBranch, ...]
    dimension_branches: tuple[DimensionBranch, ...]
    ambiguous_paths: tuple[RelationshipPath, ...]
    self_edges: tuple[RelationshipEdgeEvidence, ...]
    observation_population: str | None = None


def unique_key(table):
    for key in (table.primary_key, table.business_key):
        if key and all(not table.column(k).nullable and not table.column(k).enum_values
                       and table.column(k).type in (T.INTEGER, T.BIGINT, T.TEXT) for k in key):
            return key
    return ()


def normalized_integer(value):
    """No decimals, exponents, booleans, overflow, or silent numeric rounding."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip(" ")
    if not re.fullmatch(r"[+-]?[0-9]+", text, flags=re.ASCII):
        return None
    # Bound parsing too: source strings can be arbitrarily large.
    digits = text.lstrip("+-").lstrip("0") or "0"
    if len(digits) > 19:
        return None
    number = int(("-" if text.startswith("-") else "") + digits)
    return number if -(2**63) <= number < 2**63 else None


def relationship_graph(task, *, grain_table="", rows=None, population=None):
    if rows is not None and not population:
        raise ValueError("Graph observations require a population name")
    assignments = {a.table: a.backend for a in task.backends}
    edges = []
    for r in sorted(task.relationships, key=lambda e: (e.child_table, e.child_columns, e.parent_table, e.parent_columns)):
        child, parent = task.table(r.child_table), task.table(r.parent_table)
        key = next((k for k in (parent.primary_key, parent.business_key)
                    if k and len(k) == len(r.parent_columns) and set(k) == set(r.parent_columns)), ())
        columns = dict(zip(r.parent_columns, r.child_columns, strict=True))
        parent_columns = key or r.parent_columns
        child_columns = tuple(columns[k] for k in parent_columns)
        ct = tuple(child.column(c).type for c in child_columns)
        pt = tuple(parent.column(c).type for c in parent_columns)
        normalization = tuple("identity" if c == p or c in (T.INTEGER,T.BIGINT) and p in (T.INTEGER,T.BIGINT)
            else "decimal_integer" if c is T.TEXT and p in (T.INTEGER,T.BIGINT) else "unsupported"
            for c,p in zip(ct,pt,strict=True))
        nullable = tuple(c for c in child_columns if child.column(c).nullable)
        edge = RelationshipEdgeEvidence(child.name,child_columns,parent.name,parent_columns,bool(key),bool(nullable),nullable,
            r.required,tuple(t.value for t in ct),tuple(t.value for t in pt),normalization,
            assignments[child.name] != assignments[parent.name], confidence="declared_unique" if key else "unproven_key")
        if rows is not None:
            targets = {tuple(row.get(k) for k in parent_columns) for row in rows.get(parent.name, ())}
            fanout, eligible, matched, nulls = Counter(), 0, 0, 0
            for row in rows.get(child.name, ()):
                values = tuple(row.get(k) for k in child_columns)
                if any(v is None for v in values):
                    nulls += 1
                    continue
                eligible += 1
                values = tuple(normalized_integer(v) if rule == "decimal_integer" else v
                               for v,rule in zip(values,normalization,strict=True))
                if all(v is not None for v in values) and values in targets:
                    matched += 1
                    fanout[values] += 1
            edge = replace(edge, observed_match_rate=matched / eligible if eligible else None,
                observed_orphan_count=eligible-matched, maximum_fanout=max(fanout.values(),default=0), observed_null_count=nulls)
        edges.append(edge)
    # Enumerate bounded simple two-edge alternatives. Recipes can traverse the
    # complete edge set; this inventory is not a chain-only admission rule.
    adjacent = defaultdict(list)
    for e in edges:
        if e.child_table != e.parent_table:
            adjacent[e.child_table].append((e.parent_table,e))
            adjacent[e.parent_table].append((e.child_table,e))
    paths = defaultdict(set)
    for start in sorted(adjacent):
        for middle, first in adjacent[start]:
            for end, second in adjacent[middle]:
                if start < end and end != start and first != second:
                    paths[start,end].add(RelationshipPath((start,middle,end),(first,second)))
    ambiguous = tuple(sorted((p for group in paths.values() if len(group)>1 for p in group),key=repr))
    return RelationshipGraphEvidence(grain_table, unique_key(task.table(grain_table)) if grain_table else (), tuple(edges),
        tuple(FactBranch(e.child_table,e) for e in edges if e.parent_table == grain_table and e.child_table != grain_table),
        tuple(DimensionBranch(e.parent_table,e) for e in edges if e.child_table == grain_table and e.parent_table != grain_table),
        ambiguous,tuple(e for e in edges if e.child_table == e.parent_table),population)

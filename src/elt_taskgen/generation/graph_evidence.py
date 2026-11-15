"""Motif selection from complete FK tuples, independent of source/dataset names."""
from dataclasses import dataclass
from itertools import combinations

from elt_taskgen.generation.relationship_graph import RelationshipEdgeEvidence, relationship_graph, unique_key
from elt_taskgen.models import ColumnType as T

ROLES = "role_playing_dimension_v1"
PATHS = "ambiguous_path_rollup_v1"
BRIDGES = "multi_bridge_dedup_v1"
OPTIONAL = "optional_chain_coverage_v1"
NORMALIZED = "heterogeneous_key_join_v1"
COMPOSITE = "composite_key_bridge_v1"
GRAPH_RECIPES = (ROLES, PATHS, BRIDGES, OPTIONAL, NORMALIZED, COMPOSITE)


@dataclass(frozen=True)
class GraphBinding:
    grain_table: str
    grain_key: tuple[str, ...]
    edges: tuple[RelationshipEdgeEvidence, ...]
    label: str = ""


def repeatable(task, table, columns):
    t = task.table(table)
    return not any(k and set(k) <= set(columns) for k in (t.primary_key,t.business_key))


def graph_candidates(task, recipe):
    if recipe not in GRAPH_RECIPES:
        raise ValueError(recipe)
    graph = relationship_graph(task)
    edges = [e for e in graph.edges if e.parent_key_proven and unique_key(task.table(e.parent_table))
             and e.parent_columns == unique_key(task.table(e.parent_table)) and e.child_table != e.parent_table
             and "unsupported" not in e.normalization
             and all(not task.table(e.child_table).column(c).enum_values for c in e.child_columns)]
    plain = [e for e in edges if set(e.normalization) == {"identity"}]
    found = set()
    if recipe == NORMALIZED:
        for e in edges:
            key = unique_key(task.table(e.child_table))
            # Normalized spellings are optional raw links until interpreted by
            # this mart. Required raw FK validation is never relaxed globally.
            if key and not e.required and "decimal_integer" in e.normalization:
                found.add(GraphBinding(e.child_table,key,(e,)))
    elif recipe == ROLES:
        for first in plain:
            roles = [e for e in plain if (e.child_table,e.parent_table,e.parent_columns) ==
                     (first.child_table,first.parent_table,first.parent_columns)]
            key = unique_key(task.table(first.child_table))
            labels = [c.name for c in task.table(first.parent_table).columns if c.type is T.TEXT
                      and c.name not in first.parent_columns and (not c.enum_values or len(c.enum_values)>=3)]
            for chosen in combinations(roles,min(3,len(roles))):
                if key and labels and len(chosen)>=2 and len({c for e in chosen for c in e.child_columns}) == sum(len(e.child_columns) for e in chosen):
                    found.add(GraphBinding(first.child_table,key,chosen,sorted(labels)[0]))
    else:
        for owner in plain:
            if not repeatable(task,owner.child_table,owner.child_columns):
                continue
            for link in plain:
                if link.child_table != owner.child_table or link.parent_table == owner.parent_table:
                    continue
                if set(link.child_columns) & set(owner.child_columns):
                    continue
                pair = (owner,link)
                if recipe == COMPOSITE and len(owner.parent_columns)>1 and repeatable(task,owner.child_table,(*owner.child_columns,*link.child_columns)):
                    found.add(GraphBinding(owner.parent_table,owner.parent_columns,pair))
                elif recipe == PATHS:
                    for other in plain:
                        if (other.parent_table,other.parent_columns) != (owner.parent_table,owner.parent_columns) or other.child_table == owner.child_table:
                            continue
                        for end in plain:
                            if end.child_table == other.child_table and (end.parent_table,end.parent_columns) == (link.parent_table,link.parent_columns) and not set(end.child_columns)&set(other.child_columns):
                                if owner.child_table < other.child_table and any(repeatable(task,a.child_table,(*a.child_columns,*b.child_columns))
                                    for a,b in ((owner,link),(other,end))):
                                    found.add(GraphBinding(owner.parent_table,owner.parent_columns,(*pair,other,end)))
                elif recipe == BRIDGES and repeatable(task,owner.child_table,(*owner.child_columns,*link.child_columns)):
                    for second in plain:
                        if (second.parent_table,second.parent_columns) != (link.parent_table,link.parent_columns) or second.child_table in (owner.child_table,owner.parent_table):
                            continue
                        for target in plain:
                            if target.child_table != second.child_table or target.parent_table in (owner.parent_table,link.parent_table,owner.child_table):
                                continue
                            if not set(second.child_columns)&set(target.child_columns) and repeatable(task,second.child_table,(*second.child_columns,*target.child_columns)):
                                found.add(GraphBinding(owner.parent_table,owner.parent_columns,(*pair,second,target)))
                elif recipe == OPTIONAL and owner.required:
                    for final in plain:
                        if final.child_table == link.parent_table and final.parent_table not in (owner.parent_table,owner.child_table) and not final.required:
                            found.add(GraphBinding(owner.parent_table,owner.parent_columns,(*pair,final)))
    return tuple(sorted(found,key=repr))

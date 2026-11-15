"""Business-rule oracle using sets and lookups, without the plan or SQL builder."""
import re
from elt_taskgen.generation.graph_evidence import ROLES, PATHS, BRIDGES, OPTIONAL, NORMALIZED, COMPOSITE


def graph_expected(e,recipe,rows):
    def key(row,columns):
        return tuple(row[c] for c in columns)
    def lookup(edge,row):
        wanted=key(row,edge.child_columns)
        if None in wanted:
            return None
        return next((p for p in rows[edge.parent_table] if key(p,edge.parent_columns)==wanted),None)
    def links(edge,parent):
        return [r for r in rows[edge.child_table] if key(r,edge.child_columns)==key(parent,edge.parent_columns)]
    def members(first,second,parent):
        return {key(target,second.parent_columns) for r in links(first,parent) if (target:=lookup(second,r)) is not None}
    output=[]
    for parent in rows[e.grain_table]:
        result={k:parent[k] for k in e.grain_key}
        if recipe==ROLES:
            for i,edge in enumerate(e.edges):
                target=lookup(edge,parent)
                result[f"role_{i}_label"]=target[e.label] if target else None
                result[f"role_{i}_state"]="missing" if None in key(parent,edge.child_columns) else "matched" if target else "unmatched"
        elif recipe==NORMALIZED:
            edge=e.edges[0]
            raw=key(parent,edge.child_columns)
            normalized=[]
            for value,rule in zip(raw,edge.normalization):
                if rule=="decimal_integer" and value is not None:
                    spelling=str(value).strip(" ")
                    value=int(spelling) if re.fullmatch(r"[+-]?[0-9]+",spelling) else None
                    if value is not None and not -(2**63)<=value<2**63:
                        value=None
                normalized.append(value)
            target=None if None in normalized else next((p for p in rows[edge.parent_table] if key(p,edge.parent_columns)==tuple(normalized)),None)
            result["relationship_state"]="missing" if None in raw else "invalid" if None in normalized else "matched" if target else "unmatched"
            result.update({f"resolved_key_{i}":target[k] if target else None for i,k in enumerate(edge.parent_columns)})
        elif recipe==COMPOSITE:
            first,second=e.edges
            physical=links(first,parent)
            matched=[r for r in physical if lookup(second,r)]
            result.update(physical_link_count=len(physical),matched_link_count=len(matched),
                null_target_count=sum(None in key(r,second.child_columns) for r in physical),
                unmatched_target_count=sum(None not in key(r,second.child_columns) and lookup(second,r) is None for r in physical),
                distinct_target_count=len(members(first,second,parent)),duplicate_link_count=len(matched)-len(members(first,second,parent)))
        elif recipe==PATHS:
            requested=members(*e.edges[:2],parent)
            alternate=members(*e.edges[2:],parent)
            result.update(requested_member_count=len(requested),alternate_member_count=len(alternate),
                          both_path_count=len(requested & alternate),requested_only_count=len(requested-alternate))
        elif recipe==BRIDGES:
            first,middle,second,last=e.edges
            paths=set()
            for r in links(first,parent):
                intermediate=lookup(middle,r)
                if intermediate:
                    paths.update((key(intermediate,middle.parent_columns),target) for target in members(second,last,intermediate))
            result.update(membership_path_count=len(paths),distinct_target_count=len({t for m,t in paths}),
                          distinct_middle_count=len({m for m,t in paths}))
        else:
            first,middle,last=e.edges
            eligible=[m for r in links(first,parent) if (m:=lookup(middle,r)) is not None]
            complete=sum(lookup(last,m) is not None for m in eligible)
            result.update(eligible_record_count=len(eligible),complete_record_count=complete,
                null_final_count=sum(None in key(m,last.child_columns) for m in eligible),
                orphan_final_count=sum(None not in key(m,last.child_columns) and lookup(last,m) is None for m in eligible),
                no_complete_activity=complete==0)
        output.append(result)
    return output

"""Reachable distinct descendants and cycle membership, with no depth cutoff."""
from elt_taskgen.generation.mart_plan import quote as q
from elt_taskgen.models import JoinType, MartOpKind as K, canonical_json


def hierarchy_ops(b, e, variant):
    roots = tuple(f"root_{i}" for i in range(len(e.keys)))
    nodes = tuple(f"node_{i}" for i in range(len(e.keys)))
    parents = b.project(e.parent, ", ".join(f'{q(k)} AS {x}' for k, x in zip(e.keys, roots)), "hierarchy_roots")
    targets = b.project(e.parent, ", ".join(f'{q(k)} AS {x}' for k, x in zip(e.keys, nodes)), "hierarchy_nodes")
    raw = b.project(e.fact, ", ".join([*(f'{q(k)} AS {x}' for k, x in zip(e.links, roots)),
        *(f'{q(k)} AS {x}' for k, x in zip(e.targets, nodes))]), "hierarchy_edges")
    valid = b.join(raw, parents, roots, (), "known_from_nodes", kind=JoinType.INNER)
    valid = b.join(valid, targets, nodes, (), "known_to_nodes", kind=JoinType.INNER)
    edges = b.add(K.DEDUPE, (valid,), "distinct_edges", columns=(*roots, *nodes))
    reach = edges if variant == "one_hop" else b.add(K.TRANSITIVE_CLOSURE, (edges,), "reachable_pairs", columns=(*roots, *nodes),
        details={"from_columns": canonical_json(roots), "to_columns": canonical_json(nodes)})
    if variant == "count_paths":
        renamed = b.project(edges, ", ".join([*(f'{x} AS via_{i}' for i, x in enumerate(roots)), *nodes]), "incoming_paths")
        paths = b.add(K.JOIN, (reach, renamed), "path_multiplicity", join_type=JoinType.INNER,
            predicate=" AND ".join(f'{q(reach)}.{n}={q(renamed)}.via_{i}' for i, n in enumerate(nodes)),
            select=", ".join([*(f'{q(reach)}.{r} AS {r}' for r in roots), *(f'{q(renamed)}.{n} AS {n}' for n in nodes)]))
        reach = b.add(K.UNION, (edges, paths), "repeated_reachable_nodes", details={"mode": "all"})
    equal = " AND ".join(f'{r}={n}' for r, n in zip(roots, nodes))
    marked = b.project(reach, f'*, CASE WHEN {equal} THEN 0 ELSE 1 END AS other_node, '
        f'CASE WHEN {equal} THEN 1 ELSE 0 END AS returns_to_root', "reachable_node_kinds")
    amount = "COUNT(*)" if variant == "include_self" else "SUM(other_node)"
    totals = b.aggregate(marked, roots, {"reachable_count": amount,
        "cycle_flag": "MAX(returns_to_root)"}, "hierarchy_summary")
    current = b.join(parents, totals, roots, ("reachable_count", "cycle_flag"), "all_hierarchy_nodes",
                     kind=JoinType.INNER if variant == "inner_parent" else JoinType.LEFT)
    b.project(current, ", ".join([*(f'{x} AS {q(k)}' for k, x in zip(e.keys, roots)),
        "COALESCE(reachable_count,0) AS reachable_count", "COALESCE(cycle_flag,0)=1 AS is_in_cycle"]), "mart_output")

"""A closed transitive-closure operation, with finite set semantics on key tuples.

The reference compiler computes reachable pairs, not paths. UNION DISTINCT
terminates on cycles without a fabricated depth cutoff or exponential paths.
"""
import json

from elt_taskgen.sql_identifiers import quote_sql_identifier


def closure_keys(op):
    if (len(op.tables) != 1 or op.predicate or op.join_type is not None
            or set(op.details) != {"name", "from_columns", "to_columns"}
            or not op.details.get("name", "").strip()):
        raise ValueError("transitive_closure requires one relation and named from/to key tuples")
    a, b = (json.loads(op.details[k]) for k in ("from_columns", "to_columns"))
    if (not isinstance(a, list) or not isinstance(b, list) or not a or len(a) != len(b)
            or any(not isinstance(c, str) or not c for c in (*a, *b))
            or len(set((*a, *b))) != len(a) + len(b) or tuple((*a, *b)) != op.columns):
        raise ValueError("transitive_closure needs disjoint, equally sized key tuples matching its columns")
    return tuple(a), tuple(b)


def closure_sql(op, relation):
    left, right = closure_keys(op)
    q = lambda s: quote_sql_identifier(s, force=True)
    projection = ", ".join(map(q, (*left, *right)))
    recursive = ", ".join([*(f'w.{q(c)}' for c in left), *(f'e.{q(c)}' for c in right)])
    condition = " AND ".join(f'w.{q(b)}=e.{q(a)}' for a, b in zip(left, right, strict=True))
    known = " AND ".join(f'{q(c)} IS NOT NULL' for c in (*left, *right))
    # A source can itself be named closure_edges or closure_pairs. Local CTEs
    # must not shadow the relation that seeds this operation.
    edges, pairs = "closure_edges", "closure_pairs"
    while relation.casefold() in {edges.casefold(), q(edges).casefold()}:
        edges += "_"
    while relation.casefold() in {pairs.casefold(), q(pairs).casefold()}:
        pairs += "_"
    return (f'WITH RECURSIVE {q(edges)} AS (SELECT DISTINCT {projection} FROM {relation} WHERE {known}), '
            f'{q(pairs)} AS (SELECT {projection} FROM {q(edges)} UNION '
            f'SELECT {recursive} FROM {q(pairs)} w JOIN {q(edges)} e ON {condition}) '
            f'SELECT {projection} FROM {q(pairs)}')

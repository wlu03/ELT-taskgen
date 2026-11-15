"""Small, constraint-checked populations for independent fact branches."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from elt_taskgen.generation.mart_plan import BranchedFactShape, ShapeNotSelectable
from elt_taskgen.models import Relationship, Row, TableSpec

WITNESS_BRANCH_CROSS_PRODUCT = "branch_cross_product"
WITNESS_COMPOSITE_COLLISION = "composite_collision"


class InertBranchedWitnessError(ShapeNotSelectable):
    """The schema/closure cannot preserve the promised branched discriminator."""


def verify_branched_witnesses(
    tables: Sequence[TableSpec], relationships: Sequence[Relationship],
    shape: BranchedFactShape, rows: Mapping[str, Sequence[Row]],
) -> None:
    """Check final literals, including after other shapes have been merged."""
    from elt_taskgen.generation import populations as p

    def require(condition: bool, detail: str) -> None:
        if not condition:
            raise InertBranchedWitnessError(f"mart {shape.mart}: {detail}")

    parents = rows.get(shape.parent, ())
    keys = [tuple(row[c] for c in shape.parent_keys) for row in parents]
    require(bool(keys) and len(keys) == len(set(keys)), "parent grain is missing or non-unique")
    groups = []
    for branch in shape.branches:
        grouped: dict[tuple, list[Row]] = {}
        for row in rows.get(branch.table, ()):
            key = tuple(row[c] for c in branch.child_columns)
            grouped.setdefault(key, []).append(row)
        groups.append(grouped)
    require(len(groups) == 2, "exactly two branches are required")
    cross = [key for key in keys if all(len(group.get(key, ())) >= 2 for group in groups)]
    require(bool(cross), "both branches must fan out at the SAME parent key")
    for branch, group in zip(shape.branches, groups, strict=True):
        if branch.measure:
            require(any(
                any(value is not None and value != 0 and count >= 2 for value, count in
                    Counter(row[branch.measure] for row in group[key]).items())
                and sum(row[branch.measure] or 0 for row in group[key]) != 0
                for key in cross
            ), f"{branch.table} lost the repeated non-zero measure that defeats SUM(DISTINCT)")
    require(any(all(not group.get(key) for group in groups) for key in keys), "childless parent was lost")
    for index in range(2):
        require(any(groups[index].get(key) and not groups[1 - index].get(key) for key in keys),
                f"parent with activity only in branch {index} was lost")
    if len(shape.parent_keys) > 1:
        for component in range(len(shape.parent_keys)):
            for branch, group in zip(shape.branches, groups, strict=True):
                seen = {}
                collision = False
                for key in keys:
                    if not group.get(key):
                        continue
                    partial = key[:component] + key[component + 1:]
                    if partial in seen and seen[partial] != key:
                        collision = True
                        break
                    seen[partial] = key
                require(collision, f"{branch.table} has no collision for omitted key component {component}")
    for table in tables:
        for key in (table.primary_key, table.business_key):
            if key:
                values = [tuple(row[c] for c in key) for row in rows.get(table.name, ())]
                require(len(values) == len(set(values)), f"{table.name} violates key {key}")
    broken = p._unresolved_keys(tuple(r for r in relationships if r.required), rows)
    require(not broken, f"required FK closure failed: {sorted(broken, key=repr)[:1]}")
    p.assert_literal_rows_typed(tuple(tables), rows)


def branched_literal_rows(
    tables: tuple[TableSpec, ...], relationships: tuple[Relationship, ...],
    shape: BranchedFactShape,
) -> dict[str, tuple[Row, ...]]:
    from elt_taskgen.generation import populations as p

    tables = p._narrow_for_minting(tables, relationships)
    by_name = {table.name: table for table in tables}
    parent = by_name[shape.parent]
    arity = len(shape.parent_keys)
    parents = [p._mint_row(parent, i) for i in range(4 + (arity if arity > 1 else 0))]
    # For each omitted component, one parent agrees with the control on all
    # remaining components. The changed component is a real, distinct key value.
    for component in range(arity if arity > 1 else 0):
        row = parents[4 + component]
        changed = row[shape.parent_keys[component]]
        row.update({key: parents[0][key] for key in shape.parent_keys})
        row[shape.parent_keys[component]] = changed
    rows = {parent.name: parents}
    pins = {id(row): frozenset((*parent.primary_key, *parent.business_key, *shape.parent_keys)) for row in parents}
    protected = set(pins)
    edges = set()
    for index, branch in enumerate(shape.branches):
        table = by_name[branch.table]
        payload = []
        placements = [(0, 2 + index), (2 + index, 1)]
        placements += [(i, 1) for i in range(4, len(parents))]
        for parent_index, count in placements:
            for local in range(count):
                row = p._mint_row(table, 100 + len(payload))
                row.update({child: parents[parent_index][key]
                            for child, key in zip(branch.child_columns, shape.parent_keys, strict=True)})
                if branch.measure:
                    value = ((5, 5) if index == 0 else (2, 2, 7))[local] if parent_index == 0 else 11 + parent_index
                    row[branch.measure] = p._coerce(table.column(branch.measure), value)
                payload.append(row)
                pins[id(row)] = frozenset((*table.primary_key, *table.business_key,
                                          *branch.child_columns, *((branch.measure,) if branch.measure else ())))
                protected.add(id(row))
        rows[table.name] = payload
        edges.add((table.name, parent.name, frozenset(zip(branch.child_columns, shape.parent_keys, strict=True))))
    for table in tables:
        rows.setdefault(table.name, [p._mint_row(table, 0)])
    p._close_foreign_keys(
        by_name, relationships, rows,
        skip=lambda r: (r.child_table, r.parent_table,
                        frozenset(zip(r.child_columns, r.parent_columns, strict=True))) in edges,
        preserve=frozenset(protected), pinned_columns=pins,
    )
    p._enforce_primary_keys(by_name, rows)
    result = {name: tuple(payload) for name, payload in sorted(rows.items())}
    verify_branched_witnesses(tables, relationships, shape, result)
    return result


def branched_witness_conditions(shape: BranchedFactShape) -> tuple[str, ...]:
    scope = f"[mart {shape.mart}] "
    lines = [
        scope + "CONSTRUCTED branched fact witnesses, checked after FK closure and population merging.",
        scope + "branch_cross_product: the SAME parent has at least two rows in each fact branch. "
        "Each measured branch contains distinct source rows with a repeated non-zero amount.",
        scope + "B_childless: one parent has no fact rows. Two further parents have activity in only one branch each.",
    ]
    if len(shape.parent_keys) > 1:
        lines.append(scope + "composite_collision: for EVERY key component and EVERY branch, "
                     "two populated parent keys agree on all other components but differ on that component.")
    return tuple(lines)

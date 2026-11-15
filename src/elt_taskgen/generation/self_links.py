"""Resolve self-references after a table's identities and external links exist."""
from collections import defaultdict
import random

from elt_taskgen.models import derive_seed


def assign_self_links(task, population, policy, table, rows, relationships):
    # Changing any identity or external link would invalidate previously drawn
    # parent pools. Shared tenant/key components are constraints on the choice.
    protected = set(table.primary_key) | set(table.business_key)
    for relation in task.relationships:
        if relation.parent_table == table.name:
            protected.update(relation.parent_columns)
        if relation.child_table == table.name and relation.parent_table != table.name:
            protected.update(relation.child_columns)
    for relation in relationships:
        fixed = tuple((child, parent) for child, parent in
            zip(relation.child_columns, relation.parent_columns, strict=True) if child in protected)
        pools = defaultdict(list)
        seen = set()
        for row in rows:
            parent_key = tuple(row[c] for c in relation.parent_columns)
            if None in parent_key or parent_key in seen:
                continue
            seen.add(parent_key)
            pools[tuple(row[parent] for _, parent in fixed)].append(parent_key)
        nullable = tuple(c for c in relation.child_columns if table.column(c).nullable and c not in protected)
        rng = random.Random(derive_seed(task.task_id, population.value, table.name, 'self-fk', *relation.child_columns))
        for index, row in enumerate(rows):
            choices = pools.get(tuple(row[child] for child, _ in fixed), ())
            if not relation.required and nullable and (not choices or rng.random() < policy.null_fk_frac):
                row.update(dict.fromkeys(nullable))
                continue
            if not choices:
                if relation.required:
                    raise ValueError(f"table {table.name!r}: required self-reference {relation.child_columns} "
                                     "has no matching parent preserving its identity and external links")
                continue
            # A branching tree keeps ordinary populations tractable: closure
            # grows roughly n log n, rather than creating a giant n-node cycle.
            # Private graph witnesses independently exercise cycles and diamonds.
            chosen = choices[(max(0, index - 1) // 2) % len(choices)]
            row.update(zip(relation.child_columns, chosen, strict=True))
        protected.update(relation.child_columns)

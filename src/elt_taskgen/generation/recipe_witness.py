"""Small isolated counterexamples, using the schema's types and constraints."""
from elt_taskgen.generation.recipe_evidence import AGGREGATE_RANK, CALENDAR_STREAK
from elt_taskgen.models import ColumnType


def recipe_witness_rows(task, e, recipe, *, extra_group_cases=()):
    from elt_taskgen.generation import populations as p
    tables = p._narrow_for_minting(task.tables, task.relationships)
    by_name = {t.name: t for t in tables}
    parent, fact = by_name[e.parent], by_name[e.fact]
    with p._shape_mint_offset(700_000):
        base_count = 6 + (len(e.keys) if len(e.keys) > 1 else 0)
        parents = [p._mint_row(parent, i) for i in range(base_count + len(extra_group_cases))]
        for i in range(len(e.keys) if len(e.keys) > 1 else 0):
            distinct = parents[6 + i][e.keys[i]]
            parents[6 + i].update({k: parents[0][k] for k in e.keys})
            parents[6 + i][e.keys[i]] = distinct
        rows = {e.parent: parents, e.fact: []}
        pins = {id(row): frozenset((*parent.primary_key, *parent.business_key, *e.keys)) for row in parents}
        groups = list(fact.column(e.group).enum_values[:3]) if e.group and fact.column(e.group).enum_values else ["group_a", "group_b", "group_c"]
        null_value = None if fact.column(e.measure).nullable else 0

        def add(index, value, group=0, year=2020, day="06-15"):
            row = p._mint_row(fact, 100 + len(rows[e.fact]))
            row.update({c: parents[index][k] for c, k in zip(e.links, e.keys, strict=True)})
            row[e.measure] = p._coerce(fact.column(e.measure), value)
            if e.group:
                row[e.group] = groups[group]
            if e.timestamp:
                date = f"{year}-{day}"
                row[e.timestamp] = date + "T12:00:00" if fact.column(e.timestamp).type is ColumnType.TIMESTAMP else date
            rows[e.fact].append(row)
            pins[id(row)] = frozenset((*fact.primary_key, *fact.business_key, *e.links, e.measure,
                                      *((e.group,) if e.group else ()), *((e.timestamp,) if e.timestamp else ())))

        if recipe == CALENDAR_STREAK:
            for index, years in ((0, (2019, 2020, 2021, 2022)), (2, (2017, 2019, 2021)), (3, (2020, 2021)), (4, (2019, 2020, 2021))):
                for year in years:
                    for _ in range(3):
                        add(index, null_value if index == 4 else 10, year=year)
            # Boundaries and non-leap versus leap years use stored calendar years.
            for year in (2018, 2019, 2020):
                for day in ("01-01", "06-15", "12-31"):
                    add(5, 8, year=year, day=day)
        else:
            for group, values in ((0, [10, 10, 10]), (1, [100, 100]), (2, [10, 10, 10])):
                for value in values:
                    add(0, value, group)
            for group, values in ((0, [8, 8, 8, 8]), (1, [10, 10, 10])):
                for value in values:
                    add(2, value, group)
            for _ in range(3):
                add(3, null_value)
            add(4, -10)
        for i in range(6, base_count):
            for year in (2015, 2016, 2017):
                for _ in range(3):
                    add(i, 21 + i, year=year)
        for index, case in enumerate(extra_group_cases, start=base_count):
            for group, values in enumerate(case):
                for value in values:
                    add(index, value, group)
        for table in tables:
            rows.setdefault(table.name, [p._mint_row(table, 0)])
        owned = (e.fact, e.links, e.parent, e.keys)
        p._close_foreign_keys(by_name, task.relationships, rows,
            skip=lambda r: (r.child_table, r.child_columns, r.parent_table, r.parent_columns) == owned,
            preserve=frozenset(pins), pinned_columns=pins)
        p._enforce_primary_keys(by_name, rows)
    result = {name: tuple(values) for name, values in rows.items()}
    p.assert_literal_rows_typed(task.tables, result)
    return result

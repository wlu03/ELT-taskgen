"""Small typed, FK-closed witnesses for each temporal/allocation failure."""
from elt_taskgen.generation.advanced_evidence import ASOF, COHORT
from elt_taskgen.models import ColumnType


def advanced_witness_rows(task, e, recipe):
    from elt_taskgen.generation import populations as p
    tables = p._narrow_for_minting(task.tables, task.relationships)
    by_name = {t.name: t for t in tables}
    parent, fact = by_name[e.parent], by_name[e.fact]
    rows, pins = {}, {}
    with p._shape_mint_offset(800_000):
        def add(table, values=None):
            target = rows.setdefault(table.name, [])
            row = p._mint_row(table, 100 + len(target))
            row.update(values or {})
            target.append(row)
            pins[id(row)] = frozenset((*table.primary_key, *table.business_key, *(values or {})))
            return row

        def date(table, column, value):
            return value + "T00:00:00" if table.column(column).type is ColumnType.TIMESTAMP else value

        parents = [add(parent) for _ in range(6 + (len(e.keys) if len(e.keys) > 1 else 0))]
        for i in range(6, len(parents)):
            k = e.keys[i - 6]
            distinct = parents[i][k]
            parents[i].update({c: parents[0][c] for c in e.keys})
            parents[i][k] = distinct

        def fact_row(index, values):
            return add(fact, {**{c: parents[index][k] for c, k in zip(e.links, e.keys, strict=True)}, **values})

        if recipe == COHORT:
            for i, row in enumerate(parents):
                start = "2023-12-31" if i == 4 else "2024-02-28" if i == 5 else "2024-01-31"
                row[e.parent_time] = date(parent, e.parent_time, start)
                pins[id(row)] |= {e.parent_time}
            for index, dates in ((0, ("2024-01-31", "2024-02-01", "2024-02-01")),
                                 (1, ("2024-02-28",)), (2, ("2024-01-01",)),
                                 (4, ("2024-01-01",)), (5, ("2024-02-29", "2024-03-01"))):
                for day in dates:
                    fact_row(index, {e.event_time: date(fact, e.event_time, day)})
            for i in range(6, len(parents)):
                fact_row(i, {e.event_time: date(fact, e.event_time, "2024-03-01")})
            if fact.column(e.event_time).nullable:
                fact_row(0, {e.event_time: None})
        elif recipe == ASOF:
            h = by_name[e.history]
            values = h.column(e.attribute).enum_values or ("basic", "premium", "enterprise")
            for i, day, value in ((0, "2024-01-01", values[0]), (0, "2024-02-01", values[0]),
                                  (0, "2024-02-01", values[1]), (2, "2024-01-01", values[0])):
                add(h, {**{c: parents[i][k] for c, k in zip(e.history_links, e.keys, strict=True)},
                        e.valid_from: date(h, e.valid_from, day), e.attribute: value})
            for i in range(6, len(parents)):
                add(h, {**{c: parents[i][k] for c, k in zip(e.history_links, e.keys, strict=True)},
                        e.valid_from: date(h, e.valid_from, "2023-12-01"), e.attribute: values[1]})
            for index, day in ((0, "2023-12-01"), (0, "2024-01-31"), (0, "2024-02-01"),
                                (0, "2024-03-01"), (1, "2024-02-01"), (2, "2024-04-01")):
                fact_row(index, {e.event_time: date(fact, e.event_time, day)})
            if fact.column(e.event_time).nullable:
                fact_row(0, {e.event_time: None})
            if e.valid_to:
                for row in rows[h.name]:
                    end = "2024-01-20" if str(row[e.valid_from]).startswith("2024-01") else "2024-03-01"
                    row[e.valid_to] = date(h,e.valid_to,end)
                    pins[id(row)] |= {e.valid_to}
                def history_row(index,start,end,value):
                    return add(h,{**{c:parents[index][k] for c,k in zip(e.history_links,e.keys)},
                        e.valid_from:date(h,e.valid_from,start),e.valid_to:date(h,e.valid_to,end) if end else None,e.attribute:value})
                history_row(0,"2024-01-15","2024-02-10",values[1])
                # Same start, different ends: filter validity before selecting
                # the version-key winner. Reversed/empty intervals are invalid.
                history_row(0,"2024-02-01","2024-02-02",values[0])
                history_row(0,"2024-02-20","2024-02-19",values[0])
                history_row(2,"2024-02-01","2024-02-01",values[1])
                if h.column(e.valid_to).nullable:
                    history_row(3,"2024-01-01",None,values[0])
                    fact_row(3,{e.event_time:date(fact,e.event_time,"2025-01-01")})
                for index,day in ((0,"2024-01-10"),(0,"2024-01-16"),(0,"2024-02-15"),(2,"2024-01-20")):
                    fact_row(index,{e.event_time:date(fact,e.event_time,day)})
        else:
            for i, row in enumerate(parents):
                row[e.amount] = p._coerce(parent.column(e.amount), (1000, 90, -40, 60, 80, 10000)[min(i, 5)])
                pins[id(row)] |= {e.amount}
            # Tiny positive fractions distinguish early rounding even under
            # the evaluator's relative numeric tolerance.
            for i, weights in ((0, (1, 2, 0, -2)), (1, (1, 3)), (2, (1, 1)), (3, (0, -1)), (5, (1, 30001))):
                for value in weights:
                    fact_row(i, {e.weight: p._coerce(fact.column(e.weight), value)})
            if fact.column(e.weight).nullable:
                fact_row(0, {e.weight: None})
            if parent.column(e.amount).nullable:
                parents[4][e.amount] = None
                fact_row(4, {e.weight: p._coerce(fact.column(e.weight), 1)})
            for i in range(6, len(parents)):
                fact_row(i, {e.weight: p._coerce(fact.column(e.weight), 4)})
        for table in tables:
            if table.name not in rows:
                add(table)
        owned = {(e.fact, e.links, e.parent, e.keys)}
        if e.history:
            owned.add((e.history, e.history_links, e.parent, e.keys))
        p._close_foreign_keys(by_name, task.relationships, rows,
            skip=lambda r: (r.child_table, r.child_columns, r.parent_table, r.parent_columns) in owned,
            preserve=frozenset(pins), pinned_columns=pins)
        p._enforce_primary_keys(by_name, rows)
    result = {name: tuple(values) for name, values in rows.items()}
    p.assert_literal_rows_typed(task.tables, result)
    return result

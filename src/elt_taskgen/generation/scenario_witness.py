"""Small typed witnesses, with foreign-key closure and collision-free identities."""
from elt_taskgen.generation.scenario_evidence import INTERVAL, SESSION, LIFECYCLE, DOCUMENT, LEDGER, HIERARCHY
from elt_taskgen.models import ColumnType


def scenario_witness_rows(task, e, recipe):
    from elt_taskgen.generation import populations as p
    tables = p._narrow_for_minting(task.tables, task.relationships)
    by_name = {t.name: t for t in tables}
    parent, fact = by_name[e.parent], by_name[e.fact]
    rows, pins = {}, {}
    with p._shape_mint_offset(900_000):
        def add(table, values=None):
            target = rows.setdefault(table.name, [])
            row = p._mint_row(table, 100 + len(target))
            row.update(values or {})
            target.append(row)
            pins[id(row)] = frozenset((*table.primary_key, *table.business_key, *(values or {})))
            return row

        def set_values(row, values):
            row.update(values)
            pins[id(row)] |= values.keys()

        def stamp(table, column, day):
            return day[:10] if table.column(column).type is ColumnType.DATE else day if "T" in day else day + "T00:00:00"

        def number(table, column, value):
            return p._coerce(table.column(column), value)

        parents = [add(parent) for _ in range(7 + (len(e.keys) if len(e.keys)>1 else 0))]
        for i in range(7, len(parents)):
            key = e.keys[i-7]
            different = parents[i][key]
            set_values(parents[i], {**{k: parents[0][k] for k in e.keys}, key: different})

        def record(index, values, *, table=fact, links=e.links):
            return add(table, {**{c: parents[index][k] for c, k in zip(links, e.keys)}, **values})

        if recipe == INTERVAL:
            for row in parents:
                set_values(row, {e.parent_start: stamp(parent, e.parent_start, "2024-01-10"),
                                 e.parent_end: stamp(parent, e.parent_end, "2024-01-20")})
            for start, end in (("2024-01-05","2024-01-15"), ("2024-01-11","2024-01-12"),
                               ("2024-01-13","2024-01-18"), ("2024-01-18","2024-01-22"),
                               ("2024-01-25","2024-01-28"), ("2024-01-16","2024-01-15")):
                record(0, {e.start: stamp(fact,e.start,start), e.end: stamp(fact,e.end,end)})
            set_values(parents[2], {e.parent_end: stamp(parent,e.parent_end,"2024-01-10")})
            for i in (2, *range(7,len(parents))):
                record(i, {e.start: stamp(fact,e.start,"2024-01-10"), e.end: stamp(fact,e.end,"2024-01-11")})
            for col in (e.start,e.end):
                if fact.column(col).nullable:
                    record(0, {e.start: stamp(fact,e.start,"2024-01-10"), e.end: stamp(fact,e.end,"2024-01-20"), col: None})
        elif recipe == SESSION:
            for time in ("00:00:00", "00:30:00", "01:00:00", "01:30:01", "01:30:01"):
                record(0, {e.start: "2024-01-01T" + time})
            for i in (1, *range(7,len(parents))):
                record(i, {e.start: "2024-01-01T00:01:00"})
            if fact.column(e.start).nullable:
                record(0, {e.start: None})
        elif recipe == LIFECYCLE:
            for row in parents:
                set_values(row, {e.parent_start: stamp(parent,e.parent_start,"2024-01-31")})
            for day in ("2024-01-31", "2024-01-31", "2024-03-01", "2024-04-01", "2024-04-01", "2024-04-01", "2024-05-01"):
                record(0,{e.start:stamp(fact,e.start,day)})
            record(2,{e.start:stamp(fact,e.start,"2024-01-01")})
            for i in range(7,len(parents)):
                record(i,{e.start:stamp(fact,e.start,"2024-02-01")})
            if fact.column(e.start).nullable:
                record(0,{e.start:None})
            if parent.column(e.parent_start).nullable:
                set_values(parents[6], {e.parent_start:None})
        elif recipe == LEDGER:
            for index, debit, credit in ((0,60,0),(0,60,0),(0,0,110),(1,90,0),(1,0,100),
                                         (2,0.01,0),(4,50,50),(5,10,0)):
                # Put the fractional boundary on a fractional column when the
                # other leg is integer typed.
                if index == 2 and fact.column(e.debit).type not in (ColumnType.FLOAT,ColumnType.DECIMAL):
                    debit, credit = 0, 0.01
                record(index,{e.debit:number(fact,e.debit,debit),e.credit:number(fact,e.credit,credit)})
            for i in range(7,len(parents)):
                record(i,{e.debit:number(fact,e.debit,25),e.credit:number(fact,e.credit,3)})
        elif recipe == DOCUMENT:
            payments=by_name[e.other]
            if e.amount:
                for row in parents:
                    set_values(row,{e.amount:number(parent,e.amount,100)})
            for index, values in ((0,(50,50)),(1,(80,)),(4,(100,))):
                for value in values:
                    record(index,{e.debit:number(fact,e.debit,value)})
            for index, values in ((0,(30,30)),(1,(100,)),(4,(120,)),(5,(-20,))):
                for value in values:
                    record(index,{e.other_amount:number(payments,e.other_amount,value)},table=payments,links=e.other_links)
            for i in range(7,len(parents)):
                record(i,{e.debit:number(fact,e.debit,25)})
                record(i,{e.other_amount:number(payments,e.other_amount,3)},table=payments,links=e.other_links)
        elif recipe == HIERARCHY:
            if e.parent == e.fact:
                owners = {0: None, 1: 3, 2: 1, 3: 2, 4: 3, 5: None, 6: 0}
                for i, row in enumerate(parents):
                    owner = owners.get(i,0)
                    values={c:(None if owner is None and fact.column(c).nullable else parents[i if owner is None else owner][k])
                            for c,k in zip(e.links,e.keys)}
                    set_values(row, values)
            else:
                for start,end in ((0,1),(0,2),(1,3),(2,3),(3,1),(3,4),(4,4)):
                    record(start,{c:parents[end][k] for c,k in zip(e.targets,e.keys)})
                for i in range(7,len(parents)):
                    record(i,{c:parents[5][k] for c,k in zip(e.targets,e.keys)})
        for table in tables:
            if table.name not in rows:
                add(table)

        def owned(r):
            if r.parent_table != e.parent or set(r.parent_columns) != set(e.keys):
                return False
            mapping=dict(zip(r.parent_columns,r.child_columns))
            options=[(e.fact,e.links)]
            if e.other:
                options.append((e.other,e.other_links))
            if recipe == HIERARCHY:
                options.append((e.fact,e.targets))
            return any(r.child_table==t and mapping==dict(zip(e.keys,cols)) for t,cols in options)

        p._close_foreign_keys(by_name,task.relationships,rows,skip=owned,
            preserve=frozenset(pins),pinned_columns=pins)
    return {name:tuple(values) for name,values in rows.items()}

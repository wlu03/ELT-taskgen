"""Typed counterexamples for graph semantics; unrelated required FKs stay valid."""
from elt_taskgen.generation.graph_evidence import ROLES, PATHS, BRIDGES, OPTIONAL, NORMALIZED, COMPOSITE, repeatable


def graph_witness_rows(task, e, recipe):
    from elt_taskgen.generation import populations as p
    tables = p._narrow_for_minting(task.tables, task.relationships)
    by_name = {t.name:t for t in tables}
    rows, pins = {}, {}
    with p._shape_mint_offset(1_100_000):
        def add(name, values=None):
            table = by_name[name]
            target = rows.setdefault(name, [])
            row = p._mint_row(table, 100 + len(target))
            row.update(values or {})
            target.append(row)
            pins[id(row)] = frozenset((*table.primary_key,*table.business_key,*(values or {})))
            return row

        def update(row, values):
            row.update(values)
            pins[id(row)] |= values.keys()

        def link(edge, parent):
            return {c:parent[k] for c,k in zip(edge.child_columns,edge.parent_columns,strict=True)}

        def absent(edge):
            # Mint a value without inserting its parent. Offset beyond any
            # actual witness identity, including foreign-key closure rows.
            row = p._mint_row(by_name[edge.parent_table], 100_000)
            return link(edge,row)

        def missing(edge):
            values = absent(edge)
            column = next((c for c in edge.child_columns if by_name[edge.child_table].column(c).nullable),None)
            if column:
                values[column] = None
                return values
            return None

        def entities(edge, count=4):
            result = [add(edge.parent_table) for _ in range(count)]
            if len(edge.parent_columns)>1:
                for component in edge.parent_columns:
                    row = add(edge.parent_table)
                    different = row[component]
                    update(row,{**{k:result[0][k] for k in edge.parent_columns},component:different})
                    result.append(row)
            return result

        owner=e.edges[0]
        if recipe == ROLES:
            parents = entities(owner)
            enum = by_name[owner.parent_table].column(e.label).enum_values
            for i,row in enumerate(parents):
                update(row,{e.label:enum[i % len(enum)] if enum else f"role_entity_{i}"})
            values = {c:v for i,edge in enumerate(e.edges) for c,v in link(edge,parents[i]).items()}
            add(e.grain_table,values)
            for edge in e.edges:
                if not edge.required:
                    add(e.grain_table,{**values,**absent(edge)})
                    if (nulls:=missing(edge)):
                        add(e.grain_table,{**values,**nulls})
            # Each role has its own component collision, including roles 1/2.
            for i in range(1,len(e.edges)):
                for component in owner.parent_columns if len(owner.parent_columns)>1 else ():
                    row=add(owner.parent_table)
                    different=row[component]
                    update(row,{**{k:parents[i][k] for k in owner.parent_columns},component:different,
                                e.label:enum[0] if enum else "collision"})
        elif recipe == NORMALIZED:
            parents = entities(owner)
            numeric = next(i for i,rule in enumerate(owner.normalization) if rule=="decimal_integer")
            col,key = owner.child_columns[numeric],owner.parent_columns[numeric]
            def raw(parent):
                return {c:str(parent[k]) if rule=="decimal_integer" else parent[k]
                        for c,k,rule in zip(owner.child_columns,owner.parent_columns,owner.normalization)}
            for value in (str(parents[0][key]),f" +000{parents[0][key]} ",f"{parents[0][key]}.0",
                          f"{parents[0][key]}e0",f"\u00a0{parents[0][key]}",f"\t{parents[0][key]}","garbage","9223372036854775808",""):
                add(e.grain_table,{**raw(parents[0]),col:value})
            orphan = {c:str(v) if rule=="decimal_integer" else v for (c,v),rule in zip(absent(owner).items(),owner.normalization)}
            add(e.grain_table,orphan)
            if (nulls:=missing(owner)):
                add(e.grain_table,{**raw(parents[0]),next(c for c,v in nulls.items() if v is None):None})
            for parent in parents[1:]:
                add(e.grain_table,raw(parent))
        else:
            parents = entities(owner)
            middle_edge=e.edges[1]
            middle=entities(middle_edge)
            def record(parent,target,first=owner,second=middle_edge):
                return add(first.child_table,{**link(first,parent),**link(second,target)})
            if recipe == COMPOSITE:
                for pi,ti in ((0,0),(0,0),(0,1),(1,1)):
                    record(parents[pi],middle[ti])
                if not middle_edge.required:
                    add(owner.child_table,{**link(owner,parents[0]),**absent(middle_edge)})
                    if (nulls:=missing(middle_edge)):
                        add(owner.child_table,{**link(owner,parents[0]),**nulls})
            elif recipe == PATHS:
                for pi,ti in ((0,0),(0,1),(0,2),(1,1)):
                    record(parents[pi],middle[ti])
                other,end=e.edges[2:]
                record(parents[0],middle[1],other,end)
                record(parents[1],middle[2],other,end)
                # A duplicate membership is legal only with an independent
                # link identity. Do not manufacture duplicate primary keys.
                first,second=next(((a,b) for a,b in ((owner,middle_edge),(other,end))
                    if repeatable(task,a.child_table,(*a.child_columns,*b.child_columns))),(None,None))
                if first:
                    record(parents[0],middle[1],first,second)
            elif recipe == BRIDGES:
                second,target=e.edges[2:]
                targets=entities(target)
                for pi,mi in ((0,0),(0,0),(0,1),(1,2)):
                    record(parents[pi],middle[mi])
                for mi,ti in ((0,0),(0,0),(0,1),(1,0),(2,2)):
                    record(middle[mi],targets[ti],second,target)
            else:
                final=e.edges[2]
                targets=entities(final)
                for m in middle:
                    update(m,link(final,targets[0]))
                update(middle[1],absent(final))
                if (nulls:=missing(final)):
                    update(middle[2],nulls)
                for pi,mi in ((0,0),(0,1),(0,2),(1,3),(3,1)):
                    record(parents[pi],middle[mi])
                if not middle_edge.required:
                    add(owner.child_table,{**link(owner,parents[0]),**absent(middle_edge)})
                    if (nulls:=missing(middle_edge)):
                        add(owner.child_table,{**link(owner,parents[0]),**nulls})
            for parent in parents[4:]:
                record(parent,middle[0])
        for table in tables:
            if table.name not in rows:
                add(table.name)

        def owned(r):
            return any(r.child_table==edge.child_table and r.parent_table==edge.parent_table
                and dict(zip(r.child_columns,r.parent_columns))==dict(zip(edge.child_columns,edge.parent_columns))
                for edge in e.edges)
        p._close_foreign_keys(by_name,task.relationships,rows,skip=owned,preserve=frozenset(pins),pinned_columns=pins)
    return {name:tuple(values) for name,values in rows.items()}

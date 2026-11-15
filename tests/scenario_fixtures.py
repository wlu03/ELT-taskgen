"""Source-like schema fixtures; the business-rule oracles live separately."""
from types import SimpleNamespace
from elt_taskgen.generation.populations import default_populations
from elt_taskgen.generation.recipe_plan import PlanBuilder
from elt_taskgen.models import (Backend,BackendAssignment,ColumnSpec as C,ColumnType as T,
    MartColumn,MartPlan,MartSpec,Origin,Relationship,TableSpec,TaskIR)


def scenario_source(*, composite=False, text_keys=False, self_graph=False):
    t=T.TEXT if text_keys else T.BIGINT
    keys=("tenant_id","id") if composite else ("id",)
    links=("owner_tenant","owner_id") if composite else ("owner_id",)
    columns=lambda names:tuple(C(name=k,type=t) for k in names)
    parent=TableSpec(name="invoices",primary_key=keys,columns=(
        *columns(keys),C(name="created_at",type=T.TIMESTAMP,nullable=True),
        C(name="started_at",type=T.TIMESTAMP,nullable=True),C(name="ended_at",type=T.TIMESTAMP,nullable=True),
        C(name="amount_total",type=T.DECIMAL,nullable=True)))
    facts=TableSpec(name="invoice_lines",primary_key=("line_id",),columns=(
        *columns(("line_id",)),*columns(links),C(name="logged_at",type=T.TIMESTAMP,nullable=True),
        C(name="finished_at",type=T.TIMESTAMP,nullable=True),C(name="amount",type=T.DECIMAL,nullable=True),
        C(name="debit",type=T.DECIMAL,nullable=True),C(name="credit",type=T.DECIMAL,nullable=True)))
    payments=TableSpec(name="payments",primary_key=("payment_id",),columns=(
        *columns(("payment_id",)),*columns(links),C(name="paid_amount",type=T.DECIMAL,nullable=True)))
    node_keys=("realm","id") if composite else ("id",)
    from_keys=("parent_realm","parent_id") if composite else ("parent_id",)
    to_keys=("child_realm","child_id") if composite else ("child_id",)
    nodes=TableSpec(name="groups",primary_key=node_keys,columns=(*columns(node_keys),
        *(tuple(C(name=k,type=t,nullable=True) for k in from_keys) if self_graph else ())))
    entries=TableSpec(name="journal_entries",primary_key=keys,columns=columns(keys))
    journal_lines=facts.model_copy(update={"name":"journal_lines"})
    tables=[parent,facts,payments,nodes,entries,journal_lines]
    rels=[Relationship(child_table=f.name,child_columns=links,parent_table=parent.name,parent_columns=keys)
          for f in (facts,payments)]
    rels.append(Relationship(child_table=journal_lines.name,child_columns=links,parent_table=entries.name,parent_columns=keys))
    if self_graph:
        rels.append(Relationship(child_table=nodes.name,child_columns=from_keys,parent_table=nodes.name,parent_columns=node_keys,required=False))
    else:
        edge=TableSpec(name="group_links",primary_key=("link_id",),columns=(*columns(("link_id",)),*columns(from_keys),*columns(to_keys)))
        tables.append(edge)
        rels.extend(Relationship(child_table=edge.name,child_columns=side,parent_table=nodes.name,parent_columns=node_keys)
                    for side in (from_keys,to_keys))
    b=PlanBuilder(SimpleNamespace(tables=tables))
    b.project(parent.name,", ".join(keys),"entity_list")
    mart=MartSpec(name="invoice_list",grain="One row per invoice",key_columns=keys,
        columns=tuple(MartColumn(name=k,type=t,description=k) for k in keys),plan=MartPlan(mart="invoice_list",ops=tuple(b.ops)))
    task_id="proof__scenarios"
    return TaskIR(task_id=task_id,family_id=task_id,cluster_id=task_id,origin=Origin.SYNTHETIC,license="CC0-1.0",
        tables=tuple(tables),relationships=tuple(rels),backends=tuple(BackendAssignment(table=t.name,backend=Backend.POSTGRES) for t in tables),
        marts=(mart,),populations=default_populations(task_id,{t.name:12 for t in tables},tables=tuple(tables),relationships=tuple(rels)))

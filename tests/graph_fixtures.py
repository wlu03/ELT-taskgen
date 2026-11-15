"""Small schemas with different graph motifs and fully declared key tuples."""
from types import SimpleNamespace
from elt_taskgen.generation.graph_evidence import ROLES, PATHS, BRIDGES, OPTIONAL, NORMALIZED, COMPOSITE
from elt_taskgen.generation.populations import default_populations
from elt_taskgen.generation.recipe_plan import PlanBuilder
from elt_taskgen.models import (Backend,BackendAssignment,ColumnSpec as C,ColumnType as T,
    MartColumn,MartPlan,MartSpec,Origin,Relationship,TableSpec,TaskIR)


def graph_source(recipe, arity=2, text=False):
    keys=tuple(f"key_{i}" for i in range(arity))
    kind=T.TEXT if text else T.BIGINT
    tables,rels=[],[]
    def cols(names,type=kind,nullable=False):
        return tuple(C(name=k,type=type,nullable=nullable) for k in names)
    def entity(name):
        tables.append(TableSpec(name=name,primary_key=keys,columns=(*cols(keys),C(name="name",type=T.TEXT,nullable=True))))
    def source(name):
        tables.append(TableSpec(name=name,primary_key=("record_id",),columns=cols(("record_id",))))
    def edge(child,parent,prefix,required=True,nullable=False,type=kind):
        fk=tuple(prefix+str(i) for i in range(arity))
        index=next(i for i,t in enumerate(tables) if t.name==child)
        tables[index]=tables[index].model_copy(update={"columns":(*tables[index].columns,*cols(fk,type,nullable))})
        rels.append(Relationship(child_table=child,child_columns=fk,parent_table=parent,parent_columns=keys,required=required))
    entity("entities")
    if recipe in (ROLES,NORMALIZED):
        source("activity")
        for role in range(3 if recipe==ROLES else 1):
            edge("activity","entities",f"role_{role}_",required=False,nullable=True,type=T.TEXT if recipe==NORMALIZED else kind)
        grain="activity"
        grain_key=("record_id",)
    else:
        entity("targets")
        source("a_links")
        edge("a_links","entities","owner_")
        if recipe==BRIDGES:
            entity("intermediates")
            edge("a_links","intermediates","middle_")
            source("b_links")
            edge("b_links","intermediates","middle_")
            edge("b_links","targets","target_")
        elif recipe==OPTIONAL:
            entity("intermediates")
            edge("a_links","intermediates","middle_",required=False,nullable=True)
            edge("intermediates","targets","target_",required=False,nullable=True)
        else:
            edge("a_links","targets","target_",required=False,nullable=True)
            if recipe==PATHS:
                source("b_links")
                edge("b_links","entities","owner_")
                edge("b_links","targets","target_",required=False,nullable=True)
        grain="entities"
        grain_key=keys
    b=PlanBuilder(SimpleNamespace(tables=tables))
    b.project(grain,", ".join(grain_key),"source_list")
    mart=MartSpec(name="base_list",grain="One row per source key",key_columns=grain_key,
        columns=tuple(MartColumn(name=k,type=kind,description=k) for k in grain_key),plan=MartPlan(mart="base_list",ops=tuple(b.ops)))
    task_id="proof__graph_"+recipe
    return TaskIR(task_id=task_id,family_id=task_id,cluster_id=task_id,origin=Origin.SYNTHETIC,license="CC0-1.0",
        tables=tuple(tables),relationships=tuple(rels),backends=tuple(BackendAssignment(table=t.name,backend=Backend.POSTGRES) for t in tables),
        marts=(mart,),populations=default_populations(task_id,{t.name:12 for t in tables},tables=tuple(tables),relationships=tuple(rels)))

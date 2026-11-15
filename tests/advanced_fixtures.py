"""Independent minimal schemas for advanced recipe contracts."""
from types import SimpleNamespace

from elt_taskgen.generation.populations import default_populations
from elt_taskgen.generation.recipe_plan import PlanBuilder
from elt_taskgen.models import (Backend, BackendAssignment, ColumnSpec as C, ColumnType as T,
    MartColumn, MartPlan, MartSpec, Origin, Relationship, TableSpec, TaskIR)


def advanced_source(*, composite=False, text_keys=False):
    key_type = T.TEXT if text_keys else T.BIGINT
    key = ("tenant_id", "id") if composite else ("id",)
    links = ("tenant_id", "customer_id") if composite else ("customer_id",)
    link_columns = tuple(C(name=k, type=key_type) for k in links)
    customers = TableSpec(name="customers", primary_key=key, columns=(
        *(C(name=k, type=key_type) for k in key), C(name="created_at", type=T.TIMESTAMP, nullable=True)))
    order_key = ("tenant_id", "order_id") if composite else ("order_id",)
    orders = TableSpec(name="orders", primary_key=order_key, columns=(
        C(name="order_id", type=key_type), *link_columns, C(name="ordered_at", type=T.TIMESTAMP, nullable=True),
        C(name="amount_total", type=T.DECIMAL, nullable=True)))
    history = TableSpec(name="customer_history", primary_key=("version_id",), columns=(
        C(name="version_id", type=key_type), *link_columns, C(name="valid_from", type=T.TIMESTAMP, nullable=True),
        C(name="status", type=T.TEXT, nullable=True)))
    lines = TableSpec(name="order_lines", primary_key=("line_id",), columns=(
        C(name="line_id", type=key_type), *(C(name=k, type=key_type) for k in order_key), C(name="quantity", type=T.FLOAT, nullable=True)))
    tables = (customers, orders, history, lines)
    relationships = (
        Relationship(child_table="orders", child_columns=links, parent_table="customers", parent_columns=key, required=True),
        Relationship(child_table="customer_history", child_columns=links, parent_table="customers", parent_columns=key, required=True),
        Relationship(child_table="order_lines", child_columns=order_key, parent_table="orders", parent_columns=order_key, required=True),
    )
    b = PlanBuilder(SimpleNamespace(tables=tables))
    b.project("customers", ", ".join(key), "customer_keys")
    mart = MartSpec(name="customer_list", grain="One row per customer.", key_columns=key,
        columns=tuple(MartColumn(name=k, type=key_type, description=f"Customer {k}.") for k in key), plan=MartPlan(mart="customer_list", ops=tuple(b.ops)))
    task_id = "proof__advanced"
    return TaskIR(task_id=task_id, family_id=task_id, cluster_id=task_id, origin=Origin.SYNTHETIC, license="CC0-1.0",
        tables=tables, relationships=relationships, backends=tuple(BackendAssignment(table=t.name, backend=Backend.POSTGRES) for t in tables),
        marts=(mart,), populations=default_populations(task_id, {t.name: 20 for t in tables}, tables=tables, relationships=relationships))

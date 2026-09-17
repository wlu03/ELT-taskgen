"""Define the built-in ``customer_summary`` acceptance-test task.

Description and population strings are solver-visible, content-hashed data.
"""

from __future__ import annotations

from elt_taskgen.models import (
    AttackCase,
    AttackKind,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    JoinType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    PopulationName,
    PopulationSpec,
    ReferenceSolution,
    Relationship,
    Row,
    TableSpec,
    TaskIR,
    derive_seed,
)
from elt_taskgen.generation.populations import el_attack_cases

DEMO_TASK_ID = "demo__customer_summary"
DEMO_FAMILY_ID = "demo__customer_summary"
DEMO_CLUSTER_ID = "demo__customer_summary"
MART_NAME = "customer_summary"

# Counterfactual literal rows (the C10/C11/C12 case, verbatim from the spec).
COUNTERFACTUAL_LITERAL_ROWS: dict[str, tuple[Row, ...]] = {
    "customers": (
        {"customer_id": 10, "customer_name": "C10"},
        {"customer_id": 11, "customer_name": "C11"},
        {"customer_id": 12, "customer_name": "C12"},
    ),
    "orders": (
        {"order_id": 1101, "customer_id": 11, "status": "completed"},
        {"order_id": 1201, "customer_id": 12, "status": "cancelled"},
    ),
    "order_items": (
        {"order_id": 1101, "quantity": 1, "unit_price": 10.0},
        {"order_id": 1101, "quantity": 2, "unit_price": 15.0},
        {"order_id": 1101, "quantity": 1, "unit_price": 5.0},
        {"order_id": 1201, "quantity": 1, "unit_price": 100.0},
    ),
}

#: Expected counterfactual rows, sorted by customer_id. Reference execution
#: must reproduce these EXACTLY; tests and gates cross-check against them.
COUNTERFACTUAL_EXPECTED_MART: tuple[Row, ...] = (
    {"customer_id": 10, "completed_order_count": 0, "total_spend": 0.0},
    {"customer_id": 11, "completed_order_count": 1, "total_spend": 45.0},
    {"customer_id": 12, "completed_order_count": 0, "total_spend": 0.0},
)

# Trusted reference SQL (DuckDB) and the required attack mutants.
REFERENCE_SQL = """\
WITH completed_orders AS (
    SELECT DISTINCT order_id, customer_id
    FROM orders
    WHERE status = 'completed'
),
order_totals AS (
    SELECT order_id, SUM(quantity * unit_price) AS order_total
    FROM order_items
    GROUP BY order_id
)
SELECT
    c.customer_id AS customer_id,
    COUNT(DISTINCT co.order_id) AS completed_order_count,
    COALESCE(SUM(ot.order_total), 0) AS total_spend
FROM customers AS c
LEFT JOIN completed_orders AS co ON co.customer_id = c.customer_id
LEFT JOIN order_totals AS ot ON ot.order_id = co.order_id
GROUP BY c.customer_id
ORDER BY c.customer_id
"""

_INNER_JOIN_SQL = """\
WITH completed_orders AS (
    SELECT DISTINCT order_id, customer_id
    FROM orders
    WHERE status = 'completed'
),
order_totals AS (
    SELECT order_id, SUM(quantity * unit_price) AS order_total
    FROM order_items
    GROUP BY order_id
)
SELECT
    c.customer_id AS customer_id,
    COUNT(DISTINCT co.order_id) AS completed_order_count,
    COALESCE(SUM(ot.order_total), 0) AS total_spend
FROM customers AS c
INNER JOIN completed_orders AS co ON co.customer_id = c.customer_id
INNER JOIN order_totals AS ot ON ot.order_id = co.order_id
GROUP BY c.customer_id
ORDER BY c.customer_id
"""

# The naive item-grain query without DISTINCT: on C11 the three item rows make
# COUNT(o.order_id) = 3 instead of 1.
_NO_DISTINCT_SQL = """\
SELECT
    c.customer_id AS customer_id,
    COUNT(o.order_id) AS completed_order_count,
    COALESCE(SUM(i.quantity * i.unit_price), 0) AS total_spend
FROM customers AS c
LEFT JOIN orders AS o
    ON o.customer_id = c.customer_id AND o.status = 'completed'
LEFT JOIN order_items AS i
    ON i.order_id = o.order_id
GROUP BY c.customer_id
ORDER BY c.customer_id
"""

_NO_COALESCE_SQL = """\
WITH completed_orders AS (
    SELECT DISTINCT order_id, customer_id
    FROM orders
    WHERE status = 'completed'
),
order_totals AS (
    SELECT order_id, SUM(quantity * unit_price) AS order_total
    FROM order_items
    GROUP BY order_id
)
SELECT
    c.customer_id AS customer_id,
    COUNT(DISTINCT co.order_id) AS completed_order_count,
    SUM(ot.order_total) AS total_spend
FROM customers AS c
LEFT JOIN completed_orders AS co ON co.customer_id = c.customer_id
LEFT JOIN order_totals AS ot ON ot.order_id = co.order_id
GROUP BY c.customer_id
ORDER BY c.customer_id
"""

#: Directive form: attacks.py materializes this mutant by copying the frozen
#: primary-population gold outputs verbatim — no computation at all.
HARDCODE_PRIMARY_DIRECTIVE = "directive:hardcode-population-outputs:primary"


def demo_mart_plan() -> MartPlan:
    """Declarative record of the intended relational ops for customer_summary."""
    return MartPlan(
        mart=MART_NAME,
        ops=(
            MartOp(
                kind=MartOpKind.FILTER,
                # Only this op may name a status; SpecimenPoolTest removes it to
                # create the ambiguity-no-filter case.
                description=(
                    "Keep only orders with status = 'completed'; these are the "
                    "orders in scope for every rule below."
                ),
                tables=("orders",),
                columns=("status",),
                predicate="status = 'completed'",
            ),
            MartOp(
                kind=MartOpKind.DEDUPE,
                description=(
                    "Deduplicate exact-duplicate order header rows in scope: "
                    "DISTINCT (order_id, customer_id)."
                ),
                tables=("orders",),
                columns=("order_id", "customer_id"),
            ),
                # SpecimenPoolTest requires this policy on AGGREGATE and forbids a
                # dedup token, the table name, or any denominator-hint substring.
            MartOp(
                kind=MartOpKind.AGGREGATE,
                description=(
                    "Per-order item total: SUM(quantity * unit_price) grouped by "
                    "order_id over every line-item row of that order; two "
                    "line-item rows carrying identical values are two line items, "
                    "and both are counted."
                ),
                tables=("order_items",),
                columns=("order_id", "quantity", "unit_price"),
                details={"function": "sum", "expression": "quantity * unit_price"},
            ),
            MartOp(
                kind=MartOpKind.JOIN,
                description=(
                    "LEFT JOIN the orders in scope onto customers so customers "
                    "with no such orders are retained. Orders whose customer_id "
                    "is NULL belong to no customer: they are excluded from every "
                    "measure, and the mart never emits a row whose customer_id "
                    "is NULL."
                ),
                tables=("customers", "orders"),
                columns=("customer_id",),
                join_type=JoinType.LEFT,
                predicate="orders.customer_id = customers.customer_id",
            ),
            MartOp(
                kind=MartOpKind.JOIN,
                description="LEFT JOIN per-order item totals onto the orders in scope.",
                tables=("orders", "order_items"),
                columns=("order_id",),
                join_type=JoinType.LEFT,
                predicate="order_items.order_id = orders.order_id",
            ),
            MartOp(
                kind=MartOpKind.AGGREGATE,
                description=(
                # Both clauses must name the set; a back-reference would resolve
                # the ambiguity-no-dedupe specimen.
                    "Per customer: completed_order_count = COUNT(DISTINCT "
                    "order_id) over the orders in scope; total_spend = SUM of "
                    "item totals of the orders in scope."
                ),
                tables=("customers",),
                columns=("customer_id", "completed_order_count", "total_spend"),
                details={
                    "group_by": "customer_id",
                    "completed_order_count": "COUNT(DISTINCT order_id)",
                    "total_spend": "SUM(order_total)",
                },
            ),
            MartOp(
                kind=MartOpKind.DERIVE,
                description=(
                    "COALESCE both measures to 0 for customers without in-scope "
                    "orders (never NULL)."
                ),
                columns=("completed_order_count", "total_spend"),
                predicate="COALESCE(total_spend, 0); count is 0 when no completed orders",
            ),
            MartOp(
                kind=MartOpKind.TIE_BREAK,
                description="Deterministic output order: sort by customer_id.",
                columns=("customer_id",),
            ),
        ),
        notes=(
            "Include customers with no orders; count DISTINCT completed orders; "
            "spend from completed-order items only; COALESCE to 0."
        ),
    )


def _populations() -> tuple[PopulationSpec, ...]:
    return (
        PopulationSpec(
            name=PopulationName.DEVELOPMENT,
            seed=derive_seed(DEMO_TASK_ID, PopulationName.DEVELOPMENT.value),
            scale={"customers": 2, "orders": 4, "order_items": 8},
            conditions=(
                "Tiny C1/C2 debug data: exactly two customers.",
                "Every customer has at least one completed order "
                "(INNER JOIN is indistinguishable here by design).",
                "No NULL customer_id, no duplicate rows.",
            ),
        ),
        PopulationSpec(
            name=PopulationName.PRIMARY,
            seed=derive_seed(DEMO_TASK_ID, PopulationName.PRIMARY.value),
            scale={"customers": 1000, "orders": 3000, "order_items": 9000},
            conditions=(
                "Approximately 1000 customers.",
                "Cancelled orders are present.",
                "Some customers have no orders at all.",
                "Some customers have orders but no completed orders.",
                # Declare completed NULL-customer orders without the word
                # "dangling", which controls source_data._DANGLING_FRAC.
                "Some COMPLETED orders have NULL customer_id and carry item "
                "lines; they belong to no customer and contribute to no output "
                "row.",
                "Completed orders may have multiple items (COUNT DISTINCT matters).",
            ),
        ),
        PopulationSpec(
            name=PopulationName.RESAMPLED,
            seed=derive_seed(DEMO_TASK_ID, PopulationName.RESAMPLED.value),
            scale={"customers": 1000, "orders": 3000, "order_items": 9000},
            conditions=(
                "Same generator and conditions as primary; new seed and new id "
                "ranges (memorization check).",
            ),
        ),
        PopulationSpec(
            name=PopulationName.COUNTERFACTUAL,
            seed=derive_seed(DEMO_TASK_ID, PopulationName.COUNTERFACTUAL.value),
            conditions=(
                "Literal constructed rows only — exactly the C10/C11/C12 case.",
                "C10: customer with no orders -> (0, 0).",
                "C11: one completed order with 3 items, 1*10 + 2*15 + 1*5 = 45; "
                "COUNT DISTINCT must be 1, item-grain COUNT would be 3.",
                "C12: only a cancelled order worth 100 -> (0, 0).",
            ),
            literal_rows=COUNTERFACTUAL_LITERAL_ROWS,
        ),
        PopulationSpec(
            name=PopulationName.STRESS,
            seed=derive_seed(DEMO_TASK_ID, PopulationName.STRESS.value),
            scale={"customers": 200, "orders": 20000, "order_items": 60000},
            conditions=(
                "One heavily skewed customer holds a large share of all orders.",
                "Exact-duplicate order header rows are present (correct logic must dedupe).",
                # Name the graded policy because stress duplication also affects
                # keyless ``order_items`` rows.
                "Exact-duplicate order_items line rows are also present; line "
                "items are not deduplicated.",
                "Ties: distinct customers with identical totals.",
                "Every customer has at least one completed order "
                "(INNER JOIN is indistinguishable here by design).",
            ),
        ),
    )


def _attack_cases() -> tuple[AttackCase, ...]:
    P = PopulationName
    return (
        AttackCase(
            name="inner_join",
            kind=AttackKind.INNER_JOIN,
            description=(
                "Both LEFT JOINs replaced with INNER JOIN: customers without "
                "completed orders are dropped instead of reported as (0, 0)."
            ),
            mutation=_INNER_JOIN_SQL,
            expected_pass={
                P.DEVELOPMENT: True,
                P.STRESS: True,
                P.PRIMARY: False,
                P.RESAMPLED: False,
                P.COUNTERFACTUAL: False,
            },
        ),
        AttackCase(
            name="hardcoded_primary_outputs",
            kind=AttackKind.CONSTANTS,
            description=(
                "Emits the frozen primary-population gold outputs verbatim, "
                "computing nothing. Must score full reward ONLY on primary and "
                "fail every other population."
            ),
            mutation=HARDCODE_PRIMARY_DIRECTIVE,
            expected_pass={
                P.PRIMARY: True,
                P.DEVELOPMENT: False,
                P.RESAMPLED: False,
                P.COUNTERFACTUAL: False,
                P.STRESS: False,
            },
        ),
        AttackCase(
            name="count_without_distinct",
            kind=AttackKind.NO_DEDUP,
            description=(
                "Item-grain join with COUNT(order_id) instead of "
                "COUNT(DISTINCT order_id): returns 3 for C11."
            ),
            mutation=_NO_DISTINCT_SQL,
            expected_pass={P.COUNTERFACTUAL: False},
        ),
        AttackCase(
            name="no_coalesce",
            kind=AttackKind.NO_NULL_DEFAULT,
            description=(
                "Drops COALESCE: customers without completed orders get NULL "
                "total_spend instead of 0."
            ),
            mutation=_NO_COALESCE_SQL,
            expected_pass={
                P.COUNTERFACTUAL: False,
                P.PRIMARY: False,
                P.RESAMPLED: False,
                P.DEVELOPMENT: True,
                P.STRESS: True,
            },
        ),
    )


def demo_task() -> TaskIR:
    """The complete customer_summary demo candidate as a validated TaskIR."""
    tables = (
            TableSpec(
                name="customers",
                description="One row per customer.",
                columns=(
                    ColumnSpec(name="customer_id", type=ColumnType.INTEGER,
                               description="Unique customer identifier."),
                    ColumnSpec(name="customer_name", type=ColumnType.TEXT,
                               description="Display name of the customer."),
                ),
                primary_key=("customer_id",),
            ),
            TableSpec(
                name="orders",
                description="One row per order header (duplicates possible under stress).",
                columns=(
                    ColumnSpec(name="order_id", type=ColumnType.INTEGER,
                               description="Unique order identifier."),
                    ColumnSpec(name="customer_id", type=ColumnType.INTEGER, nullable=True,
                               description="Customer who placed the order; may be NULL."),
                    ColumnSpec(name="status", type=ColumnType.TEXT,
                               enum_values=("cancelled", "completed"),
                               description="Order status."),
                ),
                primary_key=(),  # duplicates of full header rows allowed under stress
                business_key=("order_id",),
            ),
            TableSpec(
                name="order_items",
                description="One row per line item of an order.",
                columns=(
                    ColumnSpec(name="order_id", type=ColumnType.INTEGER,
                               description="Order this line item belongs to."),
                    ColumnSpec(name="quantity", type=ColumnType.INTEGER,
                               description="Units purchased."),
                    ColumnSpec(name="unit_price", type=ColumnType.DECIMAL,
                               description="Price per unit."),
                ),
            ),
        )
    backends = (
        BackendAssignment(table="customers", backend=Backend.POSTGRES),
        BackendAssignment(table="orders", backend=Backend.MONGODB),
        BackendAssignment(table="order_items", backend=Backend.FILES,
                          options={"format": "csv"}),
    )
    return TaskIR(
        task_id=DEMO_TASK_ID,
        family_id=DEMO_FAMILY_ID,
        cluster_id=DEMO_CLUSTER_ID,
        origin=Origin.DEMO,
        license="CC0-1.0",
        attribution="elt-taskgen built-in demo fixture",
        title="Customer order summary",
        tables=tables,
        relationships=(
            Relationship(
                child_table="orders",
                child_columns=("customer_id",),
                parent_table="customers",
                parent_columns=("customer_id",),
                required=False,  # NULL customer_id allowed (primary population)
            ),
            Relationship(
                child_table="order_items",
                child_columns=("order_id",),
                parent_table="orders",
                parent_columns=("order_id",),
                required=True,
            ),
        ),
        backends=backends,
        marts=(
            MartSpec(
                name=MART_NAME,
                # SCOPE-NEUTRAL: survives every tamper, so naming the status
                # here would repair ambiguity-no-filter.
                description="Per-customer order activity summary.",
                grain="One row per customer, including customers with no orders.",
                key_columns=("customer_id",),
                columns=(
                    MartColumn(name="customer_id", type=ColumnType.INTEGER,
                               description="Unique customer identifier."),
            # Keep this scope-neutral: council appends it after the ambiguity
            # injector removes filter wording from ``solver_prompt``.
                    MartColumn(name="completed_order_count", type=ColumnType.INTEGER,
                               description="Count of DISTINCT orders in scope; 0 if none."),
                    MartColumn(name="total_spend", type=ColumnType.DECIMAL,
                               description="Sum of quantity * unit_price over items of "
                                           "the orders in scope; 0 if none."),
                ),
                plan=demo_mart_plan(),
            ),
        ),
        populations=_populations(),
        reference=ReferenceSolution(
            implementation_id="demo_customer_summary_ref",
            dialect="duckdb",
            sql_by_mart={MART_NAME: REFERENCE_SQL},
            load_notes=(
                "Load customers from the postgres load SQL, orders from the "
                "mongodb jsonl, order_items from the flat csv into DuckDB tables "
                "of the same names."
            ),
            provenance="Constructed from the mart plan in the demo spec.",
            version="1",
        ),
            # Combine transform mutants with the shared EL catalogue; its required
            # load mutants provide evidence for the EL required-mutants gate.
        attack_cases=_attack_cases()
        + el_attack_cases(
            tables,
            backend_assignments=backends,
            backends=len(backends),
            populations=_populations(),
        ),
    )

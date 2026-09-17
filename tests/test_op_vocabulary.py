"""Round-6 op vocabulary: filtered_aggregate, distinct, extrema, conditional,
ratio, window — and the MartColumn.kind instrument.

WHY THIS EXISTS
The corpus measured ZERO of 46 generated tasks producing an extremum, a window,
a ratio, a filtered aggregate or a CASE conditional, while the ELT-Bench anchor
carries 161 extremum columns (88% of Hard tasks vs 44% of Easy), 506
conditional columns and 252 filtered-aggregate columns. This module is the
existence proof for the missing vocabulary: ONE fixture task whose two marts use
every new op kind, compiled to DuckDB SQL and EXECUTED against literal witness
rows whose values are readable by eye.

The witness rows are the point. Each construct is unobservable — and therefore
unattackable — without a row that separates it from its plausible wrong
implementation, so the fixture population carries one per construct:

  row B  C2  parent with NO children        -> the filtered aggregate must be 0,
                                               not a missing row (WHERE vs CASE)
  row D  C1  two children, ONE product      -> COUNT(item) 3 != COUNT(DISTINCT
                                               product) 2 (fan-out)
  row E  C3  ALL children fail the predicate-> filtered 0 while total 10; the
                                               only row separating CASE-inside-
                                               aggregate from a WHERE
  row F  C4  EXACT tie in the measure       -> the only row on which a dropped
                                               extremum tie-break is observable
  row G  C5  categorical value OUTSIDE the  -> falsifies a missing CASE ELSE
             declared domain
  row H  C6  ratio EXACTLY on a threshold   -> separates '>' from '>='
"""

from __future__ import annotations

import unittest

import duckdb

from elt_taskgen.generation.mart_plan import (
    case_map_expr,
    column_kind_problems,
    column_kinds_from_plan,
    computed_column_count,
    conditional_op,
    distinct_count_expr,
    extrema_op,
    filtered_count_expr,
    filtered_sum_expr,
    group_by_op,
    label_problems,
    lag_delta_expr,
    op_problems,
    quote,
    ratio_expr,
    ratio_op,
    running_total_expr,
    solver_safe_plan_summary,
    threshold_ladder_expr,
    unclassified_columns,
    validate_plan,
    window_op,
)
from elt_taskgen.models import (
    AttackKind,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    JoinType,
    MartColumn,
    MartColumnKind,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    Relationship,
    TableSpec,
    TaskIR,
)
from elt_taskgen.reference.solution import (
    PlanCompilationError,
    compile_plan_sql,
    create_table,
    execute_mart,
)

K = MartColumnKind
T = ColumnType


# ---------------------------------------------------------------------------
# Source schema + witness rows
# ---------------------------------------------------------------------------

CUSTOMERS = TableSpec(
    name="customers",
    description="One row per customer.",
    columns=(
        ColumnSpec(name="customer_id", type=T.TEXT, description="Customer key."),
        ColumnSpec(
            name="region",
            type=T.TEXT,
            description="Declared sales region.",
            enum_values=("north", "south", "export"),
        ),
    ),
    primary_key=("customer_id",),
)

ORDERS = TableSpec(
    name="orders",
    description="One row per order header (the bridge).",
    columns=(
        ColumnSpec(name="order_id", type=T.TEXT, description="Order key."),
        ColumnSpec(name="customer_id", type=T.TEXT, description="Owning customer."),
        ColumnSpec(
            name="order_status",
            type=T.TEXT,
            description="Order status.",
            enum_values=("completed", "cancelled"),
        ),
        ColumnSpec(name="order_amount", type=T.INTEGER, description="Order value."),
        ColumnSpec(name="ordered_on", type=T.DATE, description="Order date."),
    ),
    primary_key=("order_id",),
)

ORDER_ITEMS = TableSpec(
    name="order_items",
    description="One row per line item (the child; the fan-out lives here).",
    columns=(
        ColumnSpec(name="item_id", type=T.TEXT, description="Line item key."),
        ColumnSpec(name="order_id", type=T.TEXT, description="Owning order."),
        ColumnSpec(name="product_id", type=T.TEXT, description="Product."),
        ColumnSpec(name="quantity", type=T.INTEGER, description="Units."),
    ),
    primary_key=("item_id",),
)

#: The witness population, written literally so the expected mart can be read
#: off it by eye (see the module docstring for which row proves what).
LITERAL_ROWS: dict[str, list[dict[str, object]]] = {
    "customers": [
        {"customer_id": "C1", "region": "north"},
        {"customer_id": "C2", "region": "north"},     # row B: no orders at all
        {"customer_id": "C3", "region": "south"},     # row E: all cancelled
        {"customer_id": "C4", "region": "export"},    # row F: tie
        {"customer_id": "C5", "region": "offshore"},  # row G: out of domain
        {"customer_id": "C6", "region": "north"},     # row H: on the threshold
    ],
    "orders": [
        {"order_id": "O-1001", "customer_id": "C1", "order_status": "completed",
         "order_amount": 100, "ordered_on": "2024-01-01"},
        {"order_id": "O-1002", "customer_id": "C1", "order_status": "cancelled",
         "order_amount": 50, "ordered_on": "2024-01-02"},
        {"order_id": "O-1003", "customer_id": "C3", "order_status": "cancelled",
         "order_amount": 70, "ordered_on": "2024-01-03"},
        {"order_id": "O-1004", "customer_id": "C3", "order_status": "cancelled",
         "order_amount": 30, "ordered_on": "2024-01-04"},
        {"order_id": "O-1005", "customer_id": "C4", "order_status": "completed",
         "order_amount": 40, "ordered_on": "2024-01-05"},
        {"order_id": "O-1006", "customer_id": "C4", "order_status": "completed",
         "order_amount": 40, "ordered_on": "2024-01-06"},  # tie with O-1005
        {"order_id": "O-1007", "customer_id": "C5", "order_status": "completed",
         "order_amount": 10, "ordered_on": "2024-01-07"},
        {"order_id": "O-1008", "customer_id": "C6", "order_status": "completed",
         "order_amount": 25, "ordered_on": "2024-01-08"},
        {"order_id": "O-1009", "customer_id": "C6", "order_status": "cancelled",
         "order_amount": 25, "ordered_on": "2024-01-09"},
    ],
    "order_items": [
        {"item_id": "I1", "order_id": "O-1001", "product_id": "P-A", "quantity": 3},
        {"item_id": "I2", "order_id": "O-1001", "product_id": "P-A", "quantity": 1},
        {"item_id": "I3", "order_id": "O-1002", "product_id": "P-B", "quantity": 2},
        {"item_id": "I4", "order_id": "O-1003", "product_id": "P-C", "quantity": 5},
        {"item_id": "I5", "order_id": "O-1004", "product_id": "P-C", "quantity": 5},
        {"item_id": "I6", "order_id": "O-1005", "product_id": "P-D", "quantity": 2},
        {"item_id": "I7", "order_id": "O-1006", "product_id": "P-E", "quantity": 2},
        {"item_id": "I8", "order_id": "O-1007", "product_id": "P-F", "quantity": 1},
        {"item_id": "I9", "order_id": "O-1008", "product_id": "P-G", "quantity": 2},
        {"item_id": "I10", "order_id": "O-1009", "product_id": "P-G", "quantity": 2},
    ],
}

COMPLETED = "\"order_status\" = 'completed'"

#: The op-level predicate of the roll-up's filtered aggregate. SCOPED, because
#: four of its six measures count every row of the group: an op-level predicate
#: that named no measure would be read as an op-wide filter and would be wrong
#: about `order_count`, `item_count`, `total_units` and `distinct_product_count`
#: — `group_by_op` refuses it for exactly that reason.
COMPLETED_SCOPE = (
    f"completed_order_count and completed_units count only rows where {COMPLETED}; "
    "order_count, item_count, total_units and distinct_product_count count every "
    "row of the group"
)


# ---------------------------------------------------------------------------
# Mart 1 — fan-out roll-up: filtered_aggregate + distinct + ratio + conditional
# ---------------------------------------------------------------------------

def _rollup_plan() -> MartPlan:
    ops: list[MartOp] = [
        MartOp(kind=MartOpKind.SOURCE, description=f"Read source table {t}.", tables=(t,))
        for t in ("customers", "orders", "order_items")
    ]
    ops.append(
        MartOp(
            kind=MartOpKind.DERIVE,
            description="Project the grain of customers: customer_id, region.",
            tables=("customers",),
            columns=("customer_id", "region"),
            details={
                "select": (
                    'customers."customer_id" AS "customer_id", '
                    'customers."region" AS "region"'
                ),
                "name": "mart_base",
            },
        )
    )
    ops.append(
        MartOp(
            kind=MartOpKind.JOIN,
            description=(
                "LEFT JOIN orders onto the grain so customers with no orders are "
                "RETAINED (an INNER join silently drops them)."
            ),
            tables=("mart_base", "orders"),
            columns=("customer_id",),
            join_type=JoinType.LEFT,
            predicate='orders."customer_id" = mart_base."customer_id"',
            details={
                "select": (
                    'mart_base."customer_id" AS "customer_id", '
                    'mart_base."region" AS "region", '
                    'orders."order_id" AS "order_id", '
                    'orders."order_status" AS "order_status"'
                ),
                "name": "mart_joined_1",
            },
        )
    )
    ops.append(
        MartOp(
            kind=MartOpKind.JOIN,
            description=(
                "LEFT JOIN order_items onto the orders bridge — the SECOND hop. "
                "This is where the fan-out lives: one order carries many items, so "
                "COUNT(order_id) and COUNT(DISTINCT order_id) diverge here."
            ),
            tables=("mart_joined_1", "order_items"),
            columns=("order_id",),
            join_type=JoinType.LEFT,
            predicate='order_items."order_id" = mart_joined_1."order_id"',
            details={
                "select": (
                    'mart_joined_1."customer_id" AS "customer_id", '
                    'mart_joined_1."region" AS "region", '
                    'mart_joined_1."order_id" AS "order_id", '
                    'mart_joined_1."order_status" AS "order_status", '
                    'order_items."item_id" AS "item_id", '
                    'order_items."product_id" AS "product_id", '
                    'order_items."quantity" AS "quantity"'
                ),
                "name": "mart_joined_2",
            },
        )
    )
    ops.append(
        group_by_op(
            source="mart_joined_2",
            name="mart_grouped",
            group_by=("customer_id", "region"),
            measures=(
                ("m_0", "order_count", distinct_count_expr("order_id")),
                ("m_1", "item_count", 'COUNT("item_id")'),
                (
                    "m_2",
                    "completed_order_count",
                    filtered_count_expr("order_id", COMPLETED, distinct=True),
                ),
                ("m_3", "completed_units", filtered_sum_expr("quantity", COMPLETED)),
                ("m_4", "total_units", 'SUM("quantity")'),
                ("m_5", "distinct_product_count", distinct_count_expr("product_id")),
            ),
            predicate=COMPLETED_SCOPE,
        )
    )
    ops.append(
        MartOp(
            kind=MartOpKind.DERIVE,
            description=(
                "Name the mart columns and COALESCE total_units to 0, so a customer "
                "with no order items reports 0 and never NULL."
            ),
            tables=("mart_grouped",),
            columns=(
                "customer_id", "region", "order_count", "item_count",
                "completed_order_count", "completed_units", "total_units",
                "distinct_product_count",
            ),
            details={
                "select": (
                    '"customer_id" AS "customer_id", "region" AS "region", '
                    'm_0 AS "order_count", m_1 AS "item_count", '
                    'm_2 AS "completed_order_count", '
                    'COALESCE(m_3, 0) AS "completed_units", '
                    'COALESCE(m_4, 0) AS "total_units", '
                    'm_5 AS "distinct_product_count"'
                ),
                "name": "mart_defaults",
            },
        )
    )
    carried = (
        '"customer_id" AS "customer_id", "region" AS "region", '
        '"order_count" AS "order_count", "item_count" AS "item_count", '
        '"completed_order_count" AS "completed_order_count", '
        '"completed_units" AS "completed_units", "total_units" AS "total_units", '
        '"distinct_product_count" AS "distinct_product_count"'
    )
    ops.append(
        ratio_op(
            source="mart_defaults",
            name="mart_ratios",
            projections=(
                (carried, ""),
                (ratio_expr("completed_units", "total_units"), "completed_unit_share"),
                (
                    ratio_expr("completed_order_count", "order_count"),
                    "completed_order_share",
                ),
            ),
            units="fraction",
            null_result="0.0 when the customer has no units at all",
            rounding="half-up to 4 decimal places",
            description=(
                "Completed-unit share = completed_units / total_units, as a FRACTION "
                "between 0 and 1 (not a percentage), rounded to 4 decimal places, "
                "reported as 0.0 when the customer has no units at all."
            ),
        )
    )
    ops.append(
        conditional_op(
            source="mart_ratios",
            name="mart_bands",
            projections=(
                (carried + ', "completed_unit_share" AS "completed_unit_share", '
                 '"completed_order_share" AS "completed_order_share"', ""),
                (
                    threshold_ladder_expr(
                        '"completed_unit_share"',
                        ((">= 0.75", "high"), (">= 0.5", "medium"), ("> 0", "low")),
                        otherwise="none",
                    ),
                    "engagement_band",
                ),
                (
                    case_map_expr(
                        "region",
                        (("north", "domestic"), ("south", "domestic"),
                         ("export", "international")),
                        otherwise="unclassified",
                    ),
                    "region_group",
                ),
            ),
            domain="customers.region enum {north, south, export}; share thresholds 0.75 / 0.5 / 0",
            description=(
                "Band the completed-unit share: 'high' at 0.75 or above, 'medium' at "
                "0.5 or above (0.5 itself is medium), 'low' above 0, 'none' at 0. Map "
                "the declared region domain to a group, with any region outside "
                "{north, south, export} reported as 'unclassified'."
            ),
        )
    )
    ops.append(
        MartOp(
            kind=MartOpKind.TIE_BREAK,
            description="Deterministic total order by customer_id.",
            columns=("customer_id",),
        )
    )
    return MartPlan(
        mart="customer_order_profile",
        ops=tuple(ops),
        notes="Two-hop fan-out roll-up: customers <- orders <- order_items.",
    )


ROLLUP = MartSpec(
    name="customer_order_profile",
    description="One row per customer, with fan-out-safe counts and shares.",
    grain="one row per customer",
    key_columns=("customer_id",),
    columns=(
        MartColumn(name="customer_id", type=T.TEXT, description="Customer key.",
                   kind=K.PASSTHROUGH),
        MartColumn(name="region", type=T.TEXT, description="Declared region.",
                   kind=K.PASSTHROUGH),
        MartColumn(name="order_count", type=T.BIGINT,
                   description="Distinct orders.", kind=K.AGGREGATED),
        MartColumn(name="item_count", type=T.BIGINT,
                   description="Line items across all orders.", kind=K.AGGREGATED),
        MartColumn(name="completed_order_count", type=T.BIGINT,
                   description="Distinct completed orders.", kind=K.AGGREGATED),
        MartColumn(name="completed_units", type=T.BIGINT,
                   description="Units on completed orders; 0 when none.",
                   kind=K.AGGREGATED),
        MartColumn(name="total_units", type=T.BIGINT,
                   description="Units on all orders; 0 when none.", kind=K.AGGREGATED),
        MartColumn(name="distinct_product_count", type=T.BIGINT,
                   description="Distinct products ordered.", kind=K.AGGREGATED),
        MartColumn(name="completed_unit_share", type=T.FLOAT,
                   description="completed_units / total_units, fraction 0-1, 4 dp.",
                   kind=K.DERIVED),
        MartColumn(name="completed_order_share", type=T.FLOAT,
                   description="completed_order_count / order_count, fraction, 4 dp.",
                   kind=K.DERIVED),
        MartColumn(name="engagement_band", type=T.TEXT,
                   description="high/medium/low/none band of completed_unit_share.",
                   kind=K.CATEGORICAL),
        MartColumn(name="region_group", type=T.TEXT,
                   description="domestic/international/unclassified.",
                   kind=K.CATEGORICAL),
    ),
    plan=_rollup_plan(),
)


# ---------------------------------------------------------------------------
# Mart 2 — argmax profile: window + extrema
# ---------------------------------------------------------------------------

def _argmax_plan() -> MartPlan:
    ops: list[MartOp] = [
        MartOp(kind=MartOpKind.SOURCE, description=f"Read source table {t}.", tables=(t,))
        for t in ("customers", "orders")
    ]
    ops.append(
        MartOp(
            kind=MartOpKind.DERIVE,
            description="Project the grain of customers: customer_id.",
            tables=("customers",),
            columns=("customer_id",),
            details={
                "select": 'customers."customer_id" AS "customer_id"',
                "name": "cust_base",
            },
        )
    )
    ops.append(
        MartOp(
            kind=MartOpKind.DERIVE,
            description="Project the order facts the ranking needs.",
            tables=("orders",),
            columns=("customer_id", "order_id", "order_amount", "ordered_on"),
            details={
                "select": (
                    'orders."customer_id" AS "customer_id", '
                    'orders."order_id" AS "order_id", '
                    'orders."order_amount" AS "order_amount", '
                    'orders."ordered_on" AS "ordered_on"'
                ),
                "name": "ord_base",
            },
        )
    )
    ops.append(
        window_op(
            source="ord_base",
            name="ord_win",
            projections=(
                ('"customer_id"', "customer_id"),
                ('"order_id"', "order_id"),
                ('"order_amount"', "order_amount"),
                ('"ordered_on"', "ordered_on"),
                (
                    running_total_expr(
                        "order_amount", ("customer_id",), ("ordered_on", "order_id")
                    ),
                    "amount_to_date",
                ),
                (
                    lag_delta_expr(
                        "order_amount", ("customer_id",), ("ordered_on", "order_id")
                    ),
                    "amount_delta",
                ),
            ),
            description=(
                "Per customer, in order of ordered_on then order_id: the running "
                "total of order_amount through and including this order (explicit "
                "ROWS frame, so tied dates do not lump together), and the change in "
                "order_amount from the previous order (0 for a customer's first)."
            ),
        )
    )
    ops.append(
        extrema_op(
            source="ord_win",
            name="ord_top",
            partition_by=("customer_id",),
            measure="order_amount",
            tie_break="order_id",
# DESC makes tie-break removal observable; DuckDB's accidental ASC order would
# otherwise let the mutant survive.
            tie_break_direction="DESC",
            projections=(
                ('"customer_id"', "customer_id"),
                ('"order_id"', "top_order_id"),
                ('"order_amount"', "top_order_amount"),
                ('"amount_to_date"', "top_amount_to_date"),
                ('"amount_delta"', "top_amount_delta"),
            ),
        )
    )
    ops.append(
        MartOp(
            kind=MartOpKind.JOIN,
            description=(
                "LEFT JOIN the per-customer top order back onto every customer, so a "
                "customer with no orders is retained."
            ),
            tables=("cust_base", "ord_top"),
            columns=("customer_id",),
            join_type=JoinType.LEFT,
            predicate='ord_top."customer_id" = cust_base."customer_id"',
            details={
                "select": (
                    'cust_base."customer_id" AS "customer_id", '
                    'ord_top."top_order_id" AS "top_order_id", '
                    'ord_top."top_order_amount" AS "top_order_amount", '
                    'ord_top."top_amount_to_date" AS "top_amount_to_date", '
                    'ord_top."top_amount_delta" AS "top_amount_delta"'
                ),
                "name": "top_joined",
            },
        )
    )
    ops.append(
        MartOp(
            kind=MartOpKind.DERIVE,
            description=(
                "Name the mart columns; a customer with no orders reports 'none' and "
                "0, never NULL (COALESCE)."
            ),
            tables=("top_joined",),
            columns=(
                "customer_id", "top_order_id", "top_order_amount",
                "top_amount_to_date", "top_amount_delta",
            ),
            details={
                "select": (
                    '"customer_id" AS "customer_id", '
                    "COALESCE(\"top_order_id\", 'none') AS \"top_order_id\", "
                    'COALESCE("top_order_amount", 0) AS "top_order_amount", '
                    'COALESCE("top_amount_to_date", 0) AS "top_amount_to_date", '
                    'COALESCE("top_amount_delta", 0) AS "top_amount_delta"'
                ),
                "name": "mart_final",
            },
        )
    )
    ops.append(
        MartOp(
            kind=MartOpKind.TIE_BREAK,
            description="Deterministic total order by customer_id.",
            columns=("customer_id",),
        )
    )
    return MartPlan(
        mart="customer_top_order",
        ops=tuple(ops),
        notes="Argmax profile: the LABEL of the extremal order, not its MAX value.",
    )


ARGMAX = MartSpec(
    name="customer_top_order",
    description="One row per customer describing their largest order.",
    grain="one row per customer",
    key_columns=("customer_id",),
    columns=(
        MartColumn(name="customer_id", type=T.TEXT, description="Customer key.",
                   kind=K.PASSTHROUGH),
        MartColumn(name="top_order_id", type=T.TEXT,
                   description="Order id of the largest order, ties broken by the "
                               "HIGHEST order_id; 'none' if the customer has none.",
                   kind=K.RANKED),
        MartColumn(name="top_order_amount", type=T.BIGINT,
                   description="Amount of that order; 0 if none.", kind=K.RANKED),
        MartColumn(name="top_amount_to_date", type=T.BIGINT,
                   description="Running total through that order; 0 if none.",
                   kind=K.RANKED),
        MartColumn(name="top_amount_delta", type=T.BIGINT,
                   description="Change from the previous order; 0 if none.",
                   kind=K.RANKED),
    ),
    plan=_argmax_plan(),
)


def op_vocabulary_task() -> TaskIR:
    """The fixture task: two marts using every round-6 op kind."""
    return TaskIR(
        task_id="opvocab__demo",
        family_id="demo__op_vocabulary",
        cluster_id="demo__op_vocabulary",
        origin=Origin.DEMO,
        license="CC0-1.0",
        title="Op vocabulary fixture",
        tables=(CUSTOMERS, ORDERS, ORDER_ITEMS),
        relationships=(
            Relationship(
                child_table="orders",
                child_columns=("customer_id",),
                parent_table="customers",
                parent_columns=("customer_id",),
            ),
            Relationship(
                child_table="order_items",
                child_columns=("order_id",),
                parent_table="orders",
                parent_columns=("order_id",),
            ),
        ),
        backends=(
            BackendAssignment(table="customers", backend=Backend.POSTGRES),
            BackendAssignment(table="orders", backend=Backend.POSTGRES),
            BackendAssignment(table="order_items", backend=Backend.FILES),
        ),
        marts=(ROLLUP, ARGMAX),
    )


def load_witness_rows(con: duckdb.DuckDBPyConnection, task: TaskIR) -> None:
    """Create the source tables and insert the literal witness rows."""
    for table in task.tables:
        create_table(con, table)
        names = [c.name for c in table.columns]
        placeholders = ", ".join("?" for _ in names)
        quoted = ", ".join(quote(n) for n in names)
        con.executemany(
            f"INSERT INTO {quote(table.name)} ({quoted}) VALUES ({placeholders})",
            [[row[n] for n in names] for row in LITERAL_ROWS[table.name]],
        )


#: The mart rows the witness population MUST produce, read off the literal rows
#: by hand. C1 proves fan-out (2 orders, 3 items, 2 distinct products); C2 is
#: the childless parent (every measure 0, share 0.0, band 'none'); C3 has orders
#: but none completed (0 of 10 units); C4 is 1.0; C5 is out of domain; C6 sits
#: EXACTLY on the 0.5 boundary and is therefore 'medium', not 'low'.
EXPECTED_ROLLUP = [
    ("C1", "north", 2, 3, 1, 4, 6, 2, 0.6667, 0.5, "medium", "domestic"),
    ("C2", "north", 0, 0, 0, 0, 0, 0, 0.0, 0.0, "none", "domestic"),
    ("C3", "south", 2, 2, 0, 0, 10, 1, 0.0, 0.0, "none", "domestic"),
    ("C4", "export", 2, 2, 2, 4, 4, 2, 1.0, 1.0, "high", "international"),
    ("C5", "offshore", 1, 1, 1, 1, 1, 1, 1.0, 1.0, "high", "unclassified"),
    ("C6", "north", 2, 2, 1, 2, 4, 1, 0.5, 0.5, "medium", "domestic"),
]

EXPECTED_ARGMAX = [
    ("C1", "O-1001", 100, 100, 100),
    ("C2", "none", 0, 0, 0),
    ("C3", "O-1003", 70, 70, 70),
    # O-1005 and O-1006 tie at 40; the declared tie-break (order_id DESC) picks
    # O-1006 — which is NOT the row DuckDB returns when the tie-break is
    # dropped, so the mutant loses reward by execution instead of by argument.
    ("C4", "O-1006", 40, 80, 0),
    ("C5", "O-1007", 10, 10, 10),
    # C6's two orders also tie at 25; the declared tie-break takes O-1009.
    ("C6", "O-1009", 25, 50, 0),
]


def _run(task: TaskIR) -> dict[str, tuple[str, list[tuple]]]:
    con = duckdb.connect(":memory:")
    try:
        load_witness_rows(con, task)
        out: dict[str, tuple[str, list[tuple]]] = {}
        for mart in task.marts:
            sql = compile_plan_sql(task, mart)
            rows = execute_mart(con, mart, sql)
            out[mart.name] = (
                sql,
                [tuple(row[c.name] for c in mart.columns) for row in rows],
            )
        return out
    finally:
        con.close()


class OpVocabularyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.task = op_vocabulary_task()

    # -- vocabulary coverage ------------------------------------------------

    def test_fixture_uses_every_new_op_kind(self) -> None:
        kinds = {op.kind for mart in self.task.marts for op in mart.plan.ops}
        for kind in (
            MartOpKind.FILTERED_AGGREGATE,
            MartOpKind.EXTREMA,
            MartOpKind.WINDOW,
            MartOpKind.CONDITIONAL,
            MartOpKind.RATIO,
        ):
            self.assertIn(kind, kinds)

    def test_group_by_op_infers_the_strongest_label(self) -> None:
        agg = [
            op for op in ROLLUP.plan.ops if op.kind is MartOpKind.FILTERED_AGGREGATE
        ]
        self.assertEqual(len(agg), 1)
        distinct_only = group_by_op(
            source="mart_joined_2",
            name="g",
            group_by=("customer_id",),
            measures=(("m_0", "order_count", distinct_count_expr("order_id")),),
        )
        self.assertIs(distinct_only.kind, MartOpKind.DISTINCT)
        plain = group_by_op(
            source="mart_joined_2",
            name="g",
            group_by=("customer_id",),
            measures=(("m_0", "item_count", 'COUNT("item_id")'),),
        )
        self.assertIs(plain.kind, MartOpKind.AGGREGATE)

    def test_plan_validates(self) -> None:
        for mart in self.task.marts:
            self.assertEqual(validate_plan(self.task, mart.plan), [], mart.name)

    # -- the instrument -----------------------------------------------------

    def test_declared_column_kinds_agree_with_the_plan(self) -> None:
        for mart in self.task.marts:
            self.assertEqual(column_kind_problems(mart), [], mart.name)
            self.assertEqual(unclassified_columns(mart), [], mart.name)

    def test_validate_plan_refuses_a_declared_kind_the_plan_does_not_produce(self) -> None:
        """The certifier is WIRED, not advisory: a passthrough declared
        'ranked' used to validate clean and count as computed."""
        mart = self.task.mart(ROLLUP.name)
        tampered_columns = tuple(
            c.model_copy(update={"kind": K.RANKED}) if c.name == "customer_id" else c
            for c in mart.columns
        )
        tampered = mart.model_copy(update={"columns": tampered_columns})
        task = self.task.model_copy(
            update={"marts": tuple(tampered if m.name == mart.name else m for m in self.task.marts)}
        )
        problems = validate_plan(task, tampered.plan)
        self.assertTrue(
            any("declared kind 'ranked' but the plan's ops imply 'passthrough'" in p
                for p in problems),
            problems,
        )
        # `computed_column_count` still counts the lie (it is a raw count) —
        # which is exactly why the certifier has to run ahead of it.
        self.assertEqual(computed_column_count(tampered), computed_column_count(mart) + 1)

    def test_computed_columns_clear_the_anchor_floor(self) -> None:
        # Anchor minima: >= 6 target and >= 4 computed columns per TASK.
        target = sum(len(m.columns) for m in self.task.marts)
        computed = sum(computed_column_count(m) for m in self.task.marts)
        self.assertEqual((target, computed), (17, 14))
        self.assertGreaterEqual(target, 6)
        self.assertGreaterEqual(computed, 4)

    def test_computed_is_derived_not_settable(self) -> None:
        col = ROLLUP.columns[0]
        self.assertFalse(col.computed)
        # A free bool is a claim; a derived one is a fact. `computed` is a
        # property, so extra="forbid" rejects any attempt to assert it.
        with self.assertRaises(Exception):
            MartColumn(name="x", type=T.INTEGER, description="d", computed=True)

    def test_kind_is_hash_neutral_until_declared(self) -> None:
        bare = MartColumn(name="x", type=T.INTEGER, description="d")
        self.assertNotIn("kind", bare.model_dump(mode="json"))
        self.assertFalse(bare.classified)
        typed = bare.model_copy(update={"kind": K.AGGREGATED})
        self.assertEqual(typed.model_dump(mode="json")["kind"], "aggregated")
        self.assertTrue(typed.computed)

    def test_argmax_columns_are_ranked_but_the_partition_key_is_not(self) -> None:
        implied = column_kinds_from_plan(ARGMAX.plan)
        self.assertEqual(implied.get("top_order_id"), K.RANKED)
        self.assertNotIn("customer_id", implied)

    # -- determinism contracts (fail closed) --------------------------------

    def test_extremum_without_a_tie_break_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            extrema_op(
                source="ord_win",
                name="x",
                partition_by=("customer_id",),
                measure="order_amount",
                tie_break="",
                projections=(('"order_id"', "top_order_id"),),
            )
        loose = MartOp(
            kind=MartOpKind.EXTREMA,
            description="top order",
            tables=("ord_win",),
            columns=("top_order_id",),
            details={
                "select": '"order_id" AS "top_order_id"',
                "partition_by": '"customer_id"',
                "order_by": '"order_amount" DESC',
                "tie_break": "order_amount",
                "name": "x",
            },
        )
        problems = op_problems(loose)
        self.assertTrue(any("not total" in p for p in problems), problems)

    def test_extremum_projecting_an_aggregate_is_refused(self) -> None:
        # argmax, not MAX: an aggregate here means the plan kept the extremal
        # VALUE and threw away the row it came from.
        as_max = MartOp(
            kind=MartOpKind.EXTREMA,
            description="top order",
            tables=("ord_win",),
            columns=("top_order_amount",),
            details={
                "select": 'MAX("order_amount") AS "top_order_amount"',
                "partition_by": '"customer_id"',
                "order_by": '"order_amount" DESC, "order_id" DESC',
                "tie_break": "order_id",
                "name": "x",
            },
        )
        self.assertTrue(any("argmax, not MAX" in p for p in op_problems(as_max)))

    def test_running_total_without_a_frame_is_refused(self) -> None:
        bad = MartOp(
            kind=MartOpKind.WINDOW,
            description="running total",
            tables=("ord_base",),
            columns=("amount_to_date",),
            details={
                "select": (
                    'SUM("order_amount") OVER (PARTITION BY "customer_id" '
                    'ORDER BY "ordered_on") AS "amount_to_date"'
                ),
                "name": "x",
            },
        )
        self.assertTrue(any("frame" in p for p in op_problems(bad)))

    def test_case_without_else_is_refused(self) -> None:
        bad = MartOp(
            kind=MartOpKind.CONDITIONAL,
            description="band",
            tables=("mart_ratios",),
            columns=("engagement_band",),
            details={
                "select": (
                    'CASE WHEN "completed_unit_share" >= 0.5 THEN \'medium\' END '
                    'AS "engagement_band"'
                ),
                "domain": "share thresholds",
                "name": "x",
            },
        )
        self.assertTrue(any("ELSE" in p for p in op_problems(bad)))

    def test_unguarded_ratio_is_refused(self) -> None:
        bad = MartOp(
            kind=MartOpKind.RATIO,
            description="share",
            tables=("mart_defaults",),
            columns=("completed_unit_share",),
            details={
                "select": '"completed_units" / "total_units" AS "completed_unit_share"',
                "units": "fraction",
                "null_result": "0.0",
                "rounding": "4 dp",
                "name": "x",
            },
        )
        problems = op_problems(bad)
        self.assertTrue(any("NULLIF" in p for p in problems), problems)
        self.assertTrue(any("CAST" in p for p in problems), problems)
        self.assertTrue(any("ROUND" in p for p in problems), problems)

    def test_ratio_without_units_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ratio_op(
                source="mart_defaults",
                name="x",
                projections=((ratio_expr("a", "b"), "share"),),
                units="",
                null_result="0.0",
                rounding="4 dp",
                description="share",
            )

    def test_mislabelled_aggregate_is_refused(self) -> None:
        # A CONSTRUCTION-time gate, not a validate_plan one: plans that predate
        # this vocabulary carry COUNT(DISTINCT) on a plain AGGREGATE op (the
        # demo fixture does, and its content hash is pinned).
        lying = MartOp(
            kind=MartOpKind.AGGREGATE,
            description="counts",
            tables=("mart_joined_2",),
            columns=("customer_id", "order_count"),
            details={
                "group_by": '"customer_id"',
                "name": "x",
                "m_0": distinct_count_expr("order_id"),
            },
        )
        self.assertEqual(op_problems(lying), [])
        problems = label_problems(lying)
        self.assertTrue(any("filtered_aggregate" in p for p in problems), problems)

    def test_compiler_refuses_what_validate_plan_refuses(self) -> None:
        broken = ARGMAX.plan.ops
        idx = next(i for i, op in enumerate(broken) if op.kind is MartOpKind.EXTREMA)
        mutated = list(broken)
        details = dict(mutated[idx].details)
        details["order_by"] = '"order_amount" DESC'
        mutated[idx] = mutated[idx].model_copy(update={"details": details})
        mart = ARGMAX.model_copy(
            update={"plan": ARGMAX.plan.model_copy(update={"ops": tuple(mutated)})}
        )
        task = self.task.model_copy(update={"marts": (ROLLUP, mart)})
        self.assertNotEqual(validate_plan(task, mart.plan), [])
        with self.assertRaises(PlanCompilationError):
            compile_plan_sql(task, mart)

    # -- attack routing -----------------------------------------------------

    def test_new_kinds_declare_their_attack_surface(self) -> None:
        from elt_taskgen.generation.mart_plan import attack_surface

        rollup = attack_surface(ROLLUP.plan)
        agg_idx = next(
            i for i, op in enumerate(ROLLUP.plan.ops)
            if op.kind is MartOpKind.FILTERED_AGGREGATE
        )
        self.assertIn(agg_idx, rollup[AttackKind.DROPPED_FILTER])
        self.assertIn(agg_idx, rollup[AttackKind.NO_DEDUP])
        self.assertIn(agg_idx, rollup[AttackKind.WRONG_GRAIN])
        ratio_idx = next(
            i for i, op in enumerate(ROLLUP.plan.ops) if op.kind is MartOpKind.RATIO
        )
        self.assertIn(ratio_idx, rollup[AttackKind.WRONG_DENOMINATOR])

        argmax = attack_surface(ARGMAX.plan)
        extrema_idx = next(
            i for i, op in enumerate(ARGMAX.plan.ops) if op.kind is MartOpKind.EXTREMA
        )
        window_idx = next(
            i for i, op in enumerate(ARGMAX.plan.ops) if op.kind is MartOpKind.WINDOW
        )
        self.assertEqual(
            sorted(argmax[AttackKind.WRONG_WINDOW]), sorted([window_idx, extrema_idx])
        )

    # -- prose firewall -----------------------------------------------------

    def test_solver_safe_summary_withholds_compiler_detail(self) -> None:
        for mart in self.task.marts:
            summary = solver_safe_plan_summary(self.task, mart)
            self.assertNotIn("predicate:", summary)
            for op in mart.plan.ops:
                for key in ("select", "sql", "group_by", "order_by", "partition_by"):
                    value = op.details.get(key)
                    if value:
                        self.assertNotIn(value, summary)
                for key, value in op.details.items():
                    if key.startswith("m_"):
                        self.assertNotIn(value, summary)
            # The determinizing intent MUST survive: units, tie-break, domain.
            if any(op.kind is MartOpKind.RATIO for op in mart.plan.ops):
                self.assertIn("units=fraction", summary)
            if any(op.kind is MartOpKind.EXTREMA for op in mart.plan.ops):
                self.assertIn("tie_break=order_id", summary)

    # -- execution: the proof ----------------------------------------------

    def test_compiles_and_executes_on_duckdb(self) -> None:
        results = _run(self.task)
        self.assertIn("QUALIFY ROW_NUMBER() OVER", results["customer_top_order"][0])
        self.assertNotIn("WHERE", results["customer_top_order"][0].upper())
        self.assertEqual(results["customer_order_profile"][1], EXPECTED_ROLLUP)
        self.assertEqual(results["customer_top_order"][1], EXPECTED_ARGMAX)

    def test_witness_rows_separate_right_from_wrong(self) -> None:
        """The population must KILL the plausible wrong implementations."""
        rows = {r[0]: r for r in _run(self.task)["customer_order_profile"][1]}
        # row D: fan-out — COUNT(item) != COUNT(DISTINCT order) != DISTINCT product
        self.assertEqual((rows["C1"][2], rows["C1"][3], rows["C1"][7]), (2, 3, 2))
        # row E: filtered aggregate must be 0 while the total is 10. A WHERE
        # would have dropped C3 entirely.
        self.assertEqual((rows["C3"][4], rows["C3"][6]), (0, 10))
        # row B: the childless parent survives with zeros, not NULLs.
        self.assertEqual(rows["C2"][2:9], (0, 0, 0, 0, 0, 0, 0.0))
        # row G: the out-of-domain region falls to the mandatory ELSE.
        self.assertEqual(rows["C5"][11], "unclassified")
        # row H: exactly on the 0.5 boundary -> '>=' says medium, '>' says low.
        self.assertEqual((rows["C6"][8], rows["C6"][10]), (0.5, "medium"))


#: Each tuple defines a plausible wrong implementation for one construct, not
#: an arbitrary text edit.
ROLLUP_MUTANTS = (
    ("no_dedup", 'COUNT(DISTINCT "order_id")', 'COUNT("order_id")'),
    (
        "dropped_filter",
        "SUM(CASE WHEN \"order_status\" = 'completed' THEN \"quantity\" ELSE 0 END)",
        'SUM("quantity")',
    ),
    (
        "filter_to_where",
        'GROUP BY "customer_id", "region"',
        "WHERE \"order_status\" = 'completed' GROUP BY \"customer_id\", \"region\"",
    ),
    ("wrong_boundary_gte", '"completed_unit_share" >= 0.5', '"completed_unit_share" > 0.5'),
    ("wrong_boundary_else", "ELSE 'unclassified' END", "END"),
    ("wrong_denominator", 'NULLIF("total_units", 0)', "1"),
)

ARGMAX_MUTANTS = (
    (
        # The mutation site carries NULLS LAST because `extrema_op` now WRITES
        # the null placement instead of inheriting DuckDB's session default.
        # The mutant is unchanged in meaning: drop the tie-break term, leaving
        # a non-total order.
        "wrong_window_tiebreak",
        'ORDER BY "order_amount" DESC NULLS LAST, "order_id" DESC NULLS LAST) = 1',
        'ORDER BY "order_amount" DESC NULLS LAST) = 1',
    ),
    (
        "wrong_window_partition",
        'OVER (PARTITION BY "customer_id" ORDER BY "order_amount" DESC NULLS LAST',
        'OVER (ORDER BY "order_amount" DESC NULLS LAST',
    ),
    (
        "argmax_as_max",
        '"order_id" AS "top_order_id"',
        'CAST("order_amount" AS VARCHAR) AS "top_order_id"',
    ),
)


class WitnessKillTest(unittest.TestCase):
    """Every construct must LOSE REWARD under its wrong implementation.

    Measured by execution on the witness rows, never by structural argument:
    a construct whose mutant produces identical output is decorative, and the
    corpus already carries 61 marts advertising a surface nothing can realize.
    """

    def setUp(self) -> None:
        self.task = op_vocabulary_task()
        self.con = duckdb.connect(":memory:")
        load_witness_rows(self.con, self.task)
        self.gold = {
            m.name: compile_plan_sql(self.task, m) for m in self.task.marts
        }

    def tearDown(self) -> None:
        self.con.close()

    def _assert_killed(self, mart: str, name: str, find: str, replace: str) -> None:
        sql = self.gold[mart]
        self.assertIn(find, sql, f"{name}: gold SQL no longer contains the mutation site")
        gold_rows = self.con.execute(sql).fetchall()
        mutant_rows = self.con.execute(sql.replace(find, replace)).fetchall()
        self.assertNotEqual(
            gold_rows, mutant_rows, f"{name} SURVIVED: the witness rows do not kill it"
        )

    def test_rollup_constructs_are_killable(self) -> None:
        for name, find, replace in ROLLUP_MUTANTS:
            with self.subTest(name):
                self._assert_killed("customer_order_profile", name, find, replace)

    def test_argmax_constructs_are_killable(self) -> None:
        for name, find, replace in ARGMAX_MUTANTS:
            with self.subTest(name):
                self._assert_killed("customer_top_order", name, find, replace)

    def test_running_total_frame_is_a_determinism_guarantee_not_a_surface(self) -> None:
        """HONEST NEGATIVE RESULT, pinned so nobody claims otherwise later.

        Dropping the explicit ROWS frame from the running total does NOT change
        the answer here, and cannot: RANGE and ROWS differ only on rows that TIE
        on the window's ORDER BY key, and this window orders by (ordered_on,
        order_id) — a total order, which is the other half of the same contract.
        The frame requirement therefore buys determinism, not attackability. It
        stays mandatory for that reason; anyone counting it as a mutation
        surface would be counting a kill that never happens.
        """
        sql = self.gold["customer_top_order"]
        unframed = sql.replace(" ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW", "")
        self.assertNotEqual(sql, unframed)
        self.assertEqual(
            self.con.execute(sql).fetchall(), self.con.execute(unframed).fetchall()
        )


def main() -> None:  # pragma: no cover - the proof rendering
    task = op_vocabulary_task()
    for name, (sql, rows) in _run(task).items():
        print(f"\n===== {name} =====\n{sql}\n")
        header = [c.name for c in task.mart(name).columns]
        print(" | ".join(header))
        for row in rows:
            print(" | ".join(str(v) for v in row))


if __name__ == "__main__":  # pragma: no cover
    main()

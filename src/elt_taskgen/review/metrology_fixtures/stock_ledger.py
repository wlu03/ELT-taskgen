"""Define the `stock_ledger` metrology fixture family.

Most strings are hashed task data; changing them rekeys the family. The schema exercises
a text key, conditional quantities, adjustments, dedupe-sensitive counts, nullable
dates, and an inclusive reorder threshold.
"""

from __future__ import annotations

from elt_taskgen.generation.populations import el_attack_cases
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
from elt_taskgen.review.metrology_fixtures.families import (
    AmbiguityVariant,
    FamilyDefinition,
    FeasibilityVariant,
    PopulationVariant,
    ShortcutVariant,
    _metrology,
    build_family,
)

FAMILY_NAME = "stock_ledger"
TASK_ID = "metrology__stock_ledger"
FAMILY_ID = "metrology__stock_ledger"
CLUSTER_ID = "metrology__stock_ledger"
MART_NAME = "product_stock"

#: Contamination canary GUID (pool source only; never rendered).
CANARY_GUID = "elt-taskgen-metrology-family-canary:stock_ledger:6d8e1a53-27c4-4f0b-b9e2-3a5c7f1d8b92"

P = PopulationName

# Counterfactual literal rows: three products, one of them exactly on its
# reorder threshold and one with no moves at all.
COUNTERFACTUAL_LITERAL_ROWS: dict[str, tuple[Row, ...]] = {
    "products": (
        {"sku": "P-901", "category": "tools", "reorder_point": 10},
        {"sku": "P-902", "category": "garden", "reorder_point": 5},
        {"sku": "P-903", "category": "kitchen", "reorder_point": 12},
    ),
    "stock_moves": (
        {"move_id": 9021, "sku": "P-902", "move_type": "inbound", "quantity": 20,
         "moved_on": "2024-01-05"},
        {"move_id": 9022, "sku": "P-902", "move_type": "outbound", "quantity": 8,
         "moved_on": "2024-01-09"},
        {"move_id": 9022, "sku": "P-902", "move_type": "outbound", "quantity": 8,
         "moved_on": "2024-01-09"},  # exact duplicate row
        {"move_id": 9023, "sku": "P-902", "move_type": "adjustment", "quantity": 3,
         "moved_on": "2024-01-02"},
        {"move_id": 9031, "sku": "P-903", "move_type": "inbound", "quantity": 12,
         "moved_on": "2024-02-01"},
        {"move_id": 9091, "sku": None, "move_type": "inbound", "quantity": 50,
         "moved_on": "2024-03-01"},
    ),
}

#: Expected counterfactual mart, sorted by sku (pinned by test).
COUNTERFACTUAL_EXPECTED_MART: tuple[Row, ...] = (
    {"sku": "P-901", "category": "tools", "inbound_units": 0, "outbound_units": 0,
     "net_units": 0, "move_count": 0, "last_moved_on": None, "stock_state": "reorder"},
    {"sku": "P-902", "category": "garden", "inbound_units": 20, "outbound_units": 8,
     "net_units": 15, "move_count": 3, "last_moved_on": "2024-01-09",
     "stock_state": "stocked"},
    {"sku": "P-903", "category": "kitchen", "inbound_units": 12, "outbound_units": 0,
     "net_units": 12, "move_count": 1, "last_moved_on": "2024-02-01",
     "stock_state": "reorder"},
)

REFERENCE_SQL = """\
WITH moves AS (
    SELECT DISTINCT move_id, sku, move_type, quantity, moved_on
    FROM stock_moves
),
per_sku AS (
    SELECT
        sku,
        SUM(CASE WHEN move_type = 'inbound' THEN quantity ELSE 0 END) AS inbound_units,
        SUM(CASE WHEN move_type = 'outbound' THEN quantity ELSE 0 END) AS outbound_units,
        SUM(CASE WHEN move_type = 'adjustment' THEN quantity ELSE 0 END) AS adjustment_units,
        COUNT(DISTINCT move_id) AS move_count,
        MAX(moved_on) AS last_moved_on
    FROM moves
    GROUP BY sku
)
SELECT
    p.sku AS sku,
    p.category AS category,
    COALESCE(m.inbound_units, 0) AS inbound_units,
    COALESCE(m.outbound_units, 0) AS outbound_units,
    COALESCE(m.inbound_units, 0) - COALESCE(m.outbound_units, 0)
        + COALESCE(m.adjustment_units, 0) AS net_units,
    COALESCE(m.move_count, 0) AS move_count,
    m.last_moved_on AS last_moved_on,
    CASE
        WHEN COALESCE(m.inbound_units, 0) - COALESCE(m.outbound_units, 0)
            + COALESCE(m.adjustment_units, 0) <= p.reorder_point THEN 'reorder'
        ELSE 'stocked'
    END AS stock_state
FROM products AS p
LEFT JOIN per_sku AS m ON m.sku = p.sku
ORDER BY p.sku
"""

# Products with no moves are dropped instead of reported as zeros.
_INNER_JOIN_SQL = REFERENCE_SQL.replace("LEFT JOIN per_sku AS m", "INNER JOIN per_sku AS m")

# The duplicate move row is summed twice (the count survives through DISTINCT).
_NO_DEDUP_SQL = REFERENCE_SQL.replace(
    "SELECT DISTINCT move_id, sku, move_type, quantity, moved_on",
    "SELECT move_id, sku, move_type, quantity, moved_on",
)

# inbound_units sums every move whatever its kind.
_NO_TYPE_GUARD_SQL = REFERENCE_SQL.replace(
    "SUM(CASE WHEN move_type = 'inbound' THEN quantity ELSE 0 END) AS inbound_units",
    "SUM(quantity) AS inbound_units",
)

# Adjustments never enter net_units.
_ADJUSTMENTS_IGNORED_SQL = REFERENCE_SQL.replace(
    "    COALESCE(m.inbound_units, 0) - COALESCE(m.outbound_units, 0)\n"
    "        + COALESCE(m.adjustment_units, 0) AS net_units,\n",
    "    COALESCE(m.inbound_units, 0) - COALESCE(m.outbound_units, 0) AS net_units,\n",
)

# NULL instead of 0 for products without moves.
_NO_COALESCE_SQL = REFERENCE_SQL.replace(
    "    COALESCE(m.inbound_units, 0) AS inbound_units,\n"
    "    COALESCE(m.outbound_units, 0) AS outbound_units,\n"
    "    COALESCE(m.inbound_units, 0) - COALESCE(m.outbound_units, 0)\n"
    "        + COALESCE(m.adjustment_units, 0) AS net_units,\n"
    "    COALESCE(m.move_count, 0) AS move_count,\n",
    "    m.inbound_units AS inbound_units,\n"
    "    m.outbound_units AS outbound_units,\n"
    "    m.inbound_units - m.outbound_units + m.adjustment_units AS net_units,\n"
    "    m.move_count AS move_count,\n",
)

# The threshold read as exclusive: a product exactly on it is 'stocked'.
_THRESHOLD_EXCLUSIVE_SQL = REFERENCE_SQL.replace(
    "+ COALESCE(m.adjustment_units, 0) <= p.reorder_point THEN 'reorder'",
    "+ COALESCE(m.adjustment_units, 0) < p.reorder_point THEN 'reorder'",
)

HARDCODE_PRIMARY_DIRECTIVE = "directive:hardcode-population-outputs:primary"


def mart_plan() -> MartPlan:
    """Declarative record of the intended relational ops for the mart."""
    return MartPlan(
        mart=MART_NAME,
        ops=(
            MartOp(
                kind=MartOpKind.DEDUPE,
                description=(
                    "Deduplicate exact-duplicate stock_moves rows: DISTINCT "
                    "(move_id, sku, move_type, quantity, moved_on), so a move "
                    "recorded twice is one move."
                ),
                tables=("stock_moves",),
                columns=("move_id", "sku", "move_type", "quantity", "moved_on"),
            ),
            MartOp(
                kind=MartOpKind.FILTERED_AGGREGATE,
                description=(
                    # EVERY MEASURE NAMES ITS OWN SET: the move-kind selection
                    # lives inside each measure, never in a WHERE.
                    "Per sku over its moves: inbound_units = SUM(quantity) over "
                    "moves whose move_type = 'inbound'; outbound_units = "
                    "SUM(quantity) over moves whose move_type = 'outbound'; "
                    "adjustment_units = SUM(quantity) over moves whose move_type = "
                    "'adjustment'; move_count = COUNT(DISTINCT move_id) over all "
                    "moves; last_moved_on = MAX(moved_on) over all moves. Within "
                    "an existing sku group, a missing move type contributes 0 to "
                    "its per-type sum."
                ),
                tables=("stock_moves",),
                columns=(
                    "sku", "inbound_units", "outbound_units", "adjustment_units",
                    "move_count", "last_moved_on",
                ),
                predicate=(
                    "move_type = 'inbound' | 'outbound' | 'adjustment', selected "
                    "INSIDE each sum; move_count and last_moved_on take every move"
                ),
                details={
                    "group_by": "sku",
                    "name": "per_sku",
                    # Compiler-only projection (withheld from every public
                    # rendering): binds the intermediate `adjustment_units`
                    # alias the DERIVE op below consumes.
                    "select": (
                        "sku, "
                        "SUM(CASE WHEN move_type = 'inbound' THEN quantity ELSE 0 END) AS inbound_units, "
                        "SUM(CASE WHEN move_type = 'outbound' THEN quantity ELSE 0 END) AS outbound_units, "
                        "SUM(CASE WHEN move_type = 'adjustment' THEN quantity ELSE 0 END) AS adjustment_units, "
                        "COUNT(DISTINCT move_id) AS move_count, "
                        "MAX(moved_on) AS last_moved_on"
                    ),
                    "inbound_units": "SUM(CASE WHEN move_type = 'inbound' THEN quantity ELSE 0 END)",
                    "outbound_units": "SUM(CASE WHEN move_type = 'outbound' THEN quantity ELSE 0 END)",
                    "adjustment_units": (
                        "SUM(CASE WHEN move_type = 'adjustment' THEN quantity ELSE 0 END)"
                    ),
                    "move_count": "COUNT(DISTINCT move_id)",
                    "last_moved_on": "MAX(moved_on)",
                },
            ),
            MartOp(
                kind=MartOpKind.JOIN,
                description=(
                    "LEFT JOIN the per-sku measures onto products so products with "
                    "no moves are retained. Moves whose sku is NULL belong to no "
                    "product: they are excluded from every measure, and the mart "
                    "never emits a row whose sku is NULL."
                ),
                tables=("products", "stock_moves"),
                columns=("sku", "category"),
                join_type=JoinType.LEFT,
                predicate="stock_moves.sku = products.sku",
            ),
            MartOp(
                kind=MartOpKind.DERIVE,
                description=(
                    "net_units = inbound_units - outbound_units + adjustment_units: "
                    "an adjustment moves stock without being a receipt or an issue."
                ),
                columns=("net_units", "inbound_units", "outbound_units", "adjustment_units"),
                predicate="inbound_units - outbound_units + adjustment_units",
            ),
            MartOp(
                kind=MartOpKind.DERIVE,
                description=(
                    "For products without moves, interpret the preceding "
                    "net_units rule with inbound_units = 0, outbound_units = 0 "
                    "and adjustment_units = 0, yielding net_units = 0. "
                    "adjustment_units is an internal term, not a mart output, "
                    "and is never NULL: rule 2 fixes it for an existing sku "
                    "group with no adjustment move, and this rule fixes it for "
                    "a product with no moves. The COALESCE list names only the "
                    "published inbound_units, outbound_units, net_units and "
                    "move_count; set each to 0 (never NULL). last_moved_on stays NULL. "
                    "The next rule derives stock_state from this final net_units."
                ),
                columns=(
                    "inbound_units", "outbound_units", "adjustment_units",
                    "net_units", "move_count", "last_moved_on",
                ),
                predicate=(
                    "for a no-move product: inbound_units := 0; "
                    "outbound_units := 0; adjustment_units := 0; net_units := 0; "
                    "move_count := 0; last_moved_on untouched"
                ),
            ),
            MartOp(
                kind=MartOpKind.CONDITIONAL,
                description=(
                    "stock_state = 'reorder' when net_units <= reorder_point (the "
                    "product's own threshold, inclusive), else 'stocked'."
                ),
                tables=("products",),
                columns=("stock_state", "net_units", "reorder_point"),
                predicate="CASE WHEN net_units <= reorder_point THEN 'reorder' ELSE 'stocked' END",
            ),
            MartOp(
                kind=MartOpKind.TIE_BREAK,
                description="Deterministic output order: sort by sku.",
                columns=("sku",),
            ),
        ),
        notes=(
            "Include products with no moves; select the move kind inside each "
            "sum; adjustments enter net_units; the reorder boundary is inclusive."
        ),
    )


def _populations() -> tuple[PopulationSpec, ...]:
    return (
        PopulationSpec(
            name=P.DEVELOPMENT,
            seed=derive_seed(TASK_ID, P.DEVELOPMENT.value),
            scale={"products": 3, "stock_moves": 12},
            conditions=(
                "Tiny debug data: exactly three products.",
                "Every product has at least one move of every move_type "
                "(INNER JOIN is indistinguishable here by design).",
                "No NULL sku, no duplicate rows.",
            ),
        ),
        PopulationSpec(
            name=P.PRIMARY,
            seed=derive_seed(TASK_ID, P.PRIMARY.value),
            scale={"products": 300, "stock_moves": 1500},
            conditions=(
                "Approximately 300 products.",
                "Adjustment moves are present.",
                "Some products have no moves at all.",
                # NO 'dangling' TOKEN: it is source_data's _DANGLING_FRAC lever.
                "Some moves have NULL sku; they belong to no product and "
                "contribute to no output row.",
                "Products at or below their reorder_point and products above it "
                "both occur.",
            ),
        ),
        PopulationSpec(
            name=P.RESAMPLED,
            seed=derive_seed(TASK_ID, P.RESAMPLED.value),
            scale={"products": 300, "stock_moves": 1500},
            conditions=(
                "Same generator and conditions as primary; new seed and new id "
                "ranges (memorization check).",
            ),
        ),
        PopulationSpec(
            name=P.COUNTERFACTUAL,
            seed=derive_seed(TASK_ID, P.COUNTERFACTUAL.value),
            conditions=(
                "Literal constructed rows only — three products.",
                "P-901: a product with no moves -> every quantity 0, move_count 0, "
                "last_moved_on NULL, stock_state 'reorder' (0 is at or below its "
                "reorder_point of 10).",
                "P-902: an inbound move of 20, an outbound move of 8 recorded "
                "twice as an exact duplicate row, and an adjustment of 3 -> "
                "inbound_units 20, outbound_units 8 (row-grain summing gives 16), "
                "net_units 15, move_count 3, last_moved_on 2024-01-09, "
                "stock_state 'stocked' against a reorder_point of 5.",
                "P-903: one inbound move of exactly 12 units against a "
                "reorder_point of 12 -> net_units 12 = reorder_point, the "
                "inclusive boundary: stock_state 'reorder'.",
                "One inbound move with NULL sku (50 units) belongs to no product "
                "and contributes to no output row.",
            ),
            literal_rows=COUNTERFACTUAL_LITERAL_ROWS,
        ),
        PopulationSpec(
            name=P.STRESS,
            seed=derive_seed(TASK_ID, P.STRESS.value),
            scale={"products": 120, "stock_moves": 8000},
            conditions=(
                "A few hot products hold a large share of all moves.",
                "Exact-duplicate stock_moves rows are present (correct logic must dedupe).",
                "Ties: distinct products with identical totals.",
                "Every product has at least one move of every move_type "
                "(INNER JOIN is indistinguishable here by design).",
            ),
        ),
    )


def _attack_cases() -> tuple[AttackCase, ...]:
    return (
        AttackCase(
            name="inner_join",
            kind=AttackKind.INNER_JOIN,
            description=(
                "LEFT JOIN replaced with INNER JOIN: products without moves are "
                "dropped instead of reported as zeros and 'reorder'."
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
            name="no_dedup",
            kind=AttackKind.NO_DEDUP,
            description=(
                "The DISTINCT over move rows is dropped: a move recorded twice is "
                "summed twice."
            ),
            mutation=_NO_DEDUP_SQL,
            expected_pass={P.DEVELOPMENT: True, P.STRESS: False, P.COUNTERFACTUAL: False},
        ),
        AttackCase(
            name="no_move_type_guard",
            kind=AttackKind.DROPPED_FILTER,
            description="inbound_units sums every move whatever its move_type.",
            mutation=_NO_TYPE_GUARD_SQL,
            expected_pass={
                P.DEVELOPMENT: False,
                P.PRIMARY: False,
                P.RESAMPLED: False,
                P.STRESS: False,
                P.COUNTERFACTUAL: False,
            },
        ),
        AttackCase(
            name="adjustments_ignored",
            kind=AttackKind.CUSTOM,
            description="net_units drops the adjustment term.",
            mutation=_ADJUSTMENTS_IGNORED_SQL,
            expected_pass={
                P.DEVELOPMENT: False,
                P.PRIMARY: False,
                P.RESAMPLED: False,
                P.STRESS: False,
                P.COUNTERFACTUAL: False,
            },
        ),
        AttackCase(
            name="no_coalesce",
            kind=AttackKind.NO_NULL_DEFAULT,
            description=(
                "Drops COALESCE: products without moves get NULL quantities and "
                "count instead of 0."
            ),
            mutation=_NO_COALESCE_SQL,
            expected_pass={
                P.DEVELOPMENT: True,
                P.STRESS: True,
                P.PRIMARY: False,
                P.RESAMPLED: False,
                P.COUNTERFACTUAL: False,
            },
        ),
        AttackCase(
            name="threshold_exclusive",
            kind=AttackKind.CUSTOM,
            description=(
                "The reorder boundary read as strict: a product whose net_units "
                "equals its reorder_point is 'stocked' instead of 'reorder'."
            ),
            mutation=_THRESHOLD_EXCLUSIVE_SQL,
            expected_pass={P.COUNTERFACTUAL: False},
        ),
    )


def task() -> TaskIR:
    """The complete stock_ledger candidate as a validated TaskIR."""
    tables = (
        TableSpec(
            name="products",
            description="One row per product.",
            columns=(
                ColumnSpec(name="sku", type=ColumnType.TEXT,
                           description="Stock-keeping unit code; unique per product."),
                ColumnSpec(name="category", type=ColumnType.TEXT,
                           enum_values=("tools", "garden", "kitchen"),
                           description="Merchandise category."),
                # THRESHOLD-NEUTRAL: naming 'at or below' here would restate the
                # stock_state rule and repair two ambiguity specimens.
                ColumnSpec(name="reorder_point", type=ColumnType.INTEGER,
                           description="Replenishment threshold configured for the product."),
            ),
            primary_key=("sku",),
        ),
        TableSpec(
            name="stock_moves",
            description="One row per stock movement (duplicates possible under stress).",
            columns=(
                ColumnSpec(name="move_id", type=ColumnType.INTEGER,
                           description="Unique movement identifier."),
                ColumnSpec(name="sku", type=ColumnType.TEXT, nullable=True,
                           description="Product moved; may be NULL."),
                ColumnSpec(name="move_type", type=ColumnType.TEXT,
                           enum_values=("inbound", "outbound", "adjustment"),
                           description="Kind of movement."),
                ColumnSpec(name="quantity", type=ColumnType.INTEGER,
                           description="Units moved (always positive)."),
                ColumnSpec(name="moved_on", type=ColumnType.DATE,
                           description="Date of the movement."),
            ),
            primary_key=(),  # duplicates of full rows allowed under stress
            business_key=("move_id",),
        ),
    )
    backends = (
        BackendAssignment(table="products", backend=Backend.S3),
        BackendAssignment(table="stock_moves", backend=Backend.POSTGRES),
    )
    populations = _populations()
    return TaskIR(
        task_id=TASK_ID,
        family_id=FAMILY_ID,
        cluster_id=CLUSTER_ID,
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        attribution="elt-taskgen metrology fixture family stock_ledger",
        title="Product stock position",
        tables=tables,
        relationships=(
            Relationship(
                child_table="stock_moves",
                child_columns=("sku",),
                parent_table="products",
                parent_columns=("sku",),
                required=False,  # NULL sku allowed (primary population)
            ),
        ),
        backends=backends,
        marts=(
            MartSpec(
                name=MART_NAME,
                description="Per-product stock position summary.",
                grain="One row per product, including products with no moves.",
                key_columns=("sku",),
                columns=(
                    MartColumn(name="sku", type=ColumnType.TEXT,
                               description="Stock-keeping unit code."),
                    MartColumn(name="category", type=ColumnType.TEXT,
                               description="Merchandise category, copied from the source."),
                    MartColumn(name="inbound_units", type=ColumnType.INTEGER,
                               description="Sum of quantity over the product's DISTINCT "
                                           "inbound moves; 0 if none."),
                    MartColumn(name="outbound_units", type=ColumnType.INTEGER,
                               description="Sum of quantity over the product's DISTINCT "
                                           "outbound moves; 0 if none."),
                    MartColumn(name="net_units", type=ColumnType.INTEGER,
                               description="inbound_units minus outbound_units plus the "
                                           "product's adjustment quantity; 0 if none."),
                    MartColumn(name="move_count", type=ColumnType.INTEGER,
                               description="Count of DISTINCT moves of the product; 0 if none."),
                    MartColumn(name="last_moved_on", type=ColumnType.DATE,
                               description="Most recent moved_on among the product's "
                                           "moves; NULL for a product with no moves."),
                    MartColumn(name="stock_state", type=ColumnType.TEXT,
                               description="'reorder' or 'stocked' from net_units and "
                                           "reorder_point as the rules state."),
                ),
                plan=mart_plan(),
            ),
        ),
        populations=populations,
        reference=ReferenceSolution(
            implementation_id="metrology_stock_ledger_ref",
            dialect="duckdb",
            sql_by_mart={MART_NAME: REFERENCE_SQL},
            load_notes=(
                "Load products from the S3 jsonl parts and stock_moves from the "
                "postgres load SQL into DuckDB tables of the same names."
            ),
            provenance="Constructed from the mart plan in the family spec.",
            version="1",
        ),
        attack_cases=_attack_cases()
        + el_attack_cases(
            tables,
            backend_assignments=backends,
            backends=len(backends),
            populations=populations,
        ),
    )


# --- Specimen declarations -------------------------------------------------

_m = _metrology

_QUANTITIES = ("inbound_units", "outbound_units", "net_units", "move_count")
_ZERO_IF_NONE = tuple((column, "; 0 if none", "") for column in _QUANTITIES)

_JOIN_BLIND_TERMS = (
    "inner join", "left join", "no moves", "without moves", "without any moves",
    "no stock moves", "never moved", "unmatched", "cannot distinguish",
    "indistinguishable", "never exercised", "not exercised", "would still score",
    "full reward",
)

_DECOY_NOTES = (
    "Note on ordering: the rule above fixes the output row order completely; "
    "there is no remaining tie to break.",
    "Note on empty results: products with no moves are still emitted, with zero "
    "(never NULL) quantities and a NULL last_moved_on, exactly as the rules state.",
    "Note on constants: no output column is constant — every quantity, the "
    "date and the state vary across products and across populations.",
    "Note on sources: every column and table named in the rules above appears "
    "in the public source schema; nothing needed is missing.",
)

#: The counterfactual conditions REWORDED: the string "no moves" is gone.
_REWORDED_COUNTERFACTUAL_CONDITIONS = (
    "Literal constructed rows only — three products, restated.",
    "P-901: a product that has never had a single movement recorded -> every "
    "quantity 0, move_count 0, last_moved_on NULL, stock_state 'reorder' (0 is "
    "at or below its reorder_point of 10).",
    "P-902: an inbound move of 20, an outbound move of 8 recorded twice as an "
    "exact duplicate row, and an adjustment of 3 -> inbound_units 20, "
    "outbound_units 8 (row-grain summing gives 16), net_units 15, move_count 3, "
    "last_moved_on 2024-01-09, stock_state 'stocked' against a reorder_point of 5.",
    "P-903: one inbound move of exactly 12 units against a reorder_point of 12 "
    "-> net_units 12 = reorder_point, the inclusive boundary: stock_state "
    "'reorder'.",
    "One inbound move with NULL sku (50 units) belongs to no product and "
    "contributes to no output row.",
)


def _ambiguity() -> tuple[AmbiguityVariant, ...]:
    m = _m()
    return (
        AmbiguityVariant(
            "ambiguity-no-dedupe",
            frozenset({0}),
            # DE-LEAK: three descriptions and the aggregate rule restate the
            # deleted dedupe rule via DISTINCT.
            {
                "columns": (
                    ("inbound_units", "DISTINCT inbound moves", "inbound moves"),
                    ("outbound_units", "DISTINCT outbound moves", "outbound moves"),
                    ("move_count", "Count of DISTINCT moves", "Count of moves"),
                ),
                "ops": (
                    (1, "move_count = COUNT(DISTINCT move_id) over all moves",
                     "move_count = COUNT of all moves"),
                ),
            },
            "prose omits the duplicate-move-row dedupe rule",
            ("duplicate", "dedup", "distinct", "twice", "repeated", "more than once",
             "double", "recorded twice"),
            ("move_id", "move row", "move_count", "stock_moves"),
        ),
        AmbiguityVariant(
            "ambiguity-no-adjustment-rule",
            frozenset({3}),
            # DE-LEAK: net_units' description restated the deleted formula.
            {
                "columns": (
                    (
                        "net_units",
                        "inbound_units minus outbound_units plus the product's "
                        "adjustment quantity",
                        "Net stock position of the product",
                    ),
                )
            },
            "prose omits how net_units is computed, so whether adjustments enter "
            "it is unspecified",
            ("not specified", "unspecified", "undefined", "ambiguous", "unclear",
             "does not say", "two readings", "interpret", "how", "adjustment",
             "formula", "never defined", "computed"),
            ("net_units", "adjustment"),
        ),
        AmbiguityVariant(
            "ambiguity-no-left-join-rule",
            frozenset({2}),
            # DE-LEAK: the grain sentence, '0 if none', last_moved_on's NULL
            # clause and the COALESCE rule each answer whether a move-less
            # product appears at all.
            {
                "grain": (", including products with no moves", ""),
                "columns": _ZERO_IF_NONE + (
                    ("last_moved_on", "; NULL for a product with no moves", ""),
                ),
                "ops": (
                    (
                        4,
                        "For products without moves, interpret the preceding "
                        "net_units rule with inbound_units = 0, outbound_units = 0 "
                        "and adjustment_units = 0, yielding net_units = 0. "
                        "adjustment_units is an internal term, not a mart output, "
                        "and is never NULL: rule 2 fixes it for an existing sku "
                        "group with no adjustment move, and this rule fixes it for "
                        "a product with no moves. The COALESCE list names only the "
                        "published inbound_units, outbound_units, net_units and "
                        "move_count; set each to 0 (never NULL). last_moved_on stays NULL. "
                        "The next rule derives stock_state from this final net_units.",
                        "Wherever the per-sku aggregate would be NULL, interpret the "
                        "preceding net_units rule with inbound_units = 0, "
                        "outbound_units = 0 and adjustment_units = 0, yielding "
                        "net_units = 0. adjustment_units is an internal term, not "
                        "a mart output, and is never NULL: rule 2 fixes it for an "
                        "existing sku group with no adjustment move, and this rule "
                        "fixes it wherever the aggregate is absent. The COALESCE "
                        "list names only the published inbound_units, outbound_units, "
                        "net_units and move_count; set each to 0 (never NULL). "
                        "last_moved_on is never coalesced. The next rule derives "
                        "stock_state from this final net_units.",
                    ),
                ),
            },
            "prose never says whether products with no moves appear",
            ("inner join", "left join", "outer join", "not specified", "unspecified",
             "undefined", "ambiguous", "unclear", "does not say", "two readings",
             "interpret", "omitted", "dropped", "retained"),
            ("products with no", "no moves", "without moves", MART_NAME),
        ),
        AmbiguityVariant(
            "ambiguity-no-null-rule",
            frozenset({4}),
            # DE-LEAK: '0 if none' and the NULL clause restate the deleted rule.
            {
                "columns": _ZERO_IF_NONE + (
                    ("last_moved_on", "; NULL for a product with no moves", ""),
                ),
            },
            "prose omits the COALESCE-to-zero null-default rule",
            ("coalesce", "null", "zero", "default", "no moves", "empty"),
            _QUANTITIES + ("last_moved_on",),
        ),
        AmbiguityVariant(
            "ambiguity-no-threshold-rule",
            frozenset({5}),
            # DE-LEAK: the description named both states and both inputs.
            {
                "columns": (
                    (
                        "stock_state",
                        "'reorder' or 'stocked' from net_units and reorder_point as "
                        "the rules state",
                        "Replenishment state of the product",
                    ),
                )
            },
            "prose never defines stock_state: no rule names its values or the "
            "threshold that decides them",
            ("not specified", "unspecified", "undefined", "never defined", "no rule",
             "ambiguous", "unclear", "does not say", "two readings", "interpret",
             "how"),
            ("stock_state", "reorder_point", "threshold"),
        ),
        AmbiguityVariant(
            # CONTRADICTION: rule 6 keeps the inclusive boundary verbatim.
            "ambiguity-state-contradicts-threshold",
            frozenset(),
            {
                "columns": (
                    (
                        "stock_state",
                        "'reorder' or 'stocked' from net_units and reorder_point as "
                        "the rules state",
                        "'reorder' only when net_units is strictly below "
                        "reorder_point, so a product exactly on its threshold is "
                        "'stocked'",
                    ),
                )
            },
            "mart column description contradicts the surviving inclusive-threshold "
            "rule for stock_state",
            m._CONTRADICTION_TERMS,
            ("stock_state",),
        ),
        AmbiguityVariant(
            # CONTRADICTION: rule 4 keeps the adjustment term verbatim.
            "ambiguity-net-contradicts-adjustments",
            frozenset(),
            {
                "columns": (
                    (
                        "net_units",
                        "inbound_units minus outbound_units plus the product's "
                        "adjustment quantity",
                        "inbound_units minus outbound_units; adjustments never enter it",
                    ),
                )
            },
            "mart column description contradicts the surviving net_units rule "
            "about adjustments",
            m._CONTRADICTION_TERMS,
            ("net_units",),
        ),
    )


_MATCHED_ONLY_CONDITIONS = {
    P.PRIMARY: (
        "Approximately 300 products.",
        "Adjustment moves are present.",
        "Every product has at least one move.",
        "Some moves have NULL sku; they belong to no product and contribute to "
        "no output row.",
        "Products at or below their reorder_point and products above it both occur.",
    ),
    P.RESAMPLED: (
        "Same generator and conditions as primary; new seed and new id ranges.",
    ),
}

_MATCHED_COUNTERFACTUAL_ROWS = {
    "products": COUNTERFACTUAL_LITERAL_ROWS["products"],
    "stock_moves": (
        {"move_id": 9011, "sku": "P-901", "move_type": "inbound", "quantity": 30,
         "moved_on": "2024-01-03"},
    ) + COUNTERFACTUAL_LITERAL_ROWS["stock_moves"],
}

_MATCHED_COUNTERFACTUAL_CONDITIONS = (
    "Literal constructed rows: three products, each with at least one move.",
    "P-902: an inbound move of 20, an outbound move of 8 recorded twice as an "
    "exact duplicate row, and an adjustment of 3 -> net_units 15, move_count 3.",
    "P-903: one inbound move of exactly 12 units against a reorder_point of 12 "
    "-> net_units 12 = reorder_point, the inclusive boundary: stock_state "
    "'reorder'.",
    "One inbound move with NULL sku (50 units) belongs to no product and "
    "contributes to no output row.",
)

#: No population declares an adjustment move: the adjustment term of
#: net_units is never exercised. None of these strings contains "adjustment".
_NO_ADJUSTMENT_CONDITIONS = {
    P.DEVELOPMENT: (
        "Tiny debug data: exactly three products.",
        "Every move is inbound or outbound; every product has at least one of "
        "each (INNER JOIN is indistinguishable here by design).",
        "No NULL sku, no duplicate rows.",
    ),
    P.PRIMARY: (
        "Approximately 300 products.",
        "Every move is a receipt (inbound) or an issue (outbound).",
        "Some products have no moves at all.",
        "Some moves have NULL sku; they belong to no product and contribute to "
        "no output row.",
        "Products at or below their reorder_point and products above it both occur.",
    ),
    P.RESAMPLED: (
        "Same generator and conditions as primary; new seed and new id ranges.",
    ),
    P.STRESS: (
        "A few hot products hold a large share of all moves.",
        "Exact-duplicate stock_moves rows are present (correct logic must dedupe).",
        "Ties: distinct products with identical totals.",
        "Every move is inbound or outbound; every product has at least one of "
        "each (INNER JOIN is indistinguishable here by design).",
    ),
    P.COUNTERFACTUAL: (
        "Literal constructed rows only — three products; every move is inbound "
        "or outbound.",
        "P-901: a product with no moves -> every quantity 0, move_count 0, "
        "last_moved_on NULL, stock_state 'reorder'.",
        "P-902: an inbound move of 20 and an outbound move of 8 recorded twice "
        "as an exact duplicate row -> inbound_units 20, outbound_units 8 "
        "(row-grain summing gives 16), net_units 12, move_count 2, last_moved_on "
        "2024-01-09, stock_state 'stocked' against a reorder_point of 5.",
        "P-903: one inbound move of exactly 12 units against a reorder_point of 12 "
        "-> net_units 12 = reorder_point, the inclusive boundary: stock_state "
        "'reorder'.",
        "One inbound move with NULL sku (50 units) belongs to no product and "
        "contributes to no output row.",
    ),
}

_NO_ADJUSTMENT_COUNTERFACTUAL_ROWS = {
    "products": COUNTERFACTUAL_LITERAL_ROWS["products"],
    "stock_moves": tuple(
        row for row in COUNTERFACTUAL_LITERAL_ROWS["stock_moves"]
        if row["move_type"] != "adjustment"
    ),
}

_NO_DUPLICATE_CONDITIONS = {
    P.STRESS: (
        "A few hot products hold a large share of all moves.",
        "Ties: distinct products with identical totals.",
        "Every product has at least one move of every move_type "
        "(INNER JOIN is indistinguishable here by design).",
    ),
    P.COUNTERFACTUAL: (
        "Literal constructed rows only — three products.",
        "P-901: a product with no moves -> every quantity 0, move_count 0, "
        "last_moved_on NULL, stock_state 'reorder' (0 is at or below its "
        "reorder_point of 10).",
        "P-902: an inbound move of 20, an outbound move of 8 and an adjustment "
        "of 3 -> inbound_units 20, outbound_units 8, net_units 15, move_count 3, "
        "last_moved_on 2024-01-09, stock_state 'stocked' against a reorder_point of 5.",
        "P-903: one inbound move of exactly 12 units against a reorder_point of 12 "
        "-> net_units 12 = reorder_point, the inclusive boundary: stock_state "
        "'reorder'.",
        "One inbound move with NULL sku (50 units) belongs to no product and "
        "contributes to no output row.",
    ),
}

_NO_DUPLICATE_COUNTERFACTUAL_ROWS = {
    "products": COUNTERFACTUAL_LITERAL_ROWS["products"],
    "stock_moves": tuple(
        row for i, row in enumerate(COUNTERFACTUAL_LITERAL_ROWS["stock_moves"]) if i != 2
    ),
}

_NO_NULL_SKU_CONDITIONS = {
    P.PRIMARY: (
        "Approximately 300 products.",
        "Adjustment moves are present.",
        "Some products have no moves at all.",
        "Every move carries the sku of the product moved.",
        "Products at or below their reorder_point and products above it both occur.",
    ),
    P.COUNTERFACTUAL: (
        "Literal constructed rows only — three products.",
        "P-901: a product with no moves -> every quantity 0, move_count 0, "
        "last_moved_on NULL, stock_state 'reorder' (0 is at or below its "
        "reorder_point of 10).",
        "P-902: an inbound move of 20, an outbound move of 8 recorded twice as "
        "an exact duplicate row, and an adjustment of 3 -> inbound_units 20, "
        "outbound_units 8 (row-grain summing gives 16), net_units 15, "
        "move_count 3, last_moved_on 2024-01-09, stock_state 'stocked' against "
        "a reorder_point of 5.",
        "P-903: one inbound move of exactly 12 units against a reorder_point of 12 "
        "-> net_units 12 = reorder_point, the inclusive boundary: stock_state "
        "'reorder'.",
    ),
}

_NO_NULL_SKU_COUNTERFACTUAL_ROWS = {
    "products": COUNTERFACTUAL_LITERAL_ROWS["products"],
    "stock_moves": tuple(
        row for row in COUNTERFACTUAL_LITERAL_ROWS["stock_moves"] if row["sku"] is not None
    ),
}

#: No population puts a product exactly ON its reorder_point: the inclusive
#: boundary is never exercised (a strict '<' scores full reward everywhere).
_NEVER_ON_THRESHOLD_CONDITIONS = {
    P.PRIMARY: (
        "Approximately 300 products.",
        "Adjustment moves are present.",
        "Some products have no moves at all.",
        "Some moves have NULL sku; they belong to no product and contribute to "
        "no output row.",
        "Products below their reorder_point and products above it both occur; "
        "no product's net_units ever lands on its reorder_point.",
    ),
    P.RESAMPLED: (
        "Same generator and conditions as primary; new seed and new id ranges.",
    ),
    P.STRESS: (
        "A few hot products hold a large share of all moves.",
        "Exact-duplicate stock_moves rows are present (correct logic must dedupe).",
        "Ties: distinct products with identical totals.",
        "Every product has at least one move of every move_type "
        "(INNER JOIN is indistinguishable here by design).",
        "No product's net_units ever lands on its reorder_point.",
    ),
    P.COUNTERFACTUAL: (
        "Literal constructed rows only — three products.",
        "P-901: a product with no moves -> every quantity 0, move_count 0, "
        "last_moved_on NULL, stock_state 'reorder' (0 is below its "
        "reorder_point of 10).",
        "P-902: an inbound move of 20, an outbound move of 8 recorded twice as "
        "an exact duplicate row, and an adjustment of 3 -> inbound_units 20, "
        "outbound_units 8 (row-grain summing gives 16), net_units 15, "
        "move_count 3, last_moved_on 2024-01-09, stock_state 'stocked' against "
        "a reorder_point of 5.",
        "P-903: one inbound move of 13 units against a reorder_point of 12 -> "
        "net_units 13, above the threshold: stock_state 'stocked'.",
        "One inbound move with NULL sku (50 units) belongs to no product and "
        "contributes to no output row.",
    ),
}

_NEVER_ON_THRESHOLD_COUNTERFACTUAL_ROWS = {
    "products": COUNTERFACTUAL_LITERAL_ROWS["products"],
    "stock_moves": tuple(
        {**row, "quantity": 13} if row["move_id"] == 9031 else row
        for row in COUNTERFACTUAL_LITERAL_ROWS["stock_moves"]
    ),
}

_DEVELOPMENT_ONLY_CONDITIONS = {
    P.DEVELOPMENT: (
        "Tiny debug data: exactly three products.",
        "Some products have no moves at all.",
        "Adjustment moves are present.",
        "Exact-duplicate stock_moves rows are present.",
        "Some moves have NULL sku.",
        "One product's net_units equals its reorder_point exactly (the inclusive "
        "boundary).",
    ),
    P.PRIMARY: (
        "Approximately 300 products.",
        "Every product has at least one move.",
        "Every move is inbound or outbound, carries a sku, and appears once.",
        "No product's net_units ever lands on its reorder_point.",
    ),
    P.RESAMPLED: (
        "Same generator and conditions as primary; new seed and new id ranges.",
    ),
    P.STRESS: (
        "A few hot products hold a large share of all moves.",
        "Every product has at least one move.",
        "Every move is inbound or outbound, carries a sku, and appears once.",
        "No product's net_units ever lands on its reorder_point.",
    ),
    P.COUNTERFACTUAL: (
        "Literal constructed rows: three products, each with exactly one inbound "
        "move above its reorder_point.",
    ),
}

_DEVELOPMENT_ONLY_COUNTERFACTUAL_ROWS = {
    "products": COUNTERFACTUAL_LITERAL_ROWS["products"],
    "stock_moves": (
        {"move_id": 9011, "sku": "P-901", "move_type": "inbound", "quantity": 30,
         "moved_on": "2024-01-03"},
        {"move_id": 9021, "sku": "P-902", "move_type": "inbound", "quantity": 20,
         "moved_on": "2024-01-05"},
        {"move_id": 9031, "sku": "P-903", "move_type": "inbound", "quantity": 13,
         "moved_on": "2024-02-01"},
    ),
}


def _population() -> tuple[PopulationVariant, ...]:
    m = _m()
    population_names = ("counterfactual", "primary", "resampled", "stress", "development")
    return (
        PopulationVariant(
            "population-dropped-counterfactual",
            _MATCHED_ONLY_CONDITIONS,
            "counterfactual population removed; remaining conditions guarantee "
            "every product a move",
            _JOIN_BLIND_TERMS,
            population_names,
            drop=(P.COUNTERFACTUAL,),
        ),
        PopulationVariant(
            "population-neutered-counterfactual",
            {**_MATCHED_ONLY_CONDITIONS, P.COUNTERFACTUAL: _MATCHED_COUNTERFACTUAL_CONDITIONS},
            "counterfactual rows replaced: every product has a move, so the "
            "wrong join is indistinguishable",
            _JOIN_BLIND_TERMS,
            population_names,
            literal_rows=_MATCHED_COUNTERFACTUAL_ROWS,
        ),
        PopulationVariant(
            "population-no-adjustment-moves",
            _NO_ADJUSTMENT_CONDITIONS,
            "no population declares an adjustment move, so the adjustment term of "
            "net_units is never exercised",
            m._BLIND_TERMS + ("adjustment", "net_units"),
            ("adjustment", "net_units", "move_type", "primary", "counterfactual"),
            literal_rows=_NO_ADJUSTMENT_COUNTERFACTUAL_ROWS,
        ),
        PopulationVariant(
            "population-no-duplicate-moves",
            _NO_DUPLICATE_CONDITIONS,
            "no population declares duplicate move rows, so the dedupe rule is "
            "never exercised",
            m._DEDUPE_BLIND_TERMS,
            ("stress", "move row", "move_id", "stock_moves", "counterfactual"),
            literal_rows=_NO_DUPLICATE_COUNTERFACTUAL_ROWS,
        ),
        PopulationVariant(
            "population-no-null-sku-moves",
            _NO_NULL_SKU_CONDITIONS,
            "no population declares a move with a NULL sku, so the rule excluding "
            "those moves is never exercised",
            m._BLIND_TERMS + ("null",),
            ("primary", "sku", "resampled", "counterfactual"),
            literal_rows=_NO_NULL_SKU_COUNTERFACTUAL_ROWS,
        ),
        PopulationVariant(
            "population-never-on-threshold",
            _NEVER_ON_THRESHOLD_CONDITIONS,
            "no population puts a product exactly on its reorder_point, so the "
            "inclusive boundary is never exercised",
            m._BLIND_TERMS + ("boundary", "threshold", "inclusive", "equal",
                              "exactly on", "strict"),
            ("reorder_point", "stock_state", "counterfactual", "primary"),
            literal_rows=_NEVER_ON_THRESHOLD_COUNTERFACTUAL_ROWS,
        ),
        PopulationVariant(
            "population-discriminators-in-development",
            _DEVELOPMENT_ONLY_CONDITIONS,
            "every distinguishing condition sits in the solver-visible "
            "development split; no GRADED population can catch wrong logic",
            m._BLIND_TERMS + ("development",),
            ("primary", "resampled", "stress", "counterfactual"),
            literal_rows=_DEVELOPMENT_ONLY_COUNTERFACTUAL_ROWS,
        ),
    )


def _shortcut() -> tuple[ShortcutVariant, ...]:
    m = _m()
    terms = m._SHORTCUT_DETECTION_TERMS
    return (
        ShortcutVariant(
            "shortcut-constant-columns",
            "mart column descriptions announce constant outputs",
            terms,
            ("net_units", "move_count", "stock_state", MART_NAME),
            suffix=" Always exactly the same value for every product in every population.",
        ),
        ShortcutVariant(
            "shortcut-identity-columns",
            "mart column descriptions make every output a function of the key alone",
            terms + ("key alone", "from the key", "copy", "identity", "echo"),
            ("sku", "net_units", "move_count"),
            suffix=(
                " For every product in every population this is determined by the "
                "sku alone (its numeric suffix), so it can be produced from the key "
                "alone."
            ),
        ),
        ShortcutVariant(
            "shortcut-constant-one-measure",
            "ONE measure is declared constant while the others still vary",
            terms,
            ("move_count",),
            columns=("move_count",),
            suffix=" This is exactly 1 for every product in every population.",
        ),
        ShortcutVariant(
            "shortcut-derivable-measure",
            "one measure is a fixed fraction of another, so it need never be "
            "computed from the sources",
            terms + ("derive", "derivable", "function of", "half of", "fraction of",
                     "closed-form", "closed form", "determines the other",
                     "determined by", "0.5 *", "/ 2", "half"),
            ("outbound_units", "inbound_units"),
            columns=("outbound_units",),
            suffix=(
                " For every product in every population this is exactly half of "
                "inbound_units, so one measure determines the other."
            ),
        ),
        ShortcutVariant(
            "shortcut-dev-only-population",
            "only the tiny development population is graded",
            terms,
            ("development", "three products", "single population",
             "only population", "one population"),
            keep=(P.DEVELOPMENT,),
        ),
        ShortcutVariant(
            "shortcut-counterfactual-only-population",
            "only the three-row counterfactual population is graded",
            terms,
            ("counterfactual", "three products", "single population",
             "only population", "one population"),
            keep=(P.COUNTERFACTUAL,),
        ),
        ShortcutVariant(
            "shortcut-tiny-populations-only",
            "only the two tiny populations are graded: six products in total, "
            "enumerable end to end",
            terms,
            ("development", "counterfactual", "three products", "six products"),
            keep=(P.DEVELOPMENT, P.COUNTERFACTUAL),
        ),
    )


def _feasibility() -> tuple[FeasibilityVariant, ...]:
    return (
        FeasibilityVariant(
            "feasibility-missing-move-type",
            "public schema lacks stock_moves.move_type; the per-kind sums are unsatisfiable",
            ("move_type", "inbound_units", "outbound_units"),
            table="stock_moves", column="move_type",
        ),
        FeasibilityVariant(
            "feasibility-missing-reorder-point",
            "public schema lacks products.reorder_point; stock_state cannot be decided",
            ("reorder_point", "reorder point", "stock_state"),
            table="products", column="reorder_point",
        ),
        FeasibilityVariant(
            "feasibility-missing-quantity",
            "public schema lacks stock_moves.quantity; no quantity can be summed",
            ("quantity", "inbound_units", "net_units"),
            table="stock_moves", column="quantity",
        ),
        FeasibilityVariant(
            "feasibility-missing-moved-on",
            "public schema lacks stock_moves.moved_on; last_moved_on cannot be computed",
            ("moved_on", "last_moved_on"),
            table="stock_moves", column="moved_on",
        ),
        FeasibilityVariant(
            "feasibility-missing-category",
            "public schema lacks products.category; the category passthrough is unsatisfiable",
            ("category",),
            table="products", column="category",
        ),
        FeasibilityVariant(
            "feasibility-unpublished-pack-size-input",
            "the mart states inbound_units is scaled by products.pack_size, a "
            "column the public schema never publishes",
            ("pack_size", "pack size", "inbound_units"),
            mart_column="inbound_units",
            suffix=(
                " Every inbound quantity is converted to base units by multiplying "
                "it by the pack_size column of the products table before it is "
                "added in."
            ),
        ),
        FeasibilityVariant(
            "feasibility-unpublished-warehouse-input",
            "the mart states move_count is restricted by stock_moves.warehouse_code, "
            "a column the public schema never publishes",
            ("warehouse_code", "warehouse code", "move_count"),
            mart_column="move_count",
            suffix=(
                " Only moves whose warehouse_code column on the stock_moves table "
                "equals 'MAIN' are counted."
            ),
        ),
    )


def _definition() -> FamilyDefinition:
    return FamilyDefinition(
        name=FAMILY_NAME,
        task=task,
        canary_guid=CANARY_GUID,
        decoy_notes=_DECOY_NOTES,
        reworded_counterfactual_conditions=_REWORDED_COUNTERFACTUAL_CONDITIONS,
        ambiguity=_ambiguity(),
        population=_population(),
        shortcut=_shortcut(),
        feasibility=_feasibility(),
    )


def family():
    """The built `FixtureFamily` (memoized by the package `__init__`)."""
    return build_family(_definition())

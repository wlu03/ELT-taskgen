"""Hand-authored fixture task for the mandatory real-data DuckDB gate.

One combined ELT task covering ALL FIVE source backends in one mixed task
(postgres, mongodb, files, rest, s3), with literal row populations that embed
every sensitive value class the gate audits (review "Non-negotiable DuckDB
test rule", items 2, 3 and 7), and HAND-DERIVED expected gold.

EXPECTED_GOLD is the independent validation demanded by gate item 6: every
value below was derived BY HAND from LITERAL_ROWS (the derivations are the
comments in ``_gold_for``), never by running the reference pipeline.  The
fixture maker (tests/make_semantic_gate_fixture.py) refuses to freeze a
release whose pipeline-produced gold disagrees with these literals.

Nothing in this module imports the reference runner, the gold freezer, or the
scorer — it is data plus pure-Python predicates.
"""

from __future__ import annotations

from pathlib import Path

from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
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

P = PopulationName

GATE_TASK_ID = "gate__five_backend_probe"
ROLLUP_MART = "customer_rollup"
WIDE_MART = "event_wide"

#: The exact backend -> load-plan reader-format map (calibration.LOAD_FORMATS).
BACKEND_FORMATS: dict[str, str] = {
    "postgres": "postgres_sql",
    "mongodb": "jsonl",
    "rest": "rest_pages",
    "s3": "s3_jsonl",
    "files": "csv",
}

#: 2**53 + 1 — the smallest positive integer a float64 cannot represent.
UNSAFE_INT = 9_007_199_254_740_993

#: A non-binary-exact decimal at the maximum portable DECIMAL(38,9) scale.
#: Its shortest float64 representation is the same decimal text, so it
#: survives JSON -> float -> DuckDB DOUBLE -> repr byte-for-byte without
#: violating the strict warehouse contract.
PRECISE_DECIMAL = 0.123456789
PRECISE_DECIMAL_TEXT = "0.123456789"

#: A single text cell of >= 32 KiB (gate item 7's large-text class).
BIG_TEXT_PREFIX = "gate-big-text:"
BIG_TEXT_FILL = "x" * 32_768


def big_text(population: P) -> str:
    return f"{BIG_TEXT_PREFIX}{population.value}:{BIG_TEXT_FILL}"


#: Per-population parameters. ``base`` namespaces every id (populations must
#: not share ids); ``k`` is the one varying quantity (an extra order_items row
#: with quantity=k at unit price 1.0), so gold DIFFERS across populations and
#: a scorer that mixed populations up would be caught.  ``b2_total`` is the
#: HAND-COMPUTED customer-2 rollup total for that k (see ``_gold_for``).
_POP_PARAMS: dict[P, dict] = {
    P.DEVELOPMENT: {"base": 100, "k": 1, "b2_total": "61.25"},
    P.PRIMARY: {"base": 10_000, "k": 2, "b2_total": "62.25"},
    P.RESAMPLED: {"base": 5_000_000, "k": 3, "b2_total": "63.25"},
    P.COUNTERFACTUAL: {"base": 900, "k": 4, "b2_total": "64.25"},
    P.STRESS: {"base": 7_000_000, "k": 5, "b2_total": "65.25"},
}


def _rows_for(population: P, base: int, k: int) -> dict[str, tuple[Row, ...]]:
    """Literal rows for one population; every value is authored, none drawn.

    Sensitive classes carried here:
      customers.segment   NULL vs literal 'null' vs '' vs literal 'None'
      orders.note         explicit JSON null vs MISSING KEY vs 'None'/'NULL'
      orders row base+104 a completed order whose customer_id is NULL
      order_items         an EXACT duplicate line row (both must be counted)
      events.big_count    +/- (2**53 + 1) — unrepresentable in float64
      events.label        MixedCase vs 'null' vs '' vs 'None'
      events.occurred_at  naive timestamps (one NULL)
      events.tz_stamp     ISO text with DISTINCT UTC offsets (+00, +02, -05)
      metrics.value       9-fractional-digit boundary decimal, and NULL
      metrics.big_note    a >= 32 KiB single cell, and the literal 'null'
    """
    return {
        "customers": (
            {"customer_id": base + 1, "customer_name": "Ada", "segment": "gold"},
            {"customer_id": base + 2, "customer_name": "Bob", "segment": None},
            {"customer_id": base + 3, "customer_name": "Cy", "segment": "null"},
            {"customer_id": base + 4, "customer_name": "Di", "segment": ""},
            {"customer_id": base + 5, "customer_name": "Ed", "segment": "None"},
        ),
        "orders": (
            {"order_id": base + 101, "customer_id": base + 1,
             "status": "completed", "note": "first"},
            {"order_id": base + 102, "customer_id": base + 1,
             "status": "cancelled", "note": None},
            # note key deliberately MISSING (JSON missing-field vs null class).
            {"order_id": base + 103, "customer_id": base + 2,
             "status": "completed"},
            {"order_id": base + 104, "customer_id": None,
             "status": "completed", "note": "orphan"},
            {"order_id": base + 105, "customer_id": base + 3,
             "status": "completed", "note": "None"},
            {"order_id": base + 106, "customer_id": base + 5,
             "status": "completed", "note": "NULL"},
        ),
        "order_items": (
            {"order_id": base + 101, "quantity": 1, "unit_price": 10.5,
             "discount": None},
            {"order_id": base + 101, "quantity": 2, "unit_price": 15.25,
             "discount": 0.25},
            # EXACT duplicate of the first line row — both are counted.
            {"order_id": base + 101, "quantity": 1, "unit_price": 10.5,
             "discount": None},
            {"order_id": base + 103, "quantity": 3, "unit_price": 20.0,
             "discount": None},
            # The one population-varying row: quantity == k at unit price 1.0.
            {"order_id": base + 103, "quantity": k, "unit_price": 1.0,
             "discount": None},
            {"order_id": base + 103, "quantity": 1, "unit_price": 0.25,
             "discount": 0.5},
            # Items of the NULL-customer completed order: contribute nothing.
            {"order_id": base + 104, "quantity": 1, "unit_price": 99.75,
             "discount": None},
            # Items of a cancelled order: contribute nothing.
            {"order_id": base + 102, "quantity": 4, "unit_price": 2.5,
             "discount": None},
        ),
        "events": (
            {"event_id": base + 1, "big_count": UNSAFE_INT,
             "label": "MiXeD Case", "occurred_at": "2024-03-01 10:00:00",
             "tz_stamp": "2024-03-01T10:00:00+00:00"},
            {"event_id": base + 2, "big_count": 1, "label": "null",
             "occurred_at": "2024-03-02 11:30:00",
             "tz_stamp": "2024-03-02T13:30:00+02:00"},
            {"event_id": base + 3, "big_count": -UNSAFE_INT, "label": "",
             "occurred_at": None, "tz_stamp": "2024-03-03T05:15:00-05:00"},
            {"event_id": base + 4, "big_count": 0, "label": "None",
             "occurred_at": "2024-03-04 00:00:00", "tz_stamp": None},
            {"event_id": base + 5, "big_count": 42, "label": "ok",
             "occurred_at": "2024-03-05 23:59:59",
             "tz_stamp": "2024-03-05T23:59:59+00:00"},
        ),
        "metrics": (
            {"metric_id": base + 1, "event_id": base + 1,
             "value": PRECISE_DECIMAL, "big_note": big_text(population)},
            {"metric_id": base + 2, "event_id": base + 2,
             "value": None, "big_note": "short"},
            {"metric_id": base + 3, "event_id": base + 5,
             "value": 2.5, "big_note": "null"},
        ),
    }


#: population -> table -> literal rows (generate_rows emits EXACTLY these).
LITERAL_ROWS: dict[P, dict[str, tuple[Row, ...]]] = {
    population: _rows_for(population, params["base"], params["k"])
    for population, params in _POP_PARAMS.items()
}

#: Independent stage-1 recount: the trusted loader loads EVERY literal row
#: (duplicates included), so the expected count is simply len(rows).
EXPECTED_STAGE1: dict[str, dict[str, int]] = {
    population.value: {table: len(rows) for table, rows in tables.items()}
    for population, tables in LITERAL_ROWS.items()
}


def _gold_for(population: P, base: int, b2_total: str) -> dict[str, str]:
    """HAND-DERIVED expected gold CSVs for one population.

    customer_rollup — derivation from LITERAL_ROWS (all sums are exact in
    binary floating point: every price is a multiple of 0.25):
      customer base+1: completed orders = {base+101} -> count 1.  Items of
        base+101 (the duplicate line counts BOTH times):
        1*10.5 + 2*15.25 + 1*10.5 = 51.5.
      customer base+2: completed orders = {base+103} -> count 1.  Items:
        3*20.0 + k*1.0 + 1*0.25 = 60.25 + k  (b2_total, hand-computed per
        population in _POP_PARAMS).
      customer base+3: order base+105 is completed but has NO items ->
        count 1, COALESCE(SUM(NULL), 0) = 0 typed DOUBLE -> "0.0".
      customer base+4: no orders at all -> COUNT(DISTINCT NULL) = 0, "0.0".
      customer base+5: order base+106 completed, no items -> 1, "0.0".
        (base+3 and base+5 tie on every non-key column — the ordering-tie
        class; the total order breaks the tie on customer_id.)
      order base+104 is completed and HAS items, but its customer_id is NULL:
        it joins to no customer and contributes to no row.
      order base+102 is cancelled: its items contribute nothing.
      Sorted ascending by customer_id (numeric sort, key column first).

    event_wide — passthrough semantics, one row per event (events LEFT JOIN
    metrics), sorted by event_id:
      big_count: BIGINT survives exactly — "9007199254740993" is 2**53 + 1
        and must not appear rounded.
      label: the comparator's canonical CSV writes pandas NA sentinels as the
        EMPTY field, so the literal strings "null" and "None" and the empty
        string all freeze as empty cells; "MiXeD Case" and "ok" survive.
      occurred_at: DuckDB parses "2024-03-01 10:00:00" as a naive TIMESTAMP;
        gold records datetime.isoformat() -> "2024-03-01T10:00:00".
      tz_stamp: TEXT — ISO strings with distinct UTC offsets survive verbatim.
      metric_value: the scale-9 boundary decimal survives as its exact repr; events
        without a metric row get NULL (empty cell).
      big_note: the >= 32 KiB cell survives verbatim for event base+1;
        metric base+3's literal "null" note freezes as the EMPTY cell (NA
        sentinel), which IS the legacy comparator contract being pinned.
    """
    rollup = (
        "customer_id,completed_orders,total_spend\n"
        f"{base + 1},1,51.5\n"
        f"{base + 2},1,{b2_total}\n"
        f"{base + 3},1,0.0\n"
        f"{base + 4},0,0.0\n"
        f"{base + 5},1,0.0\n"
    )
    wide = (
        "event_id,big_count,label,occurred_at,tz_stamp,metric_value,big_note\n"
        f"{base + 1},9007199254740993,MiXeD Case,2024-03-01T10:00:00,"
        f"2024-03-01T10:00:00+00:00,{PRECISE_DECIMAL_TEXT},"
        f"{big_text(population)}\n"
        f"{base + 2},1,,2024-03-02T11:30:00,2024-03-02T13:30:00+02:00,,short\n"
        f"{base + 3},-9007199254740993,,,2024-03-03T05:15:00-05:00,,\n"
        f"{base + 4},0,,2024-03-04T00:00:00,,,\n"
        f"{base + 5},42,ok,2024-03-05T23:59:59,2024-03-05T23:59:59+00:00,2.5,\n"
    )
    return {ROLLUP_MART: rollup, WIDE_MART: wide}


#: population value -> mart -> hand-derived gold CSV text.
EXPECTED_GOLD: dict[str, dict[str, str]] = {
    population.value: _gold_for(population, params["base"], params["b2_total"])
    for population, params in _POP_PARAMS.items()
}


ROLLUP_SQL = """\
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
    COUNT(DISTINCT co.order_id) AS completed_orders,
    COALESCE(SUM(ot.order_total), 0) AS total_spend
FROM customers AS c
LEFT JOIN completed_orders AS co ON co.customer_id = c.customer_id
LEFT JOIN order_totals AS ot ON ot.order_id = co.order_id
GROUP BY c.customer_id
ORDER BY c.customer_id
"""

WIDE_SQL = """\
SELECT
    e.event_id AS event_id,
    e.big_count AS big_count,
    e.label AS label,
    e.occurred_at AS occurred_at,
    e.tz_stamp AS tz_stamp,
    m.value AS metric_value,
    m.big_note AS big_note
FROM events AS e
LEFT JOIN metrics AS m ON m.event_id = e.event_id
ORDER BY e.event_id
"""


def _populations(task_id: str) -> tuple[PopulationSpec, ...]:
    conditions = {
        P.DEVELOPMENT: "Tiny literal probe rows, solver-visible.",
        P.PRIMARY: "Literal probe rows in the primary id range.",
        P.RESAMPLED: "Same literal shape under new ids (memorization check).",
        P.COUNTERFACTUAL: "Literal rows constructed to break wrong logic.",
        P.STRESS: "Literal rows with duplicate line items and ties.",
    }
    return tuple(
        PopulationSpec(
            name=population,
            seed=derive_seed(task_id, population.value),
            conditions=(conditions[population],),
            literal_rows=LITERAL_ROWS[population],
        )
        for population in _POP_PARAMS
    )


def gate_task(task_id: str = GATE_TASK_ID) -> TaskIR:
    """The five-backend gate probe as a validated, optionally renamed TaskIR."""
    tables = (
        TableSpec(
            name="customers",
            description="One row per customer.",
            columns=(
                ColumnSpec(name="customer_id", type=ColumnType.INTEGER,
                           description="Unique customer identifier."),
                ColumnSpec(name="customer_name", type=ColumnType.TEXT,
                           description="Display name."),
                ColumnSpec(name="segment", type=ColumnType.TEXT, nullable=True,
                           description="Free-text segment; may be NULL."),
            ),
            primary_key=("customer_id",),
        ),
        TableSpec(
            name="orders",
            description="One row per order header.",
            columns=(
                ColumnSpec(name="order_id", type=ColumnType.INTEGER,
                           description="Unique order identifier."),
                ColumnSpec(name="customer_id", type=ColumnType.INTEGER,
                           nullable=True,
                           description="Customer who placed the order; may be NULL."),
                ColumnSpec(name="status", type=ColumnType.TEXT,
                           enum_values=("cancelled", "completed"),
                           description="Order status."),
                ColumnSpec(name="note", type=ColumnType.TEXT, nullable=True,
                           description="Free-text note; may be NULL or absent."),
            ),
            primary_key=("order_id",),
        ),
        TableSpec(
            name="order_items",
            description="One row per line item; exact duplicates possible.",
            columns=(
                ColumnSpec(name="order_id", type=ColumnType.INTEGER,
                           description="Order this line item belongs to."),
                ColumnSpec(name="quantity", type=ColumnType.INTEGER,
                           description="Units purchased."),
                ColumnSpec(name="unit_price", type=ColumnType.DECIMAL,
                           description="Price per unit."),
                ColumnSpec(name="discount", type=ColumnType.DECIMAL,
                           nullable=True,
                           description="Absolute discount; may be NULL."),
            ),
        ),
        TableSpec(
            name="events",
            description="One row per event.",
            columns=(
                ColumnSpec(name="event_id", type=ColumnType.INTEGER,
                           description="Unique event identifier."),
                ColumnSpec(name="big_count", type=ColumnType.BIGINT,
                           description="64-bit counter; exceeds 2**53."),
                ColumnSpec(name="label", type=ColumnType.TEXT,
                           description="Event label; may be empty text."),
                ColumnSpec(name="occurred_at", type=ColumnType.TIMESTAMP,
                           nullable=True,
                           description="Naive event timestamp; may be NULL."),
                ColumnSpec(name="tz_stamp", type=ColumnType.TEXT,
                           nullable=True,
                           description="ISO-8601 text with a UTC offset."),
            ),
            primary_key=("event_id",),
        ),
        TableSpec(
            name="metrics",
            description="At most one measurement per event.",
            columns=(
                ColumnSpec(name="metric_id", type=ColumnType.INTEGER,
                           description="Unique metric identifier."),
                ColumnSpec(name="event_id", type=ColumnType.INTEGER,
                           description="Event the measurement belongs to."),
                ColumnSpec(name="value", type=ColumnType.DECIMAL,
                           nullable=True,
                           description="Measured value; may be NULL."),
                ColumnSpec(name="big_note", type=ColumnType.TEXT,
                           description="Free-form note; can be very large."),
            ),
            primary_key=("metric_id",),
        ),
    )
    backends = (
        BackendAssignment(table="customers", backend=Backend.POSTGRES),
        BackendAssignment(table="orders", backend=Backend.MONGODB),
        BackendAssignment(table="order_items", backend=Backend.FILES,
                          options={"format": "csv"}),
        BackendAssignment(table="events", backend=Backend.REST,
                          options={"page_size": "2"}),
        BackendAssignment(table="metrics", backend=Backend.S3),
    )
    return TaskIR(
        task_id=task_id,
        family_id=task_id,
        cluster_id=task_id,
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        attribution="elt-taskgen semantic-gate fixture",
        title="Five-backend semantic gate probe",
        tables=tables,
        relationships=(
            Relationship(
                child_table="orders",
                child_columns=("customer_id",),
                parent_table="customers",
                parent_columns=("customer_id",),
                required=False,  # NULL customer_id occurs by construction
            ),
            Relationship(
                child_table="order_items",
                child_columns=("order_id",),
                parent_table="orders",
                parent_columns=("order_id",),
                required=True,
            ),
            Relationship(
                child_table="metrics",
                child_columns=("event_id",),
                parent_table="events",
                parent_columns=("event_id",),
                required=True,
            ),
        ),
        backends=backends,
        marts=(
            MartSpec(
                name=ROLLUP_MART,
                description="Per-customer completed-order rollup.",
                grain="One row per customer, including customers with no orders.",
                key_columns=("customer_id",),
                columns=(
                    MartColumn(name="customer_id", type=ColumnType.INTEGER,
                               description="Unique customer identifier."),
                    MartColumn(name="completed_orders", type=ColumnType.INTEGER,
                               description="Count of DISTINCT completed orders; 0 if none."),
                    MartColumn(name="total_spend", type=ColumnType.DECIMAL,
                               description="Sum of quantity * unit_price over items "
                                           "of completed orders; 0 if none."),
                ),
                plan=MartPlan(
                    mart=ROLLUP_MART,
                    ops=(
                        MartOp(kind=MartOpKind.SOURCE,
                               description="Bring customers into scope.",
                               tables=("customers",)),
                    ),
                    notes="Reference SQL is authored; the plan is a stub.",
                ),
            ),
            MartSpec(
                name=WIDE_MART,
                description="One wide row per event with its measurement.",
                grain="One row per event.",
                key_columns=("event_id",),
                columns=(
                    MartColumn(name="event_id", type=ColumnType.INTEGER,
                               description="Unique event identifier."),
                    MartColumn(name="big_count", type=ColumnType.BIGINT,
                               description="64-bit counter, passed through."),
                    MartColumn(name="label", type=ColumnType.TEXT,
                               description="Event label, passed through."),
                    MartColumn(name="occurred_at", type=ColumnType.TIMESTAMP,
                               description="Event timestamp, passed through."),
                    MartColumn(name="tz_stamp", type=ColumnType.TEXT,
                               description="Offset-bearing ISO text, passed through."),
                    MartColumn(name="metric_value", type=ColumnType.DECIMAL,
                               description="Measured value; NULL when unmeasured."),
                    MartColumn(name="big_note", type=ColumnType.TEXT,
                               description="Measurement note; NULL when unmeasured."),
                ),
                plan=MartPlan(
                    mart=WIDE_MART,
                    ops=(
                        MartOp(kind=MartOpKind.SOURCE,
                               description="Bring events into scope.",
                               tables=("events",)),
                    ),
                    notes="Reference SQL is authored; the plan is a stub.",
                ),
            ),
        ),
        populations=_populations(task_id),
        reference=ReferenceSolution(
            implementation_id="gate_five_backend_probe_ref",
            dialect="duckdb",
            sql_by_mart={ROLLUP_MART: ROLLUP_SQL, WIDE_MART: WIDE_SQL},
            load_notes=(
                "Load customers from the postgres load SQL, orders from the "
                "mongodb jsonl, order_items from the flat csv, events from the "
                "paginated REST fixture, metrics from the S3 jsonl prefix."
            ),
            provenance="Hand-authored for the semantic gate fixture.",
            version="1",
        ),
    )


# ---------------------------------------------------------------------------
# Sensitive-value inventory (gate item 7a): each predicate proves one class is
# PRESENT in the rendered artifacts under a population source root.
# ---------------------------------------------------------------------------

def _slurp(root: Path, *parts: str) -> str:
    """Concatenated text of every file under root/<parts...> (or the file)."""
    path = root.joinpath(*parts)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    if path.is_dir():
        return "\n".join(
            child.read_text(encoding="utf-8")
            for child in sorted(path.rglob("*"))
            if child.is_file()
        )
    raise FileNotFoundError(f"no rendered artifact at {path}")


def _has_duplicate_line(root: Path) -> bool:
    lines = [
        line
        for line in _slurp(root, "files", "order_items.csv").splitlines()
        if line.strip()
    ]
    return any(lines.count(line) >= 2 for line in set(lines))


def _mongo_null_vs_missing(root: Path) -> bool:
    lines = _slurp(root, "mongodb", "orders.jsonl").splitlines()
    explicit_null = any('"note":null' in line for line in lines)
    missing_key = any('"note"' not in line for line in lines if line.strip())
    return explicit_null and missing_key


#: (label, predicate over one population's rendered source root).
SENSITIVE_INVENTORY: tuple = (
    ("unsafe integer 2**53+1 in REST pages",
     lambda root: str(UNSAFE_INT) in _slurp(root, "rest", "events")),
    ("negative unsafe integer in REST pages",
     lambda root: f"-{UNSAFE_INT}" in _slurp(root, "rest", "events")),
    ("9-fractional-digit boundary decimal in S3 parts",
     lambda root: PRECISE_DECIMAL_TEXT in _slurp(root, "s3", "metrics")),
    ("explicit JSON null AND missing key in mongodb jsonl",
     _mongo_null_vs_missing),
    ("literal 'null' and 'None' strings in postgres load SQL",
     lambda root: "'null'" in _slurp(root, "postgres", "customers.sql")
     and "'None'" in _slurp(root, "postgres", "customers.sql")),
    ("empty-string TEXT distinct from NULL in postgres load SQL",
     lambda root: "''" in _slurp(root, "postgres", "customers.sql")
     and "NULL" in _slurp(root, "postgres", "customers.sql")),
    ("empty-string TEXT value in REST pages",
     lambda root: '"label":""' in _slurp(root, "rest", "events")),
    ("MixedCase text in REST pages",
     lambda root: '"label":"MiXeD Case"' in _slurp(root, "rest", "events")),
    ("ISO timestamps with DISTINCT UTC offsets in REST pages",
     lambda root: "+02:00" in _slurp(root, "rest", "events")
     and "-05:00" in _slurp(root, "rest", "events")
     and "+00:00" in _slurp(root, "rest", "events")),
    (">= 32 KiB single text cell in S3 parts",
     lambda root: BIG_TEXT_FILL in _slurp(root, "s3", "metrics")),
    ("exact-duplicate ordering-tie rows in the flat CSV",
     _has_duplicate_line),
)


# Targeted probes pin frozen comparator behavior for sensitive mart cases.
# Change them only with a reviewed comparator or tolerance update.

#: (label, mart, probe SQL, expected legacy mart verdict on PRIMARY).
MART_PROBES: tuple = (
    # float64 cannot hold 2**53+1; the legacy comparator is numeric and
    # tolerance-based (REL_TOL 1e-2), so the 1-ulp loss is INVISIBLE to it.
    # Frozen verdict: True.  The strict typed diagnostic (IR-002) is the
    # instrument that reports the erasure, next to this unchanged reward.
    ("unsafe integer rounded through DOUBLE is tolerated (legacy contract)",
     WIDE_MART,
     WIDE_SQL.replace("e.big_count AS big_count",
                      "CAST(e.big_count AS DOUBLE) AS big_count"),
     True),
    # Gold froze the literal string 'null' as the EMPTY cell (pandas NA
    # sentinel); a submission emitting the *different* NA token 'None' still
    # matches.  Frozen verdict: True — NA sentinels are interchangeable.
    ("literal 'null' and 'None' text are both NA sentinels (legacy contract)",
     WIDE_MART,
     WIDE_SQL.replace(
         "e.label AS label",
         "CASE WHEN e.label = 'null' THEN 'None' ELSE e.label END AS label"),
     True),
    # String compare is case-insensitive.  Frozen verdict: True.  (Only the
    # 'ok' label is upper-cased: UPPER('None') would be 'NONE', which is NOT
    # a pandas NA sentinel while 'None' is — that asymmetry is its own class,
    # pinned by the sentinel probe above.)
    ("string compare is case-insensitive (legacy contract)",
     WIDE_MART,
     WIDE_SQL.replace(
         "e.label AS label",
         "CASE WHEN e.label = 'ok' THEN 'OK' ELSE e.label END AS label"),
     True),
    # A changed UTC offset is a TEXT mismatch: offsets survive and are graded.
    ("distinct UTC offsets are compared as text",
     WIDE_MART,
     WIDE_SQL.replace(
         "e.tz_stamp AS tz_stamp",
         "REPLACE(e.tz_stamp, '+02:00', '+0200') AS tz_stamp"),
     False),
    # Truncating the timestamp to a date is a mismatch: timestamps survive.
    ("timestamps survive to the comparator",
     WIDE_MART,
     WIDE_SQL.replace("e.occurred_at AS occurred_at",
                      "CAST(CAST(e.occurred_at AS DATE) AS TIMESTAMP) "
                      "AS occurred_at"),
     False),
    # Truncating the >= 32 KiB cell is a mismatch: large text is graded.
    ("large text cells are graded, not skipped",
     WIDE_MART,
     WIDE_SQL.replace("m.big_note AS big_note",
                      "SUBSTR(m.big_note, 1, 100) AS big_note"),
     False),
    # Rounding the 9-digit decimal to one digit moves it materially: mismatch.
    ("high-precision decimals are graded within the declared tolerance",
     WIDE_MART,
     WIDE_SQL.replace("m.value AS metric_value",
                      "ROUND(m.value, 1) AS metric_value"),
     False),
    # +1.005x is inside REL_TOL 1e-2: the tolerance is real.  Frozen: True.
    ("numeric tolerance REL_TOL=1e-2 is the legacy contract",
     ROLLUP_MART,
     ROLLUP_SQL.replace("COALESCE(SUM(ot.order_total), 0) AS total_spend",
                        "COALESCE(SUM(ot.order_total), 0) * 1.005 "
                        "AS total_spend"),
     True),
    # +1000 is far outside the tolerance: mismatch.
    ("beyond-tolerance numeric drift is a mismatch",
     ROLLUP_MART,
     ROLLUP_SQL.replace("COALESCE(SUM(ot.order_total), 0) AS total_spend",
                        "COALESCE(SUM(ot.order_total), 0) + 1000.0 "
                        "AS total_spend"),
     False),
    # Collapsing the duplicate line row moves customer base+1 from 51.5 to
    # 41.0: duplicates must BOTH be counted.
    ("exact-duplicate line rows are both counted",
     ROLLUP_MART,
     ROLLUP_SQL.replace(
         "    SELECT order_id, SUM(quantity * unit_price) AS order_total\n"
         "    FROM order_items\n",
         "    SELECT order_id, SUM(quantity * unit_price) AS order_total\n"
         "    FROM (SELECT DISTINCT order_id, quantity, unit_price, discount "
         "FROM order_items)\n"),
     False),
)

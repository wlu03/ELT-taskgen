"""Tests for reference/: trusted E+L, clean-room runner, gold freeze/load.

WHY THIS EXISTS
The reference stack is the source of all gold: if the loaders mis-parse a
rendered backend, the runner is nondeterministic, or the freeze/verify cycle
admits tampering, every downstream reward is wrong. These tests pin the spec's
worked example: the demo counterfactual gold must equal the literal C10/C11/C12
table (C10 0/0, C11 1/45, C12 0/0), determinism evidence must be byte-stable,
and load_gold must fail closed on any mutation of the frozen bundle.

The renderer fixtures here are written BY THE TESTS in the documented
rendered-artifact formats (postgres load SQL, mongodb jsonl, REST paginated
fixture dir, S3 jsonl layout, flat csv) because generation/source_data.py is
built in parallel; the formats follow docs/INTERFACES.md.
"""

from __future__ import annotations

import json
import math
import random
import tempfile
import unittest
from pathlib import Path

import duckdb

from elt_taskgen.demo_fixture import (
    COUNTERFACTUAL_EXPECTED_MART,
    COUNTERFACTUAL_LITERAL_ROWS,
    DEMO_TASK_ID,
    MART_NAME,
    demo_task,
)
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    GateResult,
    JoinType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    PopulationName,
    ReferenceSolution,
    Relationship,
    TableSpec,
    TaskIR,
)
from elt_taskgen.reference.gold import (
    EmptyGoldMartError,
    GoldBundle,
    GoldGrainError,
    NullGrainKeyError,
    freeze_gold,
    load_gold,
)
from elt_taskgen.reference.runner import (
    RunResult,
    _pin_session,
    determinism_evidence,
    open_reference_connection,
    run_reference,
)
from elt_taskgen.reference.solution import _sql_literal as duckdb_literal
from elt_taskgen.reference.solution import (
    MartOutputLimitError,
    PlanCompilationError,
    _insert_rows,
    _read_csv,
    _row_size,
    attach_reference,
    build_reference,
    coerce_value,
    compile_plan_sql,
    create_table,
    execute_mart,
    load_sources_duckdb,
)

P = PopulationName


# ---------------------------------------------------------------------------
# Fixture renderers (documented formats; generation/ is a parallel builder)
# ---------------------------------------------------------------------------

def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def render_counterfactual(workspace: Path, *, task_id: str = DEMO_TASK_ID) -> Path:
    """Materialize the demo counterfactual population into the workspace."""
    pop_dir = workspace / "tasks" / task_id / "populations" / P.COUNTERFACTUAL.value
    rendered = pop_dir / "rendered"
    rows_dir = pop_dir / "rows"

    # rows/<table>.jsonl — the frozen generated rows (canonical order).
    rows_dir.mkdir(parents=True, exist_ok=True)
    for table, rows in COUNTERFACTUAL_LITERAL_ROWS.items():
        with (rows_dir / f"{table}.jsonl").open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")

    # customers -> postgres load SQL (with transaction + DDL noise to skip).
    pg_dir = rendered / "postgres"
    pg_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "BEGIN;",
        "CREATE TABLE customers (customer_id INTEGER NOT NULL, customer_name TEXT);",
    ]
    for row in COUNTERFACTUAL_LITERAL_ROWS["customers"]:
        lines.append(
            "INSERT INTO customers (customer_id, customer_name) VALUES "
            f"({_sql_literal(row['customer_id'])}, {_sql_literal(row['customer_name'])});"
        )
    lines.append("COMMIT;")
    (pg_dir / "customers.sql").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # orders -> mongodb jsonl (with a mongo _id the schema does not know).
    mongo_dir = rendered / "mongodb"
    mongo_dir.mkdir(parents=True, exist_ok=True)
    with (mongo_dir / "orders.jsonl").open("w", encoding="utf-8") as fh:
        for i, row in enumerate(COUNTERFACTUAL_LITERAL_ROWS["orders"]):
            doc = {"_id": f"oid_{i:04d}", **row}
            fh.write(json.dumps(doc, sort_keys=True) + "\n")

    # order_items -> flat csv.
    files_dir = rendered / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    csv_lines = ["order_id,quantity,unit_price"]
    for row in COUNTERFACTUAL_LITERAL_ROWS["order_items"]:
        csv_lines.append(f"{row['order_id']},{row['quantity']},{row['unit_price']}")
    (files_dir / "order_items.csv").write_text("\n".join(csv_lines) + "\n", encoding="utf-8")
    return rendered


def _rest_s3_task() -> TaskIR:
    """Tiny task exercising the REST and S3 loader paths."""
    mart = "event_summary"
    return TaskIR(
        task_id="test__rest_s3",
        family_id="test__rest_s3",
        cluster_id="test__rest_s3",
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        tables=(
            TableSpec(
                name="events",
                columns=(
                    ColumnSpec(name="event_id", type=ColumnType.INTEGER,
                               description="Event id."),
                    ColumnSpec(name="kind", type=ColumnType.TEXT,
                               description="Event kind."),
                ),
                primary_key=("event_id",),
            ),
            TableSpec(
                name="metrics",
                columns=(
                    ColumnSpec(name="event_id", type=ColumnType.INTEGER,
                               description="Event the metric belongs to."),
                    ColumnSpec(name="value", type=ColumnType.FLOAT, nullable=True,
                               description="Measured value; may be NULL."),
                ),
            ),
        ),
        relationships=(
            Relationship(
                child_table="metrics",
                child_columns=("event_id",),
                parent_table="events",
                parent_columns=("event_id",),
                required=False,
            ),
        ),
        backends=(
            BackendAssignment(table="events", backend=Backend.REST),
            BackendAssignment(table="metrics", backend=Backend.S3),
        ),
        marts=(
            MartSpec(
                name=mart,
                grain="One row per event.",
                key_columns=("event_id",),
                columns=(
                    MartColumn(name="event_id", type=ColumnType.INTEGER,
                               description="Event id."),
                    MartColumn(name="total_value", type=ColumnType.DECIMAL,
                               description="Sum of metric values; 0 if none."),
                ),
                plan=MartPlan(
                    mart=mart,
                    ops=(
                        MartOp(kind=MartOpKind.SOURCE, description="events",
                               tables=("events",)),
                    ),
                ),
            ),
        ),
        reference=ReferenceSolution(
            implementation_id="test_rest_s3_ref",
            sql_by_mart={
                mart: (
                    "SELECT e.event_id AS event_id, "
                    "COALESCE(SUM(m.value), 0) AS total_value "
                    "FROM events AS e "
                    "LEFT JOIN metrics AS m ON m.event_id = e.event_id "
                    "GROUP BY e.event_id ORDER BY e.event_id"
                )
            },
        ),
    )


def render_rest_s3(workspace: Path, task: TaskIR) -> Path:
    rendered = (
        workspace / "tasks" / task.task_id / "populations" / P.DEVELOPMENT.value / "rendered"
    )
    # events -> paginated REST fixture dir with index.json.
    events_dir = rendered / "rest" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "page_0001.json").write_text(
        json.dumps({"data": [
            {"event_id": 1, "kind": "click"},
            {"event_id": 2, "kind": "view"},
        ]}),
        encoding="utf-8",
    )
    (events_dir / "page_0002.json").write_text(
        json.dumps({"data": [{"event_id": 3, "kind": "click"}]}), encoding="utf-8"
    )
    (events_dir / "index.json").write_text(
        json.dumps({"pages": ["page_0001.json", "page_0002.json"], "count": 3}),
        encoding="utf-8",
    )
    # metrics -> S3 jsonl layout (two objects under the table prefix).
    metrics_dir = rendered / "s3" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "part-0000.jsonl").write_text(
        json.dumps({"event_id": 1, "value": 1.5}) + "\n"
        + json.dumps({"event_id": 1, "value": 2.5}) + "\n",
        encoding="utf-8",
    )
    (metrics_dir / "part-0001.jsonl").write_text(
        json.dumps({"event_id": 2, "value": None}) + "\n", encoding="utf-8"
    )
    return rendered


def _structured_demo_plan_mart() -> MartSpec:
    """The demo mart re-declared with a fully STRUCTURED plan for the compiler."""
    name = "customer_summary_v2"
    plan = MartPlan(
        mart=name,
        ops=(
            MartOp(
                kind=MartOpKind.FILTER,
                description="Keep completed orders.",
                tables=("orders",),
                predicate="status = 'completed'",
                details={"name": "completed_orders"},
            ),
            MartOp(
                kind=MartOpKind.DEDUPE,
                description="DISTINCT completed order headers.",
                tables=("completed_orders",),
                columns=("order_id", "customer_id"),
            ),
            MartOp(
                kind=MartOpKind.AGGREGATE,
                description="Per-order item totals.",
                tables=("order_items",),
                details={
                    "group_by": "order_id",
                    "order_total": "SUM(quantity * unit_price)",
                    "name": "order_totals",
                },
            ),
            MartOp(
                kind=MartOpKind.JOIN,
                description="Retain customers without completed orders.",
                tables=("customers", "completed_orders"),
                join_type=JoinType.LEFT,
                predicate="completed_orders.customer_id = customers.customer_id",
                details={
                    "select": (
                        "customers.customer_id AS customer_id, "
                        "completed_orders.order_id AS order_id"
                    ),
                    "name": "cust_orders",
                },
            ),
            MartOp(
                kind=MartOpKind.JOIN,
                description="Attach per-order totals.",
                tables=("cust_orders", "order_totals"),
                join_type=JoinType.LEFT,
                predicate="order_totals.order_id = cust_orders.order_id",
                details={
                    "select": (
                        "cust_orders.customer_id AS customer_id, "
                        "cust_orders.order_id AS order_id, "
                        "order_totals.order_total AS order_total"
                    ),
                    "name": "cust_order_totals",
                },
            ),
            MartOp(
                kind=MartOpKind.AGGREGATE,
                description="Per-customer rollup with DISTINCT order count.",
                tables=("cust_order_totals",),
                details={
                    "group_by": "customer_id",
                    "completed_order_count": "COUNT(DISTINCT order_id)",
                    "total_spend_raw": "SUM(order_total)",
                    "name": "agg",
                },
            ),
            MartOp(
                kind=MartOpKind.DERIVE,
                description="COALESCE spend to 0.",
                tables=("agg",),
                details={
                    "select": (
                        "customer_id, completed_order_count, "
                        "COALESCE(total_spend_raw, 0) AS total_spend"
                    ),
                    "name": "final",
                },
            ),
            MartOp(
                kind=MartOpKind.TIE_BREAK,
                description="Deterministic order.",
                columns=("customer_id",),
            ),
        ),
    )
    return MartSpec(
        name=name,
        grain="One row per customer.",
        key_columns=("customer_id",),
        columns=(
            MartColumn(name="customer_id", type=ColumnType.INTEGER,
                       description="Customer id."),
            MartColumn(name="completed_order_count", type=ColumnType.INTEGER,
                       description="Distinct completed orders."),
            MartColumn(name="total_spend", type=ColumnType.DECIMAL,
                       description="Spend over completed orders; 0 if none."),
        ),
        plan=plan,
    )


def _fake_result(pop: PopulationName) -> RunResult:
    """Synthetic RunResult for populations the gold tests do not execute."""
    return RunResult(
        population=pop,
        stage1_counts={"customers": 2, "orders": 2, "order_items": 2},
        mart_rows={
            MART_NAME: [
                {"customer_id": 1, "completed_order_count": 1, "total_spend": 10.0},
                {"customer_id": 2, "completed_order_count": 0, "total_spend": 0.0},
            ]
        },
    )


EXPECTED_COUNTERFACTUAL_CSV = (
    "customer_id,completed_order_count,total_spend\n"
    "10,0,0.0\n"
    "11,1,45.0\n"
    "12,0,0.0\n"
)


# ---------------------------------------------------------------------------
# Shared workspace with the rendered counterfactual population
# ---------------------------------------------------------------------------

class CounterfactualWorkspaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.workspace = Path(cls._tmp.name)
        cls.rendered = render_counterfactual(cls.workspace)
        cls.task = demo_task()


class TestLoadSources(CounterfactualWorkspaceTest):
    def test_loads_all_three_backends_with_exact_counts(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            loaded = load_sources_duckdb(self.task, self.rendered, con)
            self.assertEqual(
                loaded.counts, {"customers": 3, "orders": 2, "order_items": 4}
            )
            rows = con.execute(
                "SELECT customer_id, customer_name FROM customers ORDER BY customer_id"
            ).fetchall()
            self.assertEqual(rows, [(10, "C10"), (11, "C11"), (12, "C12")])
            # Mongo's _id must not leak into the schema-typed table.
            cols = [d[0] for d in con.execute("SELECT * FROM orders LIMIT 0").description]
            self.assertEqual(cols, ["order_id", "customer_id", "status"])
        finally:
            con.close()

    def test_missing_artifact_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = render_counterfactual(Path(tmp))
            (broken / "mongodb" / "orders.jsonl").unlink()
            con = duckdb.connect(":memory:")
            try:
                with self.assertRaises(FileNotFoundError):
                    load_sources_duckdb(self.task, broken, con)
            finally:
                con.close()

    def test_unknown_postgres_statement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = render_counterfactual(Path(tmp))
            sql_path = broken / "postgres" / "customers.sql"
            sql_path.write_text(
                sql_path.read_text(encoding="utf-8")
                + "\nCOPY customers FROM 'more_customers.csv';\n",
                encoding="utf-8",
            )
            con = duckdb.connect(":memory:")
            try:
                with self.assertRaises(ValueError):
                    load_sources_duckdb(self.task, broken, con)
            finally:
                con.close()

    def test_coerce_value_semantics(self) -> None:
        int_col = ColumnSpec(name="n", type=ColumnType.INTEGER)
        nullable_float = ColumnSpec(name="v", type=ColumnType.FLOAT, nullable=True)
        bool_col = ColumnSpec(name="b", type=ColumnType.BOOLEAN)
        self.assertEqual(coerce_value("42", int_col, table="t"), 42)
        self.assertEqual(coerce_value("3.0", int_col, table="t"), 3)
        self.assertIsNone(coerce_value("", nullable_float, table="t"))
        self.assertEqual(coerce_value("1.5", nullable_float, table="t"), 1.5)
        self.assertIs(coerce_value("true", bool_col, table="t"), True)
        self.assertIs(coerce_value(0, bool_col, table="t"), False)
        with self.assertRaises(ValueError):
            coerce_value("3.7", int_col, table="t")
        with self.assertRaises(ValueError):
            coerce_value("maybe", bool_col, table="t")


    def test_files_empty_field_loads_as_null_for_text(self) -> None:
        """G6: a CSV field cannot carry '' distinct from NULL, and every
        served-world reader (pandas/Airbyte Files, DuckDB read_csv, dlt) reads
        an empty field as NULL — so must the trusted loader, for TEXT too."""
        table = TableSpec(
            name="t",
            columns=(
                ColumnSpec(name="id", type=ColumnType.INTEGER),
                ColumnSpec(name="label", type=ColumnType.TEXT, nullable=True),
            ),
            primary_key=("id",),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.csv"
            path.write_text("id,label\n1,\n2,x\n", encoding="utf-8")
            con = duckdb.connect(":memory:")
            try:
                create_table(con, table)
                _insert_rows(con, table, _read_csv(path))
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*), COUNT(label), "
                        "COUNT(*) FILTER (WHERE label = '') FROM t"
                    ).fetchone(),
                    (2, 1, 0),
                )
            finally:
                con.close()
        # coerce_value is unchanged: a JSON "" on a TEXT column stays ''.
        text_col = ColumnSpec(name="s", type=ColumnType.TEXT, nullable=True)
        self.assertEqual(coerce_value("", text_col, table="t"), "")

    def test_insert_rows_literal_spelling_round_trips_every_scalar(self) -> None:
        """`_insert_rows` spells coerced cells as literals (measured 60x
        faster than parameter binding); the values must be the same ones."""
        self.assertEqual(duckdb_literal(None), "NULL")
        self.assertEqual(duckdb_literal(True), "TRUE")
        self.assertEqual(duckdb_literal(False), "FALSE")
        self.assertEqual(duckdb_literal(42), "42")
        # Floats are always spelled in exponent form so DuckDB parses them as
        # DOUBLE: a bare 17-digit literal is typed DECIMAL(18,17) and its
        # DECIMAL->DOUBLE cast is not correctly rounded (one ulp off).
        self.assertEqual(duckdb_literal(0.1), "0.1e0")
        self.assertEqual(duckdb_literal(-2.5), "-2.5e0")
        self.assertEqual(duckdb_literal(1e-05), "1e-05")
        self.assertEqual(duckdb_literal(1e16), "1e+16")
        self.assertEqual(duckdb_literal(0.9214950862885405), "0.9214950862885405e0")
        self.assertEqual(duckdb_literal("it's"), "'it''s'")
        self.assertEqual(duckdb_literal("a\\b"), "'a\\b'")
        self.assertEqual(duckdb_literal(float("nan")), "'NaN'::DOUBLE")
        self.assertEqual(duckdb_literal(float("inf")), "'Infinity'::DOUBLE")
        self.assertEqual(duckdb_literal("a\x00b"), "'a' || chr(0) || 'b'")
        table = TableSpec(
            name="scalars",
            columns=(
                ColumnSpec(name="i", type=ColumnType.BIGINT),
                ColumnSpec(name="f", type=ColumnType.FLOAT, nullable=True),
                ColumnSpec(name="d", type=ColumnType.DECIMAL, nullable=True),
                ColumnSpec(name="b", type=ColumnType.BOOLEAN, nullable=True),
                ColumnSpec(name="s", type=ColumnType.TEXT, nullable=True),
                ColumnSpec(name="dt", type=ColumnType.DATE, nullable=True),
                ColumnSpec(name="ts", type=ColumnType.TIMESTAMP, nullable=True),
                ColumnSpec(name="j", type=ColumnType.JSON, nullable=True),
            ),
        )
        rows = [
            {"i": 1, "f": 0.1, "d": "12.34", "b": "true", "s": "it's a \\ 'quote'",
             "dt": "2024-01-05", "ts": "2024-01-05 10:00:00", "j": {"k": [1, 2]}},
            {"i": 2, "f": None, "d": None, "b": None, "s": None, "dt": None,
             "ts": None, "j": None},
            {"i": 3, "f": 1e-05, "d": 5, "b": 0, "s": "", "dt": "2024-12-31",
             "ts": "2024-12-31 23:59:59", "j": '{"a": 1}'},
        ]
        con = duckdb.connect(":memory:")
        try:
            create_table(con, table)
            self.assertEqual(3, _insert_rows(con, table, rows))
            got = con.execute("SELECT * FROM scalars ORDER BY i").fetchall()
        finally:
            con.close()
        self.assertEqual(got[0][0], 1)
        self.assertEqual(got[0][1], 0.1)
        self.assertEqual(got[0][2], 12.34)
        self.assertIs(got[0][3], True)
        self.assertEqual(got[0][4], "it's a \\ 'quote'")
        self.assertEqual(str(got[0][5]), "2024-01-05")
        self.assertEqual(str(got[0][6]), "2024-01-05 10:00:00")
        self.assertEqual(got[0][7], '{"k":[1,2]}')
        self.assertEqual(got[1][1:], (None,) * 7)
        self.assertEqual(got[2][1], 1e-05)
        self.assertIs(got[2][3], False)
        self.assertEqual(got[2][4], "")

    def test_insert_rows_float_literals_load_the_exact_python_double(self) -> None:
        """Regression: a finite float spelled as a bare decimal literal with
        16-17 significant digits is typed DECIMAL by DuckDB and lands one ulp
        off through its DECIMAL->DOUBLE cast (0.9214950862885405 ->
        0.9214950862885404). The loaded DOUBLE must equal the Python float
        and the ``?::DOUBLE`` bound value (what the generator froze and what
        ``read_csv``/pandas read back) for every spelling."""
        table = TableSpec(
            name="doubles",
            columns=(
                ColumnSpec(name="i", type=ColumnType.BIGINT),
                ColumnSpec(name="f", type=ColumnType.FLOAT),
                ColumnSpec(name="d", type=ColumnType.DECIMAL),
            ),
        )
        values = [
            0.9214950862885405, 0.12434361466587263, 0.42451918914251396,
            0.12380196114964559, 0.22323896460701453, 0.1, 1e-05, 1e16,
            -2.5, -0.0, 5e-324, 1.7976931348623157e308, 123456789012345678.0,
            3.141592653589793, 2.718281828459045,
        ]
        rng = random.Random(20260818)
        values += [rng.random() for _ in range(2000)]
        values += [rng.uniform(-1e9, 1e9) for _ in range(1000)]
        rows = [{"i": i, "f": v, "d": repr(v)} for i, v in enumerate(values)]
        con = duckdb.connect(":memory:")
        try:
            create_table(con, table)
            self.assertEqual(len(values), _insert_rows(con, table, rows))
            got = con.execute("SELECT i, f, d FROM doubles ORDER BY i").fetchall()
            bound = [
                con.execute("SELECT ?::DOUBLE", [v]).fetchone()[0] for v in values[:15]
            ]
        finally:
            con.close()
        mismatched = [
            (values[i], f, d) for i, f, d in got if f != values[i] or d != values[i]
        ]
        self.assertEqual(mismatched, [], mismatched[:5])
        self.assertEqual([f for _, f, _ in got[:15]], bound)
        self.assertEqual([d for _, _, d in got[:15]], bound)
        # The sign of negative zero survives too (a distinct DOUBLE bit pattern).
        self.assertEqual(math.copysign(1.0, got[9][1]), -1.0)

    def test_insert_rows_text_with_embedded_nul_loads_like_a_bound_parameter(self) -> None:
        """A NUL byte cannot sit inside a DuckDB string literal; the loader
        spells it as a chr(0) concatenation and the VARCHAR is identical to
        what the old bound-parameter path stored."""
        table = TableSpec(
            name="nuls",
            columns=(
                ColumnSpec(name="i", type=ColumnType.BIGINT),
                ColumnSpec(name="s", type=ColumnType.TEXT, nullable=True),
            ),
        )
        texts = ["a\x00b", "\x00", "it's\x00\x00'quoted'", "plain"]
        con = duckdb.connect(":memory:")
        try:
            create_table(con, table)
            _insert_rows(con, table, [{"i": i, "s": t} for i, t in enumerate(texts)])
            got = con.execute("SELECT s FROM nuls ORDER BY i").fetchall()
            bound = [con.execute("SELECT ?::VARCHAR", [t]).fetchone()[0] for t in texts]
        finally:
            con.close()
        self.assertEqual([g[0] for g in got], texts)
        self.assertEqual([g[0] for g in got], bound)


class TestRestS3Loaders(unittest.TestCase):
    def test_rest_and_s3_paths_load_and_run(self) -> None:
        task = _rest_s3_task()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            render_rest_s3(workspace, task)
            result = run_reference(task, P.DEVELOPMENT, workspace)
        self.assertEqual(result.stage1_counts, {"events": 3, "metrics": 3})
        self.assertEqual(
            result.mart_rows["event_summary"],
            [
                {"event_id": 1, "total_value": 4.0},
                {"event_id": 2, "total_value": 0.0},
                {"event_id": 3, "total_value": 0.0},
            ],
        )


class TestRunReference(CounterfactualWorkspaceTest):
    def test_counterfactual_matches_the_spec_literal_table(self) -> None:
        result = run_reference(self.task, P.COUNTERFACTUAL, self.workspace)
        self.assertEqual(
            result.stage1_counts, {"customers": 3, "orders": 2, "order_items": 4}
        )
        self.assertEqual(
            result.mart_rows[MART_NAME], list(COUNTERFACTUAL_EXPECTED_MART)
        )

    def test_loaded_counts_cross_checked_against_frozen_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            render_counterfactual(workspace)
            rows_path = (
                workspace / "tasks" / DEMO_TASK_ID / "populations"
                / P.COUNTERFACTUAL.value / "rows" / "customers.jsonl"
            )
            # Frozen rows claim 2 customers; the renderer materialized 3.
            lines = rows_path.read_text(encoding="utf-8").splitlines()[:2]
            rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                run_reference(self.task, P.COUNTERFACTUAL, workspace)

    def test_missing_rendered_dir_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                run_reference(self.task, P.COUNTERFACTUAL, Path(tmp))

    def test_loaded_contents_cross_checked_against_frozen_rows(self) -> None:
        """G6 follow-up: same row COUNT, different VALUE — the rendered
        artifact disagrees with the generator's frozen truth, so it is not
        gold. Mirrors the count cross-check above."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            render_counterfactual(workspace)
            csv_path = (
                workspace / "tasks" / DEMO_TASK_ID / "populations"
                / P.COUNTERFACTUAL.value / "rendered" / "files" / "order_items.csv"
            )
            lines = csv_path.read_text(encoding="utf-8").splitlines()
            first = lines[1].split(",")
            first[-1] = str(float(first[-1]) + 1.0)
            csv_path.write_text(
                "\n".join([lines[0], ",".join(first)] + lines[2:]) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                run_reference(self.task, P.COUNTERFACTUAL, workspace)
        message = str(ctx.exception)
        self.assertIn("diverge from frozen generated rows", message)
        self.assertIn("order_items", message)

    def test_a_null_flattened_to_empty_text_is_a_content_divergence(self) -> None:
        """The exact class G6 was: a TEXT NULL rendered as '' would count as
        a value while the frozen rows carry None. With `_read_csv` mapping
        the empty field back to NULL the CSV agrees; a jsonl artifact carrying
        a literal "" where the frozen rows say null does not."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            render_counterfactual(workspace)
            pop_dir = (
                workspace / "tasks" / DEMO_TASK_ID / "populations" / P.COUNTERFACTUAL.value
            )
            mongo = pop_dir / "rendered" / "mongodb" / "orders.jsonl"
            docs = [json.loads(line) for line in mongo.read_text(encoding="utf-8").splitlines()]
            docs[0]["status"] = ""  # frozen rows: a real status; served: ''
            mongo.write_text(
                "\n".join(json.dumps(d, sort_keys=True) for d in docs) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                run_reference(self.task, P.COUNTERFACTUAL, workspace)
        self.assertIn("diverge from frozen generated rows", str(ctx.exception))
        self.assertIn("orders", str(ctx.exception))

    def test_reference_sessions_are_pinned_and_sandboxed(self) -> None:
        con = open_reference_connection()
        try:
            (collation,) = con.execute("SELECT current_setting('default_collation')").fetchone()
            (null_order,) = con.execute("SELECT current_setting('default_null_order')").fetchone()
            (threads,) = con.execute("SELECT current_setting('threads')").fetchone()
            (external,) = con.execute(
                "SELECT current_setting('enable_external_access')"
            ).fetchone()
            self.assertEqual(collation, "binary")
            self.assertEqual(null_order, "NULLS_LAST")
            self.assertEqual(int(threads), 1)
            self.assertIn(str(external).lower(), ("false", "0"))
            with self.assertRaises(duckdb.Error):
                con.execute("SET default_collation = 'nocase'")  # locked
            with self.assertRaises(duckdb.Error):
                con.execute("SET enable_external_access = true")  # locked
        finally:
            con.close()
        raw = duckdb.connect(":memory:")
        try:
            raw.execute("SET default_collation = 'nocase'")
            _pin_session(raw)
            (collation,) = raw.execute("SELECT current_setting('default_collation')").fetchone()
            (threads,) = raw.execute("SELECT current_setting('threads')").fetchone()
            self.assertEqual(collation, "binary")
            self.assertEqual(int(threads), 1)
        finally:
            raw.close()


class TestDeterminism(CounterfactualWorkspaceTest):
    def test_three_rebuild_runs_are_byte_identical(self) -> None:
        gate = determinism_evidence(
            self.task, P.COUNTERFACTUAL, self.workspace, runs=3
        )
        self.assertTrue(gate.passed, gate.details)
        self.assertEqual(gate.gate, "determinism")
        digests = {
            gate.evidence[f"run_{i}_sha256"] for i in range(3)
        }
        self.assertEqual(len(digests), 1)

    def test_missing_environment_is_a_failed_gate_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = determinism_evidence(self.task, P.PRIMARY, Path(tmp), runs=3)
        self.assertFalse(gate.passed)
        self.assertIn("error", gate.evidence)

    def test_fewer_than_two_runs_fails(self) -> None:
        gate = determinism_evidence(self.task, P.COUNTERFACTUAL, self.workspace, runs=1)
        self.assertFalse(gate.passed)


class TestPlanCompiler(CounterfactualWorkspaceTest):
    def test_structured_plan_compiles_and_reproduces_counterfactual(self) -> None:
        mart = _structured_demo_plan_mart()
        sql = compile_plan_sql(self.task, mart)
        # Deterministic tie-break: total order over every mart column, every
        # identifier quoted (mart column names are vendored text).
        self.assertIn(
            'ORDER BY "customer_id", "completed_order_count", "total_spend"', sql
        )
        self.assertIn("LEFT JOIN", sql)
        self.assertIn("COUNT(DISTINCT order_id)", sql)
        self.assertIn("COALESCE(total_spend_raw, 0)", sql)
        con = duckdb.connect(":memory:")
        try:
            load_sources_duckdb(self.task, self.rendered, con)
            rows = execute_mart(con, mart, sql)
        finally:
            con.close()
        self.assertEqual(rows, list(COUNTERFACTUAL_EXPECTED_MART))

    def test_prose_plan_fails_closed_instead_of_guessing(self) -> None:
        with self.assertRaises(PlanCompilationError):
            compile_plan_sql(self.task, self.task.mart(MART_NAME))

    def test_existing_reference_sql_is_reused_verbatim(self) -> None:
        """Back-compat pin: a TaskIR carrying `reference.sql_by_mart` for every
        mart is never recompiled, so quoting changes in the compiler cannot
        move the content hash (or the frozen gold) of an existing task."""
        task = demo_task()
        self.assertIsNotNone(task.reference)
        again = attach_reference(task)
        self.assertEqual(again.content_hash(), task.content_hash())
        self.assertEqual(again.reference.sql_by_mart, task.reference.sql_by_mart)
        self.assertIs(build_reference(task), task.reference)

    def test_compiled_sql_quotes_mart_columns_and_measure_aliases(self) -> None:
        """G8-B: mart column names and measure aliases are vendored text; a
        column named `end`/`order` used to make the final SELECT/ORDER BY a
        ParserException after the plan validated clean."""
        mart_name = "reserved_words"
        plan = MartPlan(
            mart=mart_name,
            ops=(
                MartOp(
                    kind=MartOpKind.AGGREGATE,
                    description="Per-order rollup with reserved-word aliases.",
                    tables=("order_items",),
                    details={
                        "group_by": "order_id",
                        "end": "SUM(quantity)",
                        "order": "MAX(unit_price)",
                        "name": "agg",
                    },
                ),
                MartOp(
                    kind=MartOpKind.TIE_BREAK, description="Order.", columns=("order_id",),
                ),
            ),
        )
        mart = MartSpec(
            name=mart_name,
            grain="One row per order.",
            key_columns=("order_id",),
            columns=(
                MartColumn(name="order_id", type=ColumnType.INTEGER, description="Order id."),
                MartColumn(name="end", type=ColumnType.INTEGER, description="Total quantity."),
                MartColumn(name="order", type=ColumnType.DECIMAL, description="Max price."),
            ),
            plan=plan,
        )
        task = self.task.model_copy(
            update={"marts": (mart,), "reference": None, "attack_cases": ()}
        )
        sql = compile_plan_sql(task, mart)
        self.assertIn('SUM(quantity) AS "end"', sql)
        self.assertIn('MAX(unit_price) AS "order"', sql)
        last_two = sql.strip().splitlines()[-2:]
        self.assertTrue(
            last_two[0].startswith(
                'SELECT CAST("order_id" AS INTEGER) AS "order_id", '
                'CAST("end" AS INTEGER) AS "end", '
                'CAST("order" AS DOUBLE) AS "order" FROM'
            )
        )
        self.assertEqual(last_two[1], 'ORDER BY "order_id", "end", "order"')
        con = duckdb.connect(":memory:")
        try:
            load_sources_duckdb(self.task, self.rendered, con)
            rows = execute_mart(con, mart, sql)
        finally:
            con.close()
        self.assertEqual([r["order_id"] for r in rows], sorted(r["order_id"] for r in rows))
        self.assertTrue(rows)

    def test_final_projection_enforces_declared_integer_widths(self) -> None:
        """DuckDB widens both INTEGER and BIGINT SUM inputs to HUGEINT.

        The compiled mart boundary must narrow those intermediates to the exact
        logical types declared in the TaskIR while preserving output names.
        """
        mart_name = "integer_sum_widths"
        plan = MartPlan(
            mart=mart_name,
            ops=(
                MartOp(
                    kind=MartOpKind.AGGREGATE,
                    description="Sum integer measures per order.",
                    tables=("order_items",),
                    details={
                        "group_by": "order_id",
                        "integer_sum": "SUM(quantity)",
                        "bigint_sum": "SUM(CAST(quantity AS BIGINT))",
                        "name": "summed",
                    },
                ),
                MartOp(
                    kind=MartOpKind.TIE_BREAK,
                    description="Order by the mart key.",
                    columns=("order_id",),
                ),
            ),
        )
        mart = MartSpec(
            name=mart_name,
            grain="One row per order.",
            key_columns=("order_id",),
            columns=(
                MartColumn(
                    name="order_id",
                    type=ColumnType.INTEGER,
                    description="Order id.",
                ),
                MartColumn(
                    name="integer_sum",
                    type=ColumnType.INTEGER,
                    description="Quantity summed into an INTEGER contract.",
                ),
                MartColumn(
                    name="bigint_sum",
                    type=ColumnType.BIGINT,
                    description="Quantity summed into a BIGINT contract.",
                ),
            ),
            plan=plan,
        )
        task = self.task.model_copy(
            update={"marts": (mart,), "reference": None, "attack_cases": ()}
        )

        sql = compile_plan_sql(task, mart)
        self.assertEqual(sql, compile_plan_sql(task, mart))
        self.assertIn(
            'CAST("integer_sum" AS INTEGER) AS "integer_sum"', sql
        )
        self.assertIn('CAST("bigint_sum" AS BIGINT) AS "bigint_sum"', sql)

        con = duckdb.connect(":memory:")
        try:
            for table in task.tables:
                create_table(con, table)
            widened = con.execute(
                "SELECT typeof(SUM(quantity)), "
                "typeof(SUM(CAST(quantity AS BIGINT))) FROM order_items"
            ).fetchone()
            described = con.execute(f"DESCRIBE {sql}").fetchall()
            con.execute(
                "INSERT INTO order_items (order_id, quantity, unit_price) "
                "VALUES (1, 2147483647, 1), (1, 1, 1)"
            )
            with self.assertRaises(duckdb.Error):
                con.execute(sql).fetchall()
        finally:
            con.close()
        self.assertEqual(widened, ("HUGEINT", "HUGEINT"))
        self.assertEqual(
            [(row[0], row[1]) for row in described],
            [
                ("order_id", "INTEGER"),
                ("integer_sum", "INTEGER"),
                ("bigint_sum", "BIGINT"),
            ],
        )

    def test_task_without_reference_gets_a_compiled_solution(self) -> None:
        mart = _structured_demo_plan_mart()
        base = demo_task()
        task = base.model_copy(
            update={"reference": None, "marts": (mart,), "attack_cases": ()}
        )
        ref = build_reference(task)
        self.assertEqual(ref.dialect, "duckdb")
        self.assertIn(mart.name, ref.sql_by_mart)
        self.assertIn("compile", ref.provenance.lower())


class TestGold(CounterfactualWorkspaceTest):
    def _freeze(self, answer_key_dir: Path) -> GoldBundle:
        results = {pop: _fake_result(pop) for pop in P if pop is not P.COUNTERFACTUAL}
        results[P.COUNTERFACTUAL] = run_reference(
            self.task, P.COUNTERFACTUAL, self.workspace
        )
        return freeze_gold(self.task, results, answer_key_dir)

    def test_counterfactual_gold_equals_the_spec_literal_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            bundle = self._freeze(answer_key)
            self.assertEqual(
                bundle.stage2_csv[P.COUNTERFACTUAL.value][MART_NAME],
                EXPECTED_COUNTERFACTUAL_CSV,
            )
            self.assertEqual(
                bundle.stage1[P.COUNTERFACTUAL.value],
                {"customers": 3, "orders": 2, "order_items": 4},
            )
            on_disk = (
                answer_key / "gold" / P.COUNTERFACTUAL.value / f"{MART_NAME}.csv"
            ).read_text(encoding="utf-8")
            self.assertEqual(on_disk, EXPECTED_COUNTERFACTUAL_CSV)

    def test_freeze_writes_manifest_reference_and_all_populations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            bundle = self._freeze(answer_key)
            self.assertEqual(bundle.task_id, DEMO_TASK_ID)
            self.assertEqual(bundle.task_content_hash, self.task.content_hash())
            self.assertEqual(set(bundle.stage1), {p.value for p in P})
            self.assertIn(
                f"gold/{P.COUNTERFACTUAL.value}/{MART_NAME}.csv", bundle.file_hashes
            )
            self.assertIn(f"reference/{MART_NAME}.sql", bundle.file_hashes)
            self.assertIn("reference/solution.json", bundle.file_hashes)
            self.assertTrue((answer_key / "manifest.json").is_file())

    def test_load_gold_round_trips_the_frozen_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            frozen = self._freeze(answer_key)
            loaded = load_gold(answer_key)
            self.assertEqual(loaded, frozen)

    def test_tampered_gold_csv_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            self._freeze(answer_key)
            target = answer_key / "gold" / P.COUNTERFACTUAL.value / f"{MART_NAME}.csv"
            target.write_text(
                target.read_text(encoding="utf-8").replace("45.0", "44.0"),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_gold(answer_key)

    def test_missing_gold_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            self._freeze(answer_key)
            (answer_key / "gold" / P.PRIMARY.value / f"{MART_NAME}.csv").unlink()
            with self.assertRaises(FileNotFoundError):
                load_gold(answer_key)

    def test_unmanifested_file_under_gold_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            self._freeze(answer_key)
            (answer_key / "gold" / P.PRIMARY.value / "smuggled.csv").write_text(
                "customer_id\n999\n", encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_gold(answer_key)

    def test_missing_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_gold(Path(tmp))

    def test_freeze_requires_every_declared_population(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = {
                pop: _fake_result(pop)
                for pop in P
                if pop not in (P.COUNTERFACTUAL, P.STRESS)
            }
            results[P.COUNTERFACTUAL] = run_reference(
                self.task, P.COUNTERFACTUAL, self.workspace
            )
            with self.assertRaises(ValueError):
                freeze_gold(self.task, results, Path(tmp) / "answer_key")

    def test_freeze_rejects_result_filed_under_wrong_population(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = {pop: _fake_result(pop) for pop in P}
            results[P.STRESS] = _fake_result(P.PRIMARY)  # mislabeled
            with self.assertRaises(ValueError):
                freeze_gold(self.task, results, Path(tmp) / "answer_key")

    def _results_with(self, pop: PopulationName, result: RunResult) -> dict:
        results = {p: _fake_result(p) for p in P if p is not P.COUNTERFACTUAL}
        results[P.COUNTERFACTUAL] = run_reference(
            self.task, P.COUNTERFACTUAL, self.workspace
        )
        results[pop] = result
        return results

    def test_freeze_refuses_gold_that_violates_the_declared_grain(self) -> None:
        """Item 1 proof-by-construction: duplicate-grain gold cannot freeze.

        This is the freeze-time assert that would have refused the schemapile
        wrong-grain tasks (stress gold with many rows over few distinct keys)
        and the wikidbs split-grain marts: two gold rows sharing the mart's
        declared key columns raise a typed GoldGrainError naming the mart,
        the population and the duplicated key — before anything is written.
        """
        wrong_grain = RunResult(
            population=P.STRESS,
            stage1_counts={"customers": 2, "orders": 2, "order_items": 2},
            mart_rows={
                MART_NAME: [
                    {"customer_id": 1, "completed_order_count": 1, "total_spend": 10.0},
                    {"customer_id": 1, "completed_order_count": 2, "total_spend": 99.0},
                ]
            },
        )
        results = self._results_with(P.STRESS, wrong_grain)
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            with self.assertRaises(GoldGrainError) as ctx:
                freeze_gold(self.task, results, answer_key)
            message = str(ctx.exception)
            self.assertIn(MART_NAME, message)
            self.assertIn(P.STRESS.value, message)
            self.assertIn("customer_id", message)
            self.assertIn("1", message)
            # Refused BEFORE writing: no partial answer key on disk.
            self.assertFalse(answer_key.exists())

    def test_freeze_refuses_a_null_or_empty_grain_key(self) -> None:
        """"One row per <key>" is TWO claims; only uniqueness was checked.

        The three blocked dbt records were caught by the uniqueness assert, but
        only by luck of arithmetic: `{ad_group_id: None, date_day: None}`
        appeared 63 times and `{user_id: ''}` 58 times, so they duplicated. A
        mart with exactly ONE null-keyed row froze clean and published a row
        nobody can name. This is the missing half, and it is POOL-AGNOSTIC —
        it needs no adapter knowledge, only the declared key and the rows.

        `''` is refused alongside `None` because gold reaches the comparator
        through CSV, which cannot tell them apart — that is literally how a
        NULL `user_id` became `{user_id: ''}`.
        """
        for blank in (None, "", "   "):
            with self.subTest(value=repr(blank)):
                null_key = RunResult(
                    population=P.STRESS,
                    stage1_counts={"customers": 2, "orders": 2, "order_items": 2},
                    mart_rows={
                        MART_NAME: [
                            {
                                "customer_id": blank,
                                "completed_order_count": 1,
                                "total_spend": 10.0,
                            },
                        ]
                    },
                )
                results = self._results_with(P.STRESS, null_key)
                with tempfile.TemporaryDirectory() as tmp:
                    answer_key = Path(tmp) / "answer_key"
                    with self.assertRaises(NullGrainKeyError) as ctx:
                        freeze_gold(self.task, results, answer_key)
                    message = str(ctx.exception)
                    self.assertIn(MART_NAME, message)
                    self.assertIn("customer_id", message)
                    self.assertIn("NULL", message)
                    # Same family as the uniqueness refusal: every caller that
                    # already handles a grain refusal handles this one.
                    self.assertIsInstance(ctx.exception, GoldGrainError)
                    self.assertFalse(answer_key.exists())

    def test_the_null_grain_gate_is_quiet_on_gold_that_is_actually_keyed(
        self,
    ) -> None:
        """A gate nobody can turn on is worthless: a `0`, a `False` and a
        legitimate zero-length-looking key that is a real value must all pass.
        Falsiness is not emptiness — `0` identifies a row."""
        keyed = RunResult(
            population=P.STRESS,
            stage1_counts={"customers": 2, "orders": 2, "order_items": 2},
            mart_rows={
                MART_NAME: [
                    {"customer_id": 0, "completed_order_count": 1, "total_spend": 0.0},
                    {"customer_id": 7, "completed_order_count": 2, "total_spend": 9.0},
                ]
            },
        )
        results = self._results_with(P.STRESS, keyed)
        with tempfile.TemporaryDirectory() as tmp:
            bundle = freeze_gold(self.task, results, Path(tmp) / "answer_key")
            self.assertIn(P.STRESS.value, bundle.stage2_csv)

    def test_freeze_refuses_empty_gold_mart_on_a_graded_population(self) -> None:
        """Item 3 proof-by-construction: empty gold on a graded pop refuses.

        compare_mart scores empty-vs-empty 1.0 (free credit) and the T
        degenerate-zero gate only catches it at stage 9; the freeze refuses
        immediately, with a remedy in the message.
        """
        empty = RunResult(
            population=P.STRESS,
            stage1_counts={"customers": 2, "orders": 2, "order_items": 2},
            mart_rows={MART_NAME: []},
        )
        results = self._results_with(P.STRESS, empty)
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            with self.assertRaises(EmptyGoldMartError) as ctx:
                freeze_gold(self.task, results, answer_key)
            message = str(ctx.exception)
            self.assertIn(MART_NAME, message)
            self.assertIn(P.STRESS.value, message)
            self.assertIn("Remedy", message)
            self.assertFalse(answer_key.exists())

    def test_empty_development_gold_is_not_refused(self) -> None:
        """Development is solver-visible and UNGRADED (gates.GRADED_POPULATIONS
        excludes it), so an empty development mart is legal at freeze."""
        empty_dev = RunResult(
            population=P.DEVELOPMENT,
            stage1_counts={"customers": 2, "orders": 2, "order_items": 2},
            mart_rows={MART_NAME: []},
        )
        results = self._results_with(P.DEVELOPMENT, empty_dev)
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            bundle = freeze_gold(self.task, results, answer_key)
            self.assertEqual(
                bundle.stage2_csv[P.DEVELOPMENT.value][MART_NAME].strip().splitlines(),
                ["customer_id,completed_order_count,total_spend"],
            )

    def test_grain_check_runs_for_every_population(self) -> None:
        """The assert is per population: a duplicate key hidden in DEVELOPMENT
        (ungraded, but still frozen gold) is refused too."""
        wrong_grain_dev = RunResult(
            population=P.DEVELOPMENT,
            stage1_counts={"customers": 2, "orders": 2, "order_items": 2},
            mart_rows={
                MART_NAME: [
                    {"customer_id": 7, "completed_order_count": 1, "total_spend": 5.0},
                    {"customer_id": 7, "completed_order_count": 1, "total_spend": 5.0},
                ]
            },
        )
        results = self._results_with(P.DEVELOPMENT, wrong_grain_dev)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GoldGrainError):
                freeze_gold(self.task, results, Path(tmp) / "answer_key")

    def test_refreeze_is_a_full_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            self._freeze(answer_key)
            stale = answer_key / "gold" / P.PRIMARY.value / "stale_mart.csv"
            stale.write_text("x\n1\n", encoding="utf-8")
            frozen = self._freeze(answer_key)
            self.assertFalse(stale.exists())
            self.assertEqual(load_gold(answer_key), frozen)

    def test_refreeze_does_not_manifest_the_previous_export(self) -> None:
        """A later export may replace its private trees without breaking gold.

        Reused workspaces still carry ``evaluation/`` and ``gt/`` from the
        preceding gates run when reference re-freezes.  Those derived files are
        owned and replaced by ``export_task``; binding them into the earlier
        gold manifest makes that required replacement self-invalidating.
        """
        with tempfile.TemporaryDirectory() as tmp:
            answer_key = Path(tmp) / "answer_key"
            evaluation = answer_key / "evaluation" / "sql" / "stale_mart.sql"
            gt = answer_key / "gt" / "stale_mart.csv"
            evaluation.parent.mkdir(parents=True)
            gt.parent.mkdir(parents=True)
            evaluation.write_text("SELECT 1;\n", encoding="utf-8")
            gt.write_text("value\n1\n", encoding="utf-8")

            frozen = self._freeze(answer_key)

            self.assertNotIn("evaluation/sql/stale_mart.sql", frozen.file_hashes)
            self.assertNotIn("gt/stale_mart.csv", frozen.file_hashes)
            # Simulate export_task's atomic replacement of its two owned trees.
            evaluation.unlink()
            gt.unlink()
            self.assertEqual(load_gold(answer_key), frozen)


class _StubEngine:
    """The three-attribute surface run_generate/run_reference_stage read."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.artifacts: list[tuple[str, str]] = []

    def task_dir(self, task_id: str) -> Path:
        return self.workspace / "tasks" / task_id

    def record_artifact(self, task, rel: str, sha: str) -> None:
        self.artifacts.append((rel, sha))


class TestReferenceStageBindsDeterminism(unittest.TestCase):
    """The reddit drive failed the determinism gate because the stage wrote
    the record bound by task_id ALONE; the gate (correctly) demands the
    content-hash binding too. Pin the writer, and pin that the resume
    shortcut REBUILDS an unbound record instead of resuming into a gate
    that fails closed."""

    def test_writer_binds_both_and_resume_rebuilds_unbound_record(self) -> None:
        from elt_taskgen.cli import run_generate, run_reference_stage
        from elt_taskgen.verification.gates import DETERMINISM_CONTENT_HASH_KEY

        with tempfile.TemporaryDirectory() as tmp:
            engine = _StubEngine(Path(tmp))
            task = demo_task()
            self.assertEqual(run_generate(engine, task).verdict, "pass")
            self.assertEqual(run_reference_stage(engine, task).verdict, "pass")

            det_path = engine.task_dir(task.task_id) / "reports" / "determinism.json"
            det = GateResult.model_validate_json(det_path.read_text(encoding="utf-8"))
            self.assertEqual(det.evidence.get("task_id"), task.task_id)
            self.assertEqual(
                det.evidence.get(DETERMINISM_CONTENT_HASH_KEY), task.content_hash()
            )

            # Strip the hash binding (the record every pre-fix workspace has):
            # resuming must REBUILD it, not carry it into the gate.
            unbound = det.model_copy(
                update={
                    "evidence": {
                        k: v
                        for k, v in det.evidence.items()
                        if k != DETERMINISM_CONTENT_HASH_KEY
                    }
                }
            )
            det_path.write_text(unbound.model_dump_json(), encoding="utf-8")
            outcome = run_reference_stage(engine, task)
            self.assertEqual(outcome.verdict, "pass")
            self.assertNotIn("already frozen", outcome.payload.detail)
            rebuilt = GateResult.model_validate_json(
                det_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                rebuilt.evidence.get(DETERMINISM_CONTENT_HASH_KEY),
                task.content_hash(),
            )

            # Bound and frozen: the resume shortcut now applies.
            resumed = run_reference_stage(engine, task)
            self.assertEqual(resumed.verdict, "pass")
            self.assertIn("already frozen", resumed.payload.detail)


class TestExecuteMartStreaming(unittest.TestCase):
    """The streamed row/byte budgets in execute_mart match legacy semantics."""

    def setUp(self) -> None:
        self.mart = demo_task().mart(MART_NAME)
        self.con = duckdb.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.execute(
            "CREATE TABLE summary AS "
            "SELECT range AS customer_id, range % 7 AS completed_order_count, "
            "'spend-' || range AS total_spend FROM range(50)"
        )
        self.sql = (
            "SELECT customer_id, completed_order_count, total_spend "
            "FROM summary ORDER BY customer_id"
        )

    def test_execute_mart_streaming_matches_legacy_fetchall(self) -> None:
        legacy = execute_mart(self.con, self.mart, self.sql)
        self.assertEqual(len(legacy), 50)
        streamed = execute_mart(
            self.con, self.mart, self.sql, max_rows=1_000, max_bytes=1 << 20
        )
        self.assertEqual(streamed, legacy)
        # Boundary semantics are exactly the legacy post-hoc comparison
        # (`total > max_bytes`): the precise budget passes, one byte under
        # it fails during the fetch.
        exact = sum(_row_size(row) for row in legacy)
        self.assertEqual(
            execute_mart(self.con, self.mart, self.sql, max_bytes=exact), legacy
        )
        with self.assertRaises(MartOutputLimitError):
            execute_mart(self.con, self.mart, self.sql, max_bytes=exact - 1)
        with self.assertRaisesRegex(ValueError, "more than the allowed 49 rows"):
            execute_mart(self.con, self.mart, self.sql, max_rows=49)


if __name__ == "__main__":
    unittest.main()

"""verification/attacks.py tests — the demo attack matrix is the acceptance bar.

The reward module (verification/upstream_eval.py) and the reference pipeline
are built by other builders and are stubs at the time these tests run, so this
file provides CONTRACT-CONFORMING fixtures for both:

  * `_fake_evaluate` ports the documented upstream semantics (stage-1 exact
    counts gate stage-2; per-mart total-order sort; numeric coercion with
    rel_tol=1e-2 / abs_tol=1e-9; NaN-vs-value rejected; reward = matched/total
    marts) and is injected as `upstream_eval.evaluate` for the duration of the
    tests — attacks.py itself only ever calls that single entry point.
  * Population data for the demo fixture is generated deterministically here
    (development / primary / resampled / stress per the spec conditions;
    counterfactual verbatim from demo_fixture.COUNTERFACTUAL_LITERAL_ROWS) and
    written in the canonical populations/<pop>/rows/*.jsonl layout.

The core assertion: the demo attack matrix reproduces EXACTLY — inner join
dies on primary/resampled/counterfactual but survives development/stress,
hardcoded-primary survives only primary, COUNT-without-DISTINCT dies on the
counterfactual, no-COALESCE dies wherever customers lack completed orders,
and the correct solution earns 1.0 everywhere.
"""

from __future__ import annotations

import csv
import io
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

import duckdb

from elt_taskgen import demo_fixture
from elt_taskgen.demo_fixture import (
    COUNTERFACTUAL_EXPECTED_MART,
    COUNTERFACTUAL_LITERAL_ROWS,
    DEMO_TASK_ID,
    MART_NAME,
    REFERENCE_SQL,
)
from elt_taskgen.models import (
    AttackCase,
    AttackKind,
    CouncilRole,
    Finding,
    PopulationName,
    Severity,
    TaskVariant,
    derive_seed,
)
from elt_taskgen.verification import attacks, upstream_eval

REL_TOL = 1e-2
ABS_TOL = 1e-9


# ---------------------------------------------------------------------------
# Contract-conforming reward stand-in (injected as upstream_eval.evaluate)
# ---------------------------------------------------------------------------

def _canon(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _rows_to_csv(rows, columns) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_canon(row.get(c)) for c in columns])
    return buf.getvalue()


def _cell_match(gold_value: str, actual_value) -> bool:
    a_missing = actual_value is None
    g_missing = gold_value == ""
    if a_missing or g_missing:
        return a_missing and g_missing  # NaN-vs-value rejected
    a_num = (
        float(actual_value)
        if isinstance(actual_value, (int, float)) and not isinstance(actual_value, bool)
        else None
    )
    try:
        g_num = float(gold_value)
    except ValueError:
        g_num = None
    if a_num is not None and g_num is not None:
        return abs(a_num - g_num) <= ABS_TOL + REL_TOL * abs(g_num)
    return _canon(actual_value) == gold_value


class _RewardResult:
    def __init__(self, reward: float):
        self.reward = reward


def _fake_evaluate(task, gold, population, actual_stage1, actual_marts):
    pop = population.value
    expected_counts = gold.stage1[pop]
    stage1_pass = all(
        table in actual_stage1 and actual_stage1[table] == count
        for table, count in expected_counts.items()
    )
    if not stage1_pass:
        return _RewardResult(0.0)
    matched = 0
    for mart in task.marts:
        columns = tuple(c.name for c in mart.columns)
        sort_cols = mart.key_columns + tuple(
            c for c in columns if c not in mart.key_columns
        )
        gold_rows = list(csv.DictReader(io.StringIO(gold.stage2_csv[pop][mart.name])))
        actual_rows = actual_marts.get(mart.name, [])
        if len(gold_rows) != len(actual_rows):
            continue
        gold_sorted = sorted(
            gold_rows, key=lambda r: tuple(r.get(c, "") for c in sort_cols)
        )
        actual_sorted = sorted(
            actual_rows, key=lambda r: tuple(_canon(r.get(c)) for c in sort_cols)
        )
        ok = all(
            _cell_match(g.get(c, ""), a.get(c))
            for g, a in zip(gold_sorted, actual_sorted)
            for c in columns
        )
        if ok:
            matched += 1
    return _RewardResult(matched / len(task.marts))


# ---------------------------------------------------------------------------
# Deterministic demo population data (per the spec's population conditions)
# ---------------------------------------------------------------------------

def _dev_rows():
    return {
        "customers": [
            {"customer_id": 1, "customer_name": "C1"},
            {"customer_id": 2, "customer_name": "C2"},
        ],
        "orders": [
            {"order_id": 101, "customer_id": 1, "status": "completed"},
            {"order_id": 102, "customer_id": 2, "status": "completed"},
            {"order_id": 103, "customer_id": 1, "status": "completed"},
            {"order_id": 104, "customer_id": 2, "status": "cancelled"},
        ],
        "order_items": [
            {"order_id": 101, "quantity": 1, "unit_price": 10.0},
            {"order_id": 101, "quantity": 2, "unit_price": 5.0},
            {"order_id": 102, "quantity": 1, "unit_price": 20.0},
            {"order_id": 103, "quantity": 3, "unit_price": 3.0},
            {"order_id": 104, "quantity": 2, "unit_price": 7.5},
        ],
    }


def _standard_rows(pop_value: str, id_base: int):
    """primary/resampled: no-order customers, cancelled-only customers,
    NULL customer_id orders, multi-item completed orders."""
    rng = random.Random(derive_seed(DEMO_TASK_ID, pop_value, "testgen"))
    prices = [5.0, 7.5, 10.0, 12.25, 15.0]
    customers, orders, items = [], [], []
    oid = id_base + 100000
    for i in range(1, 101):
        cid = id_base + i
        customers.append({"customer_id": cid, "customer_name": f"Cust{cid}"})
        if i <= 70:  # at least one completed order each
            for k in range(rng.randint(1, 3)):
                oid += 1
                status = "completed" if k == 0 else rng.choice(["completed", "cancelled"])
                orders.append({"order_id": oid, "customer_id": cid, "status": status})
                for _ in range(rng.randint(1, 4)):
                    items.append(
                        {"order_id": oid, "quantity": rng.randint(1, 5),
                         "unit_price": rng.choice(prices)}
                    )
        elif i <= 85:
            pass  # no orders at all
        else:  # orders, but never a completed one
            for _ in range(rng.randint(1, 2)):
                oid += 1
                orders.append({"order_id": oid, "customer_id": cid, "status": "cancelled"})
                items.append(
                    {"order_id": oid, "quantity": rng.randint(1, 5),
                     "unit_price": rng.choice(prices)}
                )
    for j in range(5):  # orders with NULL customer_id
        oid += 1
        orders.append(
            {"order_id": oid, "customer_id": None,
             "status": "completed" if j % 2 == 0 else "cancelled"}
        )
        items.append({"order_id": oid, "quantity": 1, "unit_price": 10.0})
    return {"customers": customers, "orders": orders, "order_items": items}


def _stress_rows():
    """Skewed customer, exact-duplicate order headers, ties; every customer
    has a completed order with items (INNER JOIN indistinguishable here)."""
    customers = [
        {"customer_id": 500 + i, "customer_name": f"S{i}"} for i in range(1, 21)
    ]
    orders, items = [], []
    oid = 900000
    for _ in range(50):  # skew: customer 501 holds many orders
        oid += 1
        orders.append({"order_id": oid, "customer_id": 501, "status": "completed"})
        items.append({"order_id": oid, "quantity": 2, "unit_price": 4.0})
    for cid in (502, 503):  # ties: identical totals
        oid += 1
        orders.append({"order_id": oid, "customer_id": cid, "status": "completed"})
        items.append({"order_id": oid, "quantity": 2, "unit_price": 10.0})
    for cid in range(504, 521):
        oid += 1
        orders.append({"order_id": oid, "customer_id": cid, "status": "completed"})
        items.append({"order_id": oid, "quantity": 1, "unit_price": 6.0})
        items.append({"order_id": oid, "quantity": 1, "unit_price": 1.5})
        oid += 1
        orders.append({"order_id": oid, "customer_id": cid, "status": "cancelled"})
        items.append({"order_id": oid, "quantity": 3, "unit_price": 2.0})
    orders.extend(dict(o) for o in orders[:10])  # exact duplicate header rows
    return {"customers": customers, "orders": orders, "order_items": items}


def _population_rows() -> dict[PopulationName, dict[str, list]]:
    return {
        PopulationName.DEVELOPMENT: _dev_rows(),
        PopulationName.PRIMARY: _standard_rows("primary", 0),
        PopulationName.RESAMPLED: _standard_rows("resampled", 10000),
        PopulationName.COUNTERFACTUAL: {
            t: [dict(r) for r in rows] for t, rows in COUNTERFACTUAL_LITERAL_ROWS.items()
        },
        PopulationName.STRESS: _stress_rows(),
    }


class _Gold:
    """GoldBundle-shaped stand-in per docs/INTERFACES.md (reference/gold.py)."""

    def __init__(self, task_id, task_content_hash, stage1, stage2_csv):
        self.task_id = task_id
        self.task_content_hash = task_content_hash
        self.stage1 = stage1        # population -> table -> count
        self.stage2_csv = stage2_csv  # population -> mart -> canonical CSV text
        self.file_hashes = {}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class AttackDemoMatrixTest(unittest.TestCase):
    """End-to-end: build demo populations + gold, run every demo attack."""

    @classmethod
    def setUpClass(cls):
        cls._orig_evaluate = getattr(upstream_eval, "evaluate", None)
        upstream_eval.evaluate = _fake_evaluate

        cls.task = demo_fixture.demo_task()
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="attacks_test_"))
        cls.workspace = cls.tmpdir / "workspace"
        cls.pop_rows = _population_rows()

        for pop, tables in cls.pop_rows.items():
            rows_dir = (
                cls.workspace / "tasks" / cls.task.task_id
                / "populations" / pop.value / "rows"
            )
            rows_dir.mkdir(parents=True)
            for table, rows in tables.items():
                (rows_dir / f"{table}.jsonl").write_text(
                    "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
                )

        # Gold: execute the trusted reference SQL through the same loaders.
        mart = cls.task.marts[0]
        columns = tuple(c.name for c in mart.columns)
        stage1, stage2 = {}, {}
        cls.reference_rows = {}
        for pop in PopulationName:
            con = duckdb.connect(":memory:")
            try:
                counts = attacks._load_population_sources(
                    cls.task, pop, cls.workspace, con, frozenset()
                )
                rows = attacks._fetch_rows(con, REFERENCE_SQL)
            finally:
                con.close()
            cls.reference_rows[pop] = rows
            stage1[pop.value] = counts
            stage2[pop.value] = {mart.name: _rows_to_csv(rows, columns)}
        cls.gold = _Gold(cls.task.task_id, cls.task.content_hash(), stage1, stage2)

    @classmethod
    def tearDownClass(cls):
        if cls._orig_evaluate is None:
            del upstream_eval.evaluate
        else:
            upstream_eval.evaluate = cls._orig_evaluate
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # -- sanity of the fixture data itself ---------------------------------

    def test_reference_counterfactual_matches_spec(self):
        rows = self.reference_rows[PopulationName.COUNTERFACTUAL]
        self.assertEqual(len(rows), len(COUNTERFACTUAL_EXPECTED_MART))
        for actual, expected in zip(
            sorted(rows, key=lambda r: r["customer_id"]), COUNTERFACTUAL_EXPECTED_MART
        ):
            self.assertEqual(actual["customer_id"], expected["customer_id"])
            self.assertEqual(
                int(actual["completed_order_count"]), expected["completed_order_count"]
            )
            self.assertAlmostEqual(
                float(actual["total_spend"]), float(expected["total_spend"])
            )

    def test_primary_population_exercises_conditions(self):
        rows = self.pop_rows[PopulationName.PRIMARY]
        customers_with_orders = {
            o["customer_id"] for o in rows["orders"] if o["customer_id"] is not None
        }
        all_customers = {c["customer_id"] for c in rows["customers"]}
        self.assertTrue(all_customers - customers_with_orders)  # no-order customers
        completed = {
            o["customer_id"] for o in rows["orders"]
            if o["status"] == "completed" and o["customer_id"] is not None
        }
        self.assertTrue(customers_with_orders - completed)  # cancelled-only customers
        self.assertTrue(any(o["customer_id"] is None for o in rows["orders"]))

    # -- THE demo attack matrix --------------------------------------------

    def test_demo_attack_matrix_reproduced_exactly(self):
        for case in self.task.attack_cases:
            rewards = attacks.run_attack(self.task, case, self.gold, self.workspace)
            if not rewards:
                # A load probe with no surface on THIS fixture's data (e.g.
                # truncate_table: no table spans a read unit at ~100 rows).
                # Legal only for a non-required probe asserting nothing, and
                # only with the reason recorded on disk at the current hash.
                self.assertFalse(case.required, case.name)
                self.assertEqual(case.expected_pass, {}, case.name)
                record = json.loads(
                    (
                        self.workspace / "tasks" / self.task.task_id
                        / "attacks" / case.name / "inapplicable.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(record["case"], case.name)
                self.assertEqual(
                    record["task_content_hash"], self.task.content_hash()
                )
                self.assertTrue(record["inapplicable"])
                continue
            self.assertEqual(set(rewards), set(PopulationName))
            for pop, expected_full in case.expected_pass.items():
                got_full = rewards[pop] == 1.0
                self.assertEqual(
                    got_full,
                    expected_full,
                    msg=(
                        f"attack {case.name!r} on {pop.value}: reward "
                        f"{rewards[pop]} (expected "
                        f"{'FULL' if expected_full else 'LOST'})"
                    ),
                )

    def test_run_attack_records_a_sql_crash_and_the_t_gate_rejects_it(self):
        """A REQUIRED mutant that only CRASHES scores 0.0 everywhere — and that
        0.0 is not evidence: run_attack records the crash under `errors` and
        the per-variant required-mutants gate refuses the kill."""
        from elt_taskgen.verification import gates

        crash = AttackCase(
            name="crash_probe",
            kind=AttackKind.CUSTOM,
            description="a mutant DuckDB refuses to run",
            mutation="SELECT no_such_column FROM no_such_table",
            expected_pass={p: False for p in PopulationName},
            required=True,
        )
        task = self.task.model_copy(update={"attack_cases": self.task.attack_cases + (crash,)})
        gold = _Gold(task.task_id, task.content_hash(), self.gold.stage1, self.gold.stage2_csv)
        rewards = attacks.run_attack(task, crash, gold, self.workspace)
        self.assertEqual(set(rewards.values()), {0.0})
        record = json.loads(
            (
                self.workspace / "tasks" / task.task_id / "attacks" / "crash_probe"
                / "rewards.json"
            ).read_text(encoding="utf-8")
        )
        mart = task.marts[0].name
        for pop in PopulationName:
            self.assertIn(f"{pop.value}/{mart}", record["errors"])
        transform = record["rewards_by_variant"][TaskVariant.TRANSFORM.value]
        self.assertEqual(set(transform.values()), {0.0})
        measured = {"crash_probe": {PopulationName(p): r for p, r in transform.items()}}
        gate = gates._gate_variant_required_mutants(
            task, measured, TaskVariant.TRANSFORM, workspace=self.workspace
        )
        self.assertFalse(gate.passed)
        self.assertIn("SQL CRASH", gate.details)
        self.assertIn("crash_probe", gate.details)
        # the numeric-only parent gate (the $0 precheck's subset) still passes:
        # crash visibility is the per-variant battery's, by design
        parent = gates._gate_required_mutants(
            task.model_copy(update={"attack_cases": (crash,)}),
            {"crash_probe": measured["crash_probe"]},
        )
        self.assertTrue(parent.passed, parent.details)

    def test_correct_solution_full_reward_on_all_populations(self):
        identity = AttackCase(
            name="identity_probe",
            kind=AttackKind.CUSTOM,
            description="The trusted reference itself must score 1.0 everywhere.",
            mutation=REFERENCE_SQL,
            expected_pass={},
            required=False,
        )
        rewards = attacks.run_attack(self.task, identity, self.gold, self.workspace)
        for pop in PopulationName:
            self.assertEqual(rewards[pop], 1.0, msg=f"reference lost reward on {pop.value}")

    def test_run_attack_is_deterministic(self):
        case = self.task.attack_cases[0]
        first = attacks.run_attack(self.task, case, self.gold, self.workspace)
        second = attacks.run_attack(self.task, case, self.gold, self.workspace)
        self.assertEqual(first, second)

    def test_attack_record_written_without_wall_clock(self):
        case = self.task.attack_cases[0]
        attacks.run_attack(self.task, case, self.gold, self.workspace)
        attack_dir = (
            self.workspace / "tasks" / self.task.task_id / "attacks" / case.name
        )
        record = json.loads((attack_dir / "rewards.json").read_text(encoding="utf-8"))
        self.assertEqual(record["case"], case.name)
        self.assertEqual(set(record["rewards"]), {p.value for p in PopulationName})
        self.assertEqual(record["task_content_hash"], self.task.content_hash())
        self.assertNotIn("time", json.dumps(record).lower())
        self.assertTrue((attack_dir / f"mutation_{MART_NAME}.sql").is_file())

    # -- execution bounds (roadmap Phase 0.A) --------------------------------

    def test_attacks_run_attack_uses_sandboxed_connection(self):
        """A mutant is UNTRUSTED SQL: it runs on a sandboxed connection under
        the semantic scorer's envelope, with row/byte caps on its output."""
        import inspect
        from unittest import mock

        from elt_taskgen.reference import duckdb_sandbox
        from elt_taskgen.semantic.models import SemanticLimits

        # The bounds are the SoT anchors (SemanticLimits defaults), pinned.
        limits = SemanticLimits()
        self.assertEqual(attacks.ATTACK_MEMORY_LIMIT_MB, limits.memory_limit_mb)
        self.assertEqual(attacks.ATTACK_THREADS, limits.threads)
        self.assertEqual(
            attacks.ATTACK_MAX_RESULT_ROWS_PER_MART, limits.max_result_rows_per_mart
        )
        self.assertEqual(
            attacks.ATTACK_MAX_RESULT_BYTES_PER_MART, limits.max_result_bytes_per_mart
        )
        # run_attack opens NO plain duckdb connection any more.
        source = inspect.getsource(attacks.run_attack)
        self.assertNotIn("duckdb.connect(", source)
        self.assertIn("sandboxed_memory_connection(", source)

        # Every population's connection comes from the factory with the four
        # kwargs, and the connection it hands back is locked.
        seen: list[dict] = []
        real = duckdb_sandbox.sandboxed_memory_connection

        def recording(**kwargs):
            seen.append(dict(kwargs))
            con = real(**kwargs)
            duckdb_sandbox.assert_sandboxed(con)
            return con

        identity = AttackCase(
            name="bounds_identity_probe",
            kind=AttackKind.CUSTOM,
            description="reference SQL under the sandboxed connection",
            mutation=REFERENCE_SQL,
            expected_pass={},
            required=False,
        )
        with mock.patch.object(attacks, "sandboxed_memory_connection", recording):
            rewards = attacks.run_attack(self.task, identity, self.gold, self.workspace)
        self.assertEqual(len(seen), len(PopulationName))
        for kwargs in seen:
            self.assertEqual(
                kwargs,
                {
                    "memory_limit_mb": 512,
                    "threads": 1,
                    "disable_temp_spill": True,
                    "deterministic_settings": True,
                },
            )
        # deterministic_settings changes NO measured reward.
        self.assertEqual({rewards[p] for p in PopulationName}, {1.0})

        # A mutant that reaches for the host file system is refused by the
        # sandbox and recorded under the STABLE `external_access` code (never
        # the path it named, never DuckDB's text), never scored as a kill.
        leak = AttackCase(
            name="bounds_leak_probe",
            kind=AttackKind.CUSTOM,
            description="a mutant that reads the host",
            mutation=f"SELECT * FROM read_csv_auto('{self.workspace / 'x.csv'}')",
            expected_pass={p: False for p in PopulationName},
            required=False,
        )
        rewards = attacks.run_attack(self.task, leak, self.gold, self.workspace)
        self.assertEqual(set(rewards.values()), {0.0})
        record = json.loads(
            (
                self.workspace / "tasks" / self.task.task_id / "attacks"
                / "bounds_leak_probe" / "rewards.json"
            ).read_text(encoding="utf-8")
        )
        for pop in PopulationName:
            error = record["errors"][f"{pop.value}/{MART_NAME}"]
            self.assertEqual(error, attacks.ATTACK_EXTERNAL_ACCESS_CODE)
        self.assertNotIn(str(self.workspace), json.dumps(record["errors"]))

        # _fetch_rows caps rows and bytes mid-fetch (streamed, never fetchall).
        con = duckdb.connect(":memory:")
        try:
            self.assertEqual(
                len(attacks._fetch_rows(con, "SELECT * FROM range(5)", max_rows=5)), 5
            )
            with self.assertRaises(attacks.AttackOutputLimitError):
                attacks._fetch_rows(con, "SELECT * FROM range(6)", max_rows=5)
            with self.assertRaises(attacks.AttackOutputLimitError):
                attacks._fetch_rows(
                    con, "SELECT repeat('x', 100) AS s FROM range(3)", max_bytes=250
                )
            self.assertEqual(
                len(
                    attacks._fetch_rows(
                        con, "SELECT repeat('x', 100) AS s FROM range(3)", max_bytes=300
                    )
                ),
                3,
            )
            with self.assertRaises(ValueError):
                attacks._fetch_rows(con, "SELECT 1", max_rows=0)
        finally:
            con.close()
        # The caps are wired into run_attack's fetch, and the limit error is
        # caught beside duckdb.Error.
        self.assertIn("max_rows=ATTACK_MAX_RESULT_ROWS_PER_MART", source)
        self.assertIn("max_bytes=ATTACK_MAX_RESULT_BYTES_PER_MART", source)
        self.assertIn("AttackOutputLimitError", source)

    def test_mutant_exceeding_output_caps_is_recorded_as_stable_code(self):
        """A mutant whose output blows the row/byte cap is recorded under the
        STABLE code `output_limit` for every population (never the message
        with its numbers, never DuckDB text): the harness cap, not the data,
        stopped it, and the record says so beside the 0.0."""
        from unittest import mock

        flood = AttackCase(
            name="bounds_flood_probe",
            kind=AttackKind.CUSTOM,
            description="a mutant that floods the mart",
            mutation=(
                "SELECT r.customer_id, r.completed_order_count, r.total_spend "
                f"FROM ({REFERENCE_SQL}) r CROSS JOIN range(200000)"
            ),
            expected_pass={p: False for p in PopulationName},
            required=False,
        )
        with mock.patch.object(attacks, "ATTACK_MAX_RESULT_ROWS_PER_MART", 10):
            rewards = attacks.run_attack(self.task, flood, self.gold, self.workspace)
        self.assertEqual(set(rewards), set(PopulationName))
        record = json.loads(
            (
                self.workspace / "tasks" / self.task.task_id / "attacks"
                / "bounds_flood_probe" / "rewards.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(attacks.ATTACK_OUTPUT_LIMIT_CODE, "output_limit")
        for pop in PopulationName:
            self.assertEqual(
                record["errors"][f"{pop.value}/{MART_NAME}"], attacks.ATTACK_OUTPUT_LIMIT_CODE
            )
        errors_text = json.dumps(record["errors"])
        self.assertNotIn("10", errors_text)
        self.assertNotIn("allowed", errors_text)

    def test_bounded_mutant_is_an_error_never_a_kill(self):
        """Roadmap 0.A regression fixture: a REQUIRED mutant the sandbox
        stopped — its output blew the row/byte cap, or it reached for the
        file system the sandbox closed (`enable_external_access` off) — is
        recorded under a STABLE code (`output_limit`, `external_access`:
        never the cap's numbers, never the path or DuckDB's text) beside the
        0.0, and that 0.0 is NEVER counted as a kill: the per-variant
        required-mutants gate's crash check refuses it exactly as it refuses
        a SQL crash, because the harness cap, not the data, stopped the
        mutant."""
        from unittest import mock

        from elt_taskgen.verification import gates

        gold_rows = (
            self.workspace / "tasks" / self.task.task_id / "populations"
            / PopulationName.PRIMARY.value / "rows" / "customers.jsonl"
        )
        self.assertTrue(gold_rows.is_file())
        flood = AttackCase(
            name="bounds_flood_required",
            kind=AttackKind.CUSTOM,
            description="a mutant that floods the mart past the row cap",
            mutation=(
                "SELECT r.customer_id, r.completed_order_count, r.total_spend "
                f"FROM ({REFERENCE_SQL}) r CROSS JOIN range(200000)"
            ),
            expected_pass={p: False for p in PopulationName},
            required=True,
        )
        exfil = AttackCase(
            name="bounds_exfil_required",
            kind=AttackKind.CUSTOM,
            description="a mutant that reads the population files off the host",
            mutation=(
                "SELECT customer_id, 1 AS completed_order_count, 0.0 AS total_spend "
                f"FROM read_json_auto('{gold_rows.as_posix()}')"
            ),
            expected_pass={p: False for p in PopulationName},
            required=True,
        )
        task = self.task.model_copy(
            update={"attack_cases": self.task.attack_cases + (flood, exfil)}
        )
        gold = _Gold(task.task_id, task.content_hash(), self.gold.stage1, self.gold.stage2_csv)
        with mock.patch.object(attacks, "ATTACK_MAX_RESULT_ROWS_PER_MART", 10):
            flood_rewards = attacks.run_attack(task, flood, gold, self.workspace)
        exfil_rewards = attacks.run_attack(task, exfil, gold, self.workspace)
        self.assertEqual(attacks.ATTACK_OUTPUT_LIMIT_CODE, "output_limit")
        self.assertEqual(attacks.ATTACK_EXTERNAL_ACCESS_CODE, "external_access")
        measured = {}
        for case, rewards, code in (
            (flood, flood_rewards, attacks.ATTACK_OUTPUT_LIMIT_CODE),
            (exfil, exfil_rewards, attacks.ATTACK_EXTERNAL_ACCESS_CODE),
        ):
            with self.subTest(case=case.name):
                # Numerically a 0.0 everywhere — the mutant produced no rows.
                self.assertEqual(set(rewards.values()), {0.0})
                record = json.loads(
                    (
                        self.workspace / "tasks" / task.task_id / "attacks"
                        / case.name / "rewards.json"
                    ).read_text(encoding="utf-8")
                )
                for pop in PopulationName:
                    self.assertEqual(record["errors"][f"{pop.value}/{MART_NAME}"], code)
                errors_text = json.dumps(record["errors"])
                self.assertNotIn(str(self.workspace), errors_text)
                self.assertNotIn("Permission", errors_text)
                self.assertNotIn("disabled", errors_text)
                self.assertNotIn("allowed", errors_text)
                transform = record["rewards_by_variant"][TaskVariant.TRANSFORM.value]
                self.assertEqual(set(transform.values()), {0.0})
                measured[case.name] = {PopulationName(p): r for p, r in transform.items()}
        # Never a kill: the gate reads the record and refuses both.
        gate = gates._gate_variant_required_mutants(
            task, measured, TaskVariant.TRANSFORM, workspace=self.workspace
        )
        self.assertFalse(gate.passed)
        for case, code in (
            ("bounds_flood_required", attacks.ATTACK_OUTPUT_LIMIT_CODE),
            ("bounds_exfil_required", attacks.ATTACK_EXTERNAL_ACCESS_CODE),
        ):
            for pop in PopulationName:
                self.assertIn(
                    f"{case}: kill on {pop.value} is by SQL CRASH on {MART_NAME} ({code})",
                    gate.details,
                )
            self.assertIn(code, gate.evidence[f"crash_check:{case}"])
        self.assertNotIn(str(self.workspace), gate.details)
        self.assertNotIn(str(self.workspace), json.dumps(gate.evidence))

    # -- materialization ----------------------------------------------------

    def test_hardcode_directive_emits_primary_gold_literals(self):
        case = next(
            c for c in self.task.attack_cases if c.name == "hardcoded_primary_outputs"
        )
        sql_by_mart = attacks.materialize_mutation(self.task, case, self.gold)
        con = duckdb.connect(":memory:")  # no source tables at all
        try:
            rows = attacks._fetch_rows(con, sql_by_mart[MART_NAME])
        finally:
            con.close()
        expected = self.reference_rows[PopulationName.PRIMARY]
        self.assertEqual(len(rows), len(expected))
        key = lambda r: r["customer_id"]
        for got, want in zip(sorted(rows, key=key), sorted(expected, key=key)):
            self.assertEqual(got["customer_id"], want["customer_id"])
            self.assertAlmostEqual(float(got["total_spend"]), float(want["total_spend"]))

    def test_kind_transform_inner_join(self):
        case = AttackCase(
            name="kt_inner",
            kind=AttackKind.INNER_JOIN,
            description="kind-derived join flip",
            mutation="",
            expected_pass={PopulationName.PRIMARY: False},
        )
        sql = attacks.materialize_mutation(self.task, case, self.gold)[MART_NAME]
        upper = sql.upper()
        self.assertIn("JOIN", upper)
        self.assertNotIn("LEFT JOIN", upper)

    def test_kind_transform_no_dedup_strips_distinct(self):
        case = AttackCase(
            name="kt_nodedup",
            kind=AttackKind.NO_DEDUP,
            description="drop DISTINCT everywhere",
            mutation=f"{attacks.KIND_DIRECTIVE_PREFIX}no_dedup",
            expected_pass={PopulationName.COUNTERFACTUAL: False},
        )
        sql = attacks.materialize_mutation(self.task, case, self.gold)[MART_NAME]
        self.assertNotIn("DISTINCT", sql.upper())

    def test_kind_transform_no_null_default_strips_coalesce(self):
        case = AttackCase(
            name="kt_nocoalesce",
            kind=AttackKind.NO_NULL_DEFAULT,
            description="drop COALESCE",
            mutation="",
            expected_pass={PopulationName.PRIMARY: False},
        )
        sql = attacks.materialize_mutation(self.task, case, self.gold)[MART_NAME]
        self.assertNotIn("COALESCE", sql.upper())

    def test_inert_mutation_fails_closed(self):
        case = AttackCase(
            name="kt_window",
            kind=AttackKind.WRONG_WINDOW,
            description="no window function exists in the reference SQL",
            mutation="",
            expected_pass={PopulationName.PRIMARY: False},
        )
        with self.assertRaises(ValueError):
            attacks.materialize_mutation(self.task, case, self.gold)

    def test_missing_gold_population_fails_closed(self):
        bad_gold = _Gold(self.task.task_id, self.task.content_hash(), {}, {})
        case = next(
            c for c in self.task.attack_cases if c.name == "hardcoded_primary_outputs"
        )
        with self.assertRaises(ValueError):
            attacks.materialize_mutation(self.task, case, bad_gold)

    # -- fail-closed execution paths ----------------------------------------

    def test_missing_rows_fail_closed(self):
        empty_ws = self.tmpdir / "empty_ws"
        case = self.task.attack_cases[0]
        with self.assertRaises(FileNotFoundError):
            attacks.run_attack(self.task, case, self.gold, empty_ws)

    def test_missing_upstream_eval_fails_closed(self):
        saved = upstream_eval.evaluate
        del upstream_eval.evaluate
        try:
            with self.assertRaises(RuntimeError):
                attacks.run_attack(
                    self.task, self.task.attack_cases[0], self.gold, self.workspace
                )
        finally:
            upstream_eval.evaluate = saved


class CompileAttacksTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()

    def test_catalogue_preserved_and_customs_appended(self):
        findings = [
            Finding(
                finding_id="F002",
                role=CouncilRole.SHORTCUT_ATTACKER,
                severity=Severity.MAJOR,
                summary="A dropped filter would go unnoticed",
                suggested_attack=AttackKind.DROPPED_FILTER,
            ),
            Finding(
                finding_id="F001",
                role=CouncilRole.POPULATION_ADVERSARY,
                severity=Severity.MINOR,
                summary="Join flip risk",
                suggested_attack=AttackKind.INNER_JOIN,
            ),
            Finding(
                finding_id="F003",
                role=CouncilRole.AMBIGUITY_CRITIC,
                severity=Severity.INFO,
                summary="informational only",
                suggested_attack=AttackKind.CONSTANTS,
            ),
            Finding(
                finding_id="F004",
                role=CouncilRole.FEASIBILITY_REVIEWER,
                severity=Severity.MAJOR,
                summary="not executable",
            ),
        ]
        cases = attacks.compile_attacks(self.task, findings)
        names = [c.name for c in cases]
        self.assertEqual(names[: len(self.task.attack_cases)],
                         [c.name for c in self.task.attack_cases])
        self.assertIn("finding__F001", names)
        self.assertIn("finding__F002", names)
        self.assertNotIn("finding__F003", names)  # INFO severity skipped
        self.assertNotIn("finding__F004", names)  # no suggested attack
        # deterministic ordering by finding_id after the catalogue
        self.assertLess(names.index("finding__F001"), names.index("finding__F002"))
        custom = next(c for c in cases if c.name == "finding__F001")
        self.assertFalse(custom.required)
        self.assertEqual(custom.source_finding, "F001")
        self.assertEqual(
            custom.mutation, f"{attacks.KIND_DIRECTIVE_PREFIX}inner_join"
        )

    def test_no_findings_returns_catalogue(self):
        cases = attacks.compile_attacks(self.task, [])
        self.assertEqual(cases, self.task.attack_cases)

    def test_load_kind_findings_compile_to_a_LOAD_directive(self):
        """A critic may suggest a LOAD mutation; those have no AST rule.

        Compiling every suggestion to `directive:kind:` handed load mutants to
        the sqlglot mutator, which raised "attack kind 'null_row_drop' has no
        AST mutation rule" and took the whole attack stage down — measured on
        four tasks across three pools (dlt personio/pipedrive, synsql
        employee, wikidbs c00167).
        """
        findings = [
            Finding(
                finding_id="L001",
                role=CouncilRole.POPULATION_ADVERSARY,
                severity=Severity.MAJOR,
                summary="no population exercises an all-NULL row",
                suggested_attack=AttackKind.NULL_ROW_DROP,
            ),
            Finding(
                finding_id="L002",
                role=CouncilRole.POPULATION_ADVERSARY,
                severity=Severity.MAJOR,
                summary="one backend is never exercised",
                suggested_attack=AttackKind.PARTIAL_BACKEND,
            ),
        ]
        cases = attacks.compile_attacks(self.task, findings)
        by_name = {c.name: c for c in cases}
        for fid, kind in (("L001", "null_row_drop"), ("L002", "partial_backend")):
            case = by_name[f"finding__{fid}"]
            self.assertEqual(
                case.mutation, f"{attacks.LOAD_DIRECTIVE_PREFIX}{kind}"
            )
            # and it must be a directive the load executor actually accepts
            name, _arg = attacks.split_load_directive(
                case.mutation[len(attacks.LOAD_DIRECTIVE_PREFIX):]
            )
            self.assertEqual(name, kind)

    def test_finding_detail_text_preserved_in_compiled_case(self):
        finding = Finding(
            finding_id="F010",
            role=CouncilRole.AMBIGUITY_CRITIC,
            severity=Severity.MINOR,
            summary="Filter wording is ambiguous",
            detail="Two readings: (a) completed only, (b) all orders.",
            suggested_attack=AttackKind.DROPPED_FILTER,
        )
        cases = attacks.compile_attacks(self.task, [finding])
        case = next(c for c in cases if c.name == "finding__F010")
        self.assertIn(finding.summary, case.description)
        self.assertIn(finding.detail, case.description)

    def test_output_emission_finding_compiles_to_hardcode_directive(self):
        # A shortcut proposal to emit outputs verbatim must compile to the
        # hardcode-population-outputs directive (real frozen gold rows), NOT a
        # lossy constants AST mutation.
        finding = Finding(
            finding_id="F020",
            role=CouncilRole.SHORTCUT_ATTACKER,
            severity=Severity.MAJOR,
            summary="Hard-coded outputs must lose reward",
            detail=(
                "Compile a mutant that emits one population's outputs verbatim "
                "with no computation."
            ),
            suggested_attack=AttackKind.CONSTANTS,
        )
        cases = attacks.compile_attacks(self.task, [finding])
        case = next(c for c in cases if c.name == "finding__F020")
        # No population named in the text: defaults to the solver-visible one.
        self.assertEqual(
            case.mutation, f"{attacks.HARDCODE_DIRECTIVE_PREFIX}development"
        )
        self.assertNotIn(attacks.KIND_DIRECTIVE_PREFIX, case.mutation)

    def test_output_emission_finding_extracts_named_population(self):
        finding = Finding(
            finding_id="F021",
            role=CouncilRole.SHORTCUT_ATTACKER,
            severity=Severity.MAJOR,
            summary="Emit the primary population's outputs verbatim",
            suggested_attack=AttackKind.CONSTANTS,
        )
        cases = attacks.compile_attacks(self.task, [finding])
        case = next(c for c in cases if c.name == "finding__F021")
        self.assertEqual(
            case.mutation, f"{attacks.HARDCODE_DIRECTIVE_PREFIX}primary"
        )

    def test_plain_constants_finding_keeps_kind_directive(self):
        finding = Finding(
            finding_id="F022",
            role=CouncilRole.SHORTCUT_ATTACKER,
            severity=Severity.MAJOR,
            summary="Constant measures could sneak through a weak comparator",
            suggested_attack=AttackKind.CONSTANTS,
        )
        cases = attacks.compile_attacks(self.task, [finding])
        case = next(c for c in cases if c.name == "finding__F022")
        self.assertEqual(
            case.mutation, f"{attacks.KIND_DIRECTIVE_PREFIX}constants"
        )


if __name__ == "__main__":
    unittest.main()

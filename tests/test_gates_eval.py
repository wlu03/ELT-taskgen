"""Tests for verification/upstream_eval.py and verification/gates.py.

Covers, per the module contract:
  * comparator semantics ported from the pinned ELT-Bench evaluator (total
    order sort, numeric tolerance, case-insensitive columns, single-sided
    null mismatch, exact stage-1 counts),
  * the demo attack matrix reproduced end-to-end with real DuckDB executions
    of the demo mutants,
  * every gate individually red on missing/withdrawn evidence, and the full
    battery green on the demo-shaped workspace.

The GoldBundle stand-in below mirrors the reference/gold.py contract shape
from docs/INTERFACES.md (that module is built in parallel); gates.py and
upstream_eval.py are duck-typed against exactly these fields.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen import demo_fixture
from elt_taskgen.demo_fixture import (
    COUNTERFACTUAL_EXPECTED_MART,
    COUNTERFACTUAL_LITERAL_ROWS,
    HARDCODE_PRIMARY_DIRECTIVE,
    MART_NAME,
    REFERENCE_SQL,
)
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    GateResult,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    PopulationName,
    Row,
    TableSpec,
    TaskIR,
)
from elt_taskgen.reference.gold import gold_digests
from elt_taskgen.verification import gates
from elt_taskgen.verification.upstream_eval import (
    ABS_TOL,
    REL_TOL,
    RewardResult,
    _numeric_value,
    _vectors_match,
    compare_mart,
    compare_stage1,
    evaluate,
    parse_canonical_csv,
    rows_to_canonical_csv,
    sort_rows,
)

P = PopulationName

#: Gate-roster identity pin. Any intentional roster change must also bump
#: gates.SCORER_VERSION and update this value.
PINNED_ROSTER_DIGEST = "27defb98b7223236"

#: Coverage the contamination-clean gate demands: an ARMED index holding
#: type-blind shape fingerprints (the shape the real workspaces record after
#: measure-target; verification/contamination.IndexCoverage JSON).
ARMED_COVERAGE = {
    "level": "armed",
    "index_dir": "state/contamination",
    "benchmark_fingerprints": 1453,
    "benchmark_by_kind": {"family": 321, "schema": 200, "schema-table": 932},
    "shape_fingerprints": 200,
    "admitted_tasks": 0,
    "admitted_fingerprints": 0,
    "admitted_by_kind": {},
    "stores": [],
}


class GoldStub(BaseModel):
    """Contract shape of reference.gold.GoldBundle (docs/INTERFACES.md)."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    task_content_hash: str
    stage1: dict[str, dict[str, int]]
    stage2_csv: dict[str, dict[str, str]]
    file_hashes: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# DuckDB execution of demo SQL over hand-built populations
# ---------------------------------------------------------------------------

def run_demo_sql(sql: str, data: dict[str, list[tuple]]) -> list[Row]:
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE customers (customer_id INTEGER, customer_name VARCHAR)")
        con.execute("CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, status VARCHAR)")
        con.execute("CREATE TABLE order_items (order_id INTEGER, quantity INTEGER, unit_price DOUBLE)")
        for table, rows in data.items():
            if rows:
                ph = ",".join("?" for _ in rows[0])
                con.executemany(f"INSERT INTO {table} VALUES ({ph})", rows)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


#: Hand-built demo populations honoring every PopulationSpec condition that
#: the demo attack matrix depends on.
DEMO_DATA: dict[P, dict[str, list[tuple]]] = {
    P.DEVELOPMENT: {
        # every customer has a completed order (INNER JOIN indistinguishable)
        "customers": [(1, "C1"), (2, "C2")],
        "orders": [(101, 1, "completed"), (102, 2, "completed")],
        "order_items": [(101, 1, 10.0), (102, 2, 5.0)],
    },
    P.PRIMARY: {
        # customers with no orders (2), no completed orders (3), NULL
        # customer_id order (206), multi-item completed order (204)
        "customers": [(1, "P1"), (2, "P2"), (3, "P3"), (4, "P4"), (5, "P5")],
        "orders": [
            (201, 1, "completed"), (202, 1, "completed"),
            (203, 3, "cancelled"), (204, 4, "completed"),
            (205, 5, "completed"), (206, None, "completed"),
        ],
        "order_items": [
            (201, 1, 10.0), (202, 2, 5.0), (204, 1, 20.0),
            (204, 3, 1.0), (205, 2, 7.5), (206, 1, 9.0),
        ],
    },
    P.RESAMPLED: {
        # same conditions as primary, new ids/values (memorization check)
        "customers": [(11, "R1"), (12, "R2"), (13, "R3"), (14, "R4"), (15, "R5")],
        "orders": [
            (301, 11, "completed"), (302, 13, "cancelled"),
            (303, 14, "completed"), (304, 15, "completed"),
        ],
        "order_items": [
            (301, 1, 12.0), (303, 2, 3.0), (303, 1, 4.0), (304, 1, 6.0),
        ],
    },
    P.COUNTERFACTUAL: {
        table: [tuple(row.values()) for row in rows]
        for table, rows in COUNTERFACTUAL_LITERAL_ROWS.items()
    },
    P.STRESS: {
        # duplicate order header rows + ties; every customer completed
        "customers": [(21, "S1"), (22, "S2")],
        "orders": [(401, 21, "completed"), (401, 21, "completed"), (402, 22, "completed")],
        "order_items": [(401, 1, 10.0), (402, 1, 10.0)],
    },
}

MART_COLS = ("customer_id", "completed_order_count", "total_spend")


def build_demo_gold(task: TaskIR) -> GoldStub:
    stage1: dict[str, dict[str, int]] = {}
    stage2: dict[str, dict[str, str]] = {}
    for pop, data in DEMO_DATA.items():
        stage1[pop.value] = {t: len(rows) for t, rows in data.items()}
        mart_rows = run_demo_sql(REFERENCE_SQL, data)
        ordered = sort_rows(mart_rows, task.mart(MART_NAME).key_columns, MART_COLS)
        stage2[pop.value] = {MART_NAME: rows_to_canonical_csv(ordered, MART_COLS)}
    return GoldStub(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        stage1=stage1,
        stage2_csv=stage2,
    )


def battery_task() -> TaskIR:
    """The demo task re-scaled for this module's HAND-BUILT populations.

    WHY. DEMO_DATA above realizes a handful of rows per table, while the
    pristine fixture declares scaled populations (1000/3000/9000) whose
    realized counts generation/source_data.realized_row_count guarantees to
    land 2-7% off the declared scale — a guarantee the declared-scale-
    reconciliation gate asserts against the frozen stage-1 counts, and one a
    5-row stand-in can never satisfy. Below REALIZED_DIVERGENCE_MIN_SCALE the
    generator realizes counts exactly and the gate RECORDS the surface instead
    of asserting it, which is the honest description of this fixture (the
    asserted path is proven by construction in tests/test_gates_integrity.py
    and end-to-end by the demo replay). The scales stay UNEQUAL to the
    hand-built counts so every fabricate_counts mutant still loses, and
    primary/resampled keep a SHARED scale (the memorization-pair invariant
    every real task carries).
    """
    task = demo_fixture.demo_task()
    scaled = {
        PopulationName.PRIMARY: {"customers": 40, "orders": 45, "order_items": 45},
        PopulationName.RESAMPLED: {"customers": 40, "orders": 45, "order_items": 45},
        PopulationName.STRESS: {"customers": 30, "orders": 35, "order_items": 35},
    }
    populations = tuple(
        p.model_copy(update={"scale": scaled[p.name]}) if p.name in scaled else p
        for p in task.populations
    )
    return task.model_copy(update={"populations": populations})


def _load_directive_plans(task: TaskIR, gold: GoldStub) -> dict[str, object]:
    """case name -> LoadMutationPlan, or the no-surface REASON string.

    Planning is the REAL `verification/attacks.resolve_load_mutation`; only
    the count arithmetic in `_simulated_load_stage1` is a test-local port of
    the documented artifact-edit semantics (this file's `_fake`-style pattern).
    """
    from elt_taskgen.verification import attacks

    plans: dict[str, object] = {}
    for case in task.attack_cases:
        mutation = case.mutation.strip()
        if not mutation.startswith(attacks.LOAD_DIRECTIVE_PREFIX):
            continue
        name, arg = attacks.split_load_directive(
            mutation[len(attacks.LOAD_DIRECTIVE_PREFIX):]
        )
        try:
            plans[case.name] = attacks.resolve_load_mutation(task, name, arg, gold)
        except attacks.InapplicableLoadMutationError as exc:
            plans[case.name] = str(exc)
    return plans


def _simulated_load_stage1(
    task: TaskIR, gold: GoldStub, plan: object, pop: P
) -> dict[str, int]:
    """The stage-1 count vector one resolved load mutation lands for `pop`."""
    from elt_taskgen.verification import attacks

    if plan.fabricated_counts is not None:
        return dict(plan.fabricated_counts[pop])
    source = plan.source_population.get(pop, pop)
    counts = dict(gold.stage1[source.value])
    for table in plan.omit_tables:
        counts.pop(table, None)
    for a, b in plan.swaps:
        counts[a], counts[b] = counts[b], counts[a]
    for table, op in plan.ops.items():
        if op == attacks.OP_DUPLICATE:
            counts[table] *= 2
        elif op == attacks.OP_EMPTY:
            counts[table] = 0
        elif op == attacks.OP_HEADER_AS_ROW:
            counts[table] += 1
        elif op == attacks.OP_TRUNCATE_FIRST_UNIT:
            counts[table] = min(counts[table], attacks._unit_rows(task, table))
        elif op == attacks.OP_DROP_NULL_ROWS:
            counts[table] = sum(
                1 for row in DEMO_DATA[source][table] if None not in row
            )
        else:  # pragma: no cover - closed vocabulary
            raise AssertionError(f"unknown load op {op!r}")
    return counts


def compute_attack_rewards(task: TaskIR, gold: GoldStub) -> dict[str, dict[P, float]]:
    """Execute every demo mutant for real and score it with THE reward."""
    primary_gold_rows = parse_canonical_csv(gold.stage2_csv[P.PRIMARY.value][MART_NAME])[1]
    load_plans = _load_directive_plans(task, gold)
    rewards: dict[str, dict[P, float]] = {}
    for case in task.attack_cases:
        if isinstance(load_plans.get(case.name), str):
            # No surface on this data (e.g. truncate_table where every table
            # fits in one read unit): the real runner records
            # attacks/<case>/inapplicable.json and the case has NO entry in
            # the measured payload — mirrored here.
            continue
        per_pop: dict[P, float] = {}
        for pop in P:
            if case.name in load_plans:
                # A load mutant: the count vector is what moved; the mart
                # submission run_attack records for a broken load is empty.
                stage1 = _simulated_load_stage1(
                    task, gold, load_plans[case.name], pop
                )
                actual: list[Row] = []
            elif case.mutation == HARDCODE_PRIMARY_DIRECTIVE:
                stage1 = dict(gold.stage1[pop.value])
                actual = [dict(r) for r in primary_gold_rows]
            else:
                stage1 = dict(gold.stage1[pop.value])
                actual = run_demo_sql(case.mutation, DEMO_DATA[pop])
            result = evaluate(task, gold, pop, stage1, {MART_NAME: actual})
            per_pop[pop] = result.reward
        rewards[case.name] = per_pop
    return rewards


def write_workspace(workspace: Path, task: TaskIR, gold: GoldStub) -> None:
    tdir = workspace / "tasks" / task.task_id
    for pop, data in DEMO_DATA.items():
        rows_dir = tdir / "populations" / pop.value / "rows"
        rows_dir.mkdir(parents=True, exist_ok=True)
        for table in task.tables:
            cols = [c.name for c in table.columns]
            lines = [
                json.dumps(dict(zip(cols, row)), sort_keys=True)
                for row in data[table.name]
            ]
            (rows_dir / f"{table.name}.jsonl").write_text(
                "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
            )
        rendered = tdir / "populations" / pop.value / "rendered"
        rendered.mkdir(parents=True, exist_ok=True)
        (rendered / "placeholder.txt").write_text("rendered", encoding="utf-8")
    reports = tdir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    write_determinism_record(workspace, task, gold)
    (reports / "contamination_post.json").write_text(
        json.dumps(
            {
                "task_id": task.task_id,
                "task_content_hash": task.content_hash(),
                "collisions": [],
                "coverage": dict(ARMED_COVERAGE),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    write_dual_build_record(workspace, task)


def determinism_evidence_for(task: TaskIR, gold: GoldStub) -> dict[str, str]:
    """The evidence cli.run_reference_stage binds the determinism record with:
    task_id + task_content_hash, and the split digests of the run — which the
    gate requires to be the frozen gold's (reference.gold.gold_digests)."""
    gd = gold_digests(gold, P.PRIMARY)
    return {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "population": P.PRIMARY.value,
        "runs": "3",
        "run_digest": gd.joint,
        gates.DETERMINISM_STAGE1_DIGEST_KEY: gd.stage1,
        gates.DETERMINISM_STAGE2_DIGEST_KEY: gd.stage2,
    }


def write_determinism_record(
    workspace: Path,
    task: TaskIR,
    gold: GoldStub,
    *,
    evidence: dict[str, str] | None = None,
    passed: bool = True,
) -> Path:
    path = workspace / "tasks" / task.task_id / gates.DETERMINISM_EVIDENCE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    det = GateResult(
        gate="determinism",
        passed=passed,
        details="3/3 rebuilds byte-identical" if passed else "run 2 differed",
        evidence=determinism_evidence_for(task, gold) if evidence is None else evidence,
    )
    path.write_text(det.model_dump_json(), encoding="utf-8")
    return path


def write_dual_build_record(
    workspace: Path,
    task: TaskIR,
    *,
    status: str = "agreed",
    agreement: dict[str, float] | None = None,
    content_hash: str | None = None,
    samples: list | None = None,
) -> Path:
    """Fixture mirror of reference.independent.record_build_result output.

    The `samples` list is part of that output and the gates now require it to
    exist and to corroborate `agreement` (a record claiming agreement with no
    executed sample proves nothing), so the fixture ships one by default.
    """
    path = (
        workspace / "tasks" / task.task_id / gates.DUAL_BUILD_EVIDENCE_REL
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    agreement_map = (
        agreement if agreement is not None else {p.value: 1.0 for p in P}
    )
    record = {
        "task_id": task.task_id,
        "task_content_hash": content_hash or task.content_hash(),
        "role": "independent_implementer",
        "status": status,
        "agreement": agreement_map,
        "samples": (
            samples
            if samples is not None
            else [
                {
                    "sample_index": 0,
                    "prompt_sha256": "0" * 64,
                    "sql_by_mart": {m.name: "select 1" for m in task.marts},
                    "rewards": dict(agreement_map),
                    "errors": {},
                    "dev_pass": True,
                }
            ]
        ),
        "detail": "fixture dual-build record",
    }
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return path


def gate_by_name(report, name: str) -> GateResult:
    for g in report.gates:
        if g.gate == name:
            return g
    raise AssertionError(f"no gate named {name!r} in report")


# ---------------------------------------------------------------------------
# upstream_eval unit tests
# ---------------------------------------------------------------------------

class TestUpstreamEvalComparator(unittest.TestCase):
    def setUp(self) -> None:
        self.mart = demo_fixture.demo_task().mart(MART_NAME)

    def test_tolerances_ported_from_upstream(self) -> None:
        self.assertEqual(REL_TOL, 1e-2)
        self.assertEqual(ABS_TOL, 1e-9)

    def test_sort_rows_total_order_and_nulls_last(self) -> None:
        rows: list[Row] = [
            {"k": 1, "a": "b", "n": None},
            {"k": 1, "a": "a", "n": 2},
            {"k": None, "a": "z", "n": 1},
            {"k": 2, "a": "a", "n": 1},
        ]
        out = sort_rows(rows, ("k",), ("k", "a", "n"))
        # key first; tie on k=1 broken by column a; null key last
        self.assertEqual([r["a"] for r in out], ["a", "b", "a", "z"])
        self.assertIsNone(out[-1]["k"])

    def test_sort_rows_numeric_string_coercion(self) -> None:
        rows: list[Row] = [{"k": "10"}, {"k": "9"}, {"k": "1389.0000000000"}]
        out = sort_rows(rows, ("k",), ("k",))
        self.assertEqual([r["k"] for r in out], ["9", "10", "1389.0000000000"])

    def test_sort_rows_case_insensitive_key_resolution(self) -> None:
        rows: list[Row] = [{"ID": 2}, {"ID": 1}]
        out = sort_rows(rows, ("id",), ("ID",))
        self.assertEqual([r["ID"] for r in out], [1, 2])

    def test_csv_roundtrip_null_and_types(self) -> None:
        rows: list[Row] = [
            {"a": 1, "b": None, "c": 4.5, "d": True},
            {"a": 2, "b": "x", "c": 0.0, "d": False},
        ]
        text = rows_to_canonical_csv(rows, ("a", "b", "c", "d"))
        header, parsed = parse_canonical_csv(text)
        self.assertEqual(header, ("a", "b", "c", "d"))
        self.assertIsNone(parsed[0]["b"])
        self.assertEqual(parsed[1]["b"], "x")
        self.assertEqual(parsed[0]["c"], "4.5")

    def test_numeric_value_is_ascii_only_like_pandas(self) -> None:
        # pandas' numeric parser: ASCII only, inf tokens only unstripped,
        # overflow is a failed parse. float() would accept every one of these.
        for v in ["\u0661\u0662\u0663", "\uff11\uff12\uff13", "\u0663.\u0665",
                  "\xa0123", "1e400", "-1e400", " inf", "inf\t"]:
            self.assertIsNone(_numeric_value(v), repr(v))
        for v in ["inf", "+inf", "-Infinity", "INF", " 42 ", "\t5\n", "1e308", "0E-9"]:
            self.assertEqual(_numeric_value(v), float(v.strip()), repr(v))

    def test_compare_mart_unicode_digits_are_text_like_upstream(self) -> None:
        mart = MartSpec(
            name="m",
            grain="one row per id",
            key_columns=("id",),
            columns=(
                MartColumn(name="id", type=ColumnType.INTEGER, description="id"),
                MartColumn(name="amount", type=ColumnType.DECIMAL, description="a"),
            ),
            plan=MartPlan(
                mart="m",
                ops=(MartOp(kind=MartOpKind.SOURCE, description="src", tables=("t",)),),
            ),
        )
        gold = "id,amount\n1,123\n2,456\n"
        unicode_rows = [
            {"id": 1, "amount": "\u0661\u0662\u0663"},
            {"id": 2, "amount": "\u0664\u0665\u0666"},
        ]
        # Upstream (pandas) cannot coerce Arabic-Indic digits: the column is
        # compared as TEXT and mismatches. Measured False upstream.
        self.assertFalse(compare_mart(gold, unicode_rows, mart))
        ascii_rows = [{"id": 1, "amount": "123.5"}, {"id": 2, "amount": "456"}]
        self.assertTrue(compare_mart(gold, ascii_rows, mart))  # tolerance path

    def test_sort_rows_unicode_digit_column_sorts_as_text(self) -> None:
        rows: list[Row] = [{"k": "\u0661\u0660"}, {"k": "\u0669"}]
        out = sort_rows(rows, ("k",), ("k",))
        # codepoint order ('١٠' < '٩'), NOT numeric order (9 < 10)
        self.assertEqual([r["k"] for r in out], ["\u0661\u0660", "\u0669"])

    def test_evaluate_zero_marts_fails_closed(self) -> None:
        task = TaskIR.model_construct(
            task_id="zero__marts", marts=(), tables=(),
        )
        gold = GoldStub(
            task_id="zero__marts",
            task_content_hash="0" * 64,
            stage1={"primary": {"t": 1}},
            stage2_csv={"primary": {}},
        )
        result = evaluate(task, gold, P.PRIMARY, {"t": 1}, {})
        self.assertEqual(result.reward, 0.0)

    def test_compare_stage1_exact_pass_and_case_insensitive(self) -> None:
        ok, detail = compare_stage1({"customers": 3}, {"CUSTOMERS": 3})
        self.assertTrue(ok)
        self.assertIn("ok", detail["customers"])

    def test_compare_stage1_missing_table_fails(self) -> None:
        ok, detail = compare_stage1({"customers": 3, "orders": 2}, {"customers": 3})
        self.assertFalse(ok)
        self.assertEqual(detail["orders"], "table not found")

    def test_compare_stage1_wrong_count_fails(self) -> None:
        ok, detail = compare_stage1({"customers": 3}, {"customers": 4})
        self.assertFalse(ok)
        self.assertIn("expected 3 rows, got 4", detail["customers"])

    def test_compare_stage1_empty_expectation_fails_closed(self) -> None:
        ok, _ = compare_stage1({}, {"customers": 3})
        self.assertFalse(ok)

    def _gold_csv(self, rows: list[Row]) -> str:
        ordered = sort_rows(rows, self.mart.key_columns, MART_COLS)
        return rows_to_canonical_csv(ordered, MART_COLS)

    def test_unequal_non_finite_values_do_not_match(self) -> None:
        """R01. The tolerance is relative to the SUBMITTED value, so an infinite
        submission made the right-hand side infinite and `inf <= inf` passed:
        a finite gold of 100 matched a submitted `inf`. Unequal values where
        either side is non-finite must never match, in either argument order,
        as strings or as numbers."""
        inf = float("inf")
        for gold, actual in (
            (["100"], ["inf"]),
            (["100"], ["-inf"]),
            (["inf"], ["-inf"]),
            (["-inf"], ["inf"]),
            (["100"], ["Infinity"]),
            ([100.0], [inf]),
            ([100.0], [-inf]),
            ([inf], [-inf]),
            ([inf], [100.0]),
            ([-inf], [100.0]),
            (["inf"], ["100"]),
            (["0"], ["inf"]),
            (["-100"], ["-inf"]),
        ):
            with self.subTest(gold=gold, actual=actual):
                self.assertFalse(_vectors_match(gold, actual))

    def test_the_non_finite_guard_preserves_every_existing_outcome(self) -> None:
        """Controls measured on the comparator before the R01 guard: matching
        infinities, finite tolerance on both sides of its boundary (including
        zero and negatives, with the existing gold/submitted argument order),
        nulls, NaN-as-null, text normalization, empty and ragged vectors."""
        inf, nan = float("inf"), float("nan")
        for label, gold, actual, want in (
            ("equal finite text", ["100"], ["100"], True),
            ("equal finite number", [100.0], [100.0], True),
            ("matching +inf", ["inf"], ["inf"], True),
            ("matching -inf", ["-inf"], ["-inf"], True),
            ("matching +inf number", [inf], [inf], True),
            ("inf spellings agree", ["Infinity"], ["inf"], True),
            ("inside tolerance", ["100"], ["100.5"], True),
            ("outside tolerance", ["100"], ["102"], False),
            ("outside, other order", ["102"], ["100"], False),
            ("zero equal", ["0"], ["0"], True),
            ("zero, inside the absolute floor", ["0"], ["1e-10"], True),
            ("zero, outside the absolute floor", ["0"], ["1e-8"], False),
            ("negative inside", ["-100"], ["-100.5"], True),
            ("negative outside", ["-100"], ["-102"], False),
            ("both null", [None], [None], True),
            ("both null sentinels", ["NaN"], ["null"], True),
            ("NaN is null", [nan], [None], True),
            ("single-sided null", [None], ["1"], False),
            ("text case and trim", [" Abc "], ["abc"], True),
            ("text differs", ["abc"], ["abd"], False),
            ("empty vectors", [], [], True),
            ("unequal lengths", ["1"], ["1", "2"], False),
        ):
            with self.subTest(label):
                self.assertIs(_vectors_match(gold, actual), want)

    def test_compare_mart_rejects_a_non_finite_measure(self) -> None:
        """R01 at the mart boundary: same keys, finite gold measure. The correct
        finite submission passes; replacing only that measure with an infinity
        fails the mart."""
        gold = self._gold_csv(
            [{"customer_id": 1, "completed_order_count": 2, "total_spend": 100.0}]
        )
        correct = [{"customer_id": 1, "completed_order_count": 2, "total_spend": 100.0}]
        self.assertTrue(compare_mart(gold, correct, self.mart))
        for bad in (float("inf"), float("-inf"), "inf", "-Infinity"):
            with self.subTest(total_spend=bad):
                submitted = [dict(correct[0], total_spend=bad)]
                self.assertFalse(compare_mart(gold, submitted, self.mart))

    def test_compare_mart_row_order_irrelevant(self) -> None:
        gold_rows: list[Row] = [
            {"customer_id": 1, "completed_order_count": 2, "total_spend": 20.0},
            {"customer_id": 2, "completed_order_count": 0, "total_spend": 0.0},
        ]
        shuffled = [dict(gold_rows[1]), dict(gold_rows[0])]
        self.assertTrue(compare_mart(self._gold_csv(gold_rows), shuffled, self.mart))

    def test_compare_mart_numeric_tolerance_matches_upstream(self) -> None:
        gold = self._gold_csv(
            [{"customer_id": 1, "completed_order_count": 1, "total_spend": 45.0}]
        )
        within = [{"customer_id": 1, "completed_order_count": 1, "total_spend": 45.4}]
        self.assertTrue(compare_mart(gold, within, self.mart))  # rtol=1e-2 accepts
        beyond = [{"customer_id": 1, "completed_order_count": 1, "total_spend": 46.0}]
        self.assertFalse(compare_mart(gold, beyond, self.mart))

    def test_compare_mart_case_insensitive_columns(self) -> None:
        gold = self._gold_csv(
            [{"customer_id": 1, "completed_order_count": 1, "total_spend": 5.0}]
        )
        actual = [{"CUSTOMER_ID": 1, "Completed_Order_Count": 1, "TOTAL_SPEND": 5.0}]
        self.assertTrue(compare_mart(gold, actual, self.mart))

    def test_compare_mart_single_sided_null_is_mismatch(self) -> None:
        gold = self._gold_csv(
            [{"customer_id": 1, "completed_order_count": 0, "total_spend": 0.0}]
        )
        actual = [{"customer_id": 1, "completed_order_count": 0, "total_spend": None}]
        self.assertFalse(compare_mart(gold, actual, self.mart))

    def test_compare_mart_both_null_matches(self) -> None:
        gold = self._gold_csv(
            [{"customer_id": 1, "completed_order_count": 0, "total_spend": None}]
        )
        actual = [{"customer_id": 1, "completed_order_count": 0, "total_spend": None}]
        self.assertTrue(compare_mart(gold, actual, self.mart))

    def test_compare_mart_pandas_na_tokens_are_null_like_upstream(self) -> None:
        # Upstream reads both sides with keep_default_na=True, so literal
        # "None"/"NA"/"nan" cells become NaN and match a genuine NULL.
        gold = self._gold_csv(
            [{"customer_id": 1, "completed_order_count": 0, "total_spend": None}]
        )
        for token in ("None", "NA", "nan", "NULL", "n/a"):
            actual = [
                {"customer_id": 1, "completed_order_count": 0, "total_spend": token}
            ]
            self.assertTrue(
                compare_mart(gold, actual, self.mart), f"token {token!r}"
            )
        # Non-token strings (pandas matches the raw field exactly) stay values.
        for non_token in ("NAN", " NA ", "none "):
            actual = [
                {"customer_id": 1, "completed_order_count": 0, "total_spend": non_token}
            ]
            self.assertFalse(
                compare_mart(gold, actual, self.mart), f"non-token {non_token!r}"
            )

    def test_compare_mart_row_count_mismatch_fails(self) -> None:
        gold = self._gold_csv(
            [{"customer_id": 1, "completed_order_count": 1, "total_spend": 5.0}]
        )
        self.assertFalse(compare_mart(gold, [], self.mart))

    def test_compare_mart_missing_column_fails(self) -> None:
        gold = self._gold_csv(
            [{"customer_id": 1, "completed_order_count": 1, "total_spend": 5.0}]
        )
        actual = [{"customer_id": 1, "completed_order_count": 1}]
        self.assertFalse(compare_mart(gold, actual, self.mart))

    def test_compare_mart_string_compare_trims_and_lowercases(self) -> None:
        mart = MartSpec(
            name="m",
            grain="one row per id",
            key_columns=("id",),
            columns=(
                MartColumn(name="id", type=ColumnType.INTEGER, description="id"),
                MartColumn(name="label", type=ColumnType.TEXT, description="label"),
            ),
            plan=MartPlan(
                mart="m",
                ops=(MartOp(kind=MartOpKind.SOURCE, description="src", tables=("t",)),),
            ),
        )
        gold = rows_to_canonical_csv([{"id": 1, "label": "Widget"}], ("id", "label"))
        self.assertTrue(compare_mart(gold, [{"id": 1, "label": "  widget "}], mart))
        self.assertFalse(compare_mart(gold, [{"id": 1, "label": "gadget"}], mart))


class TestEvaluateReward(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.task = demo_fixture.demo_task()
        cls.gold = build_demo_gold(cls.task)

    def test_stage1_failure_zeroes_reward(self) -> None:
        wrong = dict(self.gold.stage1[P.PRIMARY.value])
        wrong["orders"] += 1
        result = evaluate(self.task, self.gold, P.PRIMARY, wrong, {})
        self.assertFalse(result.stage1_pass)
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(result.mart_scores, {})

    def test_missing_population_gold_fails_closed(self) -> None:
        stripped = self.gold.model_copy(
            update={"stage1": {k: v for k, v in self.gold.stage1.items() if k != "stress"}}
        )
        result = evaluate(self.task, stripped, P.STRESS, {}, {})
        self.assertFalse(result.stage1_pass)
        self.assertEqual(result.reward, 0.0)

    def test_correct_solution_scores_one_on_all_populations(self) -> None:
        for pop in P:
            actual = run_demo_sql(REFERENCE_SQL, DEMO_DATA[pop])
            result = evaluate(
                self.task, self.gold, pop, self.gold.stage1[pop.value],
                {MART_NAME: actual},
            )
            self.assertEqual(result.reward, 1.0, f"population {pop.value}")

    def test_reward_is_fraction_of_marts(self) -> None:
        table = TableSpec(
            name="t",
            columns=(
                ColumnSpec(name="id", type=ColumnType.INTEGER),
                ColumnSpec(name="v", type=ColumnType.INTEGER),
            ),
            primary_key=("id",),
        )

        def mart(name: str) -> MartSpec:
            return MartSpec(
                name=name,
                grain="one row per id",
                key_columns=("id",),
                columns=(
                    MartColumn(name="id", type=ColumnType.INTEGER, description="id"),
                    MartColumn(name="v", type=ColumnType.INTEGER, description="v"),
                ),
                plan=MartPlan(
                    mart=name,
                    ops=(MartOp(kind=MartOpKind.SOURCE, description="src", tables=("t",)),),
                ),
            )

        task = TaskIR(
            task_id="test__two_marts",
            family_id="test__two_marts",
            cluster_id="test__two_marts",
            origin=Origin.SYNTHETIC,
            license="CC0-1.0",
            tables=(table,),
            backends=(BackendAssignment(table="t", backend=Backend.FILES),),
            marts=(mart("m1"), mart("m2")),
        )
        rows: list[Row] = [{"id": 1, "v": 10}, {"id": 2, "v": 20}]
        csv_text = rows_to_canonical_csv(rows, ("id", "v"))
        gold = GoldStub(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            stage1={"primary": {"t": 2}},
            stage2_csv={"primary": {"m1": csv_text, "m2": csv_text}},
        )
        wrong = [{"id": 1, "v": 10}, {"id": 2, "v": 999}]
        result = evaluate(
            task, gold, P.PRIMARY, {"t": 2}, {"m1": [dict(r) for r in rows], "m2": wrong}
        )
        self.assertTrue(result.stage1_pass)
        self.assertEqual(result.mart_scores, {"m1": True, "m2": False})
        self.assertEqual(result.reward, 0.5)

    def test_missing_mart_gold_scores_false(self) -> None:
        stripped = self.gold.model_copy(
            update={
                "stage2_csv": {
                    k: ({} if k == "primary" else v)
                    for k, v in self.gold.stage2_csv.items()
                }
            }
        )
        actual = run_demo_sql(REFERENCE_SQL, DEMO_DATA[P.PRIMARY])
        result = evaluate(
            self.task, stripped, P.PRIMARY, self.gold.stage1["primary"],
            {MART_NAME: actual},
        )
        self.assertEqual(result.mart_scores, {MART_NAME: False})
        self.assertEqual(result.reward, 0.0)


# ---------------------------------------------------------------------------
# Demo end-to-end: gold, attack matrix, and the full gate battery
# ---------------------------------------------------------------------------

class TestDemoEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.task = battery_task()
        cls.gold = build_demo_gold(cls.task)
        cls.rewards = compute_attack_rewards(cls.task, cls.gold)
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base_workspace = Path(cls._tmp.name) / "taskgen-workspace"
        write_workspace(cls.base_workspace, cls.task, cls.gold)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def fresh_workspace(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = Path(tmp.name) / "taskgen-workspace"
        shutil.copytree(self.base_workspace, dest)
        return dest

    # -- demo semantics ----------------------------------------------------

    def test_counterfactual_reference_matches_spec(self) -> None:
        rows = run_demo_sql(REFERENCE_SQL, DEMO_DATA[P.COUNTERFACTUAL])
        ordered = sort_rows(rows, ("customer_id",), MART_COLS)
        self.assertEqual(tuple(ordered), COUNTERFACTUAL_EXPECTED_MART)

    def test_demo_attack_matrix_reproduced(self) -> None:
        for case in self.task.attack_cases:
            measured = self.rewards.get(case.name)
            if measured is None:
                # A load probe with no surface on THIS fixture's data (e.g.
                # truncate_table: no table spans a read unit at 5 rows). The
                # real runner records inapplicable.json; the helper mirrors it
                # by omission — legal only for a non-required probe that
                # asserts nothing.
                self.assertFalse(case.required, case.name)
                self.assertEqual(case.expected_pass, {}, case.name)
                continue
            for pop, expect_full in case.expected_pass.items():
                reward = measured[pop]
                if expect_full:
                    self.assertEqual(
                        reward, 1.0,
                        f"{case.name} must keep full reward on {pop.value}",
                    )
                else:
                    self.assertLess(
                        reward, 1.0,
                        f"{case.name} must lose reward on {pop.value}",
                    )

    def test_count_without_distinct_returns_3_for_c11(self) -> None:
        case = next(
            c for c in self.task.attack_cases if c.name == "count_without_distinct"
        )
        rows = run_demo_sql(case.mutation, DEMO_DATA[P.COUNTERFACTUAL])
        by_id = {r["customer_id"]: r for r in rows}
        self.assertEqual(by_id[11]["completed_order_count"], 3)

    # -- green path --------------------------------------------------------

    def test_all_gates_pass_on_demo(self) -> None:
        report = gates.run_gates(self.task, self.base_workspace, self.gold, self.rewards)
        failed = [(g.gate, g.details) for g in report.gates if not g.passed]
        self.assertEqual(failed, [])
        self.assertTrue(report.accepted)
        self.assertEqual(tuple(g.gate for g in report.gates), gates.GATE_NAMES)
        self.assertEqual(report.scorer_version, gates.SCORER_VERSION)
        self.assertEqual(report.task_content_hash, self.task.content_hash())

    # -- each gate red on missing/withdrawn evidence -----------------------

    def _run(self, *, workspace=None, gold=None, rewards=None):
        return gates.run_gates(
            self.task,
            workspace or self.base_workspace,
            gold or self.gold,
            rewards if rewards is not None else self.rewards,
        )

    def test_trusted_solution_red_on_missing_population_gold(self) -> None:
        stripped = self.gold.model_copy(
            update={"stage1": {k: v for k, v in self.gold.stage1.items() if k != "stress"}}
        )
        report = self._run(gold=stripped)
        self.assertFalse(gate_by_name(report, "trusted-solution").passed)
        self.assertFalse(report.accepted)

    def test_trusted_solution_red_on_wrong_task_gold(self) -> None:
        wrong = self.gold.model_copy(update={"task_id": "someone__else"})
        report = self._run(gold=wrong)
        self.assertFalse(gate_by_name(report, "trusted-solution").passed)

    def test_determinism_red_on_missing_evidence(self) -> None:
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.DETERMINISM_EVIDENCE_REL).unlink()
        report = self._run(workspace=ws)
        self.assertFalse(gate_by_name(report, "determinism").passed)
        self.assertFalse(report.accepted)

    def test_determinism_red_on_recorded_failure(self) -> None:
        ws = self.fresh_workspace()
        path = ws / "tasks" / self.task.task_id / gates.DETERMINISM_EVIDENCE_REL
        failed = GateResult(gate="determinism", passed=False, details="run 2 differed")
        path.write_text(failed.model_dump_json(), encoding="utf-8")
        report = self._run(workspace=ws)
        self.assertFalse(gate_by_name(report, "determinism").passed)

    def test_determinism_red_on_stale_content_hash(self) -> None:
        # An edited task must not be able to quote the determinism run of a
        # previous identity (every OTHER evidence reader already refuses this).
        ws = self.fresh_workspace()
        ev = determinism_evidence_for(self.task, self.gold)
        ev["task_content_hash"] = "0" * 64
        write_determinism_record(ws, self.task, self.gold, evidence=ev)
        gate = gate_by_name(self._run(workspace=ws), "determinism")
        self.assertFalse(gate.passed)
        self.assertIn("STALE", gate.details)
        self.assertIn("re-run reference-run", gate.details)

    def test_determinism_red_without_content_hash(self) -> None:
        # The legacy record shape (task_id only): fail closed, never a pass.
        ws = self.fresh_workspace()
        ev = determinism_evidence_for(self.task, self.gold)
        del ev["task_content_hash"]
        write_determinism_record(ws, self.task, self.gold, evidence=ev)
        gate = gate_by_name(self._run(workspace=ws), "determinism")
        self.assertFalse(gate.passed)
        self.assertIn("no task_content_hash binding", gate.details)

    def test_determinism_red_when_digest_disagrees_with_gold(self) -> None:
        # Determinism evidence describes LIVE runs; the gold is what SHIPS. A
        # self-consistent run that is not the answer key is not evidence.
        ws = self.fresh_workspace()
        ev = determinism_evidence_for(self.task, self.gold)
        ev[gates.DETERMINISM_STAGE2_DIGEST_KEY] = "f" * 64
        write_determinism_record(ws, self.task, self.gold, evidence=ev)
        gate = gate_by_name(self._run(workspace=ws), "determinism")
        self.assertFalse(gate.passed)
        self.assertIn("not the answer key that ships", gate.details)
        self.assertEqual(
            gate.evidence["gold_stage2_digest"],
            gold_digests(self.gold, P.PRIMARY).stage2[:16],
        )

    def test_determinism_green_records_the_gold_digests(self) -> None:
        gate = gate_by_name(self._run(), "determinism")
        self.assertTrue(gate.passed, gate.details)
        gd = gold_digests(self.gold, P.PRIMARY)
        self.assertEqual(gate.evidence["gold_stage1_digest"], gd.stage1[:16])
        self.assertEqual(gate.evidence["gold_stage2_digest"], gd.stage2[:16])
        self.assertEqual(
            gate.evidence["recorded:task_content_hash"], self.task.content_hash()
        )

    def test_degenerate_red_when_constant_gold_is_fakeable(self) -> None:
        # A single-row primary mart is matched by the constant strategy.
        one_row = rows_to_canonical_csv(
            [{"customer_id": 1, "completed_order_count": 1, "total_spend": 5.0}],
            MART_COLS,
        )
        stage2 = copy.deepcopy(self.gold.stage2_csv)
        stage2["primary"] = {MART_NAME: one_row}
        stage1 = copy.deepcopy(self.gold.stage1)
        gold = self.gold.model_copy(update={"stage2_csv": stage2, "stage1": stage1})
        report = self._run(gold=gold)
        self.assertFalse(gate_by_name(report, "degenerate-zero").passed)

    def test_degenerate_red_when_keys_only_scores(self) -> None:
        # Gold whose non-key columns are all NULL is matched by keys-only.
        null_rows: list[Row] = [
            {"customer_id": 1, "completed_order_count": None, "total_spend": None},
            {"customer_id": 2, "completed_order_count": None, "total_spend": None},
        ]
        stage2 = copy.deepcopy(self.gold.stage2_csv)
        stage2["primary"] = {MART_NAME: rows_to_canonical_csv(null_rows, MART_COLS)}
        gold = self.gold.model_copy(update={"stage2_csv": stage2})
        report = self._run(gold=gold)
        self.assertFalse(gate_by_name(report, "degenerate-zero").passed)

    def test_required_mutants_red_on_missing_case_rewards(self) -> None:
        partial = {k: v for k, v in self.rewards.items() if k != "inner_join"}
        report = self._run(rewards=partial)
        gate = gate_by_name(report, "required-mutants")
        self.assertFalse(gate.passed)
        self.assertIn("inner_join", gate.details)

    def test_required_mutants_red_on_leak(self) -> None:
        leaked = copy.deepcopy(self.rewards)
        leaked["inner_join"][P.PRIMARY] = 1.0  # LEAK: must lose reward there
        report = self._run(rewards=leaked)
        gate = gate_by_name(report, "required-mutants")
        self.assertFalse(gate.passed)
        self.assertIn("LEAK", gate.details)

    def test_required_mutants_red_when_expected_full_reward_lost(self) -> None:
        broken = copy.deepcopy(self.rewards)
        broken["inner_join"][P.DEVELOPMENT] = 0.0  # must keep full reward there
        report = self._run(rewards=broken)
        self.assertFalse(gate_by_name(report, "required-mutants").passed)

    def test_required_mutants_red_on_empty_rewards(self) -> None:
        report = self._run(rewards={})
        self.assertFalse(gate_by_name(report, "required-mutants").passed)

    # -- shortcut-probes gate ----------------------------------------------

    def _write_attack_record(
        self,
        ws: Path,
        name: str,
        kind: str,
        rewards_map: dict[P, float],
        content_hash: str | None = None,
    ) -> None:
        d = ws / "tasks" / self.task.task_id / "attacks" / name
        d.mkdir(parents=True, exist_ok=True)
        record = {
            "case": name,
            "kind": kind,
            "required": False,
            "source_finding": "F1",
            "rewards": {p.value: r for p, r in rewards_map.items()},
            "expected_pass": {},
            "errors": {},
            "task_content_hash": content_hash or self.task.content_hash(),
        }
        (d / "rewards.json").write_text(json.dumps(record), encoding="utf-8")

    def test_shortcut_probes_red_when_probe_keeps_full_reward(self) -> None:
        leaked = copy.deepcopy(self.rewards)
        leaked["hardcoded_primary_outputs"] = {p: 1.0 for p in P}
        report = self._run(rewards=leaked)
        gate = gate_by_name(report, "shortcut-probes")
        self.assertFalse(gate.passed)
        self.assertIn("working shortcut", gate.details)

    def test_shortcut_probes_red_on_missing_probe_rewards(self) -> None:
        partial = {
            k: v for k, v in self.rewards.items() if k != "hardcoded_primary_outputs"
        }
        report = self._run(rewards=partial)
        gate = gate_by_name(report, "shortcut-probes")
        self.assertFalse(gate.passed)
        self.assertIn("no measured rewards", gate.details)

    def test_shortcut_probes_red_on_unidentifiable_compiled_probe(self) -> None:
        rewards = copy.deepcopy(self.rewards)
        rewards["finding__mystery"] = {P.PRIMARY: 0.0}
        report = self._run(rewards=rewards)
        gate = gate_by_name(report, "shortcut-probes")
        self.assertFalse(gate.passed)
        self.assertIn("cannot establish probe kind", gate.details)

    def test_shortcut_probes_compiled_probe_asserted_from_record(self) -> None:
        ws = self.fresh_workspace()
        rewards = copy.deepcopy(self.rewards)
        full = {p: 1.0 for p in P}
        rewards["finding__F1"] = dict(full)
        self._write_attack_record(ws, "finding__F1", "keys_only", full)
        report = self._run(workspace=ws, rewards=rewards)
        gate = gate_by_name(report, "shortcut-probes")
        self.assertFalse(gate.passed)
        self.assertIn("finding__F1", gate.details)
        # Losing reward on one graded population satisfies the assertion.
        rewards["finding__F1"] = {**full, P.RESAMPLED: 0.5}
        report = self._run(workspace=ws, rewards=rewards)
        self.assertTrue(gate_by_name(report, "shortcut-probes").passed)

    def test_shortcut_probes_red_on_stale_record(self) -> None:
        ws = self.fresh_workspace()
        rewards = copy.deepcopy(self.rewards)
        rewards["finding__F2"] = {P.PRIMARY: 0.0}
        self._write_attack_record(
            ws, "finding__F2", "keys_only", {P.PRIMARY: 0.0}, content_hash="0" * 64
        )
        report = self._run(workspace=ws, rewards=rewards)
        gate = gate_by_name(report, "shortcut-probes")
        self.assertFalse(gate.passed)
        self.assertIn("STALE", gate.details)

    def test_shortcut_probes_red_when_only_development_measured(self) -> None:
        # A probe measured on the visible population alone proves nothing.
        rewards = copy.deepcopy(self.rewards)
        rewards["hardcoded_primary_outputs"] = {P.DEVELOPMENT: 0.0}
        report = self._run(rewards=rewards)
        gate = gate_by_name(report, "shortcut-probes")
        self.assertFalse(gate.passed)
        self.assertIn("no graded-population rewards", gate.details)

    def test_shortcut_probes_red_when_compiled_probe_key_is_deleted(self) -> None:
        # Deleting a compiled probe's reward ENTRY (not just emptying it) must
        # not make the probe vanish: the recorded attack tree is the ground
        # truth for which probes exist, so the gate goes red either way.
        ws = self.fresh_workspace()
        rewards = copy.deepcopy(self.rewards)
        self._write_attack_record(ws, "finding__F4", "constants", {P.PRIMARY: 0.0})
        # Present in the payload: green (a shortcut probe that loses reward).
        rewards["finding__F4"] = {P.PRIMARY: 0.0}
        self.assertTrue(
            gate_by_name(
                self._run(workspace=ws, rewards=rewards), "shortcut-probes"
            ).passed
        )
        # Key deleted from the payload: red, not silently dropped.
        del rewards["finding__F4"]
        gate = gate_by_name(
            self._run(workspace=ws, rewards=rewards), "shortcut-probes"
        )
        self.assertFalse(gate.passed)
        self.assertIn("finding__F4", gate.details)
        self.assertIn("NO entry in the measured attack rewards", gate.details)

    def test_shortcut_probes_ignores_stale_recorded_probe_absent_from_payload(
        self,
    ) -> None:
        # attacks/ is not pruned across repairs: a probe recorded at a PREVIOUS
        # content hash is not evidence about this identity and must not turn
        # the gate red on its own.
        ws = self.fresh_workspace()
        self._write_attack_record(
            ws, "finding__old", "constants", {P.PRIMARY: 0.0}, content_hash="0" * 64
        )
        report = self._run(workspace=ws, rewards=copy.deepcopy(self.rewards))
        self.assertTrue(gate_by_name(report, "shortcut-probes").passed)

    def test_shortcut_probes_ignores_non_shortcut_compiled_kinds(self) -> None:
        # A compiled inner-join probe keeping full reward everywhere is the
        # required-mutants/expected-pass story, not a shortcut leak.
        ws = self.fresh_workspace()
        rewards = copy.deepcopy(self.rewards)
        rewards["finding__F3"] = {p: 1.0 for p in P}
        self._write_attack_record(
            ws, "finding__F3", "inner_join", {p: 1.0 for p in P}
        )
        report = self._run(workspace=ws, rewards=rewards)
        self.assertTrue(gate_by_name(report, "shortcut-probes").passed)

    def test_shortcut_probes_red_when_every_probe_inapplicable(self) -> None:
        # Every shortcut probe honestly recorded inapplicable is still ZERO
        # measured probes — no evidence, not a pass (fail closed).
        ws = self.fresh_workspace()
        task2 = self.task.model_copy(
            update={
                "attack_cases": tuple(
                    c.model_copy(update={"required": False, "expected_pass": {}})
                    if c.kind in gates.SHORTCUT_KINDS
                    else c
                    for c in self.task.attack_cases
                )
            }
        )
        shortcut = [c for c in task2.attack_cases if c.kind in gates.SHORTCUT_KINDS]
        self.assertTrue(shortcut)
        for case in shortcut:
            d = ws / "tasks" / task2.task_id / "attacks" / case.name
            d.mkdir(parents=True, exist_ok=True)
            (d / "inapplicable.json").write_text(
                json.dumps(
                    {
                        "case": case.name,
                        "kind": case.kind.value,
                        "required": False,
                        "inapplicable": "no surface on this data",
                        "task_content_hash": task2.content_hash(),
                    }
                ),
                encoding="utf-8",
            )
        gate = gates._gate_shortcut_probes(task2, ws, {})
        self.assertFalse(gate.passed)
        self.assertIn("zero measured", gate.details)
        for case in shortcut:
            self.assertIn(f"inapplicable:{case.name}", gate.evidence)

    def test_shortcut_probes_ok_counts_only_measured(self) -> None:
        # One measured + one inapplicable: green, and the detail says how
        # many were actually MEASURED (1), not how many exist (2).
        ws = self.fresh_workspace()
        rewards = copy.deepcopy(self.rewards)
        self._write_attack_record(ws, "finding__F9", "keys_only", {P.PRIMARY: 0.0})
        rewards["finding__F9"] = {P.PRIMARY: 0.0}
        gate = gate_by_name(
            self._run(workspace=ws, rewards=rewards), "shortcut-probes"
        )
        self.assertTrue(gate.passed, gate.details)
        self.assertRegex(gate.details, r"^\d+ measured shortcut-kind probe")
        measured = sum(
            1 for k in gate.evidence
            if not k.startswith(("inapplicable:", "excluded:")) and k != "scope"
        )
        self.assertIn(f"{measured} measured", gate.details)

    def test_data_sensitivity_red_on_identical_resample(self) -> None:
        stage2 = copy.deepcopy(self.gold.stage2_csv)
        stage2["resampled"] = dict(stage2["primary"])
        gold = self.gold.model_copy(update={"stage2_csv": stage2})
        report = self._run(gold=gold)
        gate = gate_by_name(report, "data-sensitivity")
        self.assertFalse(gate.passed)
        self.assertIn("identical", gate.details)

    @staticmethod
    def _within_tolerance_copy(csv_text: str) -> str:
        """Byte-different, reward-equivalent: floats * 1.005 (inside rtol
        1e-2), strings upper-cased with a trailing space (compare_mart trims
        and lower-cases)."""
        cols, rows = parse_canonical_csv(csv_text)
        out: list[Row] = []
        for row in rows:
            new_row: Row = {}
            for c in cols:
                v = row.get(c)
                if v is None:
                    new_row[c] = None
                    continue
                try:
                    f = float(v)
                except ValueError:
                    new_row[c] = str(v).upper() + " "
                    continue
                new_row[c] = repr(f * 1.005) if "." in str(v) else v
            out.append(new_row)
        return rows_to_canonical_csv(out, cols)

    def test_data_sensitivity_red_on_reward_equivalent_resample(self) -> None:
        # The bytes differ, THE reward cannot tell them apart: a primary
        # memorizer scores 1.0 on resampled. Byte-distinctness was the gap.
        stage2 = copy.deepcopy(self.gold.stage2_csv)
        stage2["resampled"] = {
            MART_NAME: self._within_tolerance_copy(stage2["primary"][MART_NAME])
        }
        self.assertNotEqual(stage2["resampled"][MART_NAME], stage2["primary"][MART_NAME])
        gold2 = self.gold.model_copy(update={"stage2_csv": stage2})
        primary_rows = parse_canonical_csv(stage2["primary"][MART_NAME])[1]
        # WHY: under THE reward the primary echo IS a full-credit resampled answer.
        self.assertEqual(
            evaluate(
                self.task, gold2, P.RESAMPLED, self.gold.stage1["resampled"],
                {MART_NAME: primary_rows},
            ).reward,
            1.0,
        )
        gate = gate_by_name(self._run(gold=gold2), "data-sensitivity")
        self.assertFalse(gate.passed)
        self.assertIn("REWARD-EQUIVALENT", gate.details)
        self.assertEqual(
            gate.evidence[f"{MART_NAME}:primary-vs-resampled"],
            "differs (reward-equivalent)",
        )

    def test_data_sensitivity_green_evidence_is_reward_distinct(self) -> None:
        gate = gate_by_name(self._run(), "data-sensitivity")
        self.assertTrue(gate.passed, gate.details)
        self.assertEqual(
            gate.evidence[f"{MART_NAME}:primary-vs-resampled"], "differs (reward-distinct)"
        )
        self.assertEqual(
            gate.evidence[f"{MART_NAME}:primary-vs-counterfactual"],
            "differs (reward-distinct)",
        )

    def test_data_sensitivity_red_on_na_token_equivalent_resample(self) -> None:
        # The _NA_TOKENS path: upstream reads 'None' as NULL, so a resampled
        # gold that spells primary's NULLs as 'None' is reward-equivalent.
        cols, rows = parse_canonical_csv(self.gold.stage2_csv["primary"][MART_NAME])
        nulled = [dict(r) for r in rows]
        nulled[0]["total_spend"] = None
        primary_csv = rows_to_canonical_csv(nulled, cols)
        # The canonical serializer writes NULL as an empty field; the token
        # spelling has to be written literally (as a foreign gold might be).
        first_line = primary_csv.splitlines()[1]
        self.assertTrue(first_line.endswith(","))
        resampled_csv = primary_csv.replace(first_line + "\n", first_line + "None\n", 1)
        self.assertNotEqual(primary_csv, resampled_csv)
        stage2 = copy.deepcopy(self.gold.stage2_csv)
        stage2["primary"] = {MART_NAME: primary_csv}
        stage2["resampled"] = {MART_NAME: resampled_csv}
        gold2 = self.gold.model_copy(update={"stage2_csv": stage2})
        gate = gate_by_name(self._run(gold=gold2), "data-sensitivity")
        self.assertFalse(gate.passed)
        self.assertIn("REWARD-EQUIVALENT", gate.details)
        self.assertNotIn("primary and counterfactual gold are", gate.details)

    def test_data_sensitivity_failure_wording_never_routes_fatal(self) -> None:
        from elt_taskgen import repair

        stage2 = copy.deepcopy(self.gold.stage2_csv)
        stage2["resampled"] = {
            MART_NAME: self._within_tolerance_copy(stage2["primary"][MART_NAME])
        }
        gate = gate_by_name(
            self._run(gold=self.gold.model_copy(update={"stage2_csv": stage2})),
            "data-sensitivity",
        )
        for word in repair._FATAL_KEYWORDS:
            self.assertNotIn(word, gate.details.lower(), f"{word!r} in: {gate.details}")

    def test_info_content_red_on_constant_mart(self) -> None:
        constant_rows: list[Row] = [
            {"customer_id": i, "completed_order_count": 1, "total_spend": 5.0}
            for i in range(1, 6)
        ]
        stage2 = copy.deepcopy(self.gold.stage2_csv)
        stage2["primary"] = {MART_NAME: rows_to_canonical_csv(constant_rows, MART_COLS)}
        gold = self.gold.model_copy(update={"stage2_csv": stage2})
        report = self._run(gold=gold)
        self.assertFalse(gate_by_name(report, "info-content").passed)

    def test_populations_load_red_on_missing_rows_file(self) -> None:
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / "populations" / "stress" / "rows"
         / "orders.jsonl").unlink()
        report = self._run(workspace=ws)
        self.assertFalse(gate_by_name(report, "populations-load").passed)

    def test_populations_load_red_on_count_drift(self) -> None:
        ws = self.fresh_workspace()
        path = (ws / "tasks" / self.task.task_id / "populations" / "primary"
                / "rows" / "customers.jsonl")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"customer_id": 99, "customer_name": "extra"}) + "\n")
        report = self._run(workspace=ws)
        gate = gate_by_name(report, "populations-load")
        self.assertFalse(gate.passed)
        self.assertIn("customers", gate.details)

    def test_populations_load_red_on_missing_rendered(self) -> None:
        ws = self.fresh_workspace()
        shutil.rmtree(ws / "tasks" / self.task.task_id / "populations"
                      / "development" / "rendered")
        report = self._run(workspace=ws)
        self.assertFalse(gate_by_name(report, "populations-load").passed)

    def test_contamination_red_on_missing_scan(self) -> None:
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.CONTAMINATION_POST_EVIDENCE_REL).unlink()
        report = self._run(workspace=ws)
        self.assertFalse(gate_by_name(report, "contamination-clean").passed)

    def test_contamination_red_on_stale_hash(self) -> None:
        ws = self.fresh_workspace()
        path = ws / "tasks" / self.task.task_id / gates.CONTAMINATION_POST_EVIDENCE_REL
        data = json.loads(path.read_text(encoding="utf-8"))
        data["task_content_hash"] = "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        report = self._run(workspace=ws)
        gate = gate_by_name(report, "contamination-clean")
        self.assertFalse(gate.passed)
        self.assertIn("STALE", gate.details)

    def _write_contamination(self, ws: Path, mutate) -> None:
        path = ws / "tasks" / self.task.task_id / gates.CONTAMINATION_POST_EVIDENCE_REL
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_contamination_clean_gate_refuses_shape_less_coverage(self) -> None:
        # The typed-only pre-shape index (every runs/* record before this fix):
        # a clean scan against it could not have seen a retyped copy of a
        # benchmark schema, so the evidence is stale by construction.
        ws = self.fresh_workspace()

        def typed_only(data):
            data["coverage"] = {
                "level": "armed",
                "index_dir": "x",
                "benchmark_by_kind": {"family": 300, "schema": 100, "schema-table": 832},
            }

        self._write_contamination(ws, typed_only)
        gate = gate_by_name(self._run(workspace=ws), "contamination-clean")
        self.assertFalse(gate.passed)
        self.assertIn("predates type-blind", gate.details)
        self.assertIn("re-run measure-target", gate.details)
        self.assertEqual(gate.evidence["shape_fingerprints"], "0")
        # With shape coverage the same scan passes.
        ws2 = self.fresh_workspace()

        def with_shape(data):
            data["coverage"] = dict(ARMED_COVERAGE)

        self._write_contamination(ws2, with_shape)
        gate = gate_by_name(self._run(workspace=ws2), "contamination-clean")
        self.assertTrue(gate.passed, gate.details)
        self.assertEqual(gate.evidence["coverage_level"], "armed")

    def test_contamination_clean_gate_refuses_missing_or_unarmed_coverage(self) -> None:
        ws = self.fresh_workspace()
        self._write_contamination(ws, lambda d: d.pop("coverage"))
        gate = gate_by_name(self._run(workspace=ws), "contamination-clean")
        self.assertFalse(gate.passed)
        self.assertIn("no index coverage", gate.details)
        ws2 = self.fresh_workspace()

        def name_only(data):
            data["coverage"] = {**ARMED_COVERAGE, "level": "name_only"}

        self._write_contamination(ws2, name_only)
        gate = gate_by_name(self._run(workspace=ws2), "contamination-clean")
        self.assertFalse(gate.passed)
        self.assertIn("'name_only', not 'armed'", gate.details)

    def test_contamination_red_on_fatal_collision(self) -> None:
        ws = self.fresh_workspace()
        path = ws / "tasks" / self.task.task_id / gates.CONTAMINATION_POST_EVIDENCE_REL
        data = json.loads(path.read_text(encoding="utf-8"))
        data["collisions"] = [
            {"kind": "sql", "against": "eltbench", "detail": "ref SQL match", "fatal": True}
        ]
        path.write_text(json.dumps(data), encoding="utf-8")
        report = self._run(workspace=ws)
        self.assertFalse(gate_by_name(report, "contamination-clean").passed)

    # -- dual-build-agreement gate (#10) + trusted-solution consumption ------

    def test_dual_build_red_on_missing_record(self) -> None:
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.DUAL_BUILD_EVIDENCE_REL).unlink()
        report = self._run(workspace=ws)
        gate = gate_by_name(report, "dual-build-agreement")
        self.assertFalse(gate.passed)
        self.assertIn("independent build not performed", gate.details)
        self.assertFalse(report.accepted)

    def test_trusted_solution_red_without_independent_build(self) -> None:
        # THE self-certification fix: gold that is comparator-consistent must
        # STILL fail trusted-solution when no independent build certified it.
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.DUAL_BUILD_EVIDENCE_REL).unlink()
        report = self._run(workspace=ws)
        gate = gate_by_name(report, "trusted-solution")
        self.assertFalse(gate.passed)
        self.assertIn("independent build not performed", gate.details)
        self.assertIn("self-certify", gate.details)

    def test_dual_build_red_on_stale_hash(self) -> None:
        ws = self.fresh_workspace()
        write_dual_build_record(ws, self.task, content_hash="0" * 64)
        report = self._run(workspace=ws)
        gate = gate_by_name(report, "dual-build-agreement")
        self.assertFalse(gate.passed)
        self.assertIn("STALE", gate.details)
        self.assertFalse(gate_by_name(report, "trusted-solution").passed)

    def test_dual_build_red_on_needs_adjudication(self) -> None:
        ws = self.fresh_workspace()
        write_dual_build_record(
            ws,
            self.task,
            status="needs_adjudication",
            agreement={
                **{p.value: 1.0 for p in P},
                P.PRIMARY.value: 0.0,
            },
        )
        report = self._run(workspace=ws)
        gate = gate_by_name(report, "dual-build-agreement")
        self.assertFalse(gate.passed)
        self.assertIn("disagrees", gate.details)
        self.assertFalse(gate_by_name(report, "trusted-solution").passed)

    def test_dual_build_red_on_missing_population_agreement(self) -> None:
        ws = self.fresh_workspace()
        agreement = {p.value: 1.0 for p in P if p is not P.STRESS}
        write_dual_build_record(ws, self.task, agreement=agreement)
        report = self._run(workspace=ws)
        gate = gate_by_name(report, "dual-build-agreement")
        self.assertFalse(gate.passed)
        self.assertIn("stress", gate.details)

    def test_dual_build_red_on_wrong_task_record(self) -> None:
        ws = self.fresh_workspace()
        path = write_dual_build_record(ws, self.task)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["task_id"] = "someone__else"
        path.write_text(json.dumps(data), encoding="utf-8")
        report = self._run(workspace=ws)
        self.assertFalse(gate_by_name(report, "dual-build-agreement").passed)

    def test_contamination_nonfatal_collisions_pass(self) -> None:
        ws = self.fresh_workspace()
        path = ws / "tasks" / self.task.task_id / gates.CONTAMINATION_POST_EVIDENCE_REL
        data = json.loads(path.read_text(encoding="utf-8"))
        data["collisions"] = [
            {"kind": "family", "against": "admitted", "detail": "sibling", "fatal": False}
        ]
        path.write_text(json.dumps(data), encoding="utf-8")
        report = self._run(workspace=ws)
        self.assertTrue(gate_by_name(report, "contamination-clean").passed)

    def test_report_never_accepts_with_any_failure(self) -> None:
        report = self._run(rewards={})
        self.assertFalse(report.accepted)
        self.assertEqual(len(report.gates), len(gates.GATE_NAMES))

    def test_roster_digest_is_stamped_and_pinned(self) -> None:
        # The roster identity is DERIVED so it cannot be forgotten; this pin is
        # the discipline that a roster change ships with a SCORER_VERSION bump.
        self.assertEqual(
            gates.ROSTER_DIGEST,
            PINNED_ROSTER_DIGEST,
            "roster changed: bump gates.SCORER_VERSION and update this pin "
            "(tests/test_gates_eval.py PINNED_ROSTER_DIGEST) in the same change",
        )
        self.assertEqual(gates.roster_digest(), gates.ROSTER_DIGEST)
        self.assertEqual(gates.SCORER_VERSION, "1.3.0")
        report = self._run()
        self.assertEqual(report.roster_digest, gates.ROSTER_DIGEST)
        self.assertEqual(report.roster, gates.GATE_NAMES)
        for variant in (gates.TaskVariant.EXTRACT_LOAD, gates.TaskVariant.TRANSFORM):
            vreport = gates.run_variant_gates(
                variant, self.task, self.base_workspace, self.gold, {}
            )
            self.assertEqual(vreport.roster_digest, gates.ROSTER_DIGEST)
            self.assertEqual(vreport.roster, gates.VARIANT_GATE_NAMES[variant])

    def _write_notes(self, ws: Path, rel: str, notes: list[str], content_hash=None) -> None:
        path = ws / "tasks" / self.task.task_id / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "task_id": self.task.task_id,
                    "task_content_hash": content_hash or self.task.content_hash(),
                    "notes": notes,
                }
            ),
            encoding="utf-8",
        )

    def test_dual_build_red_detail_names_the_transport_from_a_bound_note(self) -> None:
        # The cli records WHY a witness build was not performed (transport
        # class name included); the red gate must NAME it so the engine's
        # infrastructure branch fires instead of a repair round.
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.DUAL_BUILD_EVIDENCE_REL).unlink()
        self._write_notes(
            ws,
            gates.DUAL_BUILD_NOTES_REL,
            [
                "independent build not performed: TranscriptMissingError: "
                "replay-only mode: no recorded transcript for role "
                "'independent_implementer' (searched: /ws/contamination/x, /ws/y)"
            ],
        )
        report = self._run(workspace=ws)
        gate = gate_by_name(report, "dual-build-agreement")
        self.assertFalse(gate.passed)
        self.assertIn("independent build not performed", gate.details)  # prefix kept
        self.assertIn("TranscriptMissingError", gate.details)
        self.assertNotIn("searched:", gate.details)  # paths never enter routing
        self.assertNotIn("contamination", gate.details)
        self.assertIn("TranscriptMissingError", gate.evidence["producer_note"])
        trusted = gate_by_name(report, "trusted-solution")
        self.assertFalse(trusted.passed)
        self.assertIn("TranscriptMissingError", trusted.details)

    def test_stale_producer_note_is_ignored_and_never_greens_a_gate(self) -> None:
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.DUAL_BUILD_EVIDENCE_REL).unlink()
        self._write_notes(
            ws,
            gates.DUAL_BUILD_NOTES_REL,
            ["independent build not performed: MissingCredentialsError: no key"],
            content_hash="0" * 64,
        )
        gate = gate_by_name(self._run(workspace=ws), "dual-build-agreement")
        self.assertFalse(gate.passed)
        self.assertIn("independent build not performed", gate.details)
        self.assertNotIn("MissingCredentialsError", gate.details)
        self.assertNotIn("producer_note", gate.evidence)
        # A bound note next to a PRESENT record changes nothing either.
        ws2 = self.fresh_workspace()
        self._write_notes(
            ws2, gates.DUAL_BUILD_NOTES_REL, ["independent build not performed: X"]
        )
        self.assertTrue(gate_by_name(self._run(workspace=ws2), "dual-build-agreement").passed)

    # -- referential-integrity / declared-scale-reconciliation --------------

    def _rows_path(self, ws: Path, pop: P, table: str) -> Path:
        return (
            ws / "tasks" / self.task.task_id / "populations" / pop.value
            / "rows" / f"{table}.jsonl"
        )

    def test_referential_integrity_red_on_corrupted_child_fk(self) -> None:
        """PROOF BY CONSTRUCTION: corrupt one rendered child FK -> gate RED."""
        ws = self.fresh_workspace()
        path = self._rows_path(ws, P.PRIMARY, "order_items")
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        rows[0]["order_id"] = 999_999  # no such parent order
        path.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
            encoding="utf-8",
        )
        report = self._run(workspace=ws)
        gate = gate_by_name(report, "referential-integrity")
        self.assertFalse(gate.passed)
        self.assertIn("no parent row", gate.details)
        self.assertFalse(report.accepted)

    def test_referential_integrity_green_counts_optional_nulls(self) -> None:
        """The demo primary population carries a NULL customer_id order on the
        OPTIONAL customers link — legal, and counted rather than silent."""
        report = self._run()
        gate = gate_by_name(report, "referential-integrity")
        self.assertTrue(gate.passed)
        self.assertIn("null=1", gate.evidence["primary:orders->customers"])

    def test_declared_scale_records_the_sub_floor_surface(self) -> None:
        """This module's fixture declares every scale BELOW the divergence
        floor (see battery_task), so the gate must pass by RECORDING the
        absent surface, never by asserting a band the generator does not
        guarantee there. The asserted path is proven by construction in
        tests/test_gates_integrity.py and end-to-end by the demo replay."""
        report = self._run()
        gate = gate_by_name(report, "declared-scale-reconciliation")
        self.assertTrue(gate.passed)
        self.assertEqual(gate.evidence["scaled_assertions"], "0")
        self.assertIn("below", gate.evidence["primary:customers"])
        self.assertIn("literal rows", gate.evidence["counterfactual:customers"])


class TestRewardResultModel(unittest.TestCase):
    def test_frozen(self) -> None:
        r = RewardResult(stage1_pass=True, stage1_detail={}, mart_scores={}, reward=0.0)
        with self.assertRaises(Exception):
            r.reward = 1.0  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()

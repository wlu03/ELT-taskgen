"""Data-integrity gates (verification/gates.py): referential-integrity and
declared-scale-reconciliation, proven by construction.

WHY THIS EXISTS
Both gates certify invariants the GENERATOR currently makes true by
construction, which is precisely why nothing else would notice their loss:

  * FK pools draw from actually-generated parent rows
    (generation/source_data._generate_table), so no generated key can dangle —
    but adapter-vendored literal rows (wikidbs) and future regressions get no
    such structure, and no other gate reads a key.
  * realized_row_count moves every scaled table 2-7% off its declared scale —
    but the three-legged EL reconciliation compares three legs that all
    descend from ONE generator run, so a regression to exact realization
    keeps every leg agreeing while the extract-load answer becomes the number
    the documentation quotes.

Every claim here is asserted the way the module docstrings promise: corrupt a
materialized child FK -> RED; hand-craft frozen counts equal to a scaled
declared scale -> RED; counts outside the band on the memorization pair ->
RED; composed (stress duplicate-injection) and sub-floor surfaces RECORDED,
never asserted. The green end-to-end path runs in tests/test_gates_eval.py
(hand-built workspace) and in the demo replay (real generator output).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen import demo_fixture
from elt_taskgen.generation.source_data import (
    REALIZED_DIVERGENCE_MAX_PCT,
    REALIZED_DIVERGENCE_MIN_PCT,
    REALIZED_DIVERGENCE_MIN_SCALE,
    declares_dangling,
    realized_row_count,
)
from elt_taskgen.models import PopulationName, Relationship, TaskIR, TaskVariant
from elt_taskgen.verification import gates

P = PopulationName

#: Stress duplicate-injection fraction, kept literally in sync with
#: generation/source_data._POLICIES[PopulationName.STRESS].dup_frac — the
#: composed-surface arithmetic below exists to build a gold shaped exactly
#: like the generator's output.
_STRESS_DUP_FRAC = 0.05


class GoldStub(BaseModel):
    """Contract shape of reference.gold.GoldBundle (docs/INTERFACES.md)."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    task_content_hash: str
    stage1: dict[str, dict[str, int]]
    stage2_csv: dict[str, dict[str, str]]
    file_hashes: dict[str, str] = Field(default_factory=dict)


def generator_shaped_stage1(task: TaskIR) -> dict[str, dict[str, int]]:
    """The frozen stage-1 counts a REAL generator run would freeze.

    literal rows verbatim; scaled tables at realized_row_count; stress tables
    without a primary key composed with the +5% duplicate injection — the
    same arithmetic measured live on the demo (orders 20000 -> 19285 base ->
    20249 net).
    """
    out: dict[str, dict[str, int]] = {}
    for spec in task.populations:
        counts: dict[str, int] = {}
        for table in task.tables:
            if table.name in spec.literal_rows:
                counts[table.name] = len(spec.literal_rows[table.name])
                continue
            declared = int(spec.scale.get(table.name, 0))
            n = realized_row_count(task.task_id, table.name, declared)
            if spec.name is P.STRESS and not table.primary_key and n:
                n += int(round(n * _STRESS_DUP_FRAC))
            counts[table.name] = n
        out[spec.name.value] = counts
    return out


def build_gold(task: TaskIR, stage1: dict[str, dict[str, int]] | None = None) -> GoldStub:
    """A gold stub carrying the given stage-1 vectors (stage-2 is a dummy —
    the declared-scale gate never reads a mart)."""
    return GoldStub(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        stage1=stage1 or generator_shaped_stage1(task),
        stage2_csv={
            p.value: {demo_fixture.MART_NAME: "customer_id\n1\n"} for p in P
        },
    )


def with_count(
    stage1: dict[str, dict[str, int]], pop: P, table: str, count: int
) -> dict[str, dict[str, int]]:
    out = {k: dict(v) for k, v in stage1.items()}
    out[pop.value][table] = count
    return out


# ---------------------------------------------------------------------------
# declared-scale-reconciliation
# ---------------------------------------------------------------------------

class TestDeclaredScaleReconciliation(unittest.TestCase):
    """The demo task IS the scaled shape (primary/resampled 1000/3000/9000,
    stress 200/20000/60000, counterfactual literal, development sub-floor)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.task = demo_fixture.demo_task()
        cls.stage1 = generator_shaped_stage1(cls.task)

    def run_gate(self, stage1=None):
        return gates._gate_declared_scale_reconciliation(
            self.task, build_gold(self.task, stage1)
        )

    def test_green_on_generator_shaped_counts(self) -> None:
        gate = self.run_gate()
        self.assertTrue(gate.passed, gate.details)
        # primary(3) + resampled(3) + stress customers (the one PK table) = 7
        self.assertEqual(gate.evidence["scaled_assertions"], "7")

    def test_red_when_realized_equals_declared_on_a_scaled_table(self) -> None:
        """PROOF BY CONSTRUCTION: the regression this gate exists to catch."""
        gate = self.run_gate(with_count(self.stage1, P.PRIMARY, "customers", 1000))
        self.assertFalse(gate.passed)
        self.assertIn("EQUALS the declared scale", gate.details)

    def test_red_when_primary_lands_outside_the_band(self) -> None:
        # 1100 is +10% of 1000: diverged, but not a count realized_row_count
        # can produce (band caps at 7%).
        gate = self.run_gate(with_count(self.stage1, P.PRIMARY, "customers", 1100))
        self.assertFalse(gate.passed)
        self.assertIn("outside the guaranteed", gate.details)

    def test_band_edges_use_the_generator_integer_arithmetic(self) -> None:
        declared = 1000
        span_min = max(1, -(-declared * REALIZED_DIVERGENCE_MIN_PCT // 100))
        span_max = declared * REALIZED_DIVERGENCE_MAX_PCT // 100
        for edge in (declared - span_max, declared + span_min):
            gate = self.run_gate(
                with_count(self.stage1, P.RESAMPLED, "customers", edge)
            )
            self.assertTrue(gate.passed, f"count {edge} must sit inside the band")
        for outside in (declared - span_max - 1, declared + span_min - 1):
            gate = self.run_gate(
                with_count(self.stage1, P.RESAMPLED, "customers", outside)
            )
            self.assertFalse(gate.passed, f"count {outside} must sit outside the band")

    def test_composed_stress_surface_is_recorded_not_asserted(self) -> None:
        """Stress composes +5% duplicates onto no-PK tables, so its NET count
        can legally land back ON the declared scale (declared=1000, base draw
        -4.8% -> 952 -> +48 dup -> 1000). Asserting inequality there would be
        a fabricated invariant; the gate records the surface instead."""
        gate = self.run_gate(with_count(self.stage1, P.STRESS, "orders", 20000))
        self.assertTrue(gate.passed, gate.details)
        self.assertIn("COMPOSED", gate.evidence["stress:orders"])

    def test_stress_table_with_primary_key_must_still_diverge(self) -> None:
        # customers HAS a primary key: no duplicate injection ever lands
        # there, so the pure-jitter inequality still holds.
        gate = self.run_gate(with_count(self.stage1, P.STRESS, "customers", 200))
        self.assertFalse(gate.passed)
        self.assertIn("stress/customers", gate.details)

    def test_literal_and_sub_floor_surfaces_are_recorded(self) -> None:
        gate = self.run_gate()
        self.assertIn("literal rows", gate.evidence["counterfactual:customers"])
        # development is ungraded and never appears in the evidence keys.
        self.assertNotIn("development:customers", gate.evidence)

    def test_vacuous_surface_passes_by_recording_it(self) -> None:
        small = {
            p.name: {t: min(v, REALIZED_DIVERGENCE_MIN_SCALE - 1) for t, v in p.scale.items()}
            for p in self.task.populations
        }
        populations = tuple(
            p.model_copy(update={"scale": small[p.name]}) for p in self.task.populations
        )
        task = self.task.model_copy(update={"populations": populations})
        stage1 = {
            pop.value: {t.name: 7 for t in task.tables} for pop in P
        }
        gate = gates._gate_declared_scale_reconciliation(task, build_gold(task, stage1))
        self.assertTrue(gate.passed)
        self.assertEqual(gate.evidence["scaled_assertions"], "0")
        self.assertIn("no surface", gate.details)

    def test_fails_closed_on_missing_stage1_vector(self) -> None:
        stripped = {k: dict(v) for k, v in self.stage1.items()}
        del stripped[P.RESAMPLED.value]["orders"]
        gate = self.run_gate(stripped)
        self.assertFalse(gate.passed)
        self.assertIn("resampled", gate.details)


# ---------------------------------------------------------------------------
# referential-integrity
# ---------------------------------------------------------------------------

#: A small, fully-consistent row set (used verbatim for every population):
#: order 11 carries a NULL customer_id on the OPTIONAL customers link.
CONSISTENT_ROWS: dict[str, list[dict]] = {
    "customers": [
        {"customer_id": 1, "customer_name": "A"},
        {"customer_id": 2, "customer_name": "B"},
    ],
    "orders": [
        {"order_id": 10, "customer_id": 1, "status": "completed"},
        {"order_id": 11, "customer_id": None, "status": "completed"},
    ],
    "order_items": [
        {"order_id": 10, "quantity": 1, "unit_price": 2.0},
    ],
}


class TestReferentialIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.task = demo_fixture.demo_task()
        cls._tmp = Path(tempfile.mkdtemp(prefix="integrity-gates-"))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def workspace(
        self,
        task: TaskIR | None = None,
        override: dict[P, dict[str, list[dict]]] | None = None,
        omit: tuple[P, str] | None = None,
    ) -> Path:
        """Write rows for all five populations: CONSISTENT_ROWS everywhere,
        with per-population `override` tables and an optional omitted file."""
        task = task or self.task
        ws = Path(tempfile.mkdtemp(dir=self._tmp))
        for pop in P:
            rows_dir = ws / "tasks" / task.task_id / "populations" / pop.value / "rows"
            rows_dir.mkdir(parents=True, exist_ok=True)
            tables = dict(CONSISTENT_ROWS)
            tables.update((override or {}).get(pop, {}))
            for name, rows in tables.items():
                if omit == (pop, name):
                    continue
                (rows_dir / f"{name}.jsonl").write_text(
                    "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                    encoding="utf-8",
                )
        return ws

    def test_green_and_optional_nulls_are_counted(self) -> None:
        gate = gates._gate_referential_integrity(self.task, self.workspace())
        self.assertTrue(gate.passed, gate.details)
        self.assertIn("null=1", gate.evidence["primary:orders->customers"])

    def test_red_on_corrupted_child_fk(self) -> None:
        """PROOF BY CONSTRUCTION: corrupt one child FK -> gate RED."""
        bad = {
            P.STRESS: {
                "order_items": [{"order_id": 999, "quantity": 1, "unit_price": 2.0}]
            }
        }
        gate = gates._gate_referential_integrity(self.task, self.workspace(override=bad))
        self.assertFalse(gate.passed)
        self.assertIn("no parent row", gate.details)
        self.assertIn("stress:order_items->orders", gate.details)

    def test_red_on_null_key_over_a_required_link(self) -> None:
        bad = {
            P.PRIMARY: {
                "order_items": [{"order_id": None, "quantity": 1, "unit_price": 2.0}]
            }
        }
        gate = gates._gate_referential_integrity(self.task, self.workspace(override=bad))
        self.assertFalse(gate.passed)
        self.assertIn("REQUIRED link", gate.details)

    def test_red_on_missing_rows_artifact(self) -> None:
        gate = gates._gate_referential_integrity(
            self.task, self.workspace(omit=(P.COUNTERFACTUAL, "orders"))
        )
        self.assertFalse(gate.passed)
        self.assertIn("missing or unreadable", gate.details)

    def test_dangling_tolerated_only_where_the_conditions_declare_it(self) -> None:
        """The exact lever source_data.py reads (_DANGLING_FRAC): an optional
        link may dangle ONLY on a population whose conditions say so, and the
        count is recorded, never silent."""
        dangler = {
            P.PRIMARY: {
                "orders": CONSISTENT_ROWS["orders"]
                + [{"order_id": 12, "customer_id": 42, "status": "completed"}]
            }
        }
        undeclared = gates._gate_referential_integrity(
            self.task, self.workspace(override=dangler)
        )
        self.assertFalse(undeclared.passed)
        self.assertIn("no parent row", undeclared.details)

        populations = tuple(
            p.model_copy(
                update={
                    "conditions": p.conditions
                    + ("Some optional foreign keys are dangling by design.",)
                }
            )
            if p.name is P.PRIMARY
            else p
            for p in self.task.populations
        )
        declared_task = self.task.model_copy(update={"populations": populations})
        declared = gates._gate_referential_integrity(
            declared_task, self.workspace(task=declared_task, override=dangler)
        )
        self.assertTrue(declared.passed, declared.details)
        self.assertIn(
            "declared_dangling=1", declared.evidence["primary:orders->customers"]
        )

    def test_referential_gate_and_generator_share_the_dangling_reader(self) -> None:
        """CROSS-GROUP CONTRACT: the gate reads the dangling lever through
        source_data.declares_dangling — the SAME function the generator uses
        to mint out-of-pool keys. A NEGATED sentence ("No dangling foreign
        keys anywhere.") does not arm the generator, so it must not make the
        gate tolerate an out-of-pool key either (a substring reader would:
        fail-open by divergence); the affirmative sentence arms both."""
        dangler = {
            P.PRIMARY: {
                "orders": CONSISTENT_ROWS["orders"]
                + [{"order_id": 12, "customer_id": 42, "status": "completed"}]
            }
        }

        def with_primary_condition(sentence: str) -> TaskIR:
            populations = tuple(
                p.model_copy(update={"conditions": p.conditions + (sentence,)})
                if p.name is P.PRIMARY
                else p
                for p in self.task.populations
            )
            return self.task.model_copy(update={"populations": populations})

        negated_sentence = "No dangling foreign keys anywhere."
        affirmative_sentence = "Some optional foreign keys are dangling by design."
        # The generator's reader is the oracle for both sentences.
        self.assertFalse(declares_dangling((negated_sentence,)))
        self.assertTrue(declares_dangling((affirmative_sentence,)))
        # Sanity: the plain substring reader the gate used to have would have
        # armed on the negated sentence — that is the divergence being closed.
        self.assertIn("dangling", negated_sentence.lower())

        negated = with_primary_condition(negated_sentence)
        gate = gates._gate_referential_integrity(
            negated, self.workspace(task=negated, override=dangler)
        )
        self.assertFalse(gate.passed, gate.details)
        self.assertIn("no parent row", gate.details)
        self.assertIn(
            "declared_dangling=0", gate.evidence["primary:orders->customers"]
        )

        affirmative = with_primary_condition(affirmative_sentence)
        gate = gates._gate_referential_integrity(
            affirmative, self.workspace(task=affirmative, override=dangler)
        )
        self.assertTrue(gate.passed, gate.details)
        self.assertIn(
            "declared_dangling=1", gate.evidence["primary:orders->customers"]
        )

    def test_dangling_never_tolerated_on_a_required_link(self) -> None:
        bad = {
            P.PRIMARY: {
                "order_items": [{"order_id": 999, "quantity": 1, "unit_price": 2.0}]
            }
        }
        populations = tuple(
            p.model_copy(
                update={"conditions": p.conditions + ("dangling keys present",)}
            )
            for p in self.task.populations
        )
        task = self.task.model_copy(update={"populations": populations})
        gate = gates._gate_referential_integrity(task, self.workspace(task=task, override=bad))
        self.assertFalse(gate.passed)
        self.assertIn("order_items->orders", gate.details)

    def test_red_on_partially_null_composite_key(self) -> None:
        composite = self.task.model_copy(
            update={
                "relationships": (
                    Relationship(
                        child_table="orders",
                        child_columns=("customer_id", "status"),
                        parent_table="customers",
                        parent_columns=("customer_id", "customer_name"),
                        required=False,
                    ),
                )
            }
        )
        rows = {
            pop: {
                "orders": [
                    {"order_id": 10, "customer_id": 1, "status": "A"},
                    {"order_id": 11, "customer_id": 2, "status": None},
                ]
            }
            for pop in P
        }
        gate = gates._gate_referential_integrity(
            composite, self.workspace(task=composite, override=rows)
        )
        self.assertFalse(gate.passed)
        self.assertIn("partially-NULL composite key", gate.details)

    def test_no_relationships_is_recorded_not_waived(self) -> None:
        bare = self.task.model_copy(update={"relationships": ()})
        gate = gates._gate_referential_integrity(bare, self.workspace(task=bare))
        self.assertTrue(gate.passed)
        self.assertEqual(gate.evidence["relationships"], "0")


# ---------------------------------------------------------------------------
# Roster registration
# ---------------------------------------------------------------------------

class TestMartKeyUnique(unittest.TestCase):
    """B2 (blocker1 plan): `key_columns` was verified by NOTHING before this
    gate — the adapter key ladder asserted uniqueness, and the assertion is
    what an SQL-derived grain rescue (§C4) would silently widen. Both
    witnesses proven by construction: the T/parent witness on the frozen mart
    CSVs, the EL witness on declared SOURCE primary keys in materialized rows.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.task = demo_fixture.demo_task()
        cls._tmp = Path(tempfile.mkdtemp(prefix="key-unique-"))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def gold_with_csv(self, csv_text: str) -> GoldStub:
        return GoldStub(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage1=generator_shaped_stage1(self.task),
            stage2_csv={
                p.value: {demo_fixture.MART_NAME: csv_text} for p in P
            },
        )

    # -- parent / T witness: frozen mart CSVs -------------------------------

    def test_green_on_distinct_keys(self) -> None:
        gate = gates._gate_mart_key_unique(
            self.task, self.gold_with_csv("customer_id\n1\n2\n3\n")
        )
        self.assertTrue(gate.passed, gate.details)
        self.assertIn("verified fact", gate.details)

    def test_red_on_a_duplicated_key_tuple(self) -> None:
        gate = gates._gate_mart_key_unique(
            self.task, self.gold_with_csv("customer_id\n1\n2\n1\n")
        )
        self.assertFalse(gate.passed)
        self.assertIn("appear more than once", gate.details)
        self.assertIn("declared grain is not the produced grain", gate.details)

    def test_red_when_a_key_column_is_absent_from_the_csv(self) -> None:
        gate = gates._gate_mart_key_unique(
            self.task, self.gold_with_csv("some_other_column\n1\n")
        )
        self.assertFalse(gate.passed)
        self.assertIn("absent from the frozen CSV header", gate.details)

    def test_red_on_an_undeclared_mart_in_the_gold(self) -> None:
        gold = GoldStub(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage1=generator_shaped_stage1(self.task),
            stage2_csv={
                P.PRIMARY.value: {"phantom_mart": "customer_id\n1\n"}
            },
        )
        gate = gates._gate_mart_key_unique(self.task, gold)
        self.assertFalse(gate.passed)
        self.assertIn("the task does not declare", gate.details)

    # -- EL witness: declared source primary keys ---------------------------

    def _el_workspace(
        self, customers_rows: list[dict] | None = None, omit: bool = False
    ) -> Path:
        ws = Path(tempfile.mkdtemp(dir=self._tmp))
        rows = {
            "customers": customers_rows
            if customers_rows is not None
            else [{"customer_id": 1}, {"customer_id": 2}],
        }
        for pop in P:
            rows_dir = (
                ws / "tasks" / self.task.task_id / "populations" / pop.value / "rows"
            )
            rows_dir.mkdir(parents=True, exist_ok=True)
            for name, table_rows in rows.items():
                if omit and pop is P.STRESS:
                    continue
                (rows_dir / f"{name}.jsonl").write_text(
                    "".join(
                        json.dumps(r, sort_keys=True) + "\n" for r in table_rows
                    ),
                    encoding="utf-8",
                )
        return ws

    def test_el_green_and_unkeyed_tables_are_recorded(self) -> None:
        gate = gates._gate_el_source_key_unique(self.task, self._el_workspace())
        self.assertTrue(gate.passed, gate.details)
        # orders/order_items declare no primary_key: recorded, never skipped
        # silently.
        self.assertIn("orders", gate.evidence["tables_without_primary_key"])

    def test_el_red_on_a_duplicated_primary_key(self) -> None:
        gate = gates._gate_el_source_key_unique(
            self.task,
            self._el_workspace([{"customer_id": 1}, {"customer_id": 1}]),
        )
        self.assertFalse(gate.passed)
        self.assertIn("duplicate", gate.details)

    def test_el_red_on_a_null_inside_a_primary_key(self) -> None:
        gate = gates._gate_el_source_key_unique(
            self.task,
            self._el_workspace([{"customer_id": None}, {"customer_id": 2}]),
        )
        self.assertFalse(gate.passed)
        self.assertIn("NULL", gate.details)

    def test_el_red_on_a_missing_rows_artifact(self) -> None:
        gate = gates._gate_el_source_key_unique(
            self.task, self._el_workspace(omit=True)
        )
        self.assertFalse(gate.passed)
        self.assertIn("missing or unreadable", gate.details)


class TestRosterRegistration(unittest.TestCase):
    def test_both_gates_are_parent_gates(self) -> None:
        self.assertIn("referential-integrity", gates.GATE_NAMES)
        self.assertIn("declared-scale-reconciliation", gates.GATE_NAMES)

    def test_both_gates_apply_to_every_variant_with_a_reason(self) -> None:
        for variant in TaskVariant:
            for name in ("referential-integrity", "declared-scale-reconciliation"):
                cell = gates.variant_applicability(variant, name)
                self.assertIsNotNone(cell, f"{variant.value}/{name}: no verdict")
                self.assertIsNot(
                    cell.verdict, gates.VariantGateVerdict.NOT_APPLICABLE
                )
                self.assertTrue(cell.reason)
                self.assertIn(name, gates.VARIANT_GATE_NAMES[variant])

    def test_failures_are_task_level(self) -> None:
        """A dangling key or a divergence regression is a parent-data defect:
        it must route the TASK through repair, never reject one variant
        quietly (acceptance rule R5 default)."""
        for variant in (TaskVariant.EXTRACT_LOAD, TaskVariant.TRANSFORM):
            for name in ("referential-integrity", "declared-scale-reconciliation"):
                self.assertEqual(
                    gates.classify_variant_failure(variant, name),
                    gates.FAILURE_TASK_LEVEL,
                )


if __name__ == "__main__":
    unittest.main()

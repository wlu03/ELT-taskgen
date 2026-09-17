"""Tests for the frozen Task IR: round-trip, hash stability, hash sensitivity.

WHY THIS EXISTS
models.py is the shared language ten builders code against; these tests pin its
contract: serialization round-trips losslessly, content hashes are stable
across processes and volatile-field changes, and any semantic edit moves the
hash.
"""

import unittest

from elt_taskgen import models
from elt_taskgen.demo_fixture import (
    COUNTERFACTUAL_EXPECTED_MART,
    COUNTERFACTUAL_LITERAL_ROWS,
    demo_task,
)
from elt_taskgen.models import (
    AcceptanceReport,
    AttackCase,
    AttackKind,
    GateResult,
    MartPlan,
    PopulationName,
    RepairRoute,
    SemanticPattern,
    TaskStatus,
    derive_seed,
    task_from_json,
    task_to_json,
)


class TestRoundTrip(unittest.TestCase):
    def test_task_json_round_trip(self):
        task = demo_task()
        text = task_to_json(task)
        restored = task_from_json(text)
        self.assertEqual(task, restored)
        self.assertEqual(task_to_json(restored), text)

    def test_canonical_json_is_sorted_and_compact(self):
        import json

        task = demo_task()
        text = task.to_canonical_json()
        # Canonical form is a fixed point: parsing and canonically re-dumping
        # reproduces the exact same text (sorted keys, compact separators).
        parsed = json.loads(text)
        self.assertEqual(text, models.canonical_json(parsed))
        # Volatile fields are excluded from the canonical (hashed) form.
        self.assertNotIn("status", parsed.keys())
        self.assertNotIn("revisions", parsed.keys())
        # ...but present in the full round-trip form.
        full = json.loads(task_to_json(task))
        self.assertIn("status", full.keys())
        self.assertIn("revisions", full.keys())


class TestContentHash(unittest.TestCase):
    def test_hash_is_hex_sha256(self):
        h = demo_task().content_hash()
        self.assertEqual(len(h), 64)
        int(h, 16)  # raises if not hex

    def test_volatile_status_does_not_change_hash(self):
        task = demo_task()
        moved = task.with_status(TaskStatus.GENERATED)
        self.assertNotEqual(task.status, moved.status)
        self.assertEqual(task.content_hash(), moved.content_hash())

    def test_volatile_revisions_do_not_change_hash(self):
        task = demo_task()
        revised = task.with_revision(route=None, reason="initial")
        self.assertEqual(len(revised.revisions), 1)
        self.assertEqual(task.content_hash(), revised.content_hash())

    def test_semantic_edit_changes_hash_column_rename(self):
        task = demo_task()
        customers = task.table("customers")
        renamed_cols = tuple(
            c.model_copy(update={"name": "customer_key"}) if c.name == "customer_id" else c
            for c in customers.columns
        )
        new_customers = customers.model_copy(
            update={"columns": renamed_cols, "primary_key": ("customer_key",)}
        )
        # Keep the IR self-consistent enough for hashing purposes: drop the
        # relationship and mart references by editing only through model_copy
        # on a fresh validated construction is overkill here — hash is over
        # the dump, so compare dumps directly via a targeted single-field edit.
        edited = task.model_copy(
            update={"tables": tuple(
                new_customers if t.name == "customers" else t for t in task.tables
            )}
        )
        self.assertNotEqual(task.content_hash(), edited.content_hash())

    def test_semantic_edit_changes_hash_prompt(self):
        task = demo_task()
        edited = task.model_copy(update={"solver_prompt": "Build the mart."})
        self.assertNotEqual(task.content_hash(), edited.content_hash())

    def test_semantic_edit_changes_hash_population_seed(self):
        task = demo_task()
        pops = tuple(
            p.model_copy(update={"seed": p.seed + 1})
            if p.name == PopulationName.PRIMARY else p
            for p in task.populations
        )
        edited = task.model_copy(update={"populations": pops})
        self.assertNotEqual(task.content_hash(), edited.content_hash())


class TestMartPlanSemanticIdentity(unittest.TestCase):
    def test_empty_defaults_are_absent_and_hash_neutral(self):
        task = demo_task()
        plan = task.marts[0].plan
        plan_data = plan.model_dump(mode="json")
        nested_data = task.model_dump(mode="json")["marts"][0]["plan"]

        for data in (plan_data, nested_data):
            self.assertNotIn("template_id", data)
            self.assertNotIn("semantic_patterns", data)

        reparsed = models.TaskIR.model_validate(task.model_dump(mode="json"))
        self.assertEqual(reparsed.content_hash(), task.content_hash())

    def test_declared_identity_round_trips_and_moves_hash(self):
        task = demo_task()
        old_plan = task.marts[0].plan
        new_plan = MartPlan(
            mart=old_plan.mart,
            ops=old_plan.ops,
            notes=old_plan.notes,
            template_id="nested_aggregate_having",
            semantic_patterns=(
                SemanticPattern.AGGREGATE_THEN_EXTREMA,
                SemanticPattern.NESTED_AGGREGATE,
            ),
        )
        new_mart = task.marts[0].model_copy(update={"plan": new_plan})
        edited = task.model_copy(update={"marts": (new_mart,)})

        payload = new_plan.model_dump(mode="json")
        self.assertEqual(payload["template_id"], "nested_aggregate_having")
        self.assertEqual(
            payload["semantic_patterns"],
            ["aggregate_then_extrema", "nested_aggregate"],
        )
        self.assertEqual(MartPlan.model_validate(payload), new_plan)
        self.assertTrue(new_plan.declares_pattern(SemanticPattern.NESTED_AGGREGATE))
        self.assertFalse(new_plan.declares_pattern(SemanticPattern.PIVOT))
        self.assertNotEqual(edited.content_hash(), task.content_hash())

    def test_template_id_is_closed_to_lower_snake_case(self):
        plan = demo_task().marts[0].plan
        for bad in ("NestedAggregate", "nested-aggregate", "_nested", "1_nested"):
            with self.assertRaises(Exception, msg=bad):
                MartPlan(
                    mart=plan.mart,
                    ops=plan.ops,
                    template_id=bad,
                )

    def test_semantic_patterns_must_be_unique_and_canonical(self):
        plan = demo_task().marts[0].plan
        for bad in (
            (
                SemanticPattern.NESTED_AGGREGATE,
                SemanticPattern.AGGREGATE_THEN_EXTREMA,
            ),
            (
                SemanticPattern.NESTED_AGGREGATE,
                SemanticPattern.NESTED_AGGREGATE,
            ),
        ):
            with self.assertRaises(Exception, msg=bad):
                MartPlan(
                    mart=plan.mart,
                    ops=plan.ops,
                    semantic_patterns=bad,
                )

class TestValidators(unittest.TestCase):
    def test_family_id_must_be_namespaced(self):
        task = demo_task()
        for bad in ("nonamespace", "Upper__case", "__leading", "trailing__"):
            with self.assertRaises(Exception, msg=bad):
                models.TaskIR.model_validate(
                    {**task.model_dump(mode="json"), "family_id": bad}
                )

    def test_every_table_needs_a_backend(self):
        task = demo_task()
        data = task.model_dump(mode="json")
        data["backends"] = data["backends"][:-1]
        with self.assertRaises(Exception):
            models.TaskIR.model_validate(data)

    def test_duplicate_backend_assignment_rejected(self):
        task = demo_task()
        data = task.model_dump(mode="json")
        data["backends"] = data["backends"] + [data["backends"][0]]
        with self.assertRaises(Exception):
            models.TaskIR.model_validate(data)

    def test_required_attack_must_lose_somewhere(self):
        with self.assertRaises(Exception):
            AttackCase(
                name="toothless",
                kind=AttackKind.CONSTANTS,
                description="never expected to lose",
                expected_pass={PopulationName.PRIMARY: True},
                required=True,
            )

    def test_proposed_case_refuses_non_finite_params(self):
        """Review finding 1-1 (batch-repair round 2): a proposal whose params
        carried `NaN` / `Infinity` validated, and every consumer then hashed
        it through `canonical_json(allow_nan=False)` — a bare ValueError out
        of the runner's submit, a harness fault for a model payload.  The
        model refuses the non-finite float itself, so the same proposal is a
        schema problem wherever it is built; finite floats, ints, booleans,
        strings, None and identifier tuples are untouched."""
        matrix = {p: True for p in PopulationName}
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(bad)), self.assertRaises(Exception) as caught:
                models.ProposedAttackCase(
                    kind=AttackKind.INNER_JOIN, params={"variant": bad},
                    expected_pass=matrix, rationale="r",
                )
            self.assertIn("non-finite", str(caught.exception))
        fine = models.ProposedAttackCase(
            kind=AttackKind.SKIP_EXTRACTION,
            params={"ratio": 0.5, "n": 3, "flag": True, "name": "x", "none": None,
                    "skip_tables": ("customers", "orders")},
            expected_pass=matrix, rationale="r",
        )
        self.assertEqual(fine.params["ratio"], 0.5)
        models.canonical_json(fine.model_dump(mode="json"))

    def test_acceptance_report_fail_closed(self):
        good = GateResult(gate="trusted-solution", passed=True)
        bad = GateResult(gate="required-mutants", passed=False)
        h = "0" * 64
        with self.assertRaises(Exception):
            AcceptanceReport(
                task_id="t", revision=1, task_content_hash=h,
                gates=(good, bad), accepted=True, scorer_version="1",
            )
        with self.assertRaises(Exception):
            AcceptanceReport(
                task_id="t", revision=1, task_content_hash=h,
                gates=(), accepted=True, scorer_version="1",
            )
        report = AcceptanceReport.from_gates(
            task_id="t", revision=1, task_content_hash=h,
            gates=(good, bad), scorer_version="1",
        )
        self.assertFalse(report.accepted)
        report_ok = AcceptanceReport.from_gates(
            task_id="t", revision=1, task_content_hash=h,
            gates=(good,), scorer_version="1",
        )
        self.assertTrue(report_ok.accepted)


class TestHelpers(unittest.TestCase):
    def test_derive_seed_deterministic_and_distinct(self):
        a = derive_seed("task", "primary", "orders", "order_id")
        b = derive_seed("task", "primary", "orders", "order_id")
        c = derive_seed("task", "primary", "orders", "customer_id")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertGreaterEqual(a, 0)

    def test_revision_lineage_links_hashes(self):
        task = demo_task()
        r1 = task.with_revision(route=None, reason="initial")
        r2 = r1.with_revision(route=RepairRoute.SPECIFICATION, reason="prose fix")
        self.assertEqual(r2.revisions[1].parent_content_hash, r1.revisions[0].content_hash)
        self.assertEqual(r2.current_revision, 2)


class TestDemoFixture(unittest.TestCase):
    def test_demo_task_validates_and_is_complete(self):
        task = demo_task()
        self.assertTrue(task.has_all_populations())
        self.assertEqual(len(task.marts), 1)
        self.assertIn("customer_summary", task.reference.sql_by_mart)
        # 4 hand-authored transform mutants + the 8-case extract-load
        # catalogue (partial_backend, duplicate_on_load, truncate_table,
        # header_as_row, null_row_drop, the fabricate_counts pair,
        # stale_snapshot).
        self.assertEqual(len(task.attack_cases), 12)
        load_cases = [
            c for c in task.attack_cases if c.mutation.startswith("directive:load:")
        ]
        self.assertEqual(len(load_cases), 8)

    def test_counterfactual_arithmetic_is_the_spec_case(self):
        items = COUNTERFACTUAL_LITERAL_ROWS["order_items"]
        c11_total = sum(
            r["quantity"] * r["unit_price"] for r in items if r["order_id"] == 1101
        )
        self.assertEqual(c11_total, 45.0)
        expected = {r["customer_id"]: r for r in COUNTERFACTUAL_EXPECTED_MART}
        self.assertEqual(expected[10]["completed_order_count"], 0)
        self.assertEqual(expected[11]["completed_order_count"], 1)
        self.assertEqual(expected[11]["total_spend"], 45.0)
        self.assertEqual(expected[12]["total_spend"], 0.0)

    def test_counterfactual_population_carries_literal_rows(self):
        task = demo_task()
        cf = task.population(PopulationName.COUNTERFACTUAL)
        self.assertEqual(cf.literal_rows, COUNTERFACTUAL_LITERAL_ROWS)


if __name__ == "__main__":
    unittest.main()

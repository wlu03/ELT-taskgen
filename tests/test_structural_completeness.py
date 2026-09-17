"""Unit tests of the deterministic structural-completeness gate.

WHY THIS FILE EXISTS — READ THIS BEFORE READING A FEASIBILITY SCORE
This file is where two metrology specimens WENT. `feasibility-missing-customers`
(a whole source table deleted while the mart's per-customer grain still demands
it) and `feasibility-missing-order-key` (a join/dedup key deleted while the
rules still join and deduplicate on it) used to be scored PROBABILISTICALLY, by
asking the feasibility_reviewer seat to notice them; measured at seed
8944342589527049266 they scored 0.4 and 0.2 respectively even after the prompt
was stuffed with a ROW UNIVERSE pass and a STRUCTURAL SWEEP pass — and that
stuffing REGRESSED `feasibility-missing-status` from 1.0 to 0.6.

"Every table, key and relationship the rules reference must be present in the
published source schemas, and the schema block must agree with itself" is
static analysis, not judgment. The coverage therefore moved from a critic to a
compiler: the two specimens are retired from
`review/metrology._FEASIBILITY_VARIANTS` and are asserted HERE instead, as
tests of `verification/structural_completeness.py`, which refuses such a task
BEFORE any live council call is made.

DO NOT read a future feasibility_reviewer PASS as the seat having improved on
this class. It is no longer measured on this class in production, because
production never presents it: the gate refuses first.
"""

from __future__ import annotations

import glob
import json
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen import demo_fixture
from elt_taskgen.models import task_from_json
from elt_taskgen.review import metrology as metrology_mod
from elt_taskgen.verification.structural_completeness import (
    GATE_NAME,
    check_structural_completeness,
    structural_completeness_gate,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _drop_table(name: str):
    return metrology_mod._drop_table(demo_fixture.demo_task(), name)


def _drop_column(table: str, column: str):
    return metrology_mod._drop_column(demo_fixture.demo_task(), table, column)


class TestCleanTasksPass(unittest.TestCase):
    """The gate must be green on every task the pipeline has ever produced.

    A gate that is red on the system's own fixtures is a gate nobody turns on —
    which is exactly why `generation/mart_plan.validate_plan` (which carries the
    same reference-resolution rules PLUS the compiler-grade structural contract)
    could not be used here: it reports 5 problems on the hand-authored demo plan.
    """

    def test_demo_task_is_structurally_complete(self):
        self.assertEqual(check_structural_completeness(demo_fixture.demo_task()), [])

    def test_gate_result_is_green_on_the_demo(self):
        result = structural_completeness_gate(demo_fixture.demo_task())
        self.assertTrue(result.passed)
        self.assertEqual(result.gate, GATE_NAME)
        self.assertEqual(result.evidence["problem_count"], "0")

    def test_every_task_ir_on_disk_passes(self):
        """Every pool task IR still on disk — the five canonical releases,
        one per pool (dbt, dlt, schemapile, synsql, wikidbs).

        These are the plans the BUILDERS emit — intermediate relations bound by
        `details['name']`, joins onto opaque intermediates, 17-way unions — and
        they are what proves the gate's resolution rule is the real one and not
        a rule that only the demo satisfies.
        """
        paths = sorted(
            glob.glob(
                str(REPO_ROOT / "runs" / "**" / "task_ir.json"),
                recursive=True,
            )
        )
        if not paths:
            self.skipTest("no end-to-end workspaces on disk")
        for path in paths:
            task = task_from_json(Path(path).read_text())
            with self.subTest(task=task.task_id):
                self.assertEqual(check_structural_completeness(task), [])

    def test_check_is_a_pure_function(self):
        """Purity is only observable on a task that yields problems: on a clean
        task [] == [] can never fail. Same task object twice — a checker that
        mutated nested state, or ordered problems nondeterministically, would
        make the second full problem list differ."""
        task = _drop_table("orders")
        first = check_structural_completeness(task)
        self.assertGreater(len(first), 1)
        self.assertEqual(first, check_structural_completeness(task))


class TestDeclaredKindsAreCertified(unittest.TestCase):
    """Check G: a declared MartColumn.kind the plan's ops do not produce is
    refused by the one pre-provider gate in production; UNDECLARED kinds
    (legacy marts, the demo fixture) stay tolerated."""

    @staticmethod
    def _released_task_with_kinds():
        from elt_taskgen.models import MartColumnKind

        for path in sorted(
            glob.glob(str(REPO_ROOT / "runs" / "**" / "task_ir.json"), recursive=True)
        ):
            task = task_from_json(Path(path).read_text())
            for mart in task.marts:
                if any(c.kind is MartColumnKind.PASSTHROUGH for c in mart.columns):
                    return task, mart
        return None, None

    def test_declared_kind_disagreeing_with_the_plan_is_refused(self):
        from elt_taskgen.models import MartColumnKind

        task, mart = self._released_task_with_kinds()
        if task is None:
            self.skipTest("no on-disk task declares column kinds")
        victim = next(c for c in mart.columns if c.kind is MartColumnKind.PASSTHROUGH)
        tampered = mart.model_copy(
            update={
                "columns": tuple(
                    c.model_copy(update={"kind": MartColumnKind.RANKED})
                    if c.name == victim.name
                    else c
                    for c in mart.columns
                )
            }
        )
        task = task.model_copy(
            update={"marts": tuple(tampered if m.name == mart.name else m for m in task.marts)}
        )
        problems = check_structural_completeness(task)
        self.assertTrue(problems, "the tampered kind was accepted")
        hit = [p for p in problems if "declared kind" in p]
        self.assertTrue(hit, problems)
        self.assertIn(mart.name, hit[0])
        self.assertIn(victim.name, hit[0])
        self.assertFalse(structural_completeness_gate(task).passed)

    def test_undeclared_kinds_stay_tolerated(self):
        task = demo_fixture.demo_task()
        self.assertTrue(
            any(c.kind is None for m in task.marts for c in m.columns),
            "the demo fixture is expected to carry undeclared kinds",
        )
        self.assertEqual(check_structural_completeness(task), [])


class TestRetiredStructuralSpecimens(unittest.TestCase):
    """THE COVERAGE THAT MOVED. These two were metrology specimens.

    Each asserts the same defect the specimen planted, at the same fixture, with
    the gate naming the missing object AND the op that referenced it — the
    diagnostic a critic's prose finding could never guarantee.
    """

    def test_missing_customers_table_is_refused(self):
        """WAS `feasibility-missing-customers` (measured 0.4/1.0 by the seat).

        The whole customers table is gone while the mart's grain is one row per
        customer. The row universe cannot be built even though every measure
        still has its inputs — and the plan says so out loud, because the join
        and the group-by both NAME `customers`.
        """
        problems = check_structural_completeness(_drop_table("customers"))
        self.assertTrue(problems)
        joined = " ".join(problems)
        self.assertIn("'customers'", joined)
        self.assertIn("op[3] (join)", joined)
        self.assertIn("published source schemas do not contain", joined)

    def test_missing_order_key_column_is_refused(self):
        """WAS `feasibility-missing-order-key` (measured 0.2/1.0 by the seat).

        `orders.order_id` is gone while the rules still deduplicate order
        headers on it and still join item lines to it.
        """
        problems = check_structural_completeness(_drop_column("orders", "order_id"))
        self.assertTrue(problems)
        joined = " ".join(problems)
        self.assertIn("order_id", joined)
        self.assertIn("op[1] (dedupe)", joined)
        self.assertIn("resolve to nothing", joined)

    def test_the_refusal_names_where_the_object_was_referenced(self):
        """Fail closed WITH a location: a message that names only the object
        sends a human back to re-derive which rule needed it."""
        for problem in check_structural_completeness(_drop_table("customers")):
            self.assertIn("mart 'customer_summary' plan op[", problem)


class TestEveryFeasibilityTamperIsCaught(unittest.TestCase):
    """The gate's reach is WIDER than the two specimens that moved here.

    All seven `_FEASIBILITY_VARIANTS` tampers are deterministically refused —
    including the five that REMAIN in the metrology pool. Recorded so nobody
    reads the remaining pool as measuring a class production still depends on a
    critic for; it does not. The five stay only because the pool must exceed its
    draw by POOL_HOLDOUT and because the seat's own recall is still worth
    knowing.
    """

    def test_all_seven_schema_deletions_are_refused(self):
        base = demo_fixture.demo_task()
        for name, table, column, _description, _anchors in (
            metrology_mod._FEASIBILITY_VARIANTS
            + metrology_mod._RETIRED_STRUCTURAL_VARIANTS
        ):
            tampered = (
                metrology_mod._drop_table(base, table)
                if column is None
                else metrology_mod._drop_column(base, table, column)
            )
            with self.subTest(specimen=name):
                self.assertTrue(
                    check_structural_completeness(tampered),
                    f"{name} is a deletion the gate must refuse",
                )

    def test_dropping_any_referenced_column_is_refused(self):
        """Exhaustive by construction, not by specimen list: EVERY column of
        EVERY demo source table that the plan or a key or a relationship
        references must make the gate red when deleted."""
        base = demo_fixture.demo_task()
        referenced = {
            (op_table, column)
            for mart in base.marts
            for op in mart.plan.ops
            for op_table in op.tables
            for column in op.columns
        }
        checked = 0
        for table in base.tables:
            for column in table.columns:
                if (table.name, column.name) not in referenced:
                    continue
                checked += 1
                with self.subTest(table=table.name, column=column.name):
                    self.assertTrue(
                        check_structural_completeness(
                            metrology_mod._drop_column(base, table.name, column.name)
                        )
                    )
        self.assertGreaterEqual(checked, 5)

    def test_dropping_any_table_is_refused(self):
        base = demo_fixture.demo_task()
        for table in base.tables:
            with self.subTest(table=table.name):
                self.assertTrue(
                    check_structural_completeness(
                        metrology_mod._drop_table(base, table.name)
                    )
                )


class TestNoFalseAlarms(unittest.TestCase):
    """The gate must be SILENT on every non-schema tamper in the pool.

    Ambiguity, population and shortcut specimens all ship a complete schema; a
    gate that fired on them would refuse tasks whose only defect is prose, at
    every surface variant. This is the false-alarm axis, run over the whole
    pool x every wording.
    """

    def test_only_feasibility_specimens_fire(self):
        for specimen in metrology_mod.specimen_pool():
            for variant in range(metrology_mod.PROSE_VARIANTS):
                problems = check_structural_completeness(specimen.task_at(variant))
                with self.subTest(specimen=specimen.name, variant=variant):
                    if specimen.name.startswith("feasibility-"):
                        continue
                    self.assertEqual(
                        problems,
                        [],
                        f"{specimen.name} v{variant} is not a schema defect",
                    )


class TestPublishedBlockSelfConsistency(unittest.TestCase):
    """A published source-schema block must not name a column it does not list.

    THE EXACT CONTRADICTION `feasibility-missing-order-key` ONCE EXHIBITED.
    Before `metrology._revalidated` existed, that tamper deleted
    `orders.order_id` from `TableSpec.columns` alone and left
    `business_key=('order_id',)` and the `order_items(order_id) -> orders
    (order_id)` relationship standing, so documentation.md republished the
    identifier twice in the very block the feasibility seat is told is
    authoritative — and the seat correctly scored 0.0, because it read the block
    and found the key it was told was missing.

    Pydantic now makes that IR unconstructible, so the defect is injected where
    it would actually reappear: in the RENDERER. The gate parses the shipped
    bytes, so a block that drifts from the model is caught even though the model
    is sound.
    """

    def _with_rendered_block(self, lines):
        return mock.patch(
            "elt_taskgen.export.eltbench._source_schema_markdown",
            return_value=lines,
        )

    def test_key_over_an_unlisted_column_is_a_contradiction(self):
        task = demo_fixture.demo_task()
        from elt_taskgen.export.eltbench import _source_schema_markdown

        lines = [
            line
            for line in _source_schema_markdown(task)
            if not line.startswith("- `order_id`")
        ]
        with self._with_rendered_block(lines):
            problems = check_structural_completeness(task)
        joined = " ".join(problems)
        self.assertIn("contradicts itself", joined)
        self.assertIn("order_id", joined)

    def test_relationship_over_an_unlisted_table_is_a_contradiction(self):
        task = demo_fixture.demo_task()
        from elt_taskgen.export.eltbench import _source_schema_markdown

        rendered = _source_schema_markdown(task)
        lines = [
            line
            for line in rendered
            if not line.startswith("### customers")
        ]
        with self._with_rendered_block(lines):
            problems = check_structural_completeness(task)
        joined = " ".join(problems)
        self.assertIn("contradicts itself", joined)

    def test_a_dropped_column_line_disagrees_with_the_csv(self):
        """The two shipped files must agree with each other, not merely each
        with itself."""
        task = demo_fixture.demo_task()
        from elt_taskgen.export.eltbench import _source_schema_markdown

        lines = [
            line
            for line in _source_schema_markdown(task)
            if not line.startswith("- `unit_price`")
        ]
        with self._with_rendered_block(lines):
            problems = check_structural_completeness(task)
        self.assertTrue(
            any("but the task declares" in p for p in problems), problems
        )


class TestGateRunsBeforeLiveSpend(unittest.TestCase):
    """The whole point of moving the check into code: a structurally incomplete
    task is refused WITHOUT paying four critics to notice.

    Both live-spend stages (`author` and `review`) consult the gate before they
    consult the provider, so the provider below — which raises on any call —
    must never be reached.
    """

    class ExplodingProvider:
        """Any provider call is a budget spend; here it is a test failure."""

        def complete(self, role, prompt):  # pragma: no cover - must not run
            raise AssertionError(
                f"provider was called for role {role!r}: the structural gate "
                "did not refuse before live spend"
            )

    def test_review_stage_refuses_without_calling_the_provider(self):
        from elt_taskgen import cli
        from elt_taskgen.models import RepairRoute

        run_review = cli.make_review_runner(self.ExplodingProvider())
        outcome = run_review(None, _drop_table("customers"))
        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertIn(GATE_NAME, outcome.payload.detail)
        self.assertIn("customers", outcome.payload.detail)
        self.assertEqual(outcome.route, RepairRoute.SPECIFICATION)

    def test_author_stage_refuses_without_calling_the_provider(self):
        from elt_taskgen import cli
        from elt_taskgen.models import RepairRoute

        run_author = cli.make_author_runner(self.ExplodingProvider())
        outcome = run_author(None, _drop_column("orders", "order_id"))
        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertIn(GATE_NAME, outcome.payload.error)
        self.assertIn("order_id", outcome.payload.error)
        self.assertEqual(outcome.route, RepairRoute.SPECIFICATION)

    class CannedFindingsProvider:
        """Protocol-valid stub (transport-free), same shape as the one
        `tests/test_cli.py` uses to exercise stage wiring."""

        def complete(self, role, prompt):
            populations = (
                "development",
                "primary",
                "resampled",
                "counterfactual",
                "stress",
            )
            return json.dumps(
                {
                    "findings": [
                        {
                            "severity": "major",
                            "summary": "constants shortcut must lose reward",
                            "detail": "compile a constants mutant",
                            "route_hint": None,
                            "suggested_attack": "constants",
                            "proposed_case": {
                                "kind": "constants",
                                "params": "{}",
                                "expected_pass_by_stage": {
                                    "extract_load": {
                                        name: True for name in populations
                                    },
                                    "transform": {
                                        name: False for name in populations
                                    },
                                },
                                "rationale": (
                                    "the constants mutant is an explicit "
                                    "executable shortcut probe"
                                ),
                            },
                        }
                    ]
                }
            )

    def test_a_complete_task_still_reaches_the_provider(self):
        """Fail-closed must not mean fail-always: the demo passes the gate and
        the review stage proceeds to the council as before."""
        from elt_taskgen import cli

        run_review = cli.make_review_runner(self.CannedFindingsProvider())
        outcome = run_review(None, demo_fixture.demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)


if __name__ == "__main__":
    unittest.main()

"""Tests for admitting `populations.*.literal_rows` to the POPULATION route.

WHY THIS EXISTS
docs/runs/schemapile.md recorded a live repair_proposer ABSTAINING twice on
"counterfactual population has no literal rows and the task declares no attack
cases to target", correctly quoting `ROUTE_IR_PATHS[POPULATION]` and refusing to
convert the counterfactual into a generated population to make the check
smaller. That abstention is the anti-reward-hack boundary working, so widening
the allowlist is the exact move the proposer refused to make.

The field is widened anyway — it is the only field that can repair that failure
class, and it is population data by any reading — but ONLY behind a mechanical
guard, because the counterfactual's rows are not ordinary data: they are the
DISCRIMINATOR the attack battery measures against. These tests pin the guard:

  * the allowlist now admits literal_rows at table AND cell granularity, and
    still admits it to no other route (and `attack_cases` to none at all);
  * a patch that keeps every mutant losing reward where it did commits;
  * a patch that makes a mutant stop losing reward is REJECTED even though the
    re-validation went fully green — a green re-validation only says the gates
    passed against the PATCHED rows;
  * with nothing discriminating beforehand the field is closed entirely: that
    is the schemapile case, and it must stay an abstention rather than become
    a way to satisfy the coverage check's precondition;
  * a rejected patch leaves the workspace byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from elt_taskgen import repair
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.engine import (
    Engine,
    StageOutcome,
    StagePayload,
    VERDICT_FAIL,
    VERDICT_PASS,
)
from elt_taskgen.models import (
    PopulationName,
    RepairEdit,
    RepairEditOp,
    RepairPatch,
    RepairRoute,
)
from elt_taskgen.review import repair_proposer as rp

CASE = "inner_join"


def tree_hash(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(root).rglob("*"))
        if p.is_file()
        # SQLite's WAL shared-memory file is a volatile reader-lock page. A
        # consistent read may move its bytes without changing the ledger; the
        # equivalent byte-identity helper in test_repair_proposer excludes it
        # for the same reason.
        and p.relative_to(root).as_posix() != "state/taskgen.sqlite-shm"
    }


# ---------------------------------------------------------------------------
# 1. The allowlist itself
# ---------------------------------------------------------------------------

class TestAllowlistScope(unittest.TestCase):
    def allowed(self, path: str, route: RepairRoute) -> bool:
        return rp._ir_path_allowed(path, route)

    def test_literal_rows_admitted_to_population_at_every_granularity(self) -> None:
        for path in (
            "populations.3.literal_rows",
            "populations.3.literal_rows.orders",
            "populations.3.literal_rows.orders.0.status",
        ):
            self.assertTrue(
                self.allowed(path, RepairRoute.POPULATION), path
            )

    def test_literal_rows_admitted_to_no_other_route(self) -> None:
        for route in (
            RepairRoute.SPECIFICATION,
            RepairRoute.REFERENCE,
            RepairRoute.RUNTIME,
        ):
            self.assertFalse(
                self.allowed("populations.3.literal_rows.orders.0.status", route),
                route.value,
            )

    def test_attack_cases_remain_in_no_route(self) -> None:
        for route in RepairRoute:
            self.assertFalse(self.allowed("attack_cases.0.mutation", route))
            self.assertFalse(self.allowed("attack_cases", route))

    def test_double_star_needs_at_least_one_segment(self) -> None:
        # "populations.*.literal_rows.**" must not also match the two-segment
        # prefix on its own (that is what the plain pattern is for).
        self.assertFalse(self.allowed("populations.3", RepairRoute.POPULATION))
        self.assertFalse(self.allowed("populations", RepairRoute.POPULATION))
        self.assertFalse(self.allowed("status", RepairRoute.POPULATION))
        self.assertFalse(self.allowed("revisions.0.route", RepairRoute.POPULATION))


# ---------------------------------------------------------------------------
# 2. The discrimination matrix, as pure functions
# ---------------------------------------------------------------------------

class TestDiscriminationMatrix(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)
        self.task_id = "t"

    def write(self, case: str, rewards: dict[str, float], content_hash="h") -> None:
        d = self.ws / "tasks" / self.task_id / "attacks" / case
        d.mkdir(parents=True, exist_ok=True)
        (d / repair.ATTACK_REWARDS_FILENAME).write_text(
            json.dumps(
                {"case": case, "rewards": rewards, "task_content_hash": content_hash},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def test_matrix_records_only_populations_that_lost_reward(self) -> None:
        self.write("a", {"primary": 1.0, "counterfactual": 0.0, "stress": 0.5})
        matrix = repair.discrimination_matrix(
            self.ws, self.task_id, task_content_hash="h"
        )
        self.assertEqual(matrix, {"a": frozenset({"counterfactual", "stress"})})

    def test_stale_records_are_not_evidence(self) -> None:
        self.write("a", {"counterfactual": 0.0}, content_hash="OLD")
        self.assertEqual(
            repair.discrimination_matrix(self.ws, self.task_id, task_content_hash="h"),
            {},
        )

    def test_losing_a_cell_is_a_problem(self) -> None:
        before = {"a": frozenset({"counterfactual", "stress"})}
        after = {"a": frozenset({"stress"})}
        problems = repair.discrimination_problems(before, after)
        self.assertEqual(len(problems), 1)
        self.assertIn("counterfactual", problems[0])
        self.assertIn("weakened", problems[0])

    def test_gaining_a_cell_is_allowed(self) -> None:
        before = {"a": frozenset({"stress"})}
        after = {"a": frozenset({"stress", "counterfactual"}), "b": frozenset({"x"})}
        self.assertEqual(repair.discrimination_problems(before, after), [])

    def test_a_vanished_case_is_a_deleted_test(self) -> None:
        problems = repair.discrimination_problems({"a": frozenset({"x"})}, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("deleted mutant is a deleted test", problems[0])

    def test_nothing_discriminating_before_is_a_rejection_not_a_pass(self) -> None:
        # THE schemapile case: with no attack cases the counterfactual has
        # nothing to be counterfactual to, so rows there repair nothing.
        for before in ({}, {"a": frozenset()}):
            problems = repair.discrimination_problems(before, {"a": frozenset({"x"})})
            self.assertEqual(len(problems), 1, before)
            self.assertIn("cannot be repairing anything", problems[0])

    def test_counterfactual_row_deletion_is_rejected_addition_is_not(self) -> None:
        task = demo_task()
        counts = repair.counterfactual_row_counts(task)
        self.assertEqual(counts, {"customers": 3, "orders": 2, "order_items": 4})
        self.assertEqual(repair.counterfactual_row_problems(counts, counts), [])
        fewer = dict(counts, orders=1)
        problems = repair.counterfactual_row_problems(counts, fewer)
        self.assertEqual(len(problems), 1)
        self.assertIn("never delete them", problems[0])
        more = dict(counts, orders=9)
        self.assertEqual(repair.counterfactual_row_problems(counts, more), [])


# ---------------------------------------------------------------------------
# 3. End to end through attempt_patch
# ---------------------------------------------------------------------------

def literal_rows_patch(
    old: str = "C10",
    new: str = "Acme Holdings",
    locator: str = "populations.3.literal_rows.customers.0.customer_name",
) -> RepairPatch:
    """Edit one counterfactual literal cell — an in-allowlist POPULATION patch.

    The default target is a NAME, not the `status` the mutant is discriminated
    by: this is the legitimate shape of a counterfactual repair.
    """
    return RepairPatch(
        route=RepairRoute.POPULATION,
        artifact="task_ir.json",
        edits=(
            RepairEdit(
                op=RepairEditOp.REPLACE, locator=locator, old=old, new=new
            ),
        ),
        rationale="the counterfactual carries placeholder identifiers",
        proposer_role=rp.ROLE_NAME,
    )


class TestLiteralRowsGuard(unittest.TestCase):
    """The demo counterfactual's `orders` rows carry a 'completed' status; the
    inner_join mutant loses reward there precisely because those rows exist in
    the shape they do. A fixture `attack` runner models that dependency
    faithfully: change the status away from 'completed' and the mutant stops
    being discriminated."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "taskgen-workspace"
        self.task = demo_task()
        self.task_id = self.task.task_id
        self.attack_runs = 0

    # -- fixture stage runners --------------------------------------------

    def _counterfactual(self, task):
        for pop in task.populations:
            if pop.name is PopulationName.COUNTERFACTUAL:
                return pop
        raise AssertionError("demo task has no counterfactual population")

    def _write_rewards(
        self, workspace: Path, task, discriminates: bool, *, case: str = CASE
    ) -> None:
        d = Path(workspace) / "tasks" / self.task_id / "attacks" / case
        d.mkdir(parents=True, exist_ok=True)
        (d / repair.ATTACK_REWARDS_FILENAME).write_text(
            json.dumps(
                {
                    "case": case,
                    "rewards": {
                        "primary": 1.0,
                        "counterfactual": 0.0 if discriminates else 1.0,
                    },
                    "task_content_hash": task.content_hash(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def attack_runner(self, engine, task):
        """Re-measures the mutant against the CURRENT counterfactual rows.

        Reads the MATERIALIZED rows when they exist and the IR otherwise —
        which is what `verification.attacks.run_attack` really does (it loads
        population sources from disk), and is what makes both doors into the
        counterfactual observable through the same matrix."""
        self.attack_runs += 1
        materialized = (
            Path(engine.workspace) / "tasks" / self.task_id / "populations"
            / "counterfactual" / "rows" / "orders.jsonl"
        )
        if materialized.is_file():
            rows = [
                json.loads(ln)
                for ln in materialized.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
        else:
            rows = list(self._counterfactual(task).literal_rows.get("orders", ()))
        discriminates = any(r.get("status") == "completed" for r in rows)
        self._write_rewards(engine.workspace, task, discriminates)
        return StageOutcome(VERDICT_PASS, StagePayload(detail="attack ok"))

    def pass_runner(self, engine, task):
        return StageOutcome(VERDICT_PASS, StagePayload(detail="ok"))

    def generate_runner(self, engine, task):
        """Green only once the placeholder customer name is gone — something a
        patch must genuinely fix to be allowed to commit, and something that
        does NOT touch the row shape the mutant is discriminated by."""
        rows = self._counterfactual(task).literal_rows.get("customers", ())
        if rows and rows[0].get("customer_name") != "C10":
            return StageOutcome(VERDICT_PASS, StagePayload(detail="coverage ok"))
        return StageOutcome(
            VERDICT_FAIL, StagePayload(error="population coverage: placeholder rows")
        )

    def make_engine(self, *, generate=None) -> Engine:
        engine = Engine(
            self.workspace,
            stage_runners={
                "generate": generate or self.generate_runner,
                "reference": self.pass_runner,
                "attack": self.attack_runner,
            },
            max_repair_rounds=0,
        )
        self.addCleanup(engine.close)
        engine.register(self.task)
        return engine

    def seed_baseline(self, engine, discriminates: bool = True) -> None:
        task = engine.load_task(self.task_id)
        self._write_rewards(self.workspace, task, discriminates)

    # -- the tests ---------------------------------------------------------

    def test_patch_that_preserves_discrimination_commits(self) -> None:
        engine = self.make_engine()
        self.seed_baseline(engine)
        task = engine.load_task(self.task_id)
        committed = rp.attempt_patch(engine, task, "generate", literal_rows_patch())
        self.assertIsNotNone(committed)
        reloaded = self._counterfactual(engine.load_task(self.task_id))
        self.assertEqual(
            reloaded.literal_rows["customers"][0]["customer_name"], "Acme Holdings"
        )
        # `attack` re-ran on the trial even though the failure was at
        # `generate`: without it there would be no "after" matrix at all.
        self.assertGreaterEqual(self.attack_runs, 1)

    def test_transient_finding_probe_is_not_treated_as_a_deleted_test(self) -> None:
        """Only TaskIR attack cases are durable across a population repair."""
        engine = self.make_engine()
        self.seed_baseline(engine)
        task = engine.load_task(self.task_id)
        transient = "finding__population_adversary-00-oldhash"
        self._write_rewards(
            self.workspace, task, True, case=transient
        )
        raw = repair.discrimination_matrix(
            self.workspace, self.task_id, task_content_hash=task.content_hash()
        )
        self.assertIn(transient, raw)
        self.assertNotIn(
            transient, rp._durable_discrimination_matrix(self.workspace, task)
        )

        committed = rp.attempt_patch(
            engine, task, "generate", literal_rows_patch()
        )

        self.assertIsNotNone(committed)
        self.assertGreaterEqual(self.attack_runs, 1)

    def test_patch_that_weakens_discrimination_is_rejected(self) -> None:
        engine = self.make_engine()
        self.seed_baseline(engine)
        task = engine.load_task(self.task_id)
        before = tree_hash(self.workspace)

        with self.assertRaises(rp.DiscriminationWeakened) as ctx:
            rp.attempt_patch(
                engine,
                task,
                "generate",
                # Removes the LAST 'completed' row, so the fixture measures the
                # mutant as keeping full reward on the counterfactual.
                _strip_all_completed_patch(task),
            )
        message = str(ctx.exception)
        self.assertIn(CASE, message)
        self.assertIn("counterfactual", message)
        self.assertIn("weakened", message)
        self.assertEqual(tree_hash(self.workspace), before)

    def test_zero_discrimination_before_closes_the_field(self) -> None:
        # The schemapile shape: nothing discriminated, so literal rows can only
        # make the complaint disappear. Stays an abstention.
        engine = self.make_engine()
        self.seed_baseline(engine, discriminates=False)
        task = engine.load_task(self.task_id)
        before = tree_hash(self.workspace)

        with self.assertRaises(rp.DiscriminationWeakened) as ctx:
            rp.attempt_patch(engine, task, "generate", literal_rows_patch())
        self.assertIn("cannot be repairing anything", str(ctx.exception))
        self.assertEqual(tree_hash(self.workspace), before)

    def test_the_materialized_rows_are_refused_by_the_allowlist(self) -> None:
        # Population files are derived and cannot be patched directly. Repairs
        # must change the IR and pass full discrimination remeasurement.
        engine = self.make_engine(generate=self.pass_runner)
        rows = (
            engine.task_dir(self.task_id)
            / "populations" / "counterfactual" / "rows"
        )
        rows.mkdir(parents=True, exist_ok=True)
        (rows / "orders.jsonl").write_text(
            json.dumps({"order_id": 1101, "customer_id": 11, "status": "completed"})
            + "\n",
            encoding="utf-8",
        )
        self.seed_baseline(engine)
        task = engine.load_task(self.task_id)
        before = tree_hash(self.workspace)

        patch = RepairPatch(
            route=RepairRoute.POPULATION,
            artifact="populations/counterfactual/rows/orders.jsonl",
            edits=(
                RepairEdit(
                    op=RepairEditOp.REPLACE,
                    locator="line:1",
                    old='"status": "completed"',
                    new='"status": "cancelled"',
                ),
            ),
            rationale="'normalising' the counterfactual rows",
            proposer_role=rp.ROLE_NAME,
        )
        with self.assertRaises(rp.ScopeViolation) as ctx:
            rp.attempt_patch(engine, task, "generate", patch)
        self.assertIn("populations/counterfactual/rows/orders.jsonl", str(ctx.exception))
        self.assertIn("allowlist", str(ctx.exception))
        self.assertEqual(tree_hash(self.workspace), before)
        # Defence in depth is still armed: the materialized-rows classifier
        # keeps answering True for that path, so a future allowlist that
        # reopened the door could not slip past the discrimination proof.
        self.assertTrue(
            rp._moved_counterfactual_rows(
                "{}",
                self.workspace,
                self.task_id,
                repair.ArtifactDiff(
                    changed=frozenset(
                        {
                            f"tasks/{self.task_id}/populations/counterfactual/"
                            "rows/orders.jsonl"
                        }
                    )
                ),
            )
        )

    def test_missing_leaf_locator_cannot_populate_empty_literal_rows(self) -> None:
        # A leaf replacement cannot invent a path beneath an empty
        # literal_rows dict. A trusted replace_json patch may instead replace
        # an EXISTING container wholesale (needed by bounded adjudications),
        # but it remains behind the zero-baseline refusal and the full
        # discrimination re-measurement exercised above.
        engine = self.make_engine()
        self.seed_baseline(engine)
        task = engine.load_task(self.task_id)
        stripped = task.model_copy(
            update={
                "populations": tuple(
                    p.model_copy(update={"literal_rows": {}})
                    if p.name is PopulationName.COUNTERFACTUAL
                    else p
                    for p in task.populations
                )
            }
        )
        engine.save_task(stripped)
        before = tree_hash(self.workspace)
        with self.assertRaises(rp.PatchApplicationError):
            rp.attempt_patch(
                engine,
                engine.load_task(self.task_id),
                "generate",
                literal_rows_patch(),
            )
        self.assertEqual(tree_hash(self.workspace), before)


def _strip_all_completed_patch(task) -> RepairPatch:
    """The laundering patch: fix the complaint AND remove the discriminator.

    It repairs the placeholder name (so `generate` re-validates fully GREEN —
    this patch is not caught by any existing check) while rewriting every
    counterfactual order status away from 'completed', which is what made the
    inner_join mutant lose reward there. Only the discrimination matrix can
    tell the two edits apart.
    """
    edits = [
        RepairEdit(
            op=RepairEditOp.REPLACE,
            locator="populations.3.literal_rows.customers.0.customer_name",
            old="C10",
            new="Acme Holdings",
        )
    ]
    for pop_index, pop in enumerate(task.populations):
        if pop.name is not PopulationName.COUNTERFACTUAL:
            continue
        for row_index, row in enumerate(pop.literal_rows.get("orders", ())):
            if row.get("status") == "completed":
                edits.append(
                    RepairEdit(
                        op=RepairEditOp.REPLACE,
                        locator=(
                            f"populations.{pop_index}.literal_rows.orders."
                            f"{row_index}.status"
                        ),
                        old="completed",
                        new="cancelled",
                    )
                )
    return RepairPatch(
        route=RepairRoute.POPULATION,
        artifact="task_ir.json",
        edits=tuple(edits),
        rationale="'simplifying' the counterfactual",
        proposer_role=rp.ROLE_NAME,
    )


if __name__ == "__main__":
    unittest.main()

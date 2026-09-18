"""The declared attack matrix is proven BEFORE the council is paid.

WHY THIS MODULE EXISTS
`verification/attack_matrix.py` moves one existing check —
`gates._gate_required_mutants` — from the `gates` stage (which sits two live
stages downstream) to the `author`/`review` admission path, where it costs
nothing. The properties worth pinning are therefore not "does the comparison
work" (that is `gates`' own suite) but the three that make the move SAFE:

  1. it judges EXACTLY the cases the late gate judges, so it cannot refuse a
     task the late gate would pass;
  2. it never invents a refusal out of missing inputs; and
  3. both live-spend stages consult it before they consult the provider, and a
     failure carries the POPULATION route the same wording gets out of `gates`.

The end-to-end evidence that it does not fire on healthy tasks is measured, not
asserted here, because the released workspaces are not repository fixtures:
`tools/prove_attack_matrix_gate.py <workspace>...` runs the real thing over a
whole workspace. Recorded on the synsql
`soil_profiles_horizons_top` 14/14 required cases reproduce (no refusal, 24s),
schemapile `042316_poc_corpora_with_left_wall_sql` 10/10 (no refusal, 15s), and
wikidbs `c00012 METEC_SOLARWATT_TEAM_MEMBERS_DB` REFUSES in 8s with exactly the
message its `gates` run produced a full council later.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from elt_taskgen import demo_fixture
from elt_taskgen.models import AttackCase, AttackKind, PopulationName, RepairRoute
from elt_taskgen.verification import attack_matrix

P = PopulationName


def _demo_task():
    return demo_fixture.demo_task()


class TestJudgesExactlyTheGatesSet(unittest.TestCase):
    """The pre-check inspects the REQUIRED cases and nothing else.

    This is the whole no-false-positive argument. `_gate_required_mutants`
    judges `required` cases only; a pre-check that inspected more could refuse
    a task the real gate would accept, which would make the early refusal a NEW
    predicate rather than the same one moved earlier.
    """

    def test_only_required_cases_and_in_a_stable_order(self):
        task = _demo_task()
        extra = AttackCase(
            name="aaa_informational_probe",
            kind=AttackKind.NO_OP,
            description="an informational probe the gate never judges",
            mutation="directive:kind:no_op",
            expected_pass={P.PRIMARY: False},
            required=False,
        )
        task = task.model_copy(
            update={"attack_cases": (extra,) + tuple(task.attack_cases)}
        )
        names = [c.name for c in attack_matrix.required_cases(task)]
        self.assertNotIn("aaa_informational_probe", names)
        self.assertEqual(names, sorted(names))
        self.assertEqual(
            names, sorted(c.name for c in task.attack_cases if c.required)
        )


class TestComparisonIsTheGatesComparison(unittest.TestCase):
    """Declared vs measured is decided by `gates._gate_required_mutants`.

    `run_attack` is substituted so the comparison — not DuckDB — is what these
    cases exercise; the real execution path is measured by
    `tools/prove_attack_matrix_gate.py` (see the module docstring).
    """

    def setUp(self):
        self.task = _demo_task()
        from elt_taskgen.verification import attacks as attacks_mod

        self._attacks = attacks_mod
        self._real = attacks_mod.run_attack

    def tearDown(self):
        self._attacks.run_attack = self._real

    def _with_rewards(self, fn):
        self._attacks.run_attack = fn
        return attack_matrix.check_declared_matrix(self.task, object(), Path("/x"))

    def test_green_when_every_required_case_reproduces_its_matrix(self):
        def measured(task, case, gold, workspace):
            return {
                pop: (1.0 if passes else 0.0)
                for pop, passes in case.expected_pass.items()
            }

        self.assertIsNone(self._with_rewards(measured))

    def test_a_leak_is_named_with_the_case_and_the_population(self):
        def measured(task, case, gold, workspace):
            # Every mutant keeps full reward everywhere: the catalogue's claims
            # are all false, which is the defect class this exists to catch.
            return {pop: 1.0 for pop in P}

        detail = self._with_rewards(measured)
        self.assertIsNotNone(detail)
        self.assertIn(attack_matrix.GATE_NAME, detail)
        self.assertIn("must lose reward", detail)
        self.assertIn("no_coalesce", detail)

    def test_an_expected_full_reward_that_is_lost_is_also_a_failure(self):
        # The demo's `hardcoded_primary_outputs` declares PRIMARY: True — a
        # constants submission MUST still score 1.0 there, or the case is not
        # testing what it says. Fail closed in both directions, never one.
        def measured(task, case, gold, workspace):
            return {pop: 0.0 for pop in P}

        detail = self._with_rewards(measured)
        self.assertIn("expected FULL reward on primary", detail)

    def test_a_mutant_that_cannot_be_materialized_is_a_finding_not_a_crash(self):
        from elt_taskgen.verification.attacks import InertAstMutationError

        def measured(task, case, gold, workspace):
            if case.name == "no_coalesce":
                raise InertAstMutationError("no COALESCE in this SQL")
            return {
                pop: (1.0 if passes else 0.0)
                for pop, passes in case.expected_pass.items()
            }

        detail = self._with_rewards(measured)
        self.assertIn("no_coalesce", detail)
        self.assertIn("InertAstMutationError", detail)

    def test_a_required_case_that_measures_nothing_is_a_finding(self):
        def measured(task, case, gold, workspace):
            return {} if case.name == "inner_join" else {
                pop: (1.0 if passes else 0.0)
                for pop, passes in case.expected_pass.items()
            }

        detail = self._with_rewards(measured)
        self.assertIn("inner_join", detail)
        self.assertIn("no measurement", detail)


class TestNeverInventsARefusal(unittest.TestCase):
    """Missing inputs mean "no evidence", never "reject".

    The pre-check is an accelerator for a gate that still runs. Declining to
    accelerate costs a council run; refusing on a missing file would reject
    tasks for a reason that is not about the task at all.
    """

    def test_no_answer_key_on_disk_means_no_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            self.assertIsNone(
                attack_matrix.check_declared_matrix_if_measurable(
                    _demo_task(), ws / "tasks" / "nope" / "answer_key", ws
                )
            )

    def test_an_unreadable_answer_key_means_no_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            akd = Path(tmp) / "answer_key"
            akd.mkdir()
            (akd / "manifest.json").write_text("{not json", encoding="utf-8")
            self.assertIsNone(
                attack_matrix.check_declared_matrix_if_measurable(
                    _demo_task(), akd, Path(tmp)
                )
            )


class TestTheMemoTracksEveryInput(unittest.TestCase):
    """The in-process memo may skip a re-measurement only when NOTHING moved.

    `author` and `review` each measure independently, so one pipeline pass runs
    the same mutants over the same bytes twice. Skipping the second is only
    sound if the fingerprint covers every input a reward depends on — and does
    NOT cover the measurement's own recorded artifacts, which would make it
    depend on itself. A memo that missed an input would be a gate that stopped
    catching a real defect, which is the one trade this codebase does not make.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.task = _demo_task()
        self.tdir = self.ws / "tasks" / self.task.task_id
        for rel in (
            "populations/primary/rows/customers.jsonl",
            "populations/primary/rendered/customers.csv",
            "answer_key/gold/primary/customer_summary.csv",
        ):
            path = self.tdir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("original\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _fp(self):
        return attack_matrix._inputs_fingerprint(self.task, self.ws)

    def test_stable_when_nothing_changes(self):
        self.assertEqual(self._fp(), self._fp())

    def test_moves_when_a_population_row_file_changes(self):
        before = self._fp()
        (self.tdir / "populations/primary/rows/customers.jsonl").write_text(
            "repaired\n", encoding="utf-8"
        )
        self.assertNotEqual(before, self._fp())

    def test_moves_when_a_rendered_backend_changes(self):
        # A RUNTIME repair re-renders artifacts WITHOUT moving the IR's content
        # hash, so a fingerprint built from the hash alone would serve a stale
        # verdict for data that changed underneath it.
        before = self._fp()
        (self.tdir / "populations/primary/rendered/customers.csv").write_text(
            "re-rendered\n", encoding="utf-8"
        )
        self.assertNotEqual(before, self._fp())

    def test_moves_when_frozen_gold_changes(self):
        before = self._fp()
        (self.tdir / "answer_key/gold/primary/customer_summary.csv").write_text(
            "re-frozen\n", encoding="utf-8"
        )
        self.assertNotEqual(before, self._fp())

    def test_moves_when_the_task_ir_changes(self):
        before = self._fp()
        self.task = self.task.model_copy(update={"title": "a different title"})
        self.assertNotEqual(before, self._fp())

    def test_the_measurements_own_artifacts_do_not_move_it(self):
        before = self._fp()
        recorded = self.tdir / "attacks" / "inner_join" / "mutant.sql"
        recorded.parent.mkdir(parents=True, exist_ok=True)
        recorded.write_text("SELECT 1", encoding="utf-8")
        self.assertEqual(before, self._fp())


class TestRunsBeforeLiveSpend(unittest.TestCase):
    """Both live-spend stages consult it before they consult the provider.

    Same contract, same shape, and for the same reason as
    `tests/test_structural_completeness.py::TestGateRunsBeforeLiveSpend`: the
    provider below fails the test if it is ever reached.
    """

    DETAIL = (
        "required-mutants: a declared attack matrix is not reproducible — "
        "measured deterministically before any live call; required mutant "
        "matrix not reproduced: custom__wrong_boundary_else: LEAK — must lose "
        "reward on primary, got 1.0"
    )

    class ExplodingProvider:
        def complete(self, role, prompt):  # pragma: no cover - must not run
            raise AssertionError(
                f"provider was called for role {role!r}: the attack-matrix "
                "gate did not refuse before live spend"
            )

    class FakeEngine:
        """The two accessors the check reads off a real Engine."""

        def __init__(self, workspace):
            self.workspace = Path(workspace)

        def task_dir(self, task_id):
            return self.workspace / "tasks" / task_id

    def setUp(self):
        from elt_taskgen import cli

        self.cli = cli
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = self.FakeEngine(self._tmp.name)
        self._real = attack_matrix.check_declared_matrix_if_measurable
        attack_matrix.check_declared_matrix_if_measurable = (
            lambda task, akd, ws: self.DETAIL
        )

    def tearDown(self):
        attack_matrix.check_declared_matrix_if_measurable = self._real
        self._tmp.cleanup()

    def test_author_refuses_without_calling_the_provider(self):
        run_author = self.cli.make_author_runner(self.ExplodingProvider())
        outcome = run_author(self.engine, _demo_task())
        self.assertEqual(outcome.verdict, self.cli.VERDICT_FAIL)
        self.assertIn(attack_matrix.GATE_NAME, outcome.payload.error)
        self.assertEqual(outcome.route, RepairRoute.POPULATION)

    def test_review_refuses_without_calling_the_provider(self):
        run_review = self.cli.make_review_runner(self.ExplodingProvider())
        outcome = run_review(self.engine, _demo_task())
        self.assertEqual(outcome.verdict, self.cli.VERDICT_FAIL)
        self.assertIn(attack_matrix.GATE_NAME, outcome.payload.detail)
        self.assertEqual(outcome.route, RepairRoute.POPULATION)

    def test_the_failure_routes_the_same_way_out_of_gates(self):
        """The early refusal and the late one must repair identically.

        `repair.route_for_failure` keyword-routes the `gates` wording to
        POPULATION; the pre-check declares POPULATION explicitly rather than
        relying on that, and this pins the two to the same answer so a future
        edit to either cannot make the same defect repair two different ways.
        """
        from elt_taskgen.engine import StagePayload
        from elt_taskgen.repair import route_for_failure

        payload = StagePayload(error=self.DETAIL)
        self.assertEqual(
            route_for_failure("gates", payload), RepairRoute.POPULATION
        )
        self.assertEqual(
            route_for_failure("author", payload), RepairRoute.POPULATION
        )


class TestHealthyTaskStillReachesTheProvider(unittest.TestCase):
    """Fail-closed must not mean fail-always."""

    class CannedFindingsProvider:
        def complete(self, role, prompt):
            populations = (
                "development",
                "primary",
                "resampled",
                "counterfactual",
                "stress",
            )
            stage_maps = {
                "extract_load": {name: True for name in populations},
                "transform": {name: False for name in populations},
            }
            return json.dumps(
                {
                    "findings": [
                        {
                            "severity": "major",
                            "summary": "constants shortcut must lose reward",
                            "detail": "compile a constants mutant",
                            "route_hint": None,
                            "suggested_attack": "constants", "disposition": "active",
                            "proposed_case": {
                                "kind": "constants",
                                "params": "{}",
                                "expected_pass_by_stage": stage_maps,
                                "rationale": (
                                    "the constants mutant is an explicit "
                                    "executable shortcut probe"
                                ),
                            },
                        }
                    ]
                }
            )

    def test_review_proceeds_when_nothing_is_measurable(self):
        from elt_taskgen import cli

        run_review = cli.make_review_runner(self.CannedFindingsProvider())
        outcome = run_review(None, _demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)


if __name__ == "__main__":
    unittest.main()

"""Council attack-case PROPOSALS: wire schema, parsing, and the promoter.

WHY THIS EXISTS
Every critic can attach a concrete `proposed_case` to a finding. The whole point
of the design is that the proposal decides NOTHING:

  * a proposal that predicts the five-population reward matrix EXACTLY is
    promoted to a required AttackCase and is thereafter asserted by the
    standing required-mutants gate (not by the promoter, and not by the
    agent's say-so);
  * a proposal that mispredicts even ONE population is rejected, with BOTH
    matrices — predicted and measured — written to a rejected-proposal
    artifact, so the disagreement is recorded rather than dropped;
  * a malformed or PARTIAL proposal is a schema violation that fails the
    review stage loudly (ProviderProtocolError), never a quietly-dropped
    field on an otherwise-accepted finding.

The reward stand-in, the demo populations, and the gold bundle come from
tests/test_attacks.py so proposals are measured by exactly the machinery the
demo attack matrix is measured by.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import duckdb

try:  # `python -m unittest tests.test_attack_promotion` from the repo root
    from tests import test_attacks as ta
except ImportError:  # discovered from inside tests/ (no package on sys.path)
    import test_attacks as ta
from elt_taskgen import cli as cli_mod
from elt_taskgen import demo_fixture
from elt_taskgen.demo_fixture import REFERENCE_SQL
from elt_taskgen.models import (
    AttackKind,
    CouncilRole,
    Finding,
    FindingScreen,
    FindingScreenStatus,
    PopulationName,
    ProposedAttackCase,
    RepairRoute,
    RLVR_TASK_VARIANTS,
    Severity,
    TaskIR,
    TaskVariant,
)
from elt_taskgen.review import council, providers
from elt_taskgen.verification import attacks, gates, upstream_eval

P = PopulationName

#: The demo reference joins customers -> orders LEFT, so INNER keeps full
#: reward only where every customer has a completed order.
INNER_JOIN_TRUTH = {
    P.DEVELOPMENT: True,
    P.STRESS: True,
    P.PRIMARY: False,
    P.RESAMPLED: False,
    P.COUNTERFACTUAL: False,
}


def _finding(finding_id: str, proposal: ProposedAttackCase | None, **over) -> Finding:
    kw = dict(
        finding_id=finding_id,
        role=CouncilRole.POPULATION_ADVERSARY,
        severity=Severity.MAJOR,
        summary="an INNER join is indistinguishable on the stated populations",
        detail="every development customer has a completed order",
        proposed_case=proposal,
    )
    kw.update(over)
    return Finding(**kw)


def _proposal(expected: dict[PopulationName, bool], **over) -> ProposedAttackCase:
    kw = dict(
        kind=AttackKind.INNER_JOIN,
        params={},
        expected_pass=dict(expected),
        expected_pass_by_stage={
            "extract_load": {p: True for p in PopulationName},
            "transform": dict(expected),
        },
        rationale=(
            "development and stress cannot distinguish INNER from LEFT: every "
            "customer there has at least one completed order."
        ),
    )
    kw.update(over)
    return ProposedAttackCase(**kw)


def _all_pass_add_dedup_outcome(
    finding_id: str, target: str
) -> attacks.PromotionOutcome:
    stage_maps = {
        variant.value: {
            population.value: True for population in PopulationName
        }
        for variant in RLVR_TASK_VARIANTS
    }
    return attacks.PromotionOutcome(
        finding_id=finding_id,
        case_name=f"proposed__{finding_id}",
        kind="custom",
        promoted=False,
        reason="live uncaught exploit",
        predicted={population.value: True for population in PopulationName},
        measured={population.value: 1.0 for population in PopulationName},
        measured_pass={
            population.value: True for population in PopulationName
        },
        predicted_by_stage=stage_maps,
        measured_by_stage={
            stage: {
                population.value: 1.0 for population in PopulationName
            }
            for stage in stage_maps
        },
        measured_pass_by_stage=stage_maps,
        fidelity={
            "passed": True,
            "compiler": {
                "passed": True,
                "requested": {
                    "operation": "add_dedup",
                    "dedup_table": target,
                },
                "realized": {
                    "operation": "add_dedup",
                    "dedup_table": target,
                    "distinct_source_scans": 1,
                },
            },
        },
    )


def _shortcut_handoff(
    *,
    kind: AttackKind = AttackKind.CONSTANTS,
    role: CouncilRole = CouncilRole.SHORTCUT_ATTACKER,
    extract_load_losses: frozenset[PopulationName] = frozenset(),
    transform_losses: frozenset[PopulationName] = frozenset({P.COUNTERFACTUAL}),
    fidelity_passed: object = True,
) -> tuple[Finding, attacks.PromotionOutcome]:
    """One faithful-but-mispredicted shortcut outcome, without executing SQL."""
    predicted_el = {population: True for population in P}
    predicted_t = {
        population: population in {P.PRIMARY, P.DEVELOPMENT}
        for population in P
    }
    params = (
        {"hardcode_population": P.PRIMARY.value}
        if kind is AttackKind.CONSTANTS
        else {}
    )
    proposal = ProposedAttackCase(
        kind=kind,
        params=params,
        expected_pass={
            population: predicted_el[population] and predicted_t[population]
            for population in P
        },
        expected_pass_by_stage={
            TaskVariant.EXTRACT_LOAD: predicted_el,
            TaskVariant.TRANSFORM: predicted_t,
        },
        rationale="hard-coded primary outputs should be defeated by hidden data",
    )
    finding = Finding(
        finding_id=f"shortcut-handoff-{kind.value}-{role.value}",
        role=role,
        severity=Severity.MAJOR,
        summary="hard-coded outputs can repeat across populations",
        detail="emit primary outputs verbatim instead of computing the mart",
        suggested_attack=kind,
        proposed_case=proposal,
    )

    measured_el = {
        population.value: population not in extract_load_losses for population in P
    }
    measured_t = {
        population.value: population not in transform_losses for population in P
    }
    measured_combined = {
        population.value: measured_el[population.value]
        and measured_t[population.value]
        for population in P
    }
    predicted_by_stage = {
        TaskVariant.EXTRACT_LOAD.value: {
            population.value: predicted_el[population] for population in P
        },
        TaskVariant.TRANSFORM.value: {
            population.value: predicted_t[population] for population in P
        },
    }
    return finding, attacks.PromotionOutcome(
        finding_id=finding.finding_id,
        case_name=f"proposed__{finding.finding_id}",
        kind=kind.value,
        promoted=False,
        reason="measured reward matrix does not match the proposed expectation",
        predicted={
            population.value: proposal.expected_pass[population]
            for population in P
        },
        measured={
            population: 1.0 if passed else 0.0
            for population, passed in measured_combined.items()
        },
        measured_pass=measured_combined,
        predicted_by_stage=predicted_by_stage,
        measured_by_stage={
            TaskVariant.EXTRACT_LOAD.value: {
                population: 1.0 if passed else 0.0
                for population, passed in measured_el.items()
            },
            TaskVariant.TRANSFORM.value: {
                population: 1.0 if passed else 0.0
                for population, passed in measured_t.items()
            },
        },
        measured_pass_by_stage={
            TaskVariant.EXTRACT_LOAD.value: measured_el,
            TaskVariant.TRANSFORM.value: measured_t,
        },
        fidelity={"passed": fidelity_passed},
        mismatches=("forecast differs from trusted measurement",),
    )


class ShortcutHandoffDischargeTest(unittest.TestCase):
    """A bad forecast cannot override a complete, faithful shortcut kill."""

    def assert_blocked(self, finding: Finding, outcome: attacks.PromotionOutcome):
        from elt_taskgen import cli

        blocking, problems = cli._blocking_proposal_failures([finding], [outcome])
        self.assertEqual(blocking, [finding])
        self.assertTrue(problems)

    def assert_discharged(
        self, finding: Finding, outcome: attacks.PromotionOutcome
    ):
        from elt_taskgen import cli

        self.assertEqual(
            cli._blocking_proposal_failures([finding], [outcome]),
            ([], []),
        )

    def test_wikidbs_shaped_transform_shortcut_is_discharged(self):
        """Primary/resampled/stress may repeat; counterfactual is a real kill."""
        finding, outcome = _shortcut_handoff(
            transform_losses=frozenset({P.DEVELOPMENT, P.COUNTERFACTUAL})
        )
        self.assert_discharged(finding, outcome)

    def test_universal_graded_pass_remains_blocking(self):
        finding, outcome = _shortcut_handoff(transform_losses=frozenset({P.DEVELOPMENT}))
        self.assert_blocked(finding, outcome)

    def test_incomplete_or_nonliteral_evidence_remains_blocking(self):
        finding, outcome = _shortcut_handoff()
        incomplete_combined = dict(outcome.measured_pass)
        incomplete_combined.pop(P.COUNTERFACTUAL.value)
        incomplete_stage = {
            TaskVariant.EXTRACT_LOAD.value: dict(
                outcome.measured_pass_by_stage[TaskVariant.EXTRACT_LOAD.value]
            )
        }
        inconsistent = dict(outcome.measured_pass)
        inconsistent[P.PRIMARY.value] = not inconsistent[P.PRIMARY.value]
        nonliteral = dict(outcome.measured_pass)
        nonliteral[P.COUNTERFACTUAL.value] = 0
        cases = {
            "combined population missing": outcome.model_copy(
                update={"measured_pass": incomplete_combined}
            ),
            "stage missing": outcome.model_copy(
                update={"measured_pass_by_stage": incomplete_stage}
            ),
            "integer used as boolean": outcome.model_copy(
                update={"measured_pass": nonliteral}
            ),
            "combined disagrees with stages": outcome.model_copy(
                update={"measured_pass": inconsistent}
            ),
            "fidelity false": outcome.model_copy(
                update={"fidelity": {"passed": False}}
            ),
            "fidelity integer one": outcome.model_copy(
                update={"fidelity": {"passed": 1}}
            ),
        }
        for label, malformed in cases.items():
            with self.subTest(label=label):
                self.assert_blocked(finding, malformed)

    def test_non_shortcut_role_or_kind_remains_blocking(self):
        """The shortcut discharge is specific to a shortcut attacker's kind.

        A population adversary is judged by its own rule instead, so the role
        case here loses only on ``development``: no hidden population refutes
        the blind-spot claim and the finding stays unresolved.
        """
        wrong_role, role_outcome = _shortcut_handoff(
            role=CouncilRole.POPULATION_ADVERSARY,
            transform_losses=frozenset({P.DEVELOPMENT}),
        )
        wrong_kind, kind_outcome = _shortcut_handoff(kind=AttackKind.NO_DEDUP)
        self.assert_blocked(wrong_role, role_outcome)
        self.assert_blocked(wrong_kind, kind_outcome)

    def test_a_hidden_population_kill_discharges_a_blind_spot_claim(self):
        """The same handoff with a hidden-population loss is discharged: the
        adversary's claim that the populations cannot tell the difference is
        refuted by the measurement, whatever it forecast."""
        finding, outcome = _shortcut_handoff(
            role=CouncilRole.POPULATION_ADVERSARY,
            transform_losses=frozenset({P.DEVELOPMENT, P.COUNTERFACTUAL}),
        )
        self.assert_discharged(finding, outcome)

    def test_kill_in_the_wrong_stage_cannot_discharge(self):
        # CONSTANTS is a transform shortcut: an EL-only loss proves nothing.
        constants, constants_outcome = _shortcut_handoff(
            extract_load_losses=frozenset({P.COUNTERFACTUAL}),
            transform_losses=frozenset(),
        )
        self.assert_blocked(constants, constants_outcome)

        # SKIP_EXTRACTION is an EL shortcut: a transform-only loss is irrelevant.
        skip, skip_outcome = _shortcut_handoff(
            kind=AttackKind.SKIP_EXTRACTION,
            extract_load_losses=frozenset(),
            transform_losses=frozenset({P.COUNTERFACTUAL}),
        )
        self.assert_blocked(skip, skip_outcome)

    def test_no_op_must_be_killed_in_both_applicable_stages(self):
        for label, el_losses, t_losses in (
            ("el only", frozenset({P.COUNTERFACTUAL}), frozenset()),
            ("transform only", frozenset(), frozenset({P.COUNTERFACTUAL})),
        ):
            with self.subTest(label=label):
                finding, outcome = _shortcut_handoff(
                    kind=AttackKind.NO_OP,
                    extract_load_losses=el_losses,
                    transform_losses=t_losses,
                )
                self.assert_blocked(finding, outcome)

        finding, outcome = _shortcut_handoff(
            kind=AttackKind.NO_OP,
            extract_load_losses=frozenset({P.COUNTERFACTUAL}),
            transform_losses=frozenset({P.COUNTERFACTUAL}),
        )
        self.assert_discharged(finding, outcome)


class _DemoFixtureMixin:
    """Demo populations + frozen-gold stand-in + the reward stand-in.

    Identical to tests/test_attacks.py's harness: proposals must be measured
    by exactly the machinery the demo attack matrix is measured by.
    """

    @classmethod
    def setUpClass(cls):
        cls._orig_evaluate = getattr(upstream_eval, "evaluate", None)
        upstream_eval.evaluate = ta._fake_evaluate

        cls.task = demo_fixture.demo_task()
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="promotion_test_"))
        cls.workspace = cls.tmpdir / "workspace"

        for pop, tables in ta._population_rows().items():
            rows_dir = (
                cls.workspace / "tasks" / cls.task.task_id
                / "populations" / pop.value / "rows"
            )
            rows_dir.mkdir(parents=True)
            for table, rows in tables.items():
                (rows_dir / f"{table}.jsonl").write_text(
                    "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
                )

        mart = cls.task.marts[0]
        columns = tuple(c.name for c in mart.columns)
        stage1, stage2 = {}, {}
        for pop in PopulationName:
            con = duckdb.connect(":memory:")
            try:
                counts = attacks._load_population_sources(
                    cls.task, pop, cls.workspace, con, frozenset()
                )
                rows = attacks._fetch_rows(con, REFERENCE_SQL)
            finally:
                con.close()
            stage1[pop.value] = counts
            stage2[pop.value] = {mart.name: ta._rows_to_csv(rows, columns)}
        cls.gold = ta._Gold(cls.task.task_id, cls.task.content_hash(), stage1, stage2)

    @classmethod
    def tearDownClass(cls):
        if cls._orig_evaluate is None:
            del upstream_eval.evaluate
        else:
            upstream_eval.evaluate = cls._orig_evaluate
        shutil.rmtree(cls.tmpdir, ignore_errors=True)


class PromoterTest(_DemoFixtureMixin, unittest.TestCase):
    """Execution decides. Built on the demo fixture's real populations/gold."""

    def _rejected_artifact(self, case_name: str) -> dict:
        path = (
            self.workspace / "tasks" / self.task.task_id / "attacks" / case_name
            / attacks.REJECTED_PROPOSAL_FILENAME
        )
        self.assertTrue(path.is_file(), f"no rejected-proposal artifact at {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    # -- 1. an exact prediction is promoted and then gate-asserted ----------

    def test_correct_prediction_is_promoted_and_gate_asserted(self):
        finding = _finding("pa-00-correct", _proposal(INNER_JOIN_TRUTH))
        result = attacks.promote_proposed_cases(
            self.task, [finding], self.workspace, self.gold
        )

        self.assertEqual(len(result.promoted), 1)
        self.assertEqual(result.rejected, ())
        promoted = result.promoted[0]
        self.assertEqual(promoted.name, "proposed__pa-00-correct")
        self.assertTrue(promoted.required, "a promoted case must be gate-asserted")
        self.assertEqual(promoted.kind, AttackKind.INNER_JOIN)
        self.assertEqual(promoted.source_finding, finding.finding_id)
        self.assertEqual(promoted.expected_pass, INNER_JOIN_TRUTH)

        # It joined the task's attack set (a semantic edit: new identity).
        self.assertIn(promoted, result.task.attack_cases)
        self.assertNotIn(promoted, self.task.attack_cases)
        self.assertNotEqual(result.task.content_hash(), self.task.content_hash())
        self.assertTrue(result.task_changed)

        # ...and the EXISTING required-mutants gate now asserts it.
        rewards = {
            case.name: attacks.run_attack(
                result.task, case, self.gold, self.workspace
            )
            for case in result.task.attack_cases
        }
        gate = gates._gate_required_mutants(result.task, rewards)
        self.assertTrue(gate.passed, gate.details)
        self.assertIn(promoted.name, gate.evidence)

        # A LEAK on the promoted case turns that same gate red — proof the
        # promotion is load-bearing, not decorative.
        leaked = {k: dict(v) for k, v in rewards.items()}
        leaked[promoted.name][P.COUNTERFACTUAL] = 1.0
        red = gates._gate_required_mutants(result.task, leaked)
        self.assertFalse(red.passed)
        self.assertIn(promoted.name, red.details)

    def test_promotion_is_idempotent_at_the_new_identity(self):
        finding = _finding("pa-01-correct", _proposal(INNER_JOIN_TRUTH))
        once = attacks.promote_proposed_cases(
            self.task, [finding], self.workspace, self.gold
        )
        twice = attacks.promote_proposed_cases(
            once.task, [finding], self.workspace, self.gold
        )
        self.assertEqual(twice.promoted, ())
        self.assertFalse(twice.task_changed)
        self.assertEqual(twice.task.content_hash(), once.task.content_hash())
        self.assertTrue(twice.outcomes[0].promoted)

    # -- 2. a single mispredicted population is rejected, with both matrices -

    def test_single_population_misprediction_is_rejected_with_both_matrices(self):
        wrong = dict(INNER_JOIN_TRUTH)
        wrong[P.COUNTERFACTUAL] = True  # the ONE lie
        finding = _finding("pa-02-wrong", _proposal(wrong))

        result = attacks.promote_proposed_cases(
            self.task, [finding], self.workspace, self.gold
        )

        self.assertEqual(result.promoted, ())
        self.assertFalse(result.task_changed)
        self.assertIs(result.task, self.task)
        self.assertEqual(len(result.rejected), 1)
        outcome = result.rejected[0]
        self.assertFalse(outcome.promoted)
        self.assertEqual(len(outcome.mismatches), 2)  # combined + T
        self.assertTrue(
            all("counterfactual" in mismatch for mismatch in outcome.mismatches)
        )

        # Nothing was dropped: the measured rewards are still returned so the
        # attack payload can carry the probe that actually ran.
        self.assertIn(outcome.case_name, result.rewards)

        record = self._rejected_artifact(outcome.case_name)
        self.assertFalse(record["promoted"])
        self.assertEqual(record["task_content_hash"], self.task.content_hash())
        # BOTH matrices, side by side.
        self.assertEqual(
            record["predicted"], {p.value: v for p, v in wrong.items()}
        )
        self.assertEqual(
            set(record["measured"]), {p.value for p in PopulationName}
        )
        self.assertEqual(
            record["measured_pass"], {p.value: v for p, v in INNER_JOIN_TRUTH.items()}
        )
        self.assertTrue(record["measured"]["counterfactual"] < 1.0)
        self.assertNotIn("time", json.dumps(record).lower())  # no wall clock

    def test_rejected_proposal_json_carries_a_value_free_projection(self):
        """Roadmap 0.B / trust boundary S8 §2.1 (leak row 2): beside the raw
        dump (kept for now) `rejected_proposal.json` carries `projection`,
        the post-session `project_promotion` record `audit list` reads —
        booleans and codes only. No reward float, no reason sentence (it can
        embed DuckDB text and the inert/inapplicable verdicts), no fidelity
        dump, no path enters it."""
        from elt_taskgen.review.tools.projection import (
            DIAGNOSTICS_VERSION,
            project_promotion,
        )
        from elt_taskgen.training.models import _CODE_RE

        wrong = dict(INNER_JOIN_TRUTH)
        wrong[P.COUNTERFACTUAL] = True
        finding = _finding("pa-02-projected", _proposal(wrong))
        result = attacks.promote_proposed_cases(
            self.task, [finding], self.workspace, self.gold
        )
        self.assertEqual(len(result.rejected), 1)
        outcome = result.rejected[0]
        record = self._rejected_artifact(outcome.case_name)

        # The raw dump is still there (both matrices), and the projection
        # beside it is exactly `project_promotion(outcome)`.
        self.assertIn("measured", record)
        self.assertIn("projection", record)
        projection = record["projection"]
        self.assertEqual(projection, project_promotion(outcome))
        self.assertEqual(
            set(projection),
            {
                "diagnostics_version",
                "finding_id",
                "case_name",
                "kind",
                "promoted",
                "code",
                "per_population",
                "fidelity_ok",
            },
        )
        self.assertEqual(projection["diagnostics_version"], DIAGNOSTICS_VERSION)
        self.assertEqual(projection["finding_id"], "pa-02-projected")
        self.assertEqual(projection["case_name"], outcome.case_name)
        self.assertEqual(projection["kind"], AttackKind.INNER_JOIN.value)
        self.assertIs(projection["promoted"], False)
        self.assertEqual(projection["code"], "mismatch")
        self.assertRegex(projection["code"], _CODE_RE)
        self.assertIsInstance(projection["fidelity_ok"], bool)
        # The five populations, each a pair of booleans: the one sanctioned
        # exception (the adversary authored the five-way prediction itself).
        self.assertEqual(set(projection["per_population"]), {p.value for p in PopulationName})
        for pop, cell in projection["per_population"].items():
            self.assertEqual(set(cell), {"predicted", "measured_pass"}, pop)
            self.assertIs(cell["predicted"], wrong[P(pop)], pop)
            self.assertIs(cell["measured_pass"], INNER_JOIN_TRUTH[P(pop)], pop)

        # Value-free: no number that is not a bool anywhere, and none of the
        # raw record's withheld fields leak through as text.
        def walk(value, trail="projection"):
            if isinstance(value, bool) or value is None:
                return
            if isinstance(value, (int, float)):
                self.fail(f"{trail} carries a number: {value!r}")
            if isinstance(value, str):
                self.assertNotIn(str(self.workspace), value, trail)
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    walk(item, f"{trail}.{key}")
                return
            if isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, f"{trail}[{index}]")
                return
            self.fail(f"{trail} has an unexpected type {type(value).__name__}")

        walk(projection)
        projected_text = json.dumps(projection)
        self.assertTrue(outcome.reason)
        self.assertNotIn(outcome.reason, projected_text)
        for mismatch in outcome.mismatches:
            self.assertNotIn(mismatch, projected_text)
        self.assertNotIn("measured reward", projected_text)
        for reward in outcome.measured.values():
            self.assertNotIn(repr(reward), projected_text)
        self.assertNotIn("time", projected_text.lower())  # no wall clock

    def test_uncompilable_proposal_is_rejected_not_raised(self):
        finding = _finding(
            "pa-03-badparams",
            _proposal(
                INNER_JOIN_TRUTH,
                kind=AttackKind.CONSTANTS,
                params={attacks.HARDCODE_PARAM: "not_a_population"},
            ),
        )
        result = attacks.promote_proposed_cases(
            self.task, [finding], self.workspace, self.gold
        )
        self.assertEqual(result.promoted, ())
        self.assertEqual(len(result.rejected), 1)
        self.assertIn("not_a_population", result.rejected[0].reason)
        record = self._rejected_artifact(result.rejected[0].case_name)
        self.assertFalse(record["promoted"])

    def test_hardcode_param_compiles_to_the_emission_directive(self):
        """A proposal naming a population's outputs promotes the REAL
        emission probe (frozen gold rows), matching the demo's standing case."""
        finding = _finding(
            "pa-04-hardcode",
            _proposal(
                {
                    P.PRIMARY: True,
                    P.DEVELOPMENT: False,
                    P.RESAMPLED: False,
                    P.COUNTERFACTUAL: False,
                    P.STRESS: False,
                },
                kind=AttackKind.CONSTANTS,
                params={attacks.HARDCODE_PARAM: "primary"},
            ),
            summary="emit primary outputs verbatim",
            detail="hard-code the primary population's outputs",
        )
        result = attacks.promote_proposed_cases(
            self.task, [finding], self.workspace, self.gold
        )
        self.assertEqual(len(result.promoted), 1, result.rejected)
        self.assertEqual(
            result.promoted[0].mutation,
            attacks.HARDCODE_DIRECTIVE_PREFIX + "primary",
        )

    def test_findings_without_proposals_promote_nothing(self):
        plain = _finding("pa-05-plain", None, suggested_attack=AttackKind.INNER_JOIN)
        result = attacks.promote_proposed_cases(
            self.task, [plain], self.workspace, self.gold
        )
        self.assertEqual(result.outcomes, ())
        self.assertIs(result.task, self.task)

    def test_promoter_refuses_without_gold(self):
        finding = _finding("pa-06-nogold", _proposal(INNER_JOIN_TRUTH))
        with self.assertRaises(ValueError) as ctx:
            attacks.promote_proposed_cases(self.task, [finding], self.workspace)
        self.assertIn("gold", str(ctx.exception).lower())

    def test_promotion_order_is_deterministic(self):
        findings = [
            _finding("pa-09-b", _proposal(INNER_JOIN_TRUTH)),
            _finding("pa-08-a", _proposal(INNER_JOIN_TRUTH)),
        ]
        first = attacks.promote_proposed_cases(
            self.task, findings, self.workspace, self.gold
        )
        second = attacks.promote_proposed_cases(
            self.task, list(reversed(findings)), self.workspace, self.gold
        )
        self.assertEqual(
            [c.name for c in first.promoted], [c.name for c in second.promoted]
        )
        self.assertEqual(
            [c.name for c in first.promoted],
            ["proposed__pa-08-a", "proposed__pa-09-b"],
        )
        self.assertEqual(first.task.content_hash(), second.task.content_hash())


class _StubEngine:
    """Just enough Engine for cli.run_attack_stage: a workspace, a task dir,
    and one passing review report bound to the current content hash."""

    def __init__(self, workspace: Path, task, findings):
        from elt_taskgen.engine import ReportRow

        self.workspace = workspace
        self._row = ReportRow(
            id=1,
            task_id=task.task_id,
            revision=0,
            stage="review",
            verdict="pass",
            payload_json=json.dumps(
                {"findings": [f.model_dump(mode="json") for f in findings]}
            ),
            content_hash=task.content_hash(),
            created_at="",
        )

    def task_dir(self, task_id: str) -> Path:
        return self.workspace / "tasks" / task_id

    def latest_report(self, task_id: str, stage: str):
        return self._row if stage == "review" else None


class AttackStageWiringTest(_DemoFixtureMixin, unittest.TestCase):
    """cli.run_attack_stage: probes first, promoter after, gates never see a
    stale artifact — every recorded reward is bound to the identity the gates
    will read the task at."""

    def _run_stage(self, findings):
        from unittest import mock

        from elt_taskgen import cli
        from elt_taskgen.reference import gold as gold_mod

        engine = _StubEngine(self.workspace, self.task, findings)
        with mock.patch.object(gold_mod, "load_gold", return_value=self.gold):
            return cli.run_attack_stage(engine, self.task)

    def test_promoted_and_rejected_proposals_reach_the_payload(self):
        good = _finding(
            "pa-20-good",
            _proposal(INNER_JOIN_TRUTH),
            suggested_attack=AttackKind.INNER_JOIN,
        )
        wrong_matrix = dict(INNER_JOIN_TRUTH)
        wrong_matrix[P.STRESS] = False
        bad = _finding("pa-21-bad", _proposal(wrong_matrix))

        outcome = self._run_stage([good, bad])
        # The mispredicted forecast does not fail the stage: the adversary
        # claims the populations cannot distinguish an INNER join, and the
        # measurement refutes that claim on three hidden populations. A wrong
        # guess about WHICH hidden population catches the mutant is not a
        # defect to repair (the defterp rule, 2026-09-14). An unresolved major
        # claim still fails the stage; that is
        # ``test_an_unresolved_major_claim_names_one_deterministic_subject``.
        self.assertEqual(outcome.verdict, "pass")
        payload = outcome.payload

        self.assertEqual(payload.promoted_proposals, ("proposed__pa-20-good",))
        self.assertEqual(len(payload.rejected_proposals), 1)
        rejected = payload.rejected_proposals[0]
        self.assertEqual(rejected["case_name"], "proposed__pa-21-bad")
        self.assertTrue(rejected["predicted"])
        self.assertTrue(rejected["measured"])   # BOTH matrices in the ledger
        self.assertFalse(rejected["promoted"])

        # A structured proposal is the sole executable handoff; no lossy
        # suggested_attack duplicate is compiled beside it.
        self.assertNotIn("finding__pa-20-good", payload.rewards)
        # A rejected proposal's measured rewards are reported, never deleted.
        self.assertIn("proposed__pa-21-bad", payload.rewards)

        # The stage hands back the extended task...
        self.assertIsNotNone(outcome.task)
        names = {c.name for c in outcome.task.attack_cases}
        self.assertIn("proposed__pa-20-good", names)
        promoted = next(
            c for c in outcome.task.attack_cases if c.name == "proposed__pa-20-good"
        )
        self.assertTrue(promoted.required)

        # ...and EVERY recorded attack artifact is bound to that new identity,
        # so the gates read evidence, not stale records.
        final_hash = outcome.task.content_hash()
        self.assertNotEqual(final_hash, self.task.content_hash())
        attacks_root = self.workspace / "tasks" / self.task.task_id / "attacks"
        for case_name in payload.rewards:
            record = json.loads(
                (attacks_root / case_name / "rewards.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["task_content_hash"], final_hash, case_name)

        # Every case measured is a case reported (no probe without evidence).
        self.assertEqual(
            set(payload.cases) - set(payload.rewards), set()
        )

    def test_an_unresolved_major_claim_names_one_deterministic_subject(self) -> None:
        """A major claim with no executable proposal fails the stage, and the
        repair handoff forwards a subject, not the critic's prose, id, matrix
        or private populations."""
        unresolved = _finding("pa-30-unresolved", None)
        outcome = self._run_stage([unresolved])
        self.assertEqual(outcome.verdict, "fail")
        self.assertIs(outcome.route, RepairRoute.POPULATION)
        self.assertEqual(
            outcome.payload.blocking_finding.model_dump(mode="json"),
            {
                "role": "population_adversary",
                "severity": "major",
                "identifiers": ["development", "join"],
            },
        )

    def test_pipedrive_shaped_missing_variant_proposal_is_an_unresolved_major(self):
        """Batch report 156: a MAJOR adversary claim naming `wrong_agg_stage`
        with no proposal.  The compiler never guesses the sole registered
        variant (no `finding__` probe is compiled, no ValueError leaves the
        stage) and the claim is an UNRESOLVED major routed to the adversary's
        repair surface — a structured stage FAIL, never a protocol block
        nobody can correct and never an exception."""
        plain = _finding(
            "population_adversary-01-pipedrive",
            None,
            suggested_attack=AttackKind.WRONG_AGG_STAGE,
            summary="last_update_time aggregation is not distinguished",
            detail=(
                "caches_cache_usage_distribution has no repeated cache id with "
                "different last_update_time values, so MAX, MIN, and FIRST agree"
            ),
        )
        self.assertIsNone(attacks._default_attack_directive(AttackKind.WRONG_AGG_STAGE))
        self.assertEqual(
            [c.name for c in attacks.compile_attacks(self.task, [plain])],
            [c.name for c in self.task.attack_cases],
        )
        outcome = self._run_stage([plain])
        self.assertEqual(outcome.verdict, "fail")
        self.assertIs(outcome.route, RepairRoute.POPULATION)
        self.assertIsNone(outcome.task)
        self.assertIn(
            "population_adversary-01-pipedrive: major finding has no structured "
            "proposed_case; it is unresolved",
            outcome.payload.detail,
        )
        self.assertNotIn("finding__population_adversary-01-pipedrive", outcome.payload.cases)
        self.assertEqual(outcome.payload.blocking_finding.role, CouncilRole.POPULATION_ADVERSARY)
        self.assertEqual(outcome.payload.blocking_finding.severity, Severity.MAJOR)

    def test_invalid_named_variant_blocks_before_any_attack_executes(self):
        from unittest import mock

        proposal = _proposal(
            INNER_JOIN_TRUTH,
            kind=AttackKind.WRONG_AGG_STAGE,
            params={"variant": "max_as_min"},
        )
        finding = _finding(
            "pa-22-invalid-variant",
            proposal,
            suggested_attack=AttackKind.WRONG_AGG_STAGE,
            summary="last_update_time uses the wrong aggregate",
            detail="last_update_time may use MAX or MIN for each cache id",
        )
        with mock.patch.object(attacks, "run_attack") as run:
            outcome = self._run_stage([finding])
        run.assert_not_called()
        self.assertEqual(outcome.verdict, "blocked")
        self.assertEqual(
            outcome.payload.data["failure_code"],
            "critic_attack_handoff_invalid",
        )
        self.assertIn("has no variant", outcome.payload.error)

    def test_minor_invalid_named_variant_is_also_a_protocol_block(self):
        """Severity cannot bypass the executable-handoff boundary."""
        from unittest import mock

        proposal = _proposal(
            INNER_JOIN_TRUTH,
            kind=AttackKind.WRONG_AGG_STAGE,
            params={"variant": "made_up"},
        )
        finding = _finding(
            "shortcut-22-minor-invalid-variant",
            proposal,
            suggested_attack=AttackKind.WRONG_AGG_STAGE,
            summary="try a minor alternate aggregation stage",
            detail="made_up changes the aggregation stage for customer_summary",
        ).model_copy(update={"severity": Severity.MINOR})
        with mock.patch.object(attacks, "run_attack") as run:
            outcome = self._run_stage([finding])
        run.assert_not_called()
        self.assertEqual(outcome.verdict, "blocked")
        self.assertEqual(
            outcome.payload.data["failure_code"],
            "critic_attack_handoff_invalid",
        )
        self.assertIn("has no variant", outcome.payload.error)

    def test_minor_named_attack_without_proposal_is_protocol_block(self):
        from unittest import mock

        finding = _finding(
            "shortcut-22-minor-missing-proposal",
            None,
            suggested_attack=AttackKind.NO_OP,
            summary="exercise a no-op shortcut",
            detail="no_op should be executed as a standing shortcut probe",
        ).model_copy(update={"severity": Severity.MINOR})
        with mock.patch.object(attacks, "run_attack") as run:
            outcome = self._run_stage([finding])
        run.assert_not_called()
        self.assertEqual(outcome.verdict, "blocked")
        self.assertIn("has no proposed_case", outcome.payload.error)

    def test_population_claim_with_both_attack_fields_null_is_an_unresolved_major(self):
        """Batch report 111's adversary finding: MAJOR, no attack, no proposal.
        An unresolved claim on the POPULATION route (D1), not a protocol
        block; the in-session pre-flight is where the seat is made to attach
        the executable case."""
        finding = _finding(
            "population-22-null-handoff",
            None,
            suggested_attack=None,
            summary="graded populations miss a relationship defect",
            detail="customer_summary may preserve the wrong customer rows",
        )
        outcome = self._run_stage([finding])
        self.assertEqual(outcome.verdict, "fail")
        self.assertIs(outcome.route, RepairRoute.POPULATION)
        self.assertIn(
            "population-22-null-handoff: major finding has no structured "
            "proposed_case; it is unresolved",
            outcome.payload.detail,
        )
        self.assertEqual(
            outcome.payload.blocking_finding.model_dump(mode="json"),
            {
                "role": "population_adversary",
                "severity": "major",
                "identifiers": ["customer_summary"],
            },
        )

    def test_a_self_retracted_bare_population_claim_is_neither_protocol_nor_repair(self):
        """A bare MAJOR claim the deterministic screen VOIDed as self-
        retracting carries nothing executable to certify and nothing left
        to repair: the stage passes on the standing catalogue.  (The screen
        still cannot launder EXECUTABLE content: a withheld proposal is
        re-certified from the screen record, see
        `test_minor_invalid_named_variant_is_also_a_protocol_block`.)"""
        finding = _finding(
            "population-22-screened-null-handoff",
            None,
            suggested_attack=None,
            summary="this concern retracts itself",
            detail="customer_summary is both wrong and not wrong",
        ).model_copy(
            update={
                "severity": Severity.INFO,
                "screen": FindingScreen(
                    status=FindingScreenStatus.VOID,
                    signals=("self_retracted",),
                    evidence="provider claim retracted itself",
                    claimed_severity=Severity.MAJOR,
                ),
            }
        )
        outcome = self._run_stage([finding])
        self.assertEqual(outcome.verdict, "pass")
        self.assertIsNone(outcome.payload.blocking_finding)

    def test_harness_voided_proposal_is_final_and_not_recertified(self):
        """`critic_validators.void_uncompilable_proposals` (D4) withholds a
        proposal the seat left malformed after its compile correction.  The
        attack stage reads that void as final — INFO, nothing executable —
        instead of re-certifying the withheld proposal and blocking the stage
        on the very grammar failure the seat was already told about."""
        malformed = _proposal(
            INNER_JOIN_TRUTH,
            kind=AttackKind.WRONG_AGG_STAGE,
            params={"variant": "made_up"},
        )
        finding = _finding(
            "population-22-harness-voided",
            None,
            suggested_attack=None,
            summary="last_update_time may aggregate at the wrong stage",
            detail="customer_summary keeps full reward under made_up",
        ).model_copy(
            update={
                "severity": Severity.INFO,
                "screen": FindingScreen(
                    status=FindingScreenStatus.VOID,
                    signals=("uncompilable_after_corrections",),
                    evidence="executable handoff still variant_invalid",
                    claimed_severity=Severity.MAJOR,
                    withheld_attack=AttackKind.WRONG_AGG_STAGE,
                    withheld_proposal=malformed,
                ),
            }
        )
        outcome = self._run_stage([finding])
        self.assertEqual(outcome.verdict, "pass")
        self.assertNotIn("proposed__population-22-harness-voided", outcome.payload.cases)

    def test_ambiguity_with_executable_case_is_pending_adjudication(self):
        from unittest import mock

        proposal = _proposal(INNER_JOIN_TRUTH)
        finding = _finding(
            "ambiguity-22-with-proposal",
            proposal,
            role=CouncilRole.AMBIGUITY_CRITIC,
            suggested_attack=AttackKind.INNER_JOIN,
            summary="customer_summary admits two relationship readings",
            detail="customer_summary may use INNER or LEFT preservation",
        )
        # Since 2026-09-11 (batch10 runs K and L): not a human hold. The
        # alternative is withheld from promotion — never executed as a
        # candidate gate case, so no reading is frozen — and the claim
        # fails the stage as an unresolved major finding on the
        # SPECIFICATION route.
        with mock.patch.object(attacks, "promote_proposed_cases", wraps=attacks.promote_proposed_cases) as promote:
            outcome = self._run_stage([finding])
        self.assertEqual(outcome.verdict, "fail")
        self.assertIn("withheld from promotion", outcome.payload.detail)
        self.assertEqual(outcome.payload.promoted_proposals, ())
        self.assertIs(outcome.payload.blocking_finding.role, CouncilRole.AMBIGUITY_CRITIC)
        self.assertIs(outcome.route, RepairRoute.SPECIFICATION)
        promoted_input = promote.call_args.args[1]
        self.assertEqual([f.finding_id for f in promoted_input], [])

    def test_explicit_registered_variant_compiles_without_defaulting(self):
        try:
            from tests import test_attack_variants as variant_tests
        except ImportError:  # pragma: no cover - discovery from tests/
            import test_attack_variants as variant_tests

        task = self.task.model_copy(
            update={
                "marts": (variant_tests._HAVING_MART,),
                "reference": self.task.reference.model_copy(
                    update={"sql_by_mart": {"m": variant_tests.HAVING_PLAN}}
                ),
            }
        )
        proposal = _proposal(
            INNER_JOIN_TRUTH,
            kind=AttackKind.WRONG_AGG_STAGE,
            params={"variant": "filter_before_aggregate"},
        )
        finding = _finding(
            "pa-22-explicit-variant",
            proposal,
            suggested_attack=AttackKind.WRONG_AGG_STAGE,
            summary="apply the aggregate threshold before grouping",
            detail="filter_before_aggregate moves the grouped threshold in mart m",
        )
        case, fidelity = attacks.validate_proposed_case(
            task, finding, proposal
        )
        self.assertEqual(
            case.mutation,
            "directive:kind:wrong_agg_stage@filter_before_aggregate",
        )
        self.assertTrue(fidelity["passed"])

    def test_unknown_structured_target_blocks_before_execution(self):
        from unittest import mock

        proposal = _proposal(
            INNER_JOIN_TRUTH,
            kind=AttackKind.CUSTOM,
            params={"copy_mart": ["customer_summary", "missing_mart"]},
        )
        finding = _finding(
            "pa-22-missing-target",
            proposal,
            suggested_attack=AttackKind.CUSTOM,
            summary="copy customer_summary to missing_mart",
            detail="customer_summary and missing_mart are the requested endpoints",
        )
        with mock.patch.object(attacks, "run_attack") as run:
            outcome = self._run_stage([finding])
        run.assert_not_called()
        self.assertEqual(outcome.verdict, "blocked")
        self.assertIn("unknown mart", outcome.payload.error)

    # -- Batch repair D1 (reports 111 / 123): the unresolved-major route ------

    @staticmethod
    def _batch_shaped_findings():
        """Reports 111/123 in the demo task's vocabulary: the ambiguity
        critic's MAJOR reading dispute (no proposal; 123's carried a coarse
        `suggested_attack`), the adversary's MAJOR blindness claim (no
        proposal; a coarse attack named) and the shortcut attacker's
        compiling proposal that the promoter measures beside them."""
        ambiguity = Finding(
            finding_id="ambiguity_critic-00-batch",
            role=CouncilRole.AMBIGUITY_CRITIC,
            severity=Severity.MAJOR,
            summary="customer_summary admits two readings of total_spend",
            detail=(
                "the prose rounds per row and the schema sums then rounds; "
                "customer_summary differs under either reading"
            ),
            route_hint=RepairRoute.SPECIFICATION,
            suggested_attack=AttackKind.NO_NULL_DEFAULT,
        )
        adversary = _finding(
            "population_adversary-00-batch",
            None,
            suggested_attack=AttackKind.WRONG_GRAIN,
            summary="no graded population exercises the order grain",
            detail="customer_summary keeps full reward at the item grain",
        )
        shortcut = _finding(
            "shortcut_attacker-00-batch",
            _proposal(INNER_JOIN_TRUTH),
            role=CouncilRole.SHORTCUT_ATTACKER,
            suggested_attack=AttackKind.INNER_JOIN,
        )
        return ambiguity, adversary, shortcut

    def test_batch_shaped_unresolved_majors_fail_on_the_role_route_never_block(self):
        """Reports 111 and 123: both unresolved majors are reported in one
        structured stage FAIL (the batch's own detail text), the ambiguity
        critic's claim selects the SPECIFICATION route, and without it the
        adversary's claim selects POPULATION.  Neither is a protocol block
        (`retry_guard: explicit`, blocked forever) nor a human hold."""
        ambiguity, adversary, shortcut = self._batch_shaped_findings()
        self.assertIsNone(cli_mod._critic_adjudication_block([ambiguity, adversary, shortcut]))
        self.assertEqual(
            cli_mod._validated_executable_findings(self.task, [ambiguity, adversary, shortcut]),
            [ambiguity, adversary, shortcut],
        )

        outcome = self._run_stage([ambiguity, adversary, shortcut])
        self.assertEqual(outcome.verdict, "fail")
        self.assertIs(outcome.route, RepairRoute.SPECIFICATION)
        # Both unresolved MAJORs are named, each followed by the critic's own
        # claim text (2026-09-10: the SPECIFICATION proposer's only view of
        # the finding is this string, and the id alone left it unable to
        # repair anything).
        detail = outcome.payload.detail
        self.assertIn("critic-to-mutation handoff BLOCKED:", detail)
        for finding_id in ("ambiguity_critic-00-batch", "population_adversary-00-batch"):
            with self.subTest(finding=finding_id):
                self.assertIn(
                    f"{finding_id}: major finding has no structured "
                    "proposed_case; it is unresolved — the claim to resolve is:",
                    detail,
                )
        self.assertIn("admits two readings of total_spend", detail)
        self.assertIn("no graded population exercises the order grain", detail)
        self.assertEqual(outcome.payload.blocking_finding.role, CouncilRole.AMBIGUITY_CRITIC)
        self.assertEqual(outcome.payload.promoted_proposals, ("proposed__shortcut_attacker-00-batch",))
        # As in report 123: the coarse hints with a registered default
        # compile to INFORMATIONAL probes that are measured but discharge
        # nothing; a kind without a default is never guessed (see
        # `test_pipedrive_shaped_missing_variant_proposal_is_an_unresolved_major`).
        self.assertIn("finding__population_adversary-00-batch", outcome.payload.cases)
        self.assertIn("finding__ambiguity_critic-00-batch", outcome.payload.cases)

        outcome = self._run_stage([adversary, shortcut])
        self.assertEqual(outcome.verdict, "fail")
        self.assertIs(outcome.route, RepairRoute.POPULATION)
        self.assertEqual(outcome.payload.blocking_finding.role, CouncilRole.POPULATION_ADVERSARY)

    def test_unresolved_major_spends_the_role_route_repair_round_with_bounded_repair(self):
        """With `max_repair_rounds > 0` the engine spends a repair round on
        the route the attack stage derived — SPECIFICATION for the ambiguity
        critic's unresolved major, POPULATION for the adversary's — instead
        of the batch's `max_repair_rounds: 0` FATAL; the ledger's repair row
        names that route."""
        from unittest import mock

        from elt_taskgen.engine import (
            Engine,
            StageName,
            StageOutcome,
            StagePayload,
            VERDICT_FAIL,
            VERDICT_FATAL,
            VERDICT_PASS,
        )
        from elt_taskgen.reference import gold as gold_mod

        ambiguity, adversary, shortcut = self._batch_shaped_findings()
        cases = (
            ("specification", [ambiguity, adversary, shortcut]),
            ("population", [adversary, shortcut]),
        )
        for expected_route, findings in cases:
            with self.subTest(route=expected_route), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "ws"

                def passing(stage):
                    def run(engine, task):
                        return StageOutcome(VERDICT_PASS, StagePayload(detail=f"{stage} ok"))

                    return run

                def review(engine, task):
                    return StageOutcome(
                        VERDICT_PASS,
                        cli_mod.ReviewPayload(findings=tuple(findings), detail="review"),
                    )

                def attack(engine, task):
                    with mock.patch.object(gold_mod, "load_gold", return_value=self.gold):
                        return cli_mod.run_attack_stage(engine, task)

                runners = {s.value: passing(s.value) for s in StageName}
                runners["review"] = review
                runners["attack"] = attack
                engine = Engine(workspace, stage_runners=runners, max_repair_rounds=1)
                try:
                    engine.register(self.task)
                    shutil.copytree(
                        self.workspace / "tasks" / self.task.task_id / "populations",
                        engine.task_dir(self.task.task_id) / "populations",
                    )
                    engine.run(self.task.task_id)
                    repair_row = engine.last_repair(self.task.task_id)
                    self.assertIsNotNone(repair_row)
                    self.assertEqual(repair_row.route, expected_route)
                    self.assertEqual(engine.repair_rounds_used(self.task.task_id), 1)
                    # `report_history` is newest first; read it oldest first.
                    rows = list(reversed(engine.report_history(self.task.task_id, "attack")))
                    self.assertEqual(rows[0].verdict, VERDICT_FAIL)
                    first = json.loads(rows[0].payload_json)
                    self.assertIn("it is unresolved", first["detail"])
                    # The bounded budget, not a protocol block, ends the task:
                    # the last attack row is the engine's own FATAL naming the
                    # route the round was spent on.
                    self.assertEqual(rows[-1].verdict, VERDICT_FATAL)
                    self.assertEqual(json.loads(rows[-1].payload_json)["data"]["route"], expected_route)
                    self.assertFalse(any(r.verdict == "blocked" for r in rows))
                finally:
                    engine.close()

    def test_schema_equivalent_source_dedup_is_recorded_but_not_blocking(self):
        """DISTINCT * on a declared-PK source is an identity, not a shortcut.

        The exception is deliberately narrow: the promoter must compile and
        measure complete all-pass combined and EL/T matrices, then attach its
        schema proof. The rejected-proposal artifact remains audit evidence.
        """
        everywhere = {population: True for population in PopulationName}
        finding = _finding(
            "pa-22-schema-identity",
            _proposal(
                everywhere,
                # The wire names the exact inverse operation. The runtime no
                # longer rewrites ``no_dedup`` into ``custom`` behind the
                # critic's back merely to make this compile.
                kind=AttackKind.CUSTOM,
                params={"add_dedup": True, "dedup_table": "customers"},
                expected_pass_by_stage={
                    variant: dict(everywhere) for variant in RLVR_TASK_VARIANTS
                },
            ),
            summary="add DISTINCT to the customers source scan",
            detail=(
                "Every customers row carries the declared customer_id primary "
                "key, so this rewrite is relationally identical."
            ),
        )

        outcome = self._run_stage([finding])

        self.assertEqual(outcome.verdict, "pass")
        self.assertIsNone(outcome.task)
        (record,) = outcome.payload.rejected_proposals
        self.assertFalse(record["promoted"])
        self.assertIn("output-equivalent", record["reason"])
        self.assertEqual(
            record["fidelity"]["schema_equivalence"],
            {
                "proved": True,
                "proof": "source_distinct_over_declared_primary_key_v1",
                "operation": "add_dedup",
                "form": "operation_key",
                "tables": [
                    {
                        "name": "customers",
                        "key_kind": "primary_key",
                        "columns": ["customer_id"],
                    }
                ],
            },
        )

    def test_add_dedup_wrong_kind_is_not_silently_rewritten(self):
        from unittest import mock

        everywhere = {population: True for population in PopulationName}
        finding = _finding(
            "pa-22-add-dedup-wrong-kind",
            _proposal(
                everywhere,
                kind=AttackKind.NO_DEDUP,
                params={"add_dedup": True, "dedup_table": "customers"},
                expected_pass_by_stage={
                    variant: dict(everywhere)
                    for variant in RLVR_TASK_VARIANTS
                },
            ),
            suggested_attack=AttackKind.NO_DEDUP,
            summary="add DISTINCT to customers",
            detail="add dedup to customers without changing the attack kind",
        )
        with mock.patch.object(attacks, "run_attack") as run:
            outcome = self._run_stage([finding])
        run.assert_not_called()
        self.assertEqual(outcome.verdict, "blocked")
        self.assertIn("requires proposed kind 'custom'", outcome.payload.error)

    def test_business_key_does_not_prove_source_dedup_equivalent(self):
        """Business keys permit byte-identical stress duplicates; PKs do not."""
        from elt_taskgen import cli

        everywhere = {population: True for population in PopulationName}
        proposal = _proposal(
            everywhere,
            kind=AttackKind.CUSTOM,
            params={"add_dedup": True, "dedup_table": "orders"},
            expected_pass_by_stage={
                variant: dict(everywhere) for variant in RLVR_TASK_VARIANTS
            },
        )
        self.assertEqual(self.task.table("orders").primary_key, ())
        self.assertTrue(self.task.table("orders").business_key)
        outcome = _all_pass_add_dedup_outcome(
            "pa-22-business-key", "orders"
        )
        finding = _finding(
            "pa-22-business-key",
            proposal,
            summary="add DISTINCT to orders",
            detail="orders has a business key but no primary key",
        )
        self.assertIsNone(
            cli._source_dedup_equivalence_proof(self.task, finding, outcome)
        )

    def test_add_dedup_variant_spelling_is_proved_only_over_physical_pk_scans(self):
        """`{"variant": "add_dedup"}` under kind custom (the registry spelling
        the critic contract advertises) compiles to the kind directive
        `custom@add_dedup`, whose record carries no table and no DISTINCT
        count and whose rewrite wraps EVERY scan, CTE references included.
        The proof accepts it only when every scan in every mart is a raw
        physical table with a declared primary key and the re-derived
        rewrite changes exactly the recorded marts."""
        from elt_taskgen import cli

        everywhere = {population: True for population in PopulationName}
        proposal = _proposal(
            everywhere,
            kind=AttackKind.CUSTOM,
            params={"variant": "add_dedup"},
            expected_pass_by_stage={
                variant: dict(everywhere) for variant in RLVR_TASK_VARIANTS
            },
        )
        self.assertEqual(cli._names_add_dedup(proposal), "variant")
        self.assertIsNone(cli._names_add_dedup(proposal.model_copy(update={"kind": AttackKind.NO_DEDUP})))
        finding = _finding(
            "pa-22-dedup-variant",
            proposal,
            summary="an added distinct over the customers scan keeps grading",
            detail="customers carries the declared customer_id primary key",
        )
        marts = [mart.name for mart in self.task.marts]
        record = {"operation": "kind", "kind": "custom", "variant": "add_dedup", "target_marts": list(marts)}
        base = _all_pass_add_dedup_outcome("pa-22-dedup-variant", "customers")
        outcome = base.model_copy(update={"fidelity": {"passed": True, "compiler": {"passed": True, "requested": dict(record), "realized": dict(record)}}})
        # The demo gold reads CTEs (`completed_orders`, `order_totals`), which
        # the variant rewrite would wrap too: fail closed.
        self.assertIsNone(cli._source_dedup_equivalence_proof(self.task, finding, outcome))
        # Every mart reading only the keyed physical table: proved.
        gold = 'SELECT c."customer_id" AS "customer_id" FROM "customers" AS c'
        keyed = self.task.model_copy(update={"reference": self.task.reference.model_copy(update={"sql_by_mart": {name: gold for name in marts}})})
        proof = cli._source_dedup_equivalence_proof(keyed, finding, outcome)
        self.assertEqual(
            proof,
            {
                "proved": True,
                "proof": "source_distinct_over_declared_primary_key_v1",
                "operation": "add_dedup",
                "form": "variant",
                "tables": [{"name": "customers", "key_kind": "primary_key", "columns": ["customer_id"]}],
            },
        )
        accepted = outcome.model_copy(update={"fidelity": {**outcome.fidelity, "schema_equivalence": proof}})
        self.assertEqual(cli._blocking_proposal_failures([finding], [accepted]), ([], []))
        # A record whose marts differ from the ones the re-derived rewrite
        # changes is not this rewrite.
        elsewhere = outcome.model_copy(update={"fidelity": {"passed": True, "compiler": {"passed": True, "requested": dict(record), "realized": {**record, "target_marts": ["not_a_mart"]}}}})
        self.assertIsNone(cli._source_dedup_equivalence_proof(keyed, finding, elsewhere))
        # An unkeyed physical scan fails closed under this spelling as well.
        unkeyed = keyed.model_copy(update={"reference": keyed.reference.model_copy(update={"sql_by_mart": {name: 'SELECT o."order_id" AS "customer_id" FROM "orders" AS o' for name in marts}})})
        self.assertIsNone(cli._source_dedup_equivalence_proof(unkeyed, finding, outcome))

    def test_case_insensitive_cte_shadow_cannot_prove_source_dedup(self):
        """DuckDB resolves `Customers` and `customers` as the same CTE name."""
        from elt_taskgen import cli

        everywhere = {population: True for population in PopulationName}
        proposal = _proposal(
            everywhere,
            kind=AttackKind.CUSTOM,
            params={"add_dedup": True, "dedup_table": "customers"},
            expected_pass_by_stage={
                variant: dict(everywhere) for variant in RLVR_TASK_VARIANTS
            },
        )
        finding = _finding(
            "pa-22-cte-shadow",
            proposal,
            summary="add DISTINCT to customers",
            detail="a mixed-case CTE shadows the physical customers table",
        )
        reference = self.task.reference.model_copy(
            update={
                "sql_by_mart": {
                    self.task.marts[0].name: (
                        "WITH Customers AS ("
                        "SELECT 1 AS customer_id UNION ALL SELECT 1"
                        ") SELECT * FROM customers"
                    )
                }
            }
        )
        shadowed = self.task.model_copy(update={"reference": reference})
        self.assertIsNone(
            cli._source_dedup_equivalence_proof(
                shadowed,
                finding,
                _all_pass_add_dedup_outcome(
                    "pa-22-cte-shadow", "customers"
                ),
            )
        )

    def test_row_shaping_table_modifier_cannot_use_physical_pk_proof(self):
        """UNPIVOT can discard the PK before DISTINCT sees the derived rows."""
        from elt_taskgen import cli

        everywhere = {population: True for population in PopulationName}
        proposal = _proposal(
            everywhere,
            kind=AttackKind.CUSTOM,
            params={"add_dedup": True, "dedup_table": "customers"},
            expected_pass_by_stage={
                variant: dict(everywhere) for variant in RLVR_TASK_VARIANTS
            },
        )
        finding = _finding(
            "pa-22-unpivot",
            proposal,
            summary="add DISTINCT to customers",
            detail="customers is scanned through UNPIVOT",
        )
        reference = self.task.reference.model_copy(
            update={
                "sql_by_mart": {
                    self.task.marts[0].name: (
                        "SELECT * FROM customers "
                        "UNPIVOT(val FOR col IN (customer_id, customer_name))"
                    )
                }
            }
        )
        shaped = self.task.model_copy(update={"reference": reference})
        self.assertIsNone(
            cli._source_dedup_equivalence_proof(
                shaped,
                finding,
                _all_pass_add_dedup_outcome("pa-22-unpivot", "customers"),
            )
        )

    def test_alias_column_list_cannot_use_physical_pk_proof(self):
        """The compiler drops ``c(x, y)`` while rebuilding the source alias."""
        from elt_taskgen import cli

        everywhere = {population: True for population in PopulationName}
        proposal = _proposal(
            everywhere,
            kind=AttackKind.CUSTOM,
            params={"add_dedup": True, "dedup_table": "customers"},
            expected_pass_by_stage={
                variant: dict(everywhere) for variant in RLVR_TASK_VARIANTS
            },
        )
        finding = _finding(
            "pa-22-alias-columns",
            proposal,
            summary="add DISTINCT to customers",
            detail="the source scan uses an alias column list",
        )
        reference = self.task.reference.model_copy(
            update={
                "sql_by_mart": {
                    self.task.marts[0].name: (
                        "SELECT c.x FROM customers AS c(x, y)"
                    )
                }
            }
        )
        aliased = self.task.model_copy(update={"reference": reference})
        self.assertIsNone(
            cli._source_dedup_equivalence_proof(
                aliased,
                finding,
                _all_pass_add_dedup_outcome(
                    "pa-22-alias-columns", "customers"
                ),
            )
        )

    def test_inapplicable_proposal_reports_its_verdict_not_missing_fidelity(self):
        """An inert optional proposal writes only ``inapplicable.json`` in the
        legacy promoter path.  The attack-stage handoff must retain that
        fail-closed verdict instead of replacing it with a FileNotFoundError
        for the fidelity sidecar it never wrote."""
        from unittest import mock

        from elt_taskgen import cli

        finding = _finding("pa-23-inert", _proposal(INNER_JOIN_TRUTH))
        original = attacks.materialize_mutation

        def inert_proposal(task, case, gold):
            if case.name == "proposed__pa-23-inert":
                raise attacks.InertAstMutationError(
                    "attack proposed__pa-23-inert: mutation kind 'inner_join' "
                    "is inert on every mart (fail closed)"
                )
            return original(task, case, gold)

        with mock.patch.object(
            attacks, "materialize_mutation", side_effect=inert_proposal
        ):
            outcome = self._run_stage([finding])

        self.assertEqual(outcome.verdict, "fail")
        (rejected,) = outcome.payload.rejected_proposals
        self.assertNotIn("FileNotFoundError", rejected["reason"])
        self.assertIn("InertAstMutationError", rejected["reason"])
        self.assertFalse(rejected["fidelity"]["passed"])
        self.assertFalse(rejected["fidelity"]["compiler"]["passed"])
        attack_dir = (
            self.workspace / "tasks" / self.task.task_id / "attacks"
            / "proposed__pa-23-inert"
        )
        fidelity = json.loads(
            (attack_dir / attacks.MUTATION_FIDELITY_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(fidelity["passed"])
        self.assertEqual(fidelity["task_content_hash"], self.task.content_hash())
        record = json.loads(
            (attack_dir / attacks.REJECTED_PROPOSAL_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(record["reason"], rejected["reason"])
        self.assertEqual(record["projection"]["code"], "inert")
        self.assertEqual(
            cli._blocking_proposal_failures(
                [finding],
                tuple(
                    attacks.PromotionOutcome.model_validate(value)
                    for value in outcome.payload.proposal_outcomes
                ),
            )[0],
            [finding],
        )


# ---------------------------------------------------------------------------
# 3. The wire: schema extension + loud rejection of malformed proposals
# ---------------------------------------------------------------------------

VALID_WIRE_PROPOSAL = {
    "kind": "inner_join",
    "params": "{}",
    "expected_pass_by_stage": {
        "extract_load": {
            "development": True,
            "primary": True,
            "resampled": True,
            "counterfactual": True,
            "stress": True,
        },
        "transform": {
            "development": True,
            "primary": False,
            "resampled": False,
            "counterfactual": False,
            "stress": True,
        },
    },
    "rationale": "development and stress cannot distinguish INNER from LEFT",
}


def _wire_finding(proposal):
    return {
        "severity": "major",
        "summary": "populations cannot distinguish an inner join",
        "detail": "no childless customer outside the counterfactual",
        "route_hint": "population",
        "suggested_attack": "inner_join", "disposition": "active",
        "proposed_case": proposal,
    }


class ProposalWireSchemaTest(unittest.TestCase):
    def test_every_critic_schema_carries_proposed_case(self):
        for role in providers.PROPOSAL_ROLES:
            schema = providers.findings_tool_schema(role)
            item = schema["properties"]["findings"]["items"]
            self.assertIn("proposed_case", item["properties"])
            self.assertIn("proposed_case", item["required"])

    def test_proposal_schema_requires_both_complete_stage_maps_only(self):
        schema = providers.proposed_case_schema()
        self.assertNotIn("expected_pass", schema["properties"])
        params_description = schema["properties"]["params"]["description"]
        self.assertIn("add_dedup", params_description)
        self.assertIn("requires kind=custom", params_description)
        self.assertIn("remove_dedup", params_description)
        self.assertIn("requires kind=no_dedup", params_description)
        by_stage = schema["properties"]["expected_pass_by_stage"]
        self.assertEqual(set(by_stage["required"]), {"extract_load", "transform"})
        for stage in by_stage["properties"].values():
            self.assertEqual(set(stage["required"]), {p.value for p in PopulationName})
            self.assertFalse(stage["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {
                "kind", "params", "expected_pass_by_stage", "rationale",
            },
        )

    def test_valid_proposal_survives_normalization_and_parsing(self):
        text = providers._normalized_findings_text(
            "population_adversary", {"findings": [_wire_finding(VALID_WIRE_PROPOSAL)]}
        )
        normalized_wire = json.loads(text)
        wire_proposal = normalized_wire["findings"][0]["proposed_case"]
        self.assertIsInstance(wire_proposal["params"], str)
        self.assertNotIn("expected_pass", wire_proposal)
        self.assertIsNone(
            providers.validate_payload_for(
                "population_adversary",
                {"findings": normalized_wire["findings"]},
            )
        )
        findings = council._parse_findings(
            CouncilRole.POPULATION_ADVERSARY, text, "deadbeef"
        )
        self.assertEqual(len(findings), 1)
        proposal = findings[0].proposed_case
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.kind, AttackKind.INNER_JOIN)
        self.assertEqual(proposal.expected_pass[P.COUNTERFACTUAL], False)
        self.assertEqual(proposal.expected_pass[P.STRESS], True)

    def test_council_consumer_normalizes_only_after_exact_wire_validation(self):
        raw = json.dumps(
            {"findings": [_wire_finding(VALID_WIRE_PROPOSAL)]}
        )
        findings = council._parse_findings(
            CouncilRole.POPULATION_ADVERSARY, raw, "deadbeef"
        )
        self.assertEqual(findings[0].proposed_case.params, {})

        normalized_lookalike = json.loads(json.dumps(VALID_WIRE_PROPOSAL))
        normalized_lookalike["params"] = {}
        normalized_lookalike["expected_pass"] = {
            p.value: True for p in PopulationName
        }
        with self.assertRaisesRegex(
            council.ProviderProtocolError, "active wire schema"
        ):
            council._parse_findings(
                CouncilRole.POPULATION_ADVERSARY,
                json.dumps(
                    {
                        "role": "population_adversary",
                        "findings": [_wire_finding(normalized_lookalike)],
                    }
                ),
                "deadbeef",
            )

        legacy = json.loads(json.dumps(VALID_WIRE_PROPOSAL))
        del legacy["expected_pass_by_stage"]
        legacy["expected_pass"] = {
            p.value: p is PopulationName.DEVELOPMENT for p in PopulationName
        }
        with self.assertRaisesRegex(
            council.ProviderProtocolError, "active wire schema"
        ):
            council._parse_findings(
                CouncilRole.POPULATION_ADVERSARY,
                json.dumps({"findings": [_wire_finding(legacy)]}),
                "deadbeef",
            )

    def test_active_wire_rejects_legacy_redundant_combined_map(self):
        legacy = json.loads(json.dumps(VALID_WIRE_PROPOSAL))
        legacy["expected_pass"] = {p.value: True for p in PopulationName}
        problem = providers._validate_findings_payload(
            {"findings": [_wire_finding(legacy)]}
        )
        self.assertIsNotNone(problem)
        self.assertIn("active wire schema", problem)
        self.assertIn("additional property", problem)

        # Historical records remain parseable by the compatibility normalizer,
        # but that path cannot admit active provider output.
        proposal = providers._normalized_proposal(_wire_finding(legacy))
        self.assertFalse(proposal["expected_pass"]["primary"])
        self.assertTrue(proposal["expected_pass"]["development"])

    def test_active_wire_rejects_legacy_only_matrix_and_decoded_params(self):
        legacy_only = json.loads(json.dumps(VALID_WIRE_PROPOSAL))
        del legacy_only["expected_pass_by_stage"]
        legacy_only["expected_pass"] = {
            p.value: p is PopulationName.DEVELOPMENT for p in PopulationName
        }
        decoded_params = json.loads(json.dumps(VALID_WIRE_PROPOSAL))
        decoded_params["params"] = {}

        for label, proposal in (
            ("legacy expected_pass-only", legacy_only),
            ("decoded params object", decoded_params),
        ):
            with self.subTest(label=label):
                problem = providers._validate_findings_payload(
                    {"findings": [_wire_finding(proposal)]},
                    "population_adversary",
                )
                self.assertIsNotNone(problem)
                self.assertIn("active wire schema", problem)

    def test_proposal_role_requires_and_preserves_explicit_null(self):
        explicit = _wire_finding(None)
        explicit["suggested_attack"] = None
        self.assertIsNone(
            providers._validate_findings_payload(
                {"findings": [explicit]}, "ambiguity_critic"
            )
        )
        text = providers._normalized_findings_text(
            "ambiguity_critic", {"findings": [explicit]}
        )
        self.assertIn("proposed_case", json.loads(text)["findings"][0])
        self.assertIsNone(json.loads(text)["findings"][0]["proposed_case"])

        missing = _wire_finding(None)
        del missing["proposed_case"]
        problem = providers._validate_findings_payload(
            {"findings": [missing]}, "ambiguity_critic"
        )
        self.assertIn("missing required proposed_case", problem)
        self.assertIn("send null", problem)

        raw = json.dumps(
            {"findings": [{key: value for key, value in missing.items()}]}
        )
        with self.assertRaisesRegex(
            council.ProviderProtocolError, "missing required proposed_case"
        ):
            council._parse_findings(
                CouncilRole.AMBIGUITY_CRITIC, raw, "deadbeef"
            )

    def test_runtime_validator_exactly_mirrors_closed_finding_schema(self):
        valid = _wire_finding(None)
        valid["suggested_attack"] = None
        self.assertIsNone(providers._validate_findings_payload(
            {"findings": [valid]}, "ambiguity_critic"
        ))

        for field in (
            "severity",
            "summary",
            "detail",
            "route_hint",
            "suggested_attack",
            "proposed_case",
        ):
            with self.subTest(missing=field):
                item = dict(valid)
                del item[field]
                problem = providers._validate_findings_payload(
                    {"findings": [item]}, "ambiguity_critic"
                )
                self.assertIsNotNone(problem)
                self.assertIn("missing required", problem)

        for payload, unknown in (
            ({"findings": [valid], "accept": True}, "accept"),
            ({"findings": [{**valid, "verdict": "pass"}]}, "verdict"),
        ):
            with self.subTest(unknown=unknown):
                problem = providers._validate_findings_payload(
                    payload, "ambiguity_critic"
                )
                self.assertIsNotNone(problem)
                self.assertIn("unknown field", problem)
                self.assertIn(unknown, problem)

        empty_summary = {**valid, "summary": "   "}
        self.assertIn(
            "summary is empty",
            providers._validate_findings_payload(
                {"findings": [empty_summary]}, "ambiguity_critic"
            ),
        )

    def test_one_shot_backend_retries_unknown_response_fields_then_fails(self):
        """Strict response validation is enforced even if transport is lax."""
        try:
            from tests import test_providers as tp
        except ImportError:
            import test_providers as tp

        invalid = {**_wire_finding(None), "accept": True}
        transport = tp.FakeTransport(
            [tp.anthropic_tool_response([invalid])] *
            (1 + providers.SCHEMA_RETRIES)
        )
        backend = providers.AnthropicBackend("sk-test", transport=transport)
        with self.assertRaises(council.ProviderProtocolError) as caught:
            backend.complete(
                role_name="ambiguity_critic",
                model="claude-sonnet-5",
                prompt="THE VIEW",
                max_tokens=1024,
                effort="medium",
            )
        self.assertIn("unknown field", str(caught.exception))
        self.assertIn("accept", str(caught.exception))
        self.assertEqual(len(transport.calls), 1 + providers.SCHEMA_RETRIES)

    # -- malformed proposals are SCHEMA VIOLATIONS, never dropped fields ----

    def _malformed(self):
        partial = json.loads(json.dumps(VALID_WIRE_PROPOSAL))
        del partial["expected_pass_by_stage"]["transform"]["stress"]
        unknown_kind = json.loads(json.dumps(VALID_WIRE_PROPOSAL))
        unknown_kind["kind"] = "telepathy"
        no_rationale = json.loads(json.dumps(VALID_WIRE_PROPOSAL))
        no_rationale["rationale"] = ""
        return {
            "partial expectation": partial,
            "unknown kind": unknown_kind,
            "empty rationale": no_rationale,
            "not an object": "inner_join, trust me",
        }

    def test_council_parse_rejects_malformed_proposals_loudly(self):
        for label, proposal in self._malformed().items():
            with self.subTest(label):
                raw = json.dumps(
                    {"role": "population_adversary",
                     "findings": [_wire_finding(proposal)]}
                )
                with self.assertRaises(council.ProviderProtocolError):
                    council._parse_findings(
                        CouncilRole.POPULATION_ADVERSARY, raw, "deadbeef"
                    )

    def test_provider_payload_validation_flags_malformed_proposals(self):
        for label, proposal in self._malformed().items():
            with self.subTest(label):
                problem = providers._validate_findings_payload(
                    {"findings": [_wire_finding(proposal)]}
                )
                self.assertIsNotNone(problem, "malformed proposal accepted")
                self.assertIn("proposed_case", problem)
        self.assertIsNone(
            providers._validate_findings_payload(
                {"findings": [_wire_finding(VALID_WIRE_PROPOSAL)]}
            )
        )

    def test_backend_raises_after_bounded_retries_on_a_bad_proposal(self):
        """A malformed proposal is never repaired by dropping the field: the
        backend retries, then the review stage fails."""
        try:
            from tests import test_providers as tp
        except ImportError:
            import test_providers as tp

        bad = _wire_finding(self._malformed()["partial expectation"])
        transport = tp.FakeTransport(
            [tp.anthropic_tool_response([bad])] * (1 + providers.SCHEMA_RETRIES)
        )
        backend = providers.AnthropicBackend("sk-test", transport=transport)
        with self.assertRaises(council.ProviderProtocolError) as ctx:
            backend.complete(
                role_name="population_adversary", model="claude-sonnet-5",
                prompt="THE VIEW", max_tokens=1024, effort="medium",
            )
        self.assertIn("proposed_case", str(ctx.exception))
        self.assertEqual(len(transport.calls), 1 + providers.SCHEMA_RETRIES)

    def test_backend_retries_then_rejects_legacy_only_proposal(self):
        """Compatibility fields never bypass the current provider protocol."""
        try:
            from tests import test_providers as tp
        except ImportError:
            import test_providers as tp

        legacy = json.loads(json.dumps(VALID_WIRE_PROPOSAL))
        del legacy["expected_pass_by_stage"]
        legacy["expected_pass"] = {
            p.value: p is PopulationName.DEVELOPMENT for p in PopulationName
        }
        bad = _wire_finding(legacy)
        transport = tp.FakeTransport(
            [tp.anthropic_tool_response([bad])] * (1 + providers.SCHEMA_RETRIES)
        )
        backend = providers.AnthropicBackend("sk-test", transport=transport)
        with self.assertRaises(council.ProviderProtocolError) as ctx:
            backend.complete(
                role_name="population_adversary",
                model="claude-sonnet-5",
                prompt="THE VIEW",
                max_tokens=1024,
                effort="medium",
            )
        self.assertIn("active wire schema", str(ctx.exception))
        self.assertEqual(len(transport.calls), 1 + providers.SCHEMA_RETRIES)

    def test_backend_round_trips_a_valid_proposal_to_the_council(self):
        try:
            from tests import test_providers as tp
        except ImportError:
            import test_providers as tp

        transport = tp.FakeTransport(
            [tp.anthropic_tool_response([_wire_finding(VALID_WIRE_PROPOSAL)])]
        )
        backend = providers.AnthropicBackend("sk-test", transport=transport)
        result = backend.complete(
            role_name="population_adversary", model="claude-sonnet-5",
            prompt="THE VIEW", max_tokens=1024, effort="medium",
        )
        tool = transport.calls[0][2]["tools"][0]
        self.assertIn(
            "proposed_case",
            tool["input_schema"]["properties"]["findings"]["items"]["properties"],
        )
        findings = council._parse_findings(
            CouncilRole.POPULATION_ADVERSARY, result.text, "deadbeef"
        )
        self.assertIsNotNone(findings[0].proposed_case)


class AdversaryPromptTest(unittest.TestCase):
    def test_prompt_states_the_promoter_contract(self):
        from elt_taskgen.review import prompts

        text = prompts.ROLE_SYSTEM["population_adversary"].lower()
        self.assertIn("proposed_case", text)
        self.assertIn("expected_pass_by_stage", text)
        self.assertNotRegex(text, r"\bexpected_pass\b")
        for pop in PopulationName:
            self.assertIn(pop.value, text)
        self.assertIn("exactly", text)      # exact-match promotion
        self.assertIn("rejected proposal", text)
        self.assertIn("no authority to accept", text)


_BATCH_TASKS = Path(__file__).resolve().parents[1] / "runs" / "authorized_batch_50_20260908" / "workspace-final" / "tasks"


class ClaimFidelityWholeWordTest(unittest.TestCase):
    """Review finding 0-0 (batch-repair round 2): the relaxed claim matcher
    accepted a prose naming a DIFFERENT object whenever the structured
    target's tokens were a contiguous sub-run of it (`deals_flow` satisfied
    `deals`, `users_access_logs_rollup` satisfied `users`) and an identifier
    split across a sentence break ("the deals. Flow rows" satisfied
    `deals_flow`) — 619 such (table, other object) pairs across the 50 batch
    IRs.  Identifiers now match as WHOLE prose words (or a label of whole
    words joined by whitespace only); the D3 positives (`second-hop`,
    `Second Hop`) stay.  Seeded from scratchpad/impl/batch/probe_r2_claim_
    whole_word.py; the batch's 81 distinct recorded proposals all still
    pass (probe_r2_claim_ledger_scan.py)."""

    def test_claim_fidelity_matches_whole_words_never_token_subruns(self):
        for text, identifier, want in (
            ("second-hop join", "second_hop", True),
            ("Second Hop join", "second_hop", True),
            ("the second_hop", "second_hop", True),
            ("the hop", "second_hop", False),
            ("customers", "customer", False),
            ("`deals_flow` rows", "deals", False),
            ("deals_participants duplicates", "deals", False),
            ("users_access_logs_rollup", "users", False),
            ("deals_flow_rate", "deals_flow", False),
            ("deals. Flow", "deals_flow", False),
            ("deals, flow", "deals_flow", False),
            ("(deals) flow", "deals_flow", False),
            ("deals; flow", "deals_flow", False),
            ("deals flow", "deals_flow", True),
            ("Deals-Flow", "deals_flow", True),
            ("skip 'deals'.", "deals", True),
            ("the deals table", "deals", True),
            # A single-token target inside a longer identifier-like word is
            # that OTHER word (decision 0-0: whole identifiers on token
            # boundaries, never a sub-run): `primary-key` is not `primary`.
            ("drop the primary-key dedup", "primary", False),
            ("hard-code the primary population", "primary", True),
            # A RUN of separators joins one word (the dbt `package__model`
            # convention): the mart never names its source table, and a
            # `__`-joined name never names either half (recheck 0-0).
            ("twitter_ads__promoted_tweet_report", "promoted_tweet_report", False),
            ("twitter_ads__account_report", "account_report", False),
            ("deals__flow", "deals", False),
            ("deals__flow", "flow", False),
            ("stg__deals", "deals", False),
            ("deals--flow", "flow", False),
            ("skip `promoted_tweet_report`", "promoted_tweet_report", True),
            ("the twitter_ads__promoted_tweet_report mart", "twitter_ads__promoted_tweet_report", True),
            # `.` is never inside a word: a qualified name still names its
            # table, a dotted pair never names the underscore identifier.
            ("deals.flow", "deals_flow", False),
            ("analytics.deals", "deals", True),
            # The residual, by design: an English word that IS the identifier
            # names it — the structured params stay the authority on WHAT.
            ("the total is wrong", "total", True),
            ("filter before aggregate", "filter_before_aggregate", True),
            ("", "deals", False),
            ("deals", "", False),
        ):
            with self.subTest(text=text, identifier=identifier):
                self.assertIs(attacks.claim_names_identifier(text, identifier), want)
        self.assertEqual(
            attacks.missing_claim_identifiers("skip deals_flow; keep deals", ("deals", "deals_flow", "users")),
            ("users",),
        )

    @unittest.skipUnless((_BATCH_TASKS / "dlt__pipedrive" / "task_ir.json").is_file(),
                         "batch evidence runs/authorized_batch_50_20260908 not present")
    def test_pipedrive_prose_naming_another_table_fails_fidelity(self):
        """The reviewer's exact shapes on the dlt__pipedrive IR through the
        REAL `validate_proposed_case`: `skip_tables=["deals"]` with prose
        naming only `deals_flow`, and `skip_tables=["deals_flow"]` with prose
        "Skipping the deals. Flow rows are unaffected", both fail claim
        fidelity naming the unnamed target; the same targets written out
        as whole words (hyphenated or capitalized) pass."""
        task = TaskIR.model_validate_json((_BATCH_TASKS / "dlt__pipedrive" / "task_ir.json").read_text())
        all_true = {p: True for p in PopulationName}

        def check(params, summary, detail, rationale="the mart stays graded"):
            finding = _finding(
                "population_adversary-00-batch", None, summary=summary, detail=detail,
                suggested_attack=AttackKind.SKIP_EXTRACTION,
            )
            proposal = _proposal(all_true, kind=AttackKind.SKIP_EXTRACTION, params=params, rationale=rationale)
            _case, fidelity = attacks.validate_proposed_case(task, finding, proposal, required=False)
            return bool(fidelity["passed"]), tuple(str(e) for e in fidelity.get("errors", ()))

        passed, errors = check({"skip_tables": ["deals"]}, "skip the deals_flow extraction",
                               "deals_flow rows are not loaded", "deals_flow only")
        self.assertFalse(passed)
        self.assertIn("deals", errors[0])
        passed, errors = check({"skip_tables": ["deals_flow"]}, "Skipping the deals.", "Flow rows are unaffected")
        self.assertFalse(passed)
        self.assertIn("deals_flow", errors[0])
        self.assertTrue(check({"skip_tables": ["deals_flow"]}, "skip the deals-flow table", "rows gone")[0])
        self.assertTrue(check({"skip_tables": ["deals_flow"]}, "skip the Deals Flow table", "rows gone")[0])
        self.assertTrue(check({"skip_tables": ["deals"]}, "skip the deals table", "rows gone")[0])

    @unittest.skipUnless((_BATCH_TASKS / "dbt__twitter_ads__twitter_ads__account_report_10fe7453" / "task_ir.json").is_file(),
                         "batch evidence runs/authorized_batch_50_20260908 not present")
    def test_twitter_ads_mart_prose_never_names_its_source_table(self):
        """Recheck 0-0: on the dbt__twitter_ads batch IR, `skip_tables=
        ["promoted_tweet_report"]` (a source table) with prose naming only the
        mart `twitter_ads__promoted_tweet_report` fails claim fidelity naming
        the source table; naming the source table as a whole word passes."""
        task = TaskIR.model_validate_json(
            (_BATCH_TASKS / "dbt__twitter_ads__twitter_ads__account_report_10fe7453" / "task_ir.json").read_text())
        all_true = {p: True for p in PopulationName}

        def check(params, summary, detail, rationale="the mart stays graded"):
            finding = _finding(
                "population_adversary-00-batch", None, summary=summary, detail=detail,
                suggested_attack=AttackKind.SKIP_EXTRACTION,
            )
            proposal = _proposal(all_true, kind=AttackKind.SKIP_EXTRACTION, params=params, rationale=rationale)
            _case, fidelity = attacks.validate_proposed_case(task, finding, proposal, required=False)
            return bool(fidelity["passed"]), tuple(str(e) for e in fidelity.get("errors", ()))

        passed, errors = check({"skip_tables": ["promoted_tweet_report"]},
                               "skip the twitter_ads__promoted_tweet_report extraction",
                               "twitter_ads__promoted_tweet_report rows are not loaded",
                               "twitter_ads__promoted_tweet_report only")
        self.assertFalse(passed)
        self.assertIn("promoted_tweet_report", errors[0])
        self.assertTrue(check({"skip_tables": ["promoted_tweet_report"]},
                              "skip the promoted_tweet_report source", "rows gone")[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

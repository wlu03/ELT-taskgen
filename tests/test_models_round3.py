"""WHY THIS EXISTS

Round 3 added three things to the frozen shared IR (`models.py`):
ProposedAttackCase + Finding.proposed_case, RepairEdit/RepairPatch, and the
per-variant EmpiricalDifficulty calibration payload. Every one of them is
ADDITIVE, which is a claim that has to be enforced rather than asserted:

  1. HASH STABILITY. Task identity is a content hash, and the whole pipeline
     binds evidence, gates, audit approvals and releases to it. If a model
     edit silently moves that hash, every previously frozen artifact detaches
     from the task it was measured against. The literal below is a baseline
     recovered from the Round-2 ledger BEFORE these additions landed
     (taskgen-workspace/state/taskgen.sqlite), so this test fails loudly the
     day an "additive" change is not.

  2. FAIL-CLOSED CONSTRUCTION. These models encode pipeline invariants as
     validators (a proposal must be falsifiable on all five populations; a
     patch may not claim FATAL; extract_load has no stage 2). Those validators
     are load-bearing, so each one gets a test that proves it rejects.
"""

import unittest

from pydantic import ValidationError

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.models import (
    AttackKind,
    EmpiricalDifficulty,
    Finding,
    PopulationName,
    ProposedAttackCase,
    RepairEdit,
    RepairEditOp,
    RepairPatch,
    RepairRoute,
    SolverTierResult,
    TaskVariant,
    VariantCalibration,
    solver_roster_fingerprint,
)

# Pinned demo identity. Update only for an intentional contract change; a
# mismatch means generated evidence and transcripts must be revalidated.
DEMO_TASK_CONTENT_HASH = (
    "c2e0744cfc85657f6b1680600cc35ff9bfaf67a8548b21d9d8c57ea65d56ee44"
)

ALL_POPS = {p: True for p in PopulationName}


def _proposal(**over):
    kw = dict(
        kind=AttackKind.INNER_JOIN,
        expected_pass={**ALL_POPS, PopulationName.COUNTERFACTUAL: False},
        rationale="inner join drops childless customers; only P3 has them",
    )
    kw.update(over)
    return ProposedAttackCase(**kw)


class TestHashStability(unittest.TestCase):
    """The additive claim, enforced against a pre-Round-3 baseline."""

    def test_demo_task_content_hash_unchanged(self):
        self.assertEqual(demo_task().content_hash(), DEMO_TASK_CONTENT_HASH)


class TestProposedAttackCase(unittest.TestCase):
    def test_round_trip(self):
        p = _proposal()
        self.assertEqual(ProposedAttackCase.model_validate_json(p.model_dump_json()), p)

    def test_partial_expectation_rejected(self):
        """A proposer must commit to all five populations up front."""
        partial = {
            PopulationName.PRIMARY: True,
            PopulationName.COUNTERFACTUAL: False,
        }
        with self.assertRaises(ValidationError) as ctx:
            _proposal(expected_pass=partial)
        self.assertIn("all five populations", str(ctx.exception).lower())

class TestFindingBackCompat(unittest.TestCase):
    def test_pre_round3_finding_json_still_validates(self):
        """Old serialized findings carry no proposed_case field."""
        legacy = (
            '{"finding_id":"old-1","role":"ambiguity_critic","severity":"minor",'
            '"summary":"tie-break unstated","detail":"","route_hint":null,'
            '"suggested_attack":null}'
        )
        self.assertIsNone(Finding.model_validate_json(legacy).proposed_case)


class TestRepairEdit(unittest.TestCase):
    def test_replace_must_change_text(self):
        with self.assertRaises(ValidationError):
            RepairEdit(op=RepairEditOp.REPLACE, locator="L1", old="same", new="same")

    def test_replace_requires_old(self):
        with self.assertRaises(ValidationError):
            RepairEdit(op=RepairEditOp.REPLACE, locator="L1", old="", new="x")

    def test_insert_rejects_old(self):
        with self.assertRaises(ValidationError):
            RepairEdit(op=RepairEditOp.INSERT, locator="L1", old="x", new="y")

    def test_delete_rejects_new(self):
        with self.assertRaises(ValidationError):
            RepairEdit(op=RepairEditOp.DELETE, locator="L1", old="x", new="y")

    def test_valid_ops_construct(self):
        for kw in (
            dict(op=RepairEditOp.REPLACE, old="a", new="b"),
            dict(op=RepairEditOp.INSERT, old="", new="b"),
            dict(op=RepairEditOp.DELETE, old="a", new=""),
        ):
            self.assertIsNotNone(RepairEdit(locator="L1", **kw))


class TestRepairPatch(unittest.TestCase):
    def _patch(self, **over):
        kw = dict(
            route=RepairRoute.SPECIFICATION,
            artifact="task/data_model.yaml",
            edits=(
                RepairEdit(
                    op=RepairEditOp.REPLACE,
                    locator="customer_summary.total_spend",
                    old="sum of spend",
                    new="sum of quantity * unit_price over completed orders",
                ),
            ),
            rationale="ambiguity critic: the measure was unstated",
            proposer_role="repair_proposer",
        )
        kw.update(over)
        return RepairPatch(**kw)

    def test_round_trip(self):
        p = self._patch()
        self.assertEqual(RepairPatch.model_validate_json(p.model_dump_json()), p)

class TestEmpiricalDifficulty(unittest.TestCase):
    """Per-variant calibration: EL has no stage 2, T is handed stage 1."""

    def _tier(self, key="claude-haiku-4-5", **over):
        kw = dict(model_key=key, k=8, successes=3, stage1_failures=0, stage2_failures=0)
        kw.update(over)
        return SolverTierResult(**kw)

    def _empirical(self, variants):
        keys = {t.model_key for vc in variants.values() for t in vc.tiers}
        return EmpiricalDifficulty(
            solver_config="round3-roster",
            n_attempts=8,
            success_rate=0.375,
            stage1_failure_rate=0.0,
            stage2_failure_rate=1.0,
            variants=variants,
            roster_fingerprint=solver_roster_fingerprint(keys) if keys else "",
            campaign_fingerprint="1" * 64 if keys else "",
        )

    def test_pre_round3_construction_still_valid(self):
        """All Round-3 fields default; older measurements construct unchanged."""
        e = EmpiricalDifficulty(
            solver_config="legacy",
            n_attempts=4,
            success_rate=0.5,
            stage1_failure_rate=0.5,
            stage2_failure_rate=0.5,
        )
        self.assertEqual(e.variants, {})
        self.assertEqual(e.roster_fingerprint, "")
        self.assertEqual(e.campaign_fingerprint, "")

    def test_per_variant_round_trip_and_vectors(self):
        vc = VariantCalibration(
            variant=TaskVariant.TRANSFORM, tiers=(self._tier(stage2_failures=5),)
        )
        e = self._empirical({TaskVariant.TRANSFORM: vc})
        self.assertEqual(
            EmpiricalDifficulty.model_validate_json(e.model_dump_json()), e
        )
        self.assertEqual(
            e.variants[TaskVariant.TRANSFORM].pass_rate_vector(),
            {"claude-haiku-4-5": 3 / 8},
        )

    def test_extract_load_cannot_attribute_stage2_failures(self):
        with self.assertRaises(ValidationError) as ctx:
            VariantCalibration(
                variant=TaskVariant.EXTRACT_LOAD, tiers=(self._tier(stage2_failures=2),)
            )
        self.assertIn("no stage 2", str(ctx.exception))

    def test_transform_cannot_attribute_stage1_failures(self):
        with self.assertRaises(ValidationError) as ctx:
            VariantCalibration(
                variant=TaskVariant.TRANSFORM, tiers=(self._tier(stage1_failures=2),)
            )
        self.assertIn("handed stage 1", str(ctx.exception))

    def test_tiers_must_be_sorted_and_unique(self):
        with self.assertRaises(ValidationError):
            VariantCalibration(
                variant=TaskVariant.FULL,
                tiers=(self._tier("claude-sonnet-5"), self._tier("claude-haiku-4-5")),
            )
        with self.assertRaises(ValidationError):
            VariantCalibration(
                variant=TaskVariant.FULL,
                tiers=(self._tier("claude-haiku-4-5"), self._tier("claude-haiku-4-5")),
            )

    def test_roster_fingerprint_must_match_recorded_tiers(self):
        """A measurement cannot claim a roster it did not run."""
        vc = VariantCalibration(variant=TaskVariant.FULL, tiers=(self._tier(),))
        with self.assertRaises(ValidationError) as ctx:
            EmpiricalDifficulty(
                solver_config="r",
                n_attempts=8,
                success_rate=0.5,
                stage1_failure_rate=0.0,
                stage2_failure_rate=1.0,
                variants={TaskVariant.FULL: vc},
                roster_fingerprint=solver_roster_fingerprint({"some-other-model"}),
            )
        self.assertIn("roster_fingerprint", str(ctx.exception))

    def test_every_variant_must_measure_the_exact_same_solver_roster(self):
        el = VariantCalibration(
            variant=TaskVariant.EXTRACT_LOAD,
            tiers=(self._tier("solver-a", stage1_failures=5),),
        )
        transform = VariantCalibration(
            variant=TaskVariant.TRANSFORM,
            tiers=(self._tier("solver-b", stage2_failures=5),),
        )
        with self.assertRaises(ValidationError) as ctx:
            self._empirical(
                {
                    TaskVariant.EXTRACT_LOAD: el,
                    TaskVariant.TRANSFORM: transform,
                }
            )
        self.assertIn("exactly the same solver roster", str(ctx.exception))

    def test_every_variant_must_use_the_same_tier_attempt_counts(self):
        el = VariantCalibration(
            variant=TaskVariant.EXTRACT_LOAD,
            tiers=(self._tier(stage1_failures=5),),
        )
        transform = VariantCalibration(
            variant=TaskVariant.TRANSFORM,
            tiers=(self._tier(k=4, successes=2, stage2_failures=2),),
        )
        with self.assertRaises(ValidationError) as ctx:
            self._empirical(
                {
                    TaskVariant.EXTRACT_LOAD: el,
                    TaskVariant.TRANSFORM: transform,
                }
            )
        self.assertIn("same solver tier attempt counts", str(ctx.exception))

    def test_aggregate_claims_must_reproduce_from_tier_counts(self):
        calibration = VariantCalibration(
            variant=TaskVariant.TRANSFORM,
            tiers=(self._tier(stage2_failures=5),),
        )
        valid = self._empirical({TaskVariant.TRANSFORM: calibration})
        contradictions = {
            "n_attempts": 9,
            "success_rate": 0.5,
            "stage1_failure_rate": 0.25,
            "stage2_failure_rate": 0.5,
        }
        for field, value in contradictions.items():
            with (
                self.subTest(field=field),
                self.assertRaises(ValidationError) as ctx,
            ):
                EmpiricalDifficulty.model_validate(
                    {**valid.model_dump(mode="python"), field: value}
                )
            self.assertIn(field, str(ctx.exception))
            self.assertIn("tier attempts", str(ctx.exception))

    def test_zero_failed_attempts_require_zero_stage_rates(self):
        tier = self._tier(k=2, successes=2)
        calibration = VariantCalibration(
            variant=TaskVariant.TRANSFORM, tiers=(tier,)
        )
        with self.assertRaises(ValidationError) as ctx:
            EmpiricalDifficulty(
                solver_config="r",
                n_attempts=2,
                success_rate=1.0,
                stage1_failure_rate=0.1,
                stage2_failure_rate=0.0,
                variants={TaskVariant.TRANSFORM: calibration},
                roster_fingerprint=solver_roster_fingerprint({tier.model_key}),
            )
        self.assertIn("stage1_failure_rate", str(ctx.exception))

    def test_fingerprint_without_variants_rejected(self):
        with self.assertRaises(ValidationError):
            EmpiricalDifficulty(
                solver_config="r",
                n_attempts=1,
                success_rate=0.0,
                stage1_failure_rate=0.0,
                stage2_failure_rate=0.0,
                roster_fingerprint="deadbeef",
            )


if __name__ == "__main__":
    unittest.main()

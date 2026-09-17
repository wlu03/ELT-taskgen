"""Exact critic-to-mutation handoff and stage-matrix evidence."""

from __future__ import annotations

import unittest

from pydantic import ValidationError
from sqlglot import exp, parse_one

from elt_taskgen import demo_fixture
from elt_taskgen.models import (
    AttackKind,
    Backend,
    CouncilRole,
    Finding,
    PopulationName,
    ProposedAttackCase,
    Severity,
    TaskVariant,
)
from elt_taskgen.reference.gold import GoldBundle
from elt_taskgen.verification import attacks

try:
    from tests import test_attack_promotion as promotion_tests
except ImportError:  # pragma: no cover - unittest discovery from tests/
    import test_attack_promotion as promotion_tests

P = PopulationName


def _expected():
    transform = {population: population is P.DEVELOPMENT for population in P}
    extract_load = {population: True for population in P}
    return transform, {
        TaskVariant.EXTRACT_LOAD: extract_load,
        TaskVariant.TRANSFORM: transform,
    }


def _proposal(kind: AttackKind, params: dict) -> ProposedAttackCase:
    combined, stages = _expected()
    return ProposedAttackCase(
        kind=kind,
        params=params,
        expected_pass=combined,
        expected_pass_by_stage=stages,
        rationale="execute the exact named alternative",
    )


def _finding(proposal: ProposedAttackCase, text: str) -> Finding:
    return Finding(
        finding_id="fidelity-00",
        role=CouncilRole.AMBIGUITY_CRITIC,
        severity=Severity.MAJOR,
        summary=text,
        detail=text,
        suggested_attack=proposal.kind,
        proposed_case=proposal,
    )


class ProposalContractTest(unittest.TestCase):
    def test_combined_matrix_must_equal_el_and_t(self):
        combined, stages = _expected()
        combined = dict(combined)
        combined[P.PRIMARY] = True  # EL true AND T false => combined must be false
        with self.assertRaisesRegex(ValidationError, "extract_load AND transform"):
            ProposedAttackCase(
                kind=AttackKind.INNER_JOIN,
                params={},
                expected_pass=combined,
                expected_pass_by_stage=stages,
                rationale="inconsistent prediction",
            )

    def test_unknown_or_conflicting_params_are_never_ignored(self):
        with self.assertRaisesRegex(ValueError, "unsupported proposal"):
            attacks._proposal_mutation(
                _finding(_proposal(AttackKind.CUSTOM, {"telepathy": True}), "x"),
                _proposal(AttackKind.CUSTOM, {"telepathy": True}),
            )
        proposal = _proposal(
            AttackKind.CUSTOM,
            {"copy_mart": ("a", "b"), "add_dedup": True},
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            attacks._proposal_mutation(_finding(proposal, "a b"), proposal)

    def test_hardcoded_population_requires_the_constants_kind(self):
        proposal = _proposal(
            AttackKind.INNER_JOIN,
            {attacks.HARDCODE_PARAM: PopulationName.PRIMARY.value},
        )
        finding = _finding(
            proposal,
            "hard-code primary outputs for customer_summary",
        )
        with self.assertRaisesRegex(ValueError, "kind 'constants'"):
            attacks.validate_proposed_case(
                demo_fixture.demo_task(), finding, proposal
            )

    def test_structured_constants_never_infers_hardcode_target_from_prose(self):
        proposal = _proposal(AttackKind.CONSTANTS, {})
        finding = _finding(
            proposal,
            "emit the primary population outputs verbatim",
        )

        self.assertEqual(
            attacks._proposal_mutation(finding, proposal),
            f"{attacks.KIND_DIRECTIVE_PREFIX}constants",
        )

    def test_structured_custom_never_infers_operation_from_prose(self):
        proposal = _proposal(AttackKind.CUSTOM, {})
        finding = _finding(
            proposal,
            "emit the primary population outputs verbatim",
        )

        with self.assertRaisesRegex(ValueError, "has no default variant"):
            attacks._proposal_mutation(finding, proposal)

    def test_claim_must_name_every_identifier_bearing_target(self):
        proposal = _proposal(
            AttackKind.SKIP_EXTRACTION, {"skip_tables": ("orders", "customers")}
        )
        finding = _finding(proposal, "skip orders")
        case = attacks._candidate_case(finding, proposal, required=False)
        fidelity = attacks.validate_proposal_claim_fidelity(
            demo_fixture.demo_task(), finding, proposal, case
        )
        self.assertFalse(fidelity["passed"])
        self.assertIn("customers", fidelity["errors"][0])

    def test_active_validator_refuses_legacy_combined_only_prediction(self):
        combined, _stages = _expected()
        proposal = ProposedAttackCase(
            kind=AttackKind.INNER_JOIN,
            params={},
            expected_pass=combined,
            rationale="legacy combined prediction",
        )
        finding = _finding(proposal, "inner join customer_summary")
        with self.assertRaisesRegex(ValueError, "legacy combined-only"):
            attacks.validate_proposed_case(
                demo_fixture.demo_task(), finding, proposal
            )

    def test_named_variant_must_appear_in_the_critic_claim(self):
        try:
            from tests import test_attack_variants as variant_tests
        except ImportError:  # pragma: no cover - discovery from tests/
            import test_attack_variants as variant_tests

        base = demo_fixture.demo_task()
        task = base.model_copy(
            update={
                "marts": (variant_tests._HAVING_MART,),
                "reference": base.reference.model_copy(
                    update={"sql_by_mart": {"m": variant_tests.HAVING_PLAN}}
                ),
            }
        )
        proposal = _proposal(
            AttackKind.WRONG_AGG_STAGE,
            {"variant": "filter_before_aggregate"},
        )
        finding = _finding(
            proposal,
            "MAX, MIN, and FIRST can disagree for mart m",
        )
        _case, fidelity = attacks.validate_proposed_case(task, finding, proposal)
        self.assertFalse(fidelity["passed"])
        self.assertIn("filter_before_aggregate", fidelity["errors"][0])

    def test_multi_mart_kind_claim_must_name_every_realized_target(self):
        task = demo_fixture.demo_task()
        first = task.marts[0]
        second_name = "customer_summary_copy"
        second = first.model_copy(
            update={
                "name": second_name,
                "plan": first.plan.model_copy(update={"mart": second_name}),
            }
        )
        task = task.model_copy(
            update={
                "marts": (first, second),
                "reference": task.reference.model_copy(
                    update={
                        "sql_by_mart": {
                            first.name: task.reference.sql_by_mart[first.name],
                            second_name: task.reference.sql_by_mart[first.name],
                        }
                    }
                ),
            }
        )
        proposal = _proposal(AttackKind.INNER_JOIN, {})
        finding = _finding(
            proposal,
            "inner join changes customer_summary only",
        )
        _case, fidelity = attacks.validate_proposed_case(task, finding, proposal)
        self.assertFalse(fidelity["passed"])
        self.assertIn(second_name, fidelity["errors"][0])

    def test_kind_realization_records_exact_static_mart_targets(self):
        task = demo_fixture.demo_task()
        proposal = _proposal(AttackKind.INNER_JOIN, {})
        finding = _finding(proposal, "inner join customer_summary")
        case, fidelity = attacks.validate_proposed_case(task, finding, proposal)
        self.assertTrue(fidelity["passed"])
        sql_by_mart = attacks.materialize_mutation(task, case, None)
        requested, realized, checks = attacks._validate_realized_fidelity(
            task, case, sql_by_mart, None
        )
        self.assertEqual(requested["target_marts"], [demo_fixture.MART_NAME])
        self.assertEqual(realized["target_marts"], [demo_fixture.MART_NAME])
        self.assertIn("statically applicable mart targets", checks[0])


class StructuredMaterializationTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()

    def _case(self, kind: AttackKind, params: dict, text: str):
        proposal = _proposal(kind, params)
        finding = _finding(proposal, text)
        return proposal, finding, attacks._candidate_case(
            finding, proposal, required=False
        )

    def test_zero_is_missing_is_not_compiled_as_remove_coalesce(self):
        _proposal_obj, _finding_obj, case = self._case(
            AttackKind.NO_NULL_DEFAULT,
            {"zero_is_missing": True},
            "treat zero as missing",
        )
        sql = attacks.materialize_mutation(self.task, case, None)[
            demo_fixture.MART_NAME
        ]
        ast = parse_one(sql, read="duckdb")
        self.assertTrue(list(ast.find_all(exp.Coalesce)))
        self.assertTrue(list(ast.find_all(exp.Nullif)))

    def test_add_and_remove_dedup_are_opposite_executable_operations(self):
        _p, _f, add_case = self._case(
            AttackKind.CUSTOM,
            {"add_dedup": True, "dedup_table": "orders"},
            "add dedup to orders",
        )
        add_sql = attacks.materialize_mutation(self.task, add_case, None)[
            demo_fixture.MART_NAME
        ]
        self.assertGreater(add_sql.upper().count("DISTINCT"), 1)

        _p, _f, remove_case = self._case(
            AttackKind.NO_DEDUP,
            {"remove_dedup": True},
            "remove dedup",
        )
        remove_sql = attacks.materialize_mutation(self.task, remove_case, None)[
            demo_fixture.MART_NAME
        ]
        self.assertNotIn("SELECT DISTINCT\n    ORDER_ID", remove_sql.upper())

    def test_skip_backend_and_tables_resolve_exact_targets(self):
        gold = GoldBundle(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage1={
                population.value: {table.name: 10 for table in self.task.tables}
                for population in P
            },
            stage2_csv={},
            file_hashes={},
        )
        backend_plan = attacks.resolve_load_mutation(
            self.task, "skip_backend", Backend.MONGODB.value, gold
        )
        expected = {
            assignment.table
            for assignment in self.task.backends
            if assignment.backend is Backend.MONGODB
        }
        self.assertEqual(set(backend_plan.ops), expected)

        table_plan = attacks.resolve_load_mutation(
            self.task, "skip_tables", '["orders","customers"]', gold
        )
        self.assertEqual(table_plan.omit_tables, ("customers", "orders"))


class LiveCopyMartExploitTest(
    promotion_tests._DemoFixtureMixin, unittest.TestCase
):
    def test_copy_mart_is_measured_faithfully_and_all_pass_blocks(self):
        source = self.task.marts[0]
        target_name = "copied_summary"
        target = source.model_copy(
            update={
                "name": target_name,
                "plan": source.plan.model_copy(update={"mart": target_name}),
            }
        )
        reference = self.task.reference.model_copy(
            update={
                "sql_by_mart": {
                    source.name: demo_fixture.REFERENCE_SQL,
                    target_name: demo_fixture.REFERENCE_SQL,
                }
            }
        )
        task = self.task.model_copy(
            update={"marts": (source, target), "reference": reference}
        )
        stage2 = {
            population: {
                source.name: marts[source.name],
                target_name: marts[source.name],
            }
            for population, marts in self.gold.stage2_csv.items()
        }
        gold = promotion_tests.ta._Gold(
            task.task_id,
            task.content_hash(),
            self.gold.stage1,
            stage2,
        )
        all_pass = {population: True for population in P}
        proposal = ProposedAttackCase(
            kind=AttackKind.CUSTOM,
            params={"copy_mart": (source.name, target_name)},
            expected_pass=all_pass,
            expected_pass_by_stage={
                TaskVariant.EXTRACT_LOAD: all_pass,
                TaskVariant.TRANSFORM: all_pass,
            },
            rationale=f"copy {source.name} into {target_name}",
        )
        finding = Finding(
            finding_id="copy-live",
            role=CouncilRole.SHORTCUT_ATTACKER,
            severity=Severity.MAJOR,
            summary=f"copy {source.name} into {target_name}",
            detail=f"reuse {source.name} as {target_name}",
            suggested_attack=AttackKind.CUSTOM,
            proposed_case=proposal,
        )
        result = attacks.promote_proposed_cases(
            task, [finding], self.workspace, gold
        )
        self.assertEqual(result.promoted, ())
        outcome = result.outcomes[0]
        self.assertTrue(all(outcome.measured_pass.values()))
        self.assertIn("live uncaught exploit", outcome.reason)
        self.assertTrue(outcome.fidelity["passed"])
        from elt_taskgen import cli

        blocking, problems = cli._blocking_proposal_failures([finding], [outcome])
        self.assertEqual(blocking, [finding])
        self.assertTrue(any("unresolved" in problem for problem in problems))
        self.assertEqual(
            outcome.fidelity["compiler"]["realized"]["target_mart"],
            target_name,
        )

    def test_copy_mart_quotes_reserved_mart_names_end_to_end(self):
        base = self.task.marts[0]
        source = base.model_copy(
            update={
                "name": "group",
                "plan": base.plan.model_copy(update={"mart": "group"}),
            }
        )
        target = base.model_copy(
            update={
                "name": "order",
                "plan": base.plan.model_copy(update={"mart": "order"}),
            }
        )
        reference = self.task.reference.model_copy(
            update={
                "sql_by_mart": {
                    source.name: demo_fixture.REFERENCE_SQL,
                    target.name: demo_fixture.REFERENCE_SQL,
                }
            }
        )
        task = self.task.model_copy(
            update={"marts": (source, target), "reference": reference}
        )
        stage2 = {
            population: {
                source.name: marts[demo_fixture.MART_NAME],
                target.name: marts[demo_fixture.MART_NAME],
            }
            for population, marts in self.gold.stage2_csv.items()
        }
        gold = promotion_tests.ta._Gold(
            task.task_id,
            task.content_hash(),
            self.gold.stage1,
            stage2,
        )
        all_pass = {population: True for population in P}
        proposal = ProposedAttackCase(
            kind=AttackKind.CUSTOM,
            params={"copy_mart": (source.name, target.name)},
            expected_pass=all_pass,
            expected_pass_by_stage={
                TaskVariant.EXTRACT_LOAD: all_pass,
                TaskVariant.TRANSFORM: all_pass,
            },
            rationale='copy reserved mart "group" into reserved mart "order"',
        )
        finding = Finding(
            finding_id="copy-reserved-live",
            role=CouncilRole.SHORTCUT_ATTACKER,
            severity=Severity.MAJOR,
            summary="copy group into order",
            detail="reuse group as order",
            suggested_attack=AttackKind.CUSTOM,
            proposed_case=proposal,
        )

        result = attacks.promote_proposed_cases(
            task, [finding], self.workspace, gold
        )

        self.assertEqual(result.promoted, ())
        (outcome,) = result.outcomes
        self.assertTrue(outcome.fidelity["passed"])
        self.assertTrue(all(outcome.measured_pass.values()))
        self.assertIn(
            'FROM "group"',
            attacks.materialize_mutation(
                task,
                attacks._candidate_case(finding, proposal, required=False),
                None,
            )[target.name],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

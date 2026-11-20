"""Admission retries preserve tasks and require executed, discriminating mutants."""
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

import duckdb

from elt_taskgen.engine import StageName
from elt_taskgen.generation import advanced_recipes, compositions, dependent_compositions, graph_recipes, scenario_recipes, semantic_difficulty
from elt_taskgen.generation.advanced_evidence import ASOF
from elt_taskgen.generation.graph_evidence import ROLES
from elt_taskgen.generation.recipe_evidence import ENTITY_BASELINE
from elt_taskgen.generation.scenario_evidence import INTERVAL
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
from elt_taskgen import pipeline_readiness
from tests.advanced_fixtures import advanced_source
from tests.graph_fixtures import graph_source
from tests.scenario_fixtures import scenario_source
from tests.test_recipe_registry import source_task


class RecipeAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.examples = []
        for family, source, recipe, retry_sql in (
            (semantic_difficulty._FAMILY, source_task(), semantic_difficulty.TEMPLATE, False),
            (compositions._FAMILY, source_task(), compositions.GLOBAL_BASELINE, False),
            (advanced_recipes._FAMILY, advanced_source(), ASOF, False),
            (dependent_compositions._FAMILY, source_task(), ENTITY_BASELINE, False),
            (graph_recipes._FAMILY, graph_source(ROLES), ROLES, True),
            (scenario_recipes._FAMILY, scenario_source(), INTERVAL, True),
        ):
            expected = family.apply(source, (recipe,))
            evidence = family.bind(expected, expected.marts[-1])
            cls.examples.append((family, source, recipe, retry_sql, expected, evidence))

    def test_candidate_failures_retry_without_mutating_the_input(self):
        for family, source, recipe, retry_sql, expected, evidence in self.examples:
            for error, retry in ((SemanticDifficultyError("inert witness"), True),
                                 (duckdb.BinderException("invalid candidate SQL"), retry_sql),
                                 (RuntimeError("implementation defect"), False)):
                with self.subTest(family=family.name, error=type(error).__name__):
                    original = source.model_dump_json()
                    witness = Mock(side_effect=[error, family.witness_rows(source, evidence, recipe)])
                    trial = replace(family, candidates=lambda *_: (evidence, evidence), witness_rows=witness)
                    if retry:
                        self.assertEqual(trial.apply(source, (recipe,)), expected)
                        self.assertEqual(witness.call_count, 2)
                    else:
                        with self.assertRaises(type(error)):
                            trial.apply(source, (recipe,))
                        self.assertEqual(witness.call_count, 1)
                    self.assertEqual(source.model_dump_json(), original)

    def test_successful_sql_that_matches_gold_is_not_a_witness(self):
        for family, source, recipe, _, _, evidence in self.examples:
            with self.subTest(family=family.name):
                def inert_plan(task, binding, mart, selected, variant=""):
                    return family.plan(task, binding, mart, selected)

                trial = replace(family, candidates=lambda *_: (evidence,), plan=inert_plan)
                with self.assertRaisesRegex(SemanticDifficultyError, "No executable witness"):
                    trial.apply(source, (recipe,))

    def test_retry_budget_remains_bounded(self):
        family, source, recipe, _, _, evidence = self.examples[0]
        witness = Mock(side_effect=SemanticDifficultyError("unsupported witness"))
        trial = replace(family, candidates=lambda *_: (evidence,) * 40, witness_rows=witness)
        with self.assertRaises(SemanticDifficultyError):
            trial.apply(source, (recipe,))
        self.assertEqual(witness.call_count, 32)

    def test_required_binding_is_selected_before_applying_the_retry_budget(self):
        family, source, recipe, _, expected, evidence = self.examples[0]
        other = replace(evidence, parent="other_parent")
        witness = Mock(wraps=family.witness_rows)
        trial = replace(family, candidates=lambda *_: (other,) * 32 + (evidence,), witness_rows=witness)
        self.assertEqual(trial.apply(source, (recipe,), required_evidence=evidence), expected)
        witness.assert_called_once_with(source, evidence, recipe)

    def test_required_binding_must_match_existing_mart(self):
        family, _, recipe, _, existing, evidence = self.examples[0]
        other = replace(evidence, parent="other_parent")
        with self.assertRaisesRegex(SemanticDifficultyError, "different source binding"):
            family.apply(existing, (recipe,), required_evidence=other)
        self.assertIs(family.apply(existing, (recipe,), required_evidence=evidence), existing)

    def test_reusing_a_task_does_not_retry_corrupt_evidence(self):
        for family, _, recipe, _, existing, _ in self.examples:
            with self.subTest(family=family.name):
                candidates = Mock(side_effect=AssertionError("must not replace existing evidence"))
                trial = replace(family, candidates=candidates,
                                bind=Mock(side_effect=duckdb.BinderException("corrupt binding")))
                with self.assertRaises(duckdb.BinderException):
                    trial.apply(existing, (recipe,))
                candidates.assert_not_called()

    def test_stage_seals_cover_the_extracted_admission_code(self):
        spec = pipeline_readiness.GenerationRunSpec(candidate_count=1, profile="draft")
        original_digest = pipeline_readiness._module_source_sha256
        module = "elt_taskgen.generation.recipe_admission"
        for stage in (StageName.REFERENCE, StageName.ATTACK, StageName.GATES_TRANSFORM):
            with self.subTest(stage=stage):
                original = pipeline_readiness._configured_inputs(spec, stage)
                self.assertIn(module, original["module_source_sha256"])
                with patch.object(pipeline_readiness, "_module_source_sha256",
                                  side_effect=lambda name: "0" * 64 if name == module else original_digest(name)):
                    changed = pipeline_readiness._configured_inputs(spec, stage)
                self.assertNotEqual(original["implementation_sha256"], changed["implementation_sha256"])


if __name__ == "__main__":
    unittest.main()

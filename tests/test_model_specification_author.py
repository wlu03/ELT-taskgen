from __future__ import annotations

import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elt_taskgen import demo_fixture
from elt_taskgen.export.eltbench import build_data_model
from elt_taskgen.review import model_specification as author
from elt_taskgen.specification import data_model, source_data_model


class Provider:
    routing = SimpleNamespace(roles={author.ROLE_NAME: object()})

    def __init__(self, document, issues=None):
        self.calls = []
        self.answers = [json.dumps(document), json.dumps({
            "semantic_issues": issues or [], "business_language": 5,
            "concise_output_spec": 5, "style_reason": "Concise definitions.",
        })]

    def complete(self, role, prompt):
        self.calls.append((role, prompt))
        return self.answers.pop(0)


class ModelSpecificationAuthorTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()
        self.document = copy.deepcopy(source_data_model(self.task))
        self.document["models"][0]["columns"][0]["description"] = "Unique identifier of the customer."

    def test_benchmark_name_and_private_answers_are_absent_from_all_prompts(self):
        for variant in author.PROMPT_VARIANTS:
            prompt = author.author_prompt(self.task, variant)
            self.assertNotRegex(prompt.lower(), r"elt[-_ ]?bench")
            self.assertNotIn("sql_by_mart", prompt)
            self.assertNotIn("carried_fields", prompt)
        review = author.review_prompt(self.task, source_data_model(self.task), self.document)
        self.assertNotRegex(review.lower(), r"elt[-_ ]?bench")

    def test_public_yaml_uses_authored_document_and_preserves_private_plan(self):
        provider = Provider(self.document)
        rewritten = author.author_public_specification(self.task, provider)
        self.assertEqual(build_data_model(rewritten), self.document)
        self.assertEqual(rewritten.marts, self.task.marts)
        self.assertEqual(rewritten.reference, self.task.reference)
        self.assertEqual(rewritten.solver_prompt, self.task.solver_prompt)
        self.assertNotEqual(rewritten.content_hash(), self.task.content_hash())
        self.assertEqual(len(provider.calls), 2)
        self.assertIs(author.author_public_specification(rewritten, provider), rewritten)
        self.assertEqual(len(provider.calls), 2)

    def test_missing_authored_spec_does_not_change_existing_serialization(self):
        self.assertNotIn("public_specification", self.task.model_dump(mode="json"))
        self.assertEqual(data_model(self.task), source_data_model(self.task))

    def test_source_repair_invalidates_authored_spec_instead_of_exporting_stale_rules(self):
        rewritten = author.author_public_specification(self.task, Provider(self.document))
        mart = rewritten.marts[0].model_copy(update={"grain": "A different population"})
        stale = rewritten.model_copy(update={"marts": (mart, *rewritten.marts[1:])})
        with self.assertRaisesRegex(ValueError, "stale"):
            build_data_model(stale)
        repaired = author.author_public_specification(stale, Provider(self.document))
        self.assertEqual(build_data_model(repaired), self.document)

    def test_renamed_missing_extra_columns_and_empty_descriptions_are_rejected(self):
        mutations = []
        for mode in ("rename", "remove", "extra", "empty"):
            doc = copy.deepcopy(self.document)
            columns = doc["models"][0]["columns"]
            if mode == "rename":
                columns[0]["name"] = "invented_name"
            elif mode == "remove":
                columns.pop()
            elif mode == "extra":
                columns[0]["type"] = "integer"
            else:
                columns[0]["description"] = "   "
            mutations.append(doc)
        for doc in mutations:
            with self.assertRaises(ValueError):
                author.parse_document(json.dumps(doc), self.document)
        self.assertEqual(author.parse_document("```json\n" + json.dumps(self.document) + "\n```", self.document), self.document)
        with self.assertRaises(ValueError):
            author.parse_document("Here is the answer: " + json.dumps(self.document), self.document)

    def test_semantic_loss_is_rejected(self):
        provider = Provider(self.document, [{"problem": "omitted", "source_rule": "ties use smallest identifier"}])
        with self.assertRaisesRegex(ValueError, "changed requirements"):
            author.author_public_specification(self.task, provider)

    def test_authored_descriptions_are_included_in_contamination_checks(self):
        from elt_taskgen.verification.contamination import task_fingerprints, text_fingerprints

        marker = "A unique sentence describing a highly specialized invented activity with sufficient words to create a text fingerprint."
        self.document["models"][0]["description"] = marker
        rewritten = author.author_public_specification(self.task, Provider(self.document))
        fingerprints = text_fingerprints(marker)
        self.assertTrue(fingerprints)
        self.assertTrue(fingerprints.issubset(task_fingerprints(rewritten)))

    def test_public_descriptions_cannot_hide_a_private_sql_leak_from_council(self):
        from elt_taskgen.review.council import leak_findings

        rewritten = author.author_public_specification(self.task, Provider(self.document))
        sql = next(iter(self.task.reference.sql_by_mart.values()))
        model = rewritten.public_specification.models[0].model_copy(update={"description": sql})
        poisoned = rewritten.model_copy(update={"public_specification": rewritten.public_specification.model_copy(update={"models": (model,)})})
        self.assertTrue(leak_findings(poisoned))

    def test_stage_persists_public_rewrite_even_when_internal_draft_is_unchanged(self):
        from elt_taskgen import cli

        task = self.task.model_copy(update={"solver_prompt": "unchanged draft"})
        engine = SimpleNamespace(workspace=None)
        provider = Provider(self.document)
        with (patch.object(cli, "_structural_failure_detail", return_value=None),
              patch.object(cli, "_attack_matrix_failure_detail", return_value=None),
              patch.object(cli, "_admission_gate", return_value=(None, {})),
              patch.object(cli, "_begin_task_evidence"),
              patch.object(cli, "_author_prose_for", return_value=(task.solver_prompt, {})),
              patch("elt_taskgen.review.prose_fidelity.check_prose_fidelity", return_value=[])):
            outcome = cli.make_author_runner(provider)(engine, task)
        self.assertEqual(outcome.verdict, "pass")
        self.assertIsNotNone(outcome.task)
        self.assertEqual(build_data_model(outcome.task), self.document)


if __name__ == "__main__":
    unittest.main()

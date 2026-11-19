"""A proposal must identify the exact source, graph and binding it will compile."""

import contextlib
import copy
from dataclasses import asdict
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from elt_taskgen import cli
from elt_taskgen.engine import Engine
from elt_taskgen.export.eltbench import build_data_model
from elt_taskgen.generation.composite_proposals import (
    CompositeProposal, apply_composite_proposal, list_composite_proposals,
)
from elt_taskgen.generation.composite_recipes import bound_composite
from elt_taskgen.models import (
    Origin, PublicSpecification, TaskStatus, canonical_json, sha256_hex, task_from_json, task_to_json,
)
from elt_taskgen.specification import source_fingerprint
from tests.test_recipe_registry import source_task


def run_cli(*args):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = cli.main([str(a) for a in args])
    return code, output.getvalue(), errors.getvalue()


class CompositeProposalTests(unittest.TestCase):
    def test_choice_preserves_requested_binding_and_renders_new_public_contract(self):
        source = source_task()
        authored = PublicSpecification(
            models=build_data_model(source)["models"],
            source_sha256=source_fingerprint(source), prompt_sha256="f" * 64,
        )
        source = source.model_copy(update={
            "status": TaskStatus.REVIEWED, "public_specification": authored, "solver_prompt": "old draft",
        }).with_revision(route=None, reason="old task")
        choices = list_composite_proposals(source)
        choice = [p for p in choices if p.schema_version == "composite-proposal-v1"][-1]
        self.assertNotEqual(choice.binding, choices[0].binding)
        result = apply_composite_proposal(source, choice)
        mart = result.marts[-1]
        actual = asdict(bound_composite(result, mart))
        self.assertEqual(actual["parent"], choice.binding["parent"])
        self.assertEqual(actual["group"], choice.binding["group"])
        self.assertEqual(result.status, TaskStatus.DRAFT)
        self.assertEqual(result.solver_prompt, "")
        self.assertIsNone(result.public_specification)
        self.assertEqual(result.revisions[-1].content_hash, result.content_hash())
        self.assertIn(source.content_hash(), result.revisions[-1].reason)
        self.assertIn(mart.name, result.reference.sql_by_mart)
        public = build_data_model(result)["models"][-1]
        self.assertEqual(public["name"], mart.name)
        self.assertIn(choice.binding["group"], json.dumps(public))
        self.assertNotIn("source_sha256", json.dumps(public))
        with self.assertRaisesRegex(ValueError, "before semantic recipe selection"):
            list_composite_proposals(result)

    def test_unknown_fields_bindings_recipes_and_stale_source_are_refused(self):
        source = source_task()
        fields = list_composite_proposals(source)[0].model_dump(mode="json")
        for changes in (
            {"recipe": "freeform"}, {"schema_version": "future"}, {"sql": "SELECT 1"},
            {"binding": {**fields["binding"], "measure_nullable": 1}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                CompositeProposal.model_validate({**fields, **changes})
        for changes in (
            {"task_id": "other"}, {"source_sha256": "a" * 64},
            {"binding": {**fields["binding"], "measure": "unknown_amount"}},
            {"binding": {**fields["binding"], "ordering": "descending"}},
            {"binding": {**fields["binding"], "links": []}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                apply_composite_proposal(source, CompositeProposal.model_validate({**fields, **changes}))
        changed = source.model_copy(update={"title": "A different source revision"})
        with self.assertRaisesRegex(ValueError, "stale source"):
            apply_composite_proposal(changed, CompositeProposal.model_validate(fields))
        with self.assertRaisesRegex(ValueError, "reserved for measurement"):
            list_composite_proposals(source.model_copy(update={"origin": Origin.ELTBENCH_ANCHOR}))

    def test_cli_lists_and_applies_without_modifying_source_or_overwriting_output(self):
        source = source_task()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "source.json"
            proposal = root / "proposal.json"
            output_path = root / "result.json"
            workspace = root / "workspace"
            source_path.write_text(task_to_json(source), encoding="utf-8")
            engine = Engine(workspace)
            try:
                engine.register(source)
            finally:
                engine.close()
            code, listed, err = run_cli("compose", "list", "--workspace", workspace, "--task-id", source.task_id)
            self.assertEqual(code, 0, err)
            choice = [p for p in json.loads(listed) if p["schema_version"] == "composite-proposal-v1"][-1]
            self.assertEqual(set(choice), {"schema_version", "task_id", "source_sha256", "recipe", "binding"})
            proposal.write_text(json.dumps(choice), encoding="utf-8")
            args = ("compose", "apply", "--task-ir", source_path, "--proposal", proposal, "--output", output_path)
            code, _, err = run_cli(*args)
            self.assertEqual(code, 0, err)
            saved = task_from_json(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, apply_composite_proposal(source, CompositeProposal.model_validate(choice)))
            original_result = output_path.read_bytes()
            self.assertEqual(run_cli(*args)[0], 2)
            self.assertEqual(output_path.read_bytes(), original_result)
            self.assertEqual(source_path.read_text(encoding="utf-8"), task_to_json(source))

            output_path.unlink()
            proposal.write_text(json.dumps({**choice, "binding": {}}), encoding="utf-8")
            code, _, err = run_cli(*args)
            self.assertEqual(code, 2)
            self.assertIn("does not support", err)
            self.assertFalse(output_path.exists())
            code, _, err = run_cli("compose", "list", "--task-ir", source_path, "--manifest", "unused")
            self.assertEqual(code, 2)
            self.assertIn("--task-id", err)

    def test_discovered_proposal_rejects_tampering_even_with_a_recomputed_id(self):
        source = source_task()
        choices = [p for p in list_composite_proposals(source) if p.schema_version == "composite-proposal-v2"]
        self.assertGreater(len({canonical_json(p.graph) for p in choices}), 1)
        self.assertEqual(len({p.proposal_id for p in choices}), len(choices))
        fields = choices[0].model_dump(mode="json")
        for changes in (
            {"proposal_id": "f" * 64},
            {"source_sha256": "a" * 64},
            {"graph": {"nodes": [{"op": "sql", "inputs": []}]}},
            {"recipe": "freeform"},
            {"schema_version": "composite-proposal-v1"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                CompositeProposal.model_validate({**fields, **changes})
        changed = CompositeProposal.model_validate(fields)
        changed.graph["nodes"][1]["inputs"][0] = False
        with self.assertRaisesRegex(ValueError, "does not match"):
            apply_composite_proposal(source, changed)
        forged = copy.deepcopy(fields)
        del forged["proposal_id"]
        forged["binding"]["measure"] = "unavailable_measure"
        forged["proposal_id"] = sha256_hex(canonical_json(forged))
        with self.assertRaisesRegex(ValueError, "does not support"):
            apply_composite_proposal(source, CompositeProposal.model_validate(forged))

    def test_auto_output_replays_via_a_listed_graph_and_saved_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = source_task()
            root = Path(tmp)
            path = root / "source.json"
            path.write_text(task_to_json(source), encoding="utf-8")
            src = ("--task-ir", path)
            code, listed, err = run_cli("compose", "list", *src)
            self.assertEqual(code, 0, err)
            auto = root / "auto.json"
            args = ("compose", "auto", *src, "--output", auto, "--seed")
            code, _, err = run_cli(*args, "-1")
            self.assertEqual(code, 2)
            self.assertFalse(auto.exists())
            code, _, err = run_cli(*args, "11")
            self.assertEqual(code, 0, err)
            result = task_from_json(auto.read_text(encoding="utf-8"))
            reason = result.revisions[-1].reason
            self.assertIn("seed 11", reason)
            matching = [
                p for p in json.loads(listed)
                if p["schema_version"] == "composite-proposal-v2" and p["proposal_id"] in reason
            ]
            self.assertEqual(len(matching), 1)
            proposal = root / "exact.json"
            proposal.write_text(json.dumps(matching[0]), encoding="utf-8")
            replay = root / "replay.json"
            code, _, err = run_cli("compose", "apply", *src, "--output", replay, "--proposal", proposal)
            self.assertEqual(code, 0, err)
            self.assertEqual(task_from_json(replay.read_text(encoding="utf-8")).content_hash(), result.content_hash())

    def test_manifest_preflight_errors_propagate_without_writing_a_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "roster.json"
            manifest.write_text("{}", encoding="utf-8")
            args = ("compose", "list", "--task-id", "test", "--manifest", manifest, "--workspace", root / "new")
            code, _, err = run_cli(*args)
            self.assertEqual(code, 2)
            self.assertTrue(err)
            self.assertFalse((root / "new").exists())
            with patch("elt_taskgen.ingest_manifest.load_five_source_manifest", return_value=object()), \
                    patch("elt_taskgen.ingest_manifest.prepare_five_sources", return_value=()) as preflight:
                code, _, err = run_cli(*args)
            self.assertEqual(code, 2)
            self.assertIn("missing from the source manifest", err)
            self.assertEqual(preflight.call_args.kwargs["difficulty_profile"], "standard")

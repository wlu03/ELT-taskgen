"""Tests for the `independent_loader` role — the EXTRACT_LOAD dual build.

WHY THIS EXISTS
EL gold used to have NO independent witness of any kind: the reference and the
gold both go through `solution.load_sources_duckdb`, so the frozen stage-1
counts were certified by the loader that produced them. `el-independent-load`
adds an outsider who is shown ONLY the emitted public bundle and must answer
with the same submission a real EL solver makes.

WHAT THESE TESTS ASSERT, AND WHAT THEY DELIBERATELY DO NOT
They prove, with REAL execution over rendered populations and REAL frozen gold:

  * the loader view is assembled from the BYTES OF THE EMITTED BUNDLE (missing
    bundle / missing config.yaml / missing schemas all fail closed), carries no
    answer-side material, and trips the shared leak guard when prose does;
  * a plan naming the right artifact with the right reader agrees 1.0 on all
    five populations and turns `el-independent-load` green;
  * the two CONVENTION errors this witness exists to catch — a path with the
    wrong root ("sources/…" copied off the bundle rather than the source root)
    and a reader that does not match the artifact's layout — are caught, and
    the gate goes RED;
  * a plan that aces the public sample tree and then fails a hidden population
    is NOT resampled away (that is the finding, not noise), while a plan that
    fails the public tree too is resampled, bounded by MAX_SAMPLES;
  * missing/stale/wrong-role evidence is RED, and same-family routing is
    refused.

They do NOT assert that this witness certifies the gold, because it does not:
both sides read the artifact with the same trusted reader, so a reader bug is
invisible here by construction. That claim belongs to `el-artifact-census`
(verification/el_probes.py), and `gates._gate_el_independent_load` says so in
its own success text.

No network, no API keys: the provider doubles return canned text; the
execution/scoring path underneath is the real one (trusted readers via
`calibration.execute_load_plan`, scored by `evaluate_variant(EXTRACT_LOAD)`).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

from elt_taskgen.demo_fixture import REFERENCE_SQL
from elt_taskgen.export import eltbench
from elt_taskgen.models import PopulationName, TaskVariant
from elt_taskgen.reference import independent
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.verification import gates

try:  # `unittest discover -s tests` puts tests/ on sys.path; -m tests.x does not
    from test_independent import (
        IndependentTestCase,
        OneShotOnlyDouble,
        ScriptedProvider,
        SessionDouble,
        tool_use_block,
    )
except ImportError:  # pragma: no cover - depends on how the suite is invoked
    from tests.test_independent import (
        IndependentTestCase,
        OneShotOnlyDouble,
        ScriptedProvider,
        SessionDouble,
        tool_use_block,
    )

P = PopulationName

#: The plan a correct loader emits for the demo fixture: one artifact and one
#: reader per source table, PATHS RELATIVE TO A POPULATION'S rendered root.
CORRECT_PLAN = {
    "customers": {"path": "postgres/customers.sql", "format": "postgres_sql"},
    "orders": {"path": "mongodb/orders.jsonl", "format": "jsonl"},
    "order_items": {"path": "files/order_items.csv", "format": "csv"},
}


def plan_response(plan: dict) -> str:
    return json.dumps({"load_plan": plan})


def with_table(plan: dict, table: str, **changes) -> dict:
    updated = {t: dict(step) for t, step in plan.items()}
    updated[table].update(changes)
    return updated


class LoaderTestCase(IndependentTestCase):
    """A workspace with the EL bundle emitted, as the variant stage leaves it."""

    def bundled_workspace(self) -> Path:
        workspace = self.fresh_workspace()
        task_dir = workspace / "tasks" / self.task.task_id
        eltbench.emit_variant(
            self.task,
            self.gold,
            TaskVariant.EXTRACT_LOAD,
            task_dir / "variants" / "extract_load",
            populations_dir=task_dir / "populations",
        )
        return workspace


# ---------------------------------------------------------------------------
# The loader view: the emitted bundle, and nothing else
# ---------------------------------------------------------------------------

class TestLoaderView(LoaderTestCase):
    def test_view_is_assembled_from_the_emitted_bundle(self) -> None:
        workspace = self.bundled_workspace()
        bundle = independent.el_bundle_dir(workspace, self.task.task_id)
        view = independent.loader_view(self.task, bundle)

        # The literal bytes of the shipped files, not a TaskIR paraphrase.
        config_text = (bundle / "config.yaml").read_text(encoding="utf-8")
        self.assertIn(config_text.rstrip(), view)
        for schema in sorted((bundle / "schemas").glob("*.csv")):
            self.assertIn(schema.read_text(encoding="utf-8").rstrip(), view)
        # The sample source tree is shown as a LISTING, relative to the root a
        # plan's paths are resolved against.
        for rel in ("postgres/customers.sql", "mongodb/orders.jsonl",
                    "files/order_items.csv"):
            self.assertIn(rel, view)
        # The response contract is the ONE the executor enforces.
        for fmt in ("postgres_sql", "jsonl", "rest_pages", "s3_jsonl", "csv"):
            self.assertIn(fmt, view)
        for table in self.task.tables:
            self.assertIn(table.name, view)

    def test_view_carries_no_answer_side_material(self) -> None:
        workspace = self.bundled_workspace()
        view = independent.loader_view(
            self.task, independent.el_bundle_dir(workspace, self.task.task_id)
        )
        norm = independent._normalize(view)
        self.assertNotIn("answer_key", norm)
        self.assertNotIn(independent._normalize(REFERENCE_SQL), norm)
        # The transform side is out of scope for this variant entirely.
        self.assertNotIn("data_model.yaml", view)

    def test_leaky_prose_trips_the_shared_guard(self) -> None:
        leaky = self.task.model_copy(
            update={"solver_prompt": "Here is the answer:\n" + REFERENCE_SQL}
        )
        workspace = self.fresh_workspace()
        task_dir = workspace / "tasks" / self.task.task_id
        bundle = task_dir / "variants" / "extract_load" / "task"
        bundle.mkdir(parents=True)
        (bundle / "config.yaml").write_text("postgres: {}\n")
        (bundle / "schemas").mkdir()
        (bundle / "schemas" / "customers.csv").write_text("column_name,column_description\n")
        (bundle / "documentation.md").write_text(eltbench.el_documentation(leaky))
        with self.assertRaises(ValueError):
            independent.loader_view(leaky, bundle)

    def test_missing_bundle_fails_closed(self) -> None:
        workspace = self.fresh_workspace()
        with self.assertRaises(FileNotFoundError):
            independent.loader_view(
                self.task, independent.el_bundle_dir(workspace, self.task.task_id)
            )

    def test_bundle_without_config_or_schemas_fails_closed(self) -> None:
        workspace = self.bundled_workspace()
        bundle = independent.el_bundle_dir(workspace, self.task.task_id)
        (bundle / "config.yaml").unlink()
        with self.assertRaises(ValueError):
            independent.loader_view(self.task, bundle)

    def test_resample_prompts_are_distinct_transcript_keys(self) -> None:
        workspace = self.bundled_workspace()
        bundle = independent.el_bundle_dir(workspace, self.task.task_id)
        first = independent.load_sample_prompt(self.task, bundle, 0)
        second = independent.load_sample_prompt(self.task, bundle, 1)
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith(first))


# ---------------------------------------------------------------------------
# Response schema enforcement (delegated to the ONE submission parser)
# ---------------------------------------------------------------------------

class TestPlanParsing(LoaderTestCase):
    def test_valid_plan_parses_to_load_steps(self) -> None:
        plan = independent.parse_load_plan(self.task, plan_response(CORRECT_PLAN))
        self.assertEqual(sorted(plan), sorted(t.name for t in self.task.tables))
        self.assertEqual(plan["orders"].path, "mongodb/orders.jsonl")
        self.assertEqual(plan["orders"].format, "jsonl")

    def test_missing_table_is_a_protocol_failure(self) -> None:
        partial = {k: v for k, v in CORRECT_PLAN.items() if k != "orders"}
        with self.assertRaises(ProviderProtocolError):
            independent.parse_load_plan(self.task, plan_response(partial))

    def test_unknown_reader_is_a_protocol_failure(self) -> None:
        bogus = with_table(CORRECT_PLAN, "orders", format="parquet")
        with self.assertRaises(ProviderProtocolError):
            independent.parse_load_plan(self.task, plan_response(bogus))

    def test_prose_around_the_object_is_never_read_for_intent(self) -> None:
        with self.assertRaises(ProviderProtocolError):
            independent.parse_load_plan(
                self.task, "Sure! Here is the plan: " + plan_response(CORRECT_PLAN)
            )


# ---------------------------------------------------------------------------
# The build: agreement, and the convention errors it exists to catch
# ---------------------------------------------------------------------------

class TestIndependentLoadBuild(LoaderTestCase):
    def run_build(self, workspace: Path, *responses: str, max_samples: int = 2):
        provider = ScriptedProvider(list(responses))
        return provider, independent.run_independent_load_build(
            self.task,
            workspace,
            provider,
            self.gold,
            max_samples=max_samples,
        )

    def test_correct_plan_agrees_on_all_five_populations(self) -> None:
        workspace = self.bundled_workspace()
        _, result = self.run_build(workspace, plan_response(CORRECT_PLAN))
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(result.role, independent.LOADER_ROLE_NAME)
        self.assertEqual(
            result.agreement, {pop.value: 1.0 for pop in P}
        )
        self.assertEqual(result.samples[-1].load_plan, CORRECT_PLAN)
        self.assertEqual(result.bundle, independent.EL_BUNDLE_REL)
        # The honesty clause is part of the record, not just the docstring.
        self.assertIn("BUNDLE SUFFICIENCY", result.detail)

    def test_recorded_agreement_turns_the_gate_green(self) -> None:
        workspace = self.bundled_workspace()
        _, result = self.run_build(workspace, plan_response(CORRECT_PLAN))
        path = independent.record_load_build_result(workspace, self.task, result)
        self.assertEqual(
            path,
            workspace / "tasks" / self.task.task_id
            / gates.EL_INDEPENDENT_LOAD_EVIDENCE_REL,
        )
        gate = gates._gate_el_independent_load(self.task, workspace)
        self.assertTrue(gate.passed, gate.details)
        self.assertEqual(gate.evidence["role"], gates.EL_INDEPENDENT_LOAD_ROLE)
        self.assertEqual(gate.evidence["load_plan_tables"], str(len(self.task.tables)))
        # It must not overclaim: the census is what certifies the gold.
        self.assertIn("does NOT certify the gold", gate.details)

    def test_wrong_path_root_is_caught(self) -> None:
        """Layer-1 hazard: paths copied off the BUNDLE tree ('sources/...')
        instead of a population's source root. Every population fails."""
        workspace = self.bundled_workspace()
        wrong = {
            table: {"path": f"sources/{step['path']}", "format": step["format"]}
            for table, step in CORRECT_PLAN.items()
        }
        _, result = self.run_build(workspace, plan_response(wrong))
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(result.agreement, {pop.value: 0.0 for pop in P})
        self.assertIn(
            "does not exist under the source root",
            result.samples[-1].errors[P.PRIMARY.value],
        )
        independent.record_load_build_result(workspace, self.task, result)
        gate = gates._gate_el_independent_load(self.task, workspace)
        self.assertFalse(gate.passed)

    def test_wrong_reader_for_the_layout_is_caught(self) -> None:
        """Layer-2 hazard: the right artifact read with the wrong reader."""
        workspace = self.bundled_workspace()
        wrong = with_table(CORRECT_PLAN, "customers", format="csv")
        _, result = self.run_build(workspace, plan_response(wrong))
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertLess(result.agreement[P.PRIMARY.value], 1.0)

    def test_plan_escaping_the_source_root_is_refused(self) -> None:
        workspace = self.bundled_workspace()
        escape = with_table(
            CORRECT_PLAN, "customers", path="../../answer_key/manifest.json"
        )
        _, result = self.run_build(workspace, plan_response(escape))
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertIn(
            "escapes the source root", result.samples[-1].errors[P.PRIMARY.value]
        )

    def test_public_failure_is_resampled_hidden_failure_is_not(self) -> None:
        """The sampling policy, both branches, on ONE workspace.

        A plan that cannot even read the tree it was SHOWN is loader-side noise
        -> resample. A plan that reads the sample tree perfectly and then fails
        a hidden population has found a real divergence -> adjudicate, because
        resampling it away is exactly how a witness stops being one.
        """
        workspace = self.bundled_workspace()
        broken = {
            table: {"path": f"nope/{step['path']}", "format": step["format"]}
            for table, step in CORRECT_PLAN.items()
        }
        _, resampled = self.run_build(
            workspace, plan_response(broken), plan_response(CORRECT_PLAN)
        )
        self.assertEqual(resampled.status, independent.STATUS_AGREED)
        self.assertEqual(len(resampled.samples), 2)
        self.assertFalse(resampled.samples[0].dev_pass)

        # A decoy artifact that exists ONLY in the population whose rendered
        # tree the bundle ships: a plan naming it aces development and fails
        # every hidden population.
        task_dir = workspace / "tasks" / self.task.task_id
        rendered = task_dir / "populations" / P.DEVELOPMENT.value / "rendered"
        shutil.copy(
            rendered / "mongodb" / "orders.jsonl",
            rendered / "mongodb" / "orders_v2.jsonl",
        )
        decoy = with_table(CORRECT_PLAN, "orders", path="mongodb/orders_v2.jsonl")
        _, adjudicate = self.run_build(
            workspace, plan_response(decoy), plan_response(CORRECT_PLAN)
        )
        self.assertEqual(adjudicate.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(len(adjudicate.samples), 1, "a hidden-only failure was resampled away")
        self.assertTrue(adjudicate.samples[0].dev_pass)
        self.assertEqual(adjudicate.agreement[P.DEVELOPMENT.value], 1.0)
        self.assertEqual(adjudicate.agreement[P.PRIMARY.value], 0.0)

    def test_unparseable_output_exhausts_the_budget_then_raises(self) -> None:
        workspace = self.bundled_workspace()
        with self.assertRaises(ProviderProtocolError):
            self.run_build(workspace, "I would start by inspecting the sources.",
                           max_samples=2)

    def test_every_sample_is_addressed_to_the_loader_role(self) -> None:
        workspace = self.bundled_workspace()
        provider, _ = self.run_build(workspace, plan_response(CORRECT_PLAN))
        self.assertEqual(
            {role for role, _ in provider.calls}, {independent.LOADER_ROLE_NAME}
        )


# ---------------------------------------------------------------------------
# Evidence binding + routing
# ---------------------------------------------------------------------------

class TestEvidenceAndRouting(LoaderTestCase):
    def _recorded(self, workspace: Path):
        provider = ScriptedProvider([plan_response(CORRECT_PLAN)])
        result = independent.run_independent_load_build(
            self.task, workspace, provider, self.gold
        )
        independent.record_load_build_result(workspace, self.task, result)
        return result

    def test_record_is_canonical_and_reloadable(self) -> None:
        workspace = self.bundled_workspace()
        result = self._recorded(workspace)
        loaded = independent.load_load_build_result(workspace, self.task.task_id)
        self.assertEqual(loaded["role"], independent.LOADER_ROLE_NAME)
        self.assertEqual(loaded["task_content_hash"], self.task.content_hash())
        self.assertEqual(loaded["samples"][-1]["load_plan"], CORRECT_PLAN)
        # Re-recording the same result is byte-identical (deterministic artifact).
        path = workspace / "tasks" / self.task.task_id / independent.INDEPENDENT_LOAD_EVIDENCE_REL
        first = path.read_bytes()
        independent.record_load_build_result(workspace, self.task, result)
        self.assertEqual(first, path.read_bytes())

    def test_absent_record_reads_as_none_never_as_a_pass(self) -> None:
        workspace = self.fresh_workspace()
        self.assertIsNone(
            independent.load_load_build_result(workspace, self.task.task_id)
        )
        gate = gates._gate_el_independent_load(self.task, workspace)
        self.assertFalse(gate.passed)
        self.assertIn("never performed", gate.details)

    def test_stale_record_is_red(self) -> None:
        workspace = self.bundled_workspace()
        self._recorded(workspace)
        moved = self.task.model_copy(update={"title": "a different task"})
        gate = gates._gate_el_independent_load(moved, workspace)
        self.assertFalse(gate.passed)

    def test_result_for_another_task_is_refused(self) -> None:
        workspace = self.bundled_workspace()
        result = self._recorded(workspace)
        other = self.task.model_copy(update={"task_id": "someone_else"})
        with self.assertRaises(ValueError):
            independent.record_load_build_result(workspace, other, result)

    def test_same_family_routing_is_refused(self) -> None:
        routing = types.SimpleNamespace(
            for_role=lambda role: types.SimpleNamespace(provider="anthropic"),
            roles={
                "semantic_author": types.SimpleNamespace(provider="anthropic")
            },
        )
        provider = ScriptedProvider([plan_response(CORRECT_PLAN)])
        provider.routing = routing
        with self.assertRaises(ValueError) as ctx:
            independent.run_independent_load_build(
                self.task, self.base_workspace, provider, self.gold
            )
        self.assertIn(independent.LOADER_ROLE_NAME, str(ctx.exception))

    def test_openrouter_claude_behind_openai_compat_is_refused(self) -> None:
        """R2: the loader role is held to the same FAMILY rule — an
        OpenAI-compatible gateway serving a Claude model is same-family."""
        try:
            from test_independent import TestCrossFamilyMandate
        except ImportError:  # pragma: no cover - suite invocation dependent
            from tests.test_independent import TestCrossFamilyMandate

        provider = TestCrossFamilyMandate._routed_provider(
            "anthropic/claude-opus-5", "https://openrouter.ai/api/v1",
            role=independent.LOADER_ROLE_NAME,
        )
        with self.assertRaises(ValueError) as ctx:
            independent._assert_cross_family(provider, independent.LOADER_ROLE_NAME)
        self.assertIn(independent.LOADER_ROLE_NAME, str(ctx.exception))
        self.assertIn("cross-family", str(ctx.exception))

    def test_anthropic_base_url_behind_openai_compat_is_refused(self) -> None:
        try:
            from test_independent import TestCrossFamilyMandate
        except ImportError:  # pragma: no cover - suite invocation dependent
            from tests.test_independent import TestCrossFamilyMandate

        provider = TestCrossFamilyMandate._routed_provider(
            "some-model", "https://api.anthropic.com/v1",
            role=independent.LOADER_ROLE_NAME,
        )
        with self.assertRaises(ValueError) as ctx:
            independent.run_independent_load_build(
                self.task, self.base_workspace, provider, self.gold
            )
        self.assertIn("api.anthropic.com", str(ctx.exception))

    def test_load_result_records_provenance(self) -> None:
        try:
            from test_independent import TestCrossFamilyMandate
        except ImportError:  # pragma: no cover - suite invocation dependent
            from tests.test_independent import TestCrossFamilyMandate

        provider = TestCrossFamilyMandate._routed_provider(
            "moonshotai/kimi-k2.7-code", "https://openrouter.ai/api/v1",
            role=independent.LOADER_ROLE_NAME,
        )
        provider.complete = lambda role, prompt: plan_response(CORRECT_PLAN)
        workspace = self.bundled_workspace()
        result = independent.run_independent_load_build(
            self.task, workspace, provider, self.gold
        )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(result.provider, "openai_compat")
        self.assertEqual(result.model, "moonshotai/kimi-k2.7-code")
        self.assertEqual(result.endpoint_host, "openrouter.ai")
        independent.record_load_build_result(workspace, self.task, result)
        loaded = independent.load_load_build_result(workspace, self.task.task_id)
        self.assertEqual(loaded["model"], "moonshotai/kimi-k2.7-code")

    def test_the_role_answers_in_prose_not_through_the_findings_schema(self) -> None:
        """A forced report_findings tool call cannot express a load plan."""
        from elt_taskgen.review import providers as providers_mod

        self.assertIn(independent.LOADER_ROLE_NAME, providers_mod.PROSE_ROLES)
        self.assertFalse(
            providers_mod.uses_findings_schema(independent.LOADER_ROLE_NAME)
        )


# ---------------------------------------------------------------------------
# Evidence provenance: the record must outlive the bundle it cites
# ---------------------------------------------------------------------------

class TestBundleProvenance(LoaderTestCase):
    """The audit-trail defect this class pins down, and the shape of the fix.

    cli.py DELETES a refused variant's emitted bundle from variants/ (R7:
    tasks/<id>/variants/ never holds a bundle that failed its own battery).
    That is correct behavior — but the load-build evidence used to identify
    the bundle it measured by PATH alone ("variants/extract_load/task"), so a
    refusal left reports/independent_load_build.json citing a path that no
    longer exists and could never be re-inspected: a dangling audit trail.

    The fix is a self-certifying record: `bundle_digest`, sha256 over the
    bundle's sorted (relpath, per-file sha256) pairs — the same discipline as
    every other digest in the factory. These tests PROVE the property end to
    end with real emission: the digest identifies the bundle after deletion,
    a deterministic re-emission at the same task content hash reproduces it,
    and a differing tree provably does not. Pre-digest records (written before
    the field existed) still validate, with '' meaning "not recorded" — a
    consumer must never read '' as a match.
    """

    def _recorded(self, workspace: Path):
        provider = ScriptedProvider([plan_response(CORRECT_PLAN)])
        result = independent.run_independent_load_build(
            self.task, workspace, provider, self.gold
        )
        independent.record_load_build_result(workspace, self.task, result)
        return result

    def test_record_digests_the_exact_bundle_the_loader_saw(self) -> None:
        workspace = self.bundled_workspace()
        bundle = independent.el_bundle_dir(workspace, self.task.task_id)
        result = self._recorded(workspace)
        self.assertRegex(result.bundle_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            result.bundle_digest, independent.bundle_tree_digest(bundle)
        )
        # The persisted evidence carries it too, not just the in-memory model.
        loaded = independent.load_load_build_result(workspace, self.task.task_id)
        self.assertEqual(loaded["bundle_digest"], result.bundle_digest)
        self.assertEqual(loaded["bundle"], independent.EL_BUNDLE_REL)

    def test_disagreeing_record_carries_the_digest_too(self) -> None:
        """The refusal path is the one whose bundle actually gets deleted, so
        THAT record above all must identify its bundle without the path."""
        workspace = self.bundled_workspace()
        bundle = independent.el_bundle_dir(workspace, self.task.task_id)
        wrong = with_table(CORRECT_PLAN, "customers", format="csv")
        provider = ScriptedProvider([plan_response(wrong)] * 2)
        result = independent.run_independent_load_build(
            self.task, workspace, provider, self.gold
        )
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(
            result.bundle_digest, independent.bundle_tree_digest(bundle)
        )

    def test_digest_survives_deletion_and_reemission_reproduces_it(self) -> None:
        """The whole point, executed: emit -> record -> DELETE (exactly what
        cli.py's R7 does: shutil.rmtree of the variant out_dir, reports/ kept)
        -> the record still identifies the bundle -> a re-emission at the same
        task content hash reproduces the recorded digest bit for bit."""
        workspace = self.bundled_workspace()
        task_dir = workspace / "tasks" / self.task.task_id
        result = self._recorded(workspace)

        shutil.rmtree(task_dir / "variants" / "extract_load")
        bundle = independent.el_bundle_dir(workspace, self.task.task_id)
        self.assertFalse(bundle.exists(), "the cited path must actually dangle")

        loaded = independent.load_load_build_result(workspace, self.task.task_id)
        self.assertEqual(loaded["bundle_digest"], result.bundle_digest,
                         "evidence must identify the bundle after deletion")

        eltbench.emit_variant(
            self.task,
            self.gold,
            TaskVariant.EXTRACT_LOAD,
            task_dir / "variants" / "extract_load",
            populations_dir=task_dir / "populations",
        )
        self.assertEqual(
            independent.bundle_tree_digest(bundle),
            result.bundle_digest,
            "a deterministic re-emission at the same content hash must "
            "reproduce the recorded digest, or the witness is unverifiable",
        )

    def test_a_different_tree_provably_fails_to_reproduce_the_digest(self) -> None:
        """Both halves of 'two trees digest equal iff bytes and layout agree':
        changed bytes at the same path, and the same bytes at a moved path."""
        workspace = self.bundled_workspace()
        bundle = independent.el_bundle_dir(workspace, self.task.task_id)
        recorded = self._recorded(workspace).bundle_digest

        config = bundle / "config.yaml"
        original = config.read_bytes()
        config.write_bytes(original + b"\n# tampered\n")
        self.assertNotEqual(independent.bundle_tree_digest(bundle), recorded)
        config.write_bytes(original)
        self.assertEqual(independent.bundle_tree_digest(bundle), recorded)

        moved = bundle / "config_v2.yaml"
        config.rename(moved)
        self.assertNotEqual(independent.bundle_tree_digest(bundle), recorded)
        moved.rename(config)
        self.assertEqual(independent.bundle_tree_digest(bundle), recorded)

    def test_pre_digest_records_still_validate_and_never_match(self) -> None:
        """Backward compatibility: a record written before `bundle_digest`
        existed parses with '' — "not recorded" — and the gate consumer, which
        keys on status/agreement/samples, still reads it. '' is 0 chars of
        sha256, so no real tree digest can ever equal it (fail closed)."""
        workspace = self.bundled_workspace()
        result = self._recorded(workspace)
        old_shape = result.model_dump(mode="json")
        del old_shape["bundle_digest"]
        revived = independent.IndependentLoadBuildResult.model_validate(old_shape)
        self.assertEqual(revived.bundle_digest, "")
        bundle = independent.el_bundle_dir(workspace, self.task.task_id)
        self.assertNotEqual(independent.bundle_tree_digest(bundle), "")

        # The gate still reads the old shape (it never required the field).
        path = (workspace / "tasks" / self.task.task_id
                / independent.INDEPENDENT_LOAD_EVIDENCE_REL)
        path.write_text(json.dumps(old_shape), encoding="utf-8")
        gate = gates._gate_el_independent_load(self.task, workspace)
        self.assertTrue(gate.passed, gate.details)

    def test_transform_build_evidence_has_no_path_to_dangle(self) -> None:
        """The defect check the lane asked for: does independent_build.json
        share it? No — the transform record cites no filesystem artifact at
        all (its view is TaskIR-derived; samples carry prompt hashes and SQL,
        both self-contained), so there is nothing to digest. Pin that shape so
        a future field addition that DOES cite a path revisits provenance.
        (provider / model / endpoint_host are BUILDER provenance — strings
        naming the route, not filesystem paths.)"""
        fields = set(independent.IndependentBuildResult.model_fields)
        self.assertEqual(
            fields,
            {"task_id", "task_content_hash", "role", "status", "agreement",
             "samples", "detail", "provider", "model", "endpoint_host"},
        )
        sample_fields = set(independent.IndependentSample.model_fields)
        self.assertNotIn("bundle", fields)
        self.assertTrue(
            {"prompt_sha256", "sql_by_mart"} <= sample_fields,
            "transform evidence is self-contained by construction",
        )


# Loader-session tests: replace and statically validate one plan before submit.
# Certification errors are never fed back; this block enables the session.
_LOADER_SESSION_BLOCK: dict = {
    "enabled": True,
    "max_turns": 2,
    "max_tool_calls": 2,
    "max_usd": 0.20,
    "wall_clock_s": 600,
    "hard_caps": {"turns": 2, "tool_calls": 2, "usd": 0.20, "wall_clock_s": 600},
}

_ONE_SHOT_LOAD_SAMPLE_KEYS = frozenset(
    {"sample_index", "prompt_sha256", "load_plan", "rewards", "errors", "dev_pass"}
)


def _loader_policy(block: dict | None = None):
    from elt_taskgen.review.tools import validators as witness_tools

    return witness_tools.loader_policy(witness_tools.loader_limits(dict(block or _LOADER_SESSION_BLOCK)))


def plan_entries(plan: dict) -> list[dict]:
    """The wire shape of `replace_load_plan`: one {table, path, format} per table."""
    return [
        {"table": table, "path": step["path"], "format": step["format"]}
        for table, step in sorted(plan.items())
    ]


def replace_call(plan: dict, id_: str = "tr") -> dict:
    return tool_use_block("replace_load_plan", {"plan": plan_entries(plan)}, id_)


def submit_load_call(id_: str = "ts") -> dict:
    return tool_use_block("submit_load_plan", {}, id_)


class TestLoaderSession(LoaderTestCase):
    def setUp(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        providers_mod.clear_behavior_caches()
        self.addCleanup(providers_mod.clear_behavior_caches)

    def test_pipeline_workspace_under_repository_runs_uses_scratch_tool_root(self) -> None:
        """The loader's logical source paths stay confined to rendered_dev;
        its generic ToolContext root must not become the protected pipeline
        workspace just because the normal workspace lives under ``runs/``."""
        from unittest import mock

        from elt_taskgen import workspace as workspace_mod
        from elt_taskgen.review.tools import registry as registry_mod

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        checkout = Path(tmp.name) / "checkout"
        workspace = checkout / "runs" / "pipeline"
        shutil.copytree(self.base_workspace, workspace)
        task_dir = workspace / "tasks" / self.task.task_id
        eltbench.emit_variant(
            self.task,
            self.gold,
            TaskVariant.EXTRACT_LOAD,
            task_dir / "variants" / "extract_load",
            populations_dir=task_dir / "populations",
        )
        provider = SessionDouble(
            [[replace_call(CORRECT_PLAN)], [submit_load_call()]]
        )

        with mock.patch.object(workspace_mod, "repo_root", lambda: checkout):
            self.assertTrue(registry_mod.path_under_runs(workspace))
            result = independent.run_independent_load_build(
                self.task,
                workspace,
                provider,
                self.gold,
                session_policy=_loader_policy(),
            )
            ctx = provider.sessions[0]["ctx"]
            self.assertFalse(registry_mod.path_under_runs(ctx.root))

        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertNotEqual(ctx.root, workspace.resolve())
        self.assertFalse(ctx.root.exists(), "per-session tool scratch must be removed")

    def test_session_disabled_witness_is_byte_identical_to_cold_resample(self) -> None:
        from unittest import mock

        from elt_taskgen.review import providers as providers_mod

        workspace = self.bundled_workspace()
        bundle = independent.el_bundle_dir(workspace, self.task.task_id)
        self.assertTrue(providers_mod.role_loop_limits(independent.LOADER_ROLE_NAME)["enabled"])
        disabled = json.loads(json.dumps(providers_mod._agents_doc()))
        disabled["roles"][independent.LOADER_ROLE_NAME]["session"]["enabled"] = False
        with mock.patch.object(providers_mod, "_agents_doc", lambda: disabled):
            providers_mod.clear_behavior_caches()
            self.assertFalse(providers_mod.role_loop_limits(independent.LOADER_ROLE_NAME)["enabled"])
            gated = OneShotOnlyDouble([plan_response(CORRECT_PLAN)])
            self.assertIsNone(independent._witness_session_block(gated, independent.LOADER_ROLE_NAME))
            result = independent.run_independent_load_build(self.task, workspace, gated, self.gold)
            self.assertEqual(gated.sessions, 0)
            self.assertEqual(
                gated.calls,
                [(independent.LOADER_ROLE_NAME, independent.load_sample_prompt(self.task, bundle, 0))],
            )
            reference = independent.run_independent_load_build(
                self.task, workspace, ScriptedProvider([plan_response(CORRECT_PLAN)]), self.gold
            )
            self.assertEqual(result, reference)
            path = independent.record_load_build_result(workspace, self.task, result)
            document = json.loads(path.read_bytes())
            for sample in document["samples"]:
                self.assertEqual(set(sample), _ONE_SHOT_LOAD_SAMPLE_KEYS)
        providers_mod.clear_behavior_caches()
        self.assertIsNotNone(independent._witness_session_block(gated, independent.LOADER_ROLE_NAME))
        self.assertEqual(
            set(independent.IndependentLoadBuildResult.model_fields),
            {"task_id", "task_content_hash", "role", "status", "agreement", "samples",
             "bundle", "bundle_digest", "detail", "provider", "model", "endpoint_host"},
        )

    def test_loader_errors_record_is_never_fed_back(self) -> None:
        from unittest import mock

        from elt_taskgen.review import session as S

        workspace = self.bundled_workspace()
        bundle = independent.el_bundle_dir(workspace, self.task.task_id)
        policy = _loader_policy()
        # Session 1 uses a bundle-relative path that certifies as missing.
        # The corrected second session must not see any first-session state.
        wrong = {
            table: {"path": f"sources/{step['path']}", "format": step["format"]}
            for table, step in CORRECT_PLAN.items()
        }
        provider = SessionDouble(
            [[replace_call(wrong)], [submit_load_call()]],
            [[replace_call(CORRECT_PLAN)], [submit_load_call()]],
        )
        certified: list[tuple[dict, list]] = []
        real_evaluate = independent.evaluate_load_build

        def evaluate_after_the_session(task, gold, load_plan, ws):
            # CERTIFY only after the session has closed: every scripted turn
            # of the CURRENT session is consumed before the plan is executed.
            self.assertEqual(provider.transports[-1].script, [])
            rewards, errors = real_evaluate(task, gold, load_plan, ws)
            certified.append((dict(rewards), sorted(errors.values())))
            return rewards, errors

        with mock.patch.object(independent, "evaluate_load_build", evaluate_after_the_session):
            result = independent.run_independent_load_build(
                self.task, workspace, provider, self.gold, session_policy=policy
            )
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertEqual(provider.completes, 0)
        self.assertEqual(len(result.samples), 2)
        first, second = result.samples
        self.assertFalse(first.dev_pass)
        self.assertEqual(first.rewards, {pop.value: 0.0 for pop in P})
        self.assertTrue(first.errors)
        self.assertIn("does not exist under the source root", first.errors[P.PRIMARY.value])
        self.assertEqual(first.terminal, S.TerminalState.SUBMITTED.name)
        self.assertEqual([c.name for c in first.tool_calls], ["replace_load_plan", "check_load_plan"])
        self.assertEqual(second.load_plan, CORRECT_PLAN)
        self.assertEqual(len(certified), 2)
        # The record the certifier wrote is EVIDENCE ONLY: no fragment of any
        # `errors[pop]` text, no `expected N rows, got M`, no count reaches
        # any message of either session — the second session's view is the
        # bundle view under its salt, and every tool result is a static code.
        error_texts = [text for sample in result.samples for text in sample.errors.values()]
        self.assertTrue(error_texts)
        views = provider.views_seen()
        self.assertEqual(len(views), 2)
        self.assertEqual(views[1], independent.loader_session_view(self.task, bundle, 1, limits=policy.limits))
        self.assertTrue(views[1].startswith(views[0]))
        # Beyond the initial view (the bundle and the factory protocol, which
        # legitimately say "the expected row count" in the abstract), nothing
        # the certifier wrote reaches a message ...
        for text in provider.messages_seen():
            lowered = text.lower()
            for token in ("expected", "rows, got", "does not exist", "dev_pass", "reward", "answer_key", "stage1"):
                self.assertNotIn(token, lowered, token)
        # ... and the views carry no measured value or error text either: the
        # second view is the bundle under its salt, nothing of session 1.
        for text in provider.messages_seen() + views:
            lowered = text.lower()
            for token in ("rows, got", "does not exist", "dev_pass", "reward", "answer_key", "stage1"):
                self.assertNotIn(token, lowered, token)
            for error in error_texts:
                self.assertNotIn(error, text)
        # What the model DID see after replacing the plan: the static check's
        # code only (ok for both plans: the wrong root is inside the source
        # root, which is exactly why it is a convention error the certifier,
        # not the static check, catches), rendered without a single digit.
        for transport in provider.transports:
            last = transport.calls[-1]["messages"]
            results = [
                block["content"]
                for message in last
                if message.get("role") == "user" and isinstance(message.get("content"), list)
                for block in message["content"]
                if block.get("type") == "tool_result"
            ]
            self.assertEqual(len(results), 1)
            self.assertIn("[load_plan] ok", results[0])
            self.assertIn("ok=true", results[0])
            self.assertIsNone(re.search(r"\d", results[0]), results[0])
        # A plan that aces the public tree and fails a hidden population is
        # ONE session and NEEDS_ADJUDICATION (the rule is the one-shot rule),
        # and its `expected N rows, got M` record is written, never sent.
        task_dir = workspace / "tasks" / self.task.task_id
        rendered = task_dir / "populations" / P.DEVELOPMENT.value / "rendered"
        shutil.copy(rendered / "mongodb" / "orders.jsonl", rendered / "mongodb" / "orders_v2.jsonl")
        decoy = with_table(CORRECT_PLAN, "orders", path="mongodb/orders_v2.jsonl")
        provider = SessionDouble(
            [[replace_call(decoy)], [submit_load_call()]],
            [[replace_call(CORRECT_PLAN)], [submit_load_call()]],
        )
        adjudicate = independent.run_independent_load_build(
            self.task, workspace, provider, self.gold, session_policy=policy
        )
        self.assertEqual(adjudicate.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(len(adjudicate.samples), 1)
        self.assertTrue(adjudicate.samples[0].dev_pass)
        self.assertEqual(len(provider.transports), 1, "a hidden-only failure was resampled away")
        hidden_error = adjudicate.samples[0].errors[P.PRIMARY.value]
        self.assertTrue(hidden_error)
        for text in provider.messages_seen():
            self.assertNotIn("expected", text.lower())
        for text in provider.messages_seen() + provider.views_seen():
            self.assertNotIn(hidden_error, text)
            self.assertIsNone(re.search(r"expected \d+ rows?, got \d+", text))
        # The certifier's `expected N rows, got M` record itself
        # (verification/upstream_eval.py, the count-mismatch shape the roadmap
        # names): produced for session 1, written into its sample, and absent
        # from everything session 2 is shown — the second view is the bundle
        # view under its salt and nothing else.
        counted = {pop.value: (1.0 if pop is P.DEVELOPMENT else 0.0) for pop in P}
        mismatch = {P.PRIMARY.value: "expected 7 rows, got 3", P.STRESS.value: "expected 40 rows, got 3"}
        provider = SessionDouble(
            [[replace_call(CORRECT_PLAN)], [submit_load_call()]],
            [[replace_call(CORRECT_PLAN)], [submit_load_call()]],
        )
        outcomes = iter([(dict(counted), dict(mismatch)), ({pop.value: 1.0 for pop in P}, {})])
        with mock.patch.object(independent, "evaluate_load_build", lambda *a, **k: next(outcomes)):
            # dev_pass with a hidden mismatch adjudicates after ONE session ...
            first_only = independent.run_independent_load_build(
                self.task, workspace, provider, self.gold, session_policy=policy
            )
        self.assertEqual(first_only.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(first_only.samples[0].errors, mismatch)
        self.assertEqual(len(provider.transports), 1)
        failing = {pop.value: 0.0 for pop in P}
        provider = SessionDouble(
            [[replace_call(CORRECT_PLAN)], [submit_load_call()]],
            [[replace_call(CORRECT_PLAN)], [submit_load_call()]],
        )
        outcomes = iter([(dict(failing), dict(mismatch)), ({pop.value: 1.0 for pop in P}, {})])
        with mock.patch.object(independent, "evaluate_load_build", lambda *a, **k: next(outcomes)):
            # ... while a public failure is resampled, and the record of the
            # first session is nowhere in the second.
            resampled = independent.run_independent_load_build(
                self.task, workspace, provider, self.gold, session_policy=policy
            )
        self.assertEqual(resampled.status, independent.STATUS_AGREED)
        self.assertEqual(resampled.samples[0].errors, mismatch)
        self.assertEqual(len(provider.transports), 2)
        for text in provider.messages_seen() + provider.views_seen():
            for record in mismatch.values():
                self.assertNotIn(record, text)
            self.assertIsNone(re.search(r"expected \d+ rows?, got \d+", text))
        self.assertEqual(
            provider.views_seen()[1],
            independent.loader_session_view(self.task, bundle, 1, limits=policy.limits),
        )
        # The static check DOES answer with a code (and the table) when the
        # plan is malformed, and a red check leaves the session to decide.
        provider = SessionDouble(
            [[replace_call({k: v for k, v in CORRECT_PLAN.items() if k != "orders"})], [submit_load_call()]],
        )
        # Submitting an uncovered plan is the certifier's parse refusal
        # (the unchanged `parse_load_plan`), recorded on a scored-0 sample
        # with no artifact — NEEDS_ADJUDICATION, never a ProviderProtocolError
        # the EL gate would lift into an infrastructure halt (review finding
        # 1-5, batch-repair round 2).
        refused = independent.run_independent_load_build(
            self.task, workspace, provider, self.gold, session_policy=policy, max_samples=1
        )
        self.assertEqual(refused.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(refused.samples[-1].rewards, {})
        self.assertIn("must cover exactly the source tables", refused.samples[-1].submission_refused)
        self.assertIn("the certifier refused the load plan", refused.detail)
        answered = provider.transports[0].calls[-1]["messages"]
        codes = [
            block["content"]
            for message in answered
            if message.get("role") == "user" and isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_result"
        ]
        self.assertEqual(len(codes), 1)
        self.assertIn("[load_plan] table_uncovered", codes[0])
        self.assertIn("subject=orders", codes[0])
        self.assertIn("ok=false", codes[0])
        # A harness fault inside the loader session propagates unchanged
        # (could-not-measure at the gates stage, C7), nothing recorded.
        from elt_taskgen import cli

        provider = SessionDouble([[replace_call(CORRECT_PLAN)], S.SandboxFault("worker died", code="worker_failed")])
        with self.assertRaises(S.SandboxFault):
            independent.run_independent_load_build(
                self.task, workspace, provider, self.gold, session_policy=policy
            )
        self.assertIsNone(independent.load_load_build_result(workspace, self.task.task_id))
        self.assertEqual(
            cli._transport_marker(["independent LOAD build FAILED: SandboxFault: worker died"]),
            "SandboxFault",
        )
        # Session-mode evidence still passes the gate reader (one-shot keys).
        independent.record_load_build_result(workspace, self.task, result)
        gate = gates._gate_el_independent_load(self.task, workspace)
        self.assertTrue(gate.passed, gate.details)
        loaded = independent.load_load_build_result(workspace, self.task.task_id)
        self.assertEqual(loaded["samples"][-1]["terminal"], "SUBMITTED")
        self.assertEqual(loaded["samples"][-1]["limits"], policy.limits.as_manifest())


#: The 50-task batch's evidence (READ-ONLY; the test below skips when absent).
_BATCH = Path(__file__).resolve().parent.parent / "runs" / "authorized_batch_50_20260908" / "workspace-final"
_D5_TASK_ID = "synsql__educational_expenditure_data_and_analysis__users_expenditures_distribution"
_D5_LOADER_TRANSCRIPT = (
    _BATCH / "transcripts" / "independent_loader"
    / "82fdbd04dcabd5c58ef8f736512813288df9eb18bac80997f47b0e6c57418d81.json"
)


class TestBatchD5LoaderSession(unittest.TestCase):
    """Batch D5 (reports 338/339): the loader session on the batch task
    COMPLETES through the real bounded runner and the el-independent-load
    gate can run on its record. The task's populations are generated, its
    reference executed and its gold frozen offline from the on-disk IR; the
    RECORDED loader turn is replayed verbatim."""

    def setUp(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        if not (_D5_LOADER_TRANSCRIPT.is_file() and (_BATCH / "tasks" / _D5_TASK_ID / "task_ir.json").is_file()):
            self.skipTest("batch evidence not on disk")
        providers_mod.clear_behavior_caches()
        self.addCleanup(providers_mod.clear_behavior_caches)

    def test_recorded_turn_is_scored_and_resampled_then_a_prefix_plan_turns_the_gate_green(self) -> None:
        from elt_taskgen.models import TaskIR
        from elt_taskgen.review import session as S

        try:
            from test_independent import build_workspace
        except ImportError:  # pragma: no cover - depends on how the suite is invoked
            from tests.test_independent import build_workspace

        task = TaskIR.model_validate(json.loads((_BATCH / "tasks" / _D5_TASK_ID / "task_ir.json").read_bytes()))
        record = json.loads(_D5_LOADER_TRANSCRIPT.read_bytes())
        self.assertEqual(record["task_content_hash"], task.content_hash())
        recorded = next(b for b in record["turn"]["content"] if b.get("type") == "tool_use")
        self.assertEqual(recorded["name"], "replace_load_plan")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name) / "taskgen-workspace"
        gold = build_workspace(task, workspace)
        task_dir = workspace / "tasks" / task.task_id
        eltbench.emit_variant(
            task, gold, TaskVariant.EXTRACT_LOAD,
            task_dir / "variants" / "extract_load", populations_dir=task_dir / "populations",
        )
        bundle = independent.el_bundle_dir(workspace, task.task_id)
        # What the loader was shown: the part FILE and the page FILES are in
        # the listing; the readers want their prefix directories.
        listing = independent._sources_listing(bundle)
        self.assertIn("s3/regions/part-00000.jsonl", listing)
        self.assertIn("rest/users/index.json", listing)
        prefixed = [
            dict(item, path=item["path"].rsplit("/", 1)[0])
            if item["format"] in ("s3_jsonl", "rest_pages") else dict(item)
            for item in recorded["input"]["plan"]
        ]
        provider = SessionDouble(
            [[tool_use_block("replace_load_plan", recorded["input"], recorded["id"])], [submit_load_call()]],
            [[tool_use_block("replace_load_plan", {"plan": prefixed}, "tr2")], [submit_load_call("ts2")]],
        )
        policy = _loader_policy()
        result = independent.run_independent_load_build(
            task, workspace, provider, gold, session_policy=policy
        )
        # Session 1 — the recorded turn: the static check's correction is
        # DELIVERED (no tripwire), the plan is submitted, scored 0 with the
        # certifier's `s3_jsonl names a FILE` record, and resampled.
        self.assertEqual(len(result.samples), 2)
        first, second = result.samples
        self.assertEqual(first.terminal, S.TerminalState.SUBMITTED.name)
        self.assertEqual([c.name for c in first.tool_calls], ["replace_load_plan", "check_load_plan"])
        self.assertEqual(first.load_plan["regions"], {"path": "s3/regions/part-00000.jsonl", "format": "s3_jsonl"})
        self.assertFalse(first.dev_pass)
        self.assertEqual(first.rewards, {pop.value: 0.0 for pop in P})
        self.assertIn("s3_jsonl names a FILE", first.errors[P.DEVELOPMENT.value])
        delivered = [
            block["content"]
            for message in provider.transports[0].calls[-1]["messages"]
            if message.get("role") == "user" and isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_result"
        ]
        self.assertEqual(len(delivered), 1)
        self.assertIn("[load_plan] s3_part_file subject=regions ok=false", delivered[0])
        # The certifier's record never reaches a message of either session.
        for text in provider.messages_seen():
            self.assertNotIn("names a FILE", text)
            self.assertIsNone(re.search(r"expected \d+ rows?, got \d+", text))
        # Session 2 — the prefix directories: AGREED on all five populations.
        self.assertEqual(result.status, independent.STATUS_AGREED)
        self.assertTrue(second.dev_pass)
        self.assertEqual(second.rewards, {pop.value: 1.0 for pop in P})
        self.assertEqual(second.load_plan["regions"], {"path": "s3/regions", "format": "s3_jsonl"})
        self.assertEqual(second.load_plan["users"], {"path": "rest/users", "format": "rest_pages"})
        # ... and the gate can run on the record, green.
        independent.record_load_build_result(workspace, task, result)
        gate = gates._gate_el_independent_load(task, workspace)
        self.assertTrue(gate.passed, gate.details)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestLoadPlanShapeRefusals(LoaderTestCase):
    """Review finding 1-5 (batch-repair round 2): a loader plan carrying an
    entry the certifier's `parse_submission` refuses — an extra or case-
    variant table, or one table named twice — passed the static pre-flight
    `ok`, and the refusal then surfaced only after the session as a
    `ProviderProtocolError` the EL gates stage lifted into an infrastructure
    halt (`cli._transport_marker`; exit 2, no round) for a model slip.  The
    static check now refuses both shapes in-session with a closed code, and
    a session-submitted plan the certifier still refuses is a scored-0
    sample, never a transport class in the producer note.  Seeded from
    scratchpad/impl/batch/probe_r2_loader_plan_shape.py."""

    def setUp(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        providers_mod.clear_behavior_caches()
        self.addCleanup(providers_mod.clear_behavior_caches)

    def test_static_check_refuses_unknown_and_duplicate_tables_in_session(self) -> None:
        from elt_taskgen.review.tools import projection as PJ
        from elt_taskgen.review.tools import validators as V

        workspace = self.bundled_workspace()
        session = V.LoaderSession(workspace=workspace, task=self.task)
        registry = V.loader_registry()
        good = plan_entries(CORRECT_PLAN)
        extra = {"table": "customers_extra", "path": "files/extra.csv", "format": "csv"}
        variant = {"table": "Customers", "path": "files/extra.csv", "format": "csv"}
        mart = {"table": "customer_summary", "path": "files/extra.csv", "format": "csv"}
        twice = {"table": "customers", "path": "postgres/other.sql", "format": "postgres_sql"}
        for label, plan, code, subject in (
            ("extra table", good + [extra], "table_unknown", ""),
            ("case variant", good + [variant], "table_unknown", ""),
            ("public non-table name", good + [mart], "table_unknown", "customer_summary"),
            ("duplicate", good + [twice], "table_duplicate", "customers"),
            ("good", good, "ok", ""),
        ):
            with self.subTest(label=label):
                diag = registry.dispatch(session.context(), "check_load_plan", {"plan": plan})
                self.assertEqual((diag.code, diag.subject, diag.ok), (code, subject, code == "ok"))
                self.assertIn(diag.code, PJ.LOAD_PLAN_CODES)
                payload = PJ.serialize_for_transport(diag, task=self.task)
                PJ.assert_value_free(payload.encode("utf-8"), task=self.task)
                self.assertIsNone(re.search(r"\d", diag.render()), diag.render())
                # Exactly the plans the certifier would refuse are red.
                text = json.dumps({"load_plan": V._coerce_load_plan(plan)})
                if code == "table_unknown":
                    with self.assertRaises(ProviderProtocolError):
                        independent.parse_load_plan(self.task, text)
                else:
                    independent.parse_load_plan(self.task, text)
        # The duplicate is reported before the collapsed plan could hide it.
        self.assertEqual(V._load_plan_duplicates(good + [twice]), ("customers",))
        self.assertEqual(V._load_plan_duplicates(good), ())
        # And `replace_load_plan` auto-runs the check on the raw entries, so
        # the seat is told in-session (the collapsed plan keeps the last step).
        policy = _loader_policy()
        bad = dict(CORRECT_PLAN)
        bad["customers_extra"] = {"path": "files/extra.csv", "format": "csv"}
        provider = SessionDouble([[replace_call(bad)], [submit_load_call()]])
        with mock.patch.object(independent, "evaluate_load_build", side_effect=AssertionError("never executed")):
            result = independent.run_independent_load_build(
                self.task, workspace, provider, self.gold, session_policy=policy, max_samples=1
            )
        answered = provider.transports[0].calls[-1]["messages"]
        codes = [
            block["content"]
            for message in answered
            if message.get("role") == "user" and isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_result"
        ]
        self.assertEqual(len(codes), 1)
        self.assertIn("[load_plan] table_unknown", codes[0])
        self.assertNotIn("customers_extra", codes[0])  # model-authored: withheld

    def test_refused_session_plan_is_a_scored_zero_sample_never_a_transport_class(self) -> None:
        from elt_taskgen import cli

        workspace = self.bundled_workspace()
        policy = _loader_policy()
        bad = dict(CORRECT_PLAN)
        bad["customers_extra"] = {"path": "files/extra.csv", "format": "csv"}
        provider = SessionDouble(
            [[replace_call(bad)], [submit_load_call()]],
            [[replace_call(bad)], [submit_load_call()]],
        )
        with mock.patch.object(independent, "evaluate_load_build", side_effect=AssertionError("never executed")):
            result = independent.run_independent_load_build(
                self.task, workspace, provider, self.gold, session_policy=policy, max_samples=2
            )
        self.assertEqual(result.status, independent.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(len(result.samples), 2)
        for sample in result.samples:
            self.assertEqual((sample.rewards, sample.load_plan, sample.dev_pass), ({}, {}, False))
            self.assertIn("must cover exactly the source tables", sample.submission_refused)
            self.assertEqual(sample.terminal, "SUBMITTED")
        self.assertIn("the certifier refused the load plan", result.detail)
        self.assertNotIn("ProviderProtocolError", result.detail)
        self.assertEqual(cli._transport_marker([f"independent LOAD build FAILED: {result.detail}"]), "")
        # Recorded evidence: the refusal travels on the refused sample only,
        # and a one-shot record stays byte for byte what it was.
        path = independent.record_load_build_result(workspace, self.task, result)
        document = json.loads(path.read_bytes())
        self.assertTrue(all("submission_refused" in sample for sample in document["samples"]))
        one_shot = independent.run_independent_load_build(
            self.task, workspace, ScriptedProvider([plan_response(CORRECT_PLAN)]), self.gold
        )
        for sample in independent._evidence_document(one_shot)["samples"]:
            self.assertEqual(set(sample), _ONE_SHOT_LOAD_SAMPLE_KEYS)
        # A one-shot loader keeps its contract: the last unparseable sample raises.
        with self.assertRaises(ProviderProtocolError):
            independent.run_independent_load_build(
                self.task, workspace, ScriptedProvider(["not a plan", "not a plan"]), self.gold, max_samples=2
            )

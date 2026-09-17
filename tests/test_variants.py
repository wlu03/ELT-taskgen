"""Tests for Phase B: export-time subtask variants + reward decomposition.

WHY THIS EXISTS
The two-subtask decomposition (EXTRACT_LOAD / TRANSFORM) must be exactly
export-time variants over the SAME frozen artifacts the parent task validated:
the EL reward is strict-binary compare_stage1, the T reward is the existing
mart-fraction path, and no third comparator exists. These tests run the real
demo task end-to-end (generate -> reference -> freeze gold -> emit variants)
and prove: the trusted reference solution scores exactly 1.0 against both
subtask variants; a skip-load submission scores EL=0.0 (binary, never a
fraction); a keys-only submission scores T=0.0; the provided T warehouse holds
SOURCE tables only (no marts, counts pinned to frozen gold); variant ids share
the parent family_id; and release ships/leak-checks/checksums the variant
trees fail-closed.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import duckdb
import yaml

from elt_taskgen import cli, demo_fixture
from elt_taskgen.engine import Engine, variant_gate_stage
from elt_taskgen.verification.gates import VARIANT_GATE_NAMES as variant_gate_names_map
from elt_taskgen.export import eltbench, release
from elt_taskgen.generation import source_data
from elt_taskgen.models import (
    PopulationName,
    RLVR_TASK_VARIANTS,
    TaskVariant,
    variant_task_id,
)
from elt_taskgen.reference.gold import freeze_gold, load_gold
from elt_taskgen.reference.runner import run_reference
from elt_taskgen.reference.solution import (
    build_reference,
    execute_mart,
    find_rendered_artifact,
)
from elt_taskgen.verification import upstream_eval

P = PopulationName
MART = demo_fixture.MART_NAME
SOURCE_TABLES = {"customers", "orders", "order_items"}


#: One real demo-task pipeline run shared by EVERY class in this module
#: (generation + reference execution over five populations is the expensive
#: part; tests only READ the frozen artifacts — the CLI test's re-emission
#: rewrites content-equivalent bundles).
_STATE: dict = {}


def setUpModule():
    workspace = Path(tempfile.mkdtemp(prefix="elt-variants-"))
    _STATE["workspace"] = workspace
    _STATE["prior_base_url"] = os.environ.pop(eltbench.FLAT_FILES_BASE_URL_ENV, None)

    task = demo_fixture.demo_task()
    task_root = workspace / "tasks" / task.task_id
    populations_dir = task_root / "populations"
    answer_key_dir = task_root / "answer_key"

    for pop in PopulationName:
        rows = source_data.generate_rows(task, pop)
        pop_dir = populations_dir / pop.value
        source_data.write_rows(rows, pop_dir / "rows")
        source_data.render_population(task, pop, rows, pop_dir / "rendered")

    results = {pop: run_reference(task, pop, workspace) for pop in PopulationName}
    gold = freeze_gold(task, results, answer_key_dir)

    eltbench.export_task(task, gold, task_root / "task", answer_key_dir)
    variants_root = task_root / "variants"
    for variant in TaskVariant:
        eltbench.emit_variant(
            task,
            gold,
            variant,
            variants_root / variant.value,
            populations_dir=populations_dir,
        )

    # Registered engine state so the CLI export subcommand can run here.
    engine = Engine(workspace)
    try:
        engine.register(task)
    finally:
        engine.close()

    _STATE.update(
        task=task,
        gold=gold,
        results=results,
        task_root=task_root,
        populations_dir=populations_dir,
        answer_key_dir=answer_key_dir,
        variants_root=variants_root,
    )


def tearDownModule():
    shutil.rmtree(_STATE["workspace"], ignore_errors=True)
    prior = _STATE.get("prior_base_url")
    if prior is not None:
        os.environ[eltbench.FLAT_FILES_BASE_URL_ENV] = prior


class VariantPipelineBase(unittest.TestCase):
    """Read-only view over the module-scoped pipeline run."""

    @classmethod
    def setUpClass(cls):
        cls.workspace = _STATE["workspace"]
        cls.task = _STATE["task"]
        cls.gold = _STATE["gold"]
        cls.results = _STATE["results"]
        cls.task_root = _STATE["task_root"]
        cls.populations_dir = _STATE["populations_dir"]
        cls.answer_key_dir = _STATE["answer_key_dir"]
        cls.variants_root = _STATE["variants_root"]

    # -- helpers -----------------------------------------------------------

    @classmethod
    def reference_marts(cls, population: PopulationName) -> dict[str, list]:
        """Run the trusted reference SQL against the PROVIDED variant warehouse."""
        db_path = (
            cls.variants_root
            / TaskVariant.TRANSFORM.value
            / "task"
            / eltbench.WAREHOUSE_DIRNAME
            / f"{population.value}.duckdb"
        )
        reference = build_reference(cls.task)
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            return {
                mart.name: execute_mart(con, mart, reference.sql_by_mart[mart.name])
                for mart in cls.task.marts
            }
        finally:
            con.close()

    def reward_json(self, variant: TaskVariant) -> dict:
        path = self.variants_root / variant.value / eltbench.REWARD_MANIFEST
        return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Rewards: reference solution round-trips, adversarial submissions score 0.0
# ---------------------------------------------------------------------------

class TestVariantRewards(VariantPipelineBase):
    def test_el_reference_solution_scores_exactly_one(self):
        for pop in PopulationName:
            result = upstream_eval.evaluate_variant(
                TaskVariant.EXTRACT_LOAD,
                self.task,
                self.gold,
                pop,
                actual_stage1=self.results[pop].stage1_counts,
            )
            self.assertTrue(result.stage1_pass, pop.value)
            self.assertEqual(result.reward, 1.0, pop.value)
            # Per-table detail is recorded even though the reward is binary.
            self.assertEqual(set(result.stage1_detail), SOURCE_TABLES)
            self.assertEqual(result.mart_scores, {})

    def test_el_skip_load_submission_scores_zero(self):
        counts = dict(self.results[P.PRIMARY].stage1_counts)
        counts.pop("order_items")  # one source backend never loaded
        result = upstream_eval.evaluate_variant(
            TaskVariant.EXTRACT_LOAD, self.task, self.gold, P.PRIMARY,
            actual_stage1=counts,
        )
        self.assertFalse(result.stage1_pass)
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(result.stage1_detail["order_items"], "table not found")

    def test_el_reward_is_binary_not_a_fraction(self):
        # 2 of 3 tables exact must still be 0.0, never 2/3.
        counts = dict(self.results[P.PRIMARY].stage1_counts)
        counts["orders"] += 1
        result = upstream_eval.evaluate_variant(
            TaskVariant.EXTRACT_LOAD, self.task, self.gold, P.PRIMARY,
            actual_stage1=counts,
        )
        self.assertEqual(result.reward, 0.0)

    def test_el_empty_submission_scores_zero(self):
        result = upstream_eval.evaluate_variant(
            TaskVariant.EXTRACT_LOAD, self.task, self.gold, P.PRIMARY,
            actual_stage1={},
        )
        self.assertEqual(result.reward, 0.0)

    def test_t_reference_solution_scores_exactly_one(self):
        for pop in (P.PRIMARY, P.COUNTERFACTUAL):
            result = upstream_eval.evaluate_variant(
                TaskVariant.TRANSFORM,
                self.task,
                self.gold,
                pop,
                actual_marts=self.reference_marts(pop),
            )
            self.assertEqual(result.reward, 1.0, pop.value)
            self.assertEqual(result.mart_scores, {MART: True})
            self.assertTrue(result.stage1_pass)

    def test_t_keys_only_submission_scores_zero(self):
        _, gold_rows = upstream_eval.parse_canonical_csv(
            self.gold.stage2_csv[P.PRIMARY.value][MART]
        )
        keys_only = [
            {
                "customer_id": row["customer_id"],
                "completed_order_count": "-1",  # junk measures, valid keys
                "total_spend": "-1",
            }
            for row in gold_rows
        ]
        result = upstream_eval.evaluate_variant(
            TaskVariant.TRANSFORM, self.task, self.gold, P.PRIMARY,
            actual_marts={MART: keys_only},
        )
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(result.mart_scores, {MART: False})

    def test_t_missing_population_gold_fails_closed(self):
        stripped = self.gold.model_copy(update={"stage2_csv": {}})
        result = upstream_eval.evaluate_variant(
            TaskVariant.TRANSFORM, self.task, stripped, P.PRIMARY,
            actual_marts=self.reference_marts(P.PRIMARY),
        )
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(result.mart_scores, {MART: False})

    def test_full_variant_dispatches_to_the_single_reward(self):
        via_variant = upstream_eval.evaluate_variant(
            TaskVariant.FULL,
            self.task,
            self.gold,
            P.PRIMARY,
            actual_stage1=self.results[P.PRIMARY].stage1_counts,
            actual_marts=self.results[P.PRIMARY].mart_rows,
        )
        direct = upstream_eval.evaluate(
            self.task,
            self.gold,
            P.PRIMARY,
            dict(self.results[P.PRIMARY].stage1_counts),
            dict(self.results[P.PRIMARY].mart_rows),
        )
        self.assertEqual(via_variant, direct)
        self.assertEqual(via_variant.reward, 1.0)


# ---------------------------------------------------------------------------
# Bundles: layout, warehouses, reward manifests, identity
# ---------------------------------------------------------------------------

class TestVariantBundles(VariantPipelineBase):
    def test_el_bundle_layout(self):
        el_task = self.variants_root / TaskVariant.EXTRACT_LOAD.value / "task"
        self.assertTrue((el_task / "config.yaml").is_file())
        self.assertTrue((el_task / eltbench.EL_DOCUMENTATION_FILENAME).is_file())
        for table in SOURCE_TABLES:
            self.assertTrue((el_task / "schemas" / f"{table}.csv").is_file(), table)
        # The transform stage is out of scope: no mart definitions shipped.
        self.assertFalse((el_task / "data_model.yaml").exists())
        # reward.json is PRIVATE: beside task/, never inside it.
        self.assertFalse((el_task / eltbench.REWARD_MANIFEST).exists())
        self.assertTrue(
            (el_task.parent / eltbench.REWARD_MANIFEST).is_file()
        )

    def test_t_bundle_layout(self):
        t_task = self.variants_root / TaskVariant.TRANSFORM.value / "task"
        self.assertTrue((t_task / "data_model.yaml").is_file())
        for table in SOURCE_TABLES:
            self.assertTrue((t_task / "schemas" / f"{table}.csv").is_file(), table)
        # No config.yaml: extract+load is not part of the T subtask.
        self.assertFalse((t_task / "config.yaml").exists())
        for pop in PopulationName:
            self.assertTrue(
                (t_task / eltbench.WAREHOUSE_DIRNAME / f"{pop.value}.duckdb").is_file(),
                pop.value,
            )
        self.assertTrue((t_task / eltbench.T_DOCUMENTATION_FILENAME).is_file())

    def test_t_bundle_documentation_states_the_warehouse_contract(self):
        """THE EXPORT MUST CARRY WHAT THE MEASUREMENT ASSUMES.

        The T bundle used to ship the PARENT specification and nothing else:
        no statement that a warehouse is provided, no DuckDB, no `main`, no
        submission contract, no scoring rule — while the calibrator that
        SCORES T difficulty tells its solver all of it. The difficulty label
        was therefore measured on a materially different task.
        """
        text = (
            self.variants_root / TaskVariant.TRANSFORM.value / "task"
            / eltbench.T_DOCUMENTATION_FILENAME
        ).read_text()
        self.assertIn(f"{eltbench.WAREHOUSE_DIRNAME}/", text)
        self.assertIn(".duckdb", text)
        self.assertIn("`main`", text)
        self.assertIn("data_model.yaml", text)
        for pop in PopulationName:
            self.assertIn(pop.value, text)
        self.assertIn("fraction of target marts", text)
        self.assertIn("standalone DuckDB SELECT", text)
        # The source-table block is the SAME text the parent bundle publishes
        # (structural_completeness and council read exactly this block).
        block = "\n".join(eltbench._source_schema_markdown(self.task))
        self.assertIn(block.strip(), text)
        # ... and the EL bundle still says nothing about a warehouse file.
        el_text = (
            self.variants_root / TaskVariant.EXTRACT_LOAD.value / "task"
            / eltbench.EL_DOCUMENTATION_FILENAME
        ).read_text()
        self.assertNotIn(".duckdb", el_text)

    def test_t_documentation_and_calibration_share_the_scoring_text(self):
        """One statement of the T reward contract, in both places.

        The bundle a solver receives and the prompt whose pass rate becomes
        this task's difficulty label must describe the SAME reward, or the
        label measures a different task than the one that ships.
        """
        from elt_taskgen.corpus import calibration

        doc = eltbench.t_documentation(self.task, sorted(self.gold.stage1))
        view = calibration.solver_view(self.task, TaskVariant.TRANSFORM)
        for line in eltbench.T_SCORING_CONTRACT:
            self.assertIn(line, doc)
            self.assertIn(line, view)

    def test_t_warehouse_is_source_tables_only_with_gold_counts(self):
        db_path = (
            self.variants_root
            / TaskVariant.TRANSFORM.value
            / "task"
            / eltbench.WAREHOUSE_DIRNAME
            / f"{P.COUNTERFACTUAL.value}.duckdb"
        )
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            tables = {
                r[0]
                for r in con.execute(
                    "select table_name from information_schema.tables"
                ).fetchall()
            }
            self.assertEqual(tables, SOURCE_TABLES)
            self.assertNotIn(MART, tables)
            for table in sorted(SOURCE_TABLES):
                (count,) = con.execute(f'select count(*) from "{table}"').fetchone()
                self.assertEqual(
                    int(count),
                    self.gold.stage1[P.COUNTERFACTUAL.value][table],
                    table,
                )
        finally:
            con.close()

    def test_variant_ids_share_parent_family(self):
        tid = self.task.task_id
        for variant, suffix in (
            (TaskVariant.EXTRACT_LOAD, "__el"),
            (TaskVariant.TRANSFORM, "__t"),
        ):
            manifest = self.reward_json(variant)
            self.assertEqual(manifest["task_id"], tid + suffix)
            self.assertEqual(manifest["task_id"], variant_task_id(tid, variant))
            self.assertEqual(manifest["parent_task_id"], tid)
            self.assertEqual(manifest["family_id"], self.task.family_id)

    def test_el_reward_manifest_contract(self):
        manifest = self.reward_json(TaskVariant.EXTRACT_LOAD)
        self.assertEqual(manifest["variant"], "extract_load")
        self.assertEqual(
            manifest["evaluator"],
            "elt_taskgen.verification.upstream_eval.compare_stage1",
        )
        self.assertEqual(manifest["mode"], "strict_binary")
        self.assertIs(manifest["per_table_detail"], True)
        self.assertEqual(set(manifest["expected"]), {p.value for p in PopulationName})
        self.assertEqual(
            manifest["expected"][P.PRIMARY.value],
            "answer_key/gold/primary/stage1_counts.json",
        )
        # The named answer-key input actually exists and matches frozen gold.
        counts = json.loads(
            (self.task_root / manifest["expected"][P.PRIMARY.value]).read_text()
        )
        self.assertEqual(counts, self.gold.stage1[P.PRIMARY.value])
        # ... and so does the GRADED SOURCE ROOT for every population: the
        # reward is compare_stage1 over all of them, so naming only the
        # expected counts left four fifths of the graded surface unnamed (and,
        # in a release, unshipped).
        self.assertEqual(
            manifest["sources"],
            {p.value: f"populations/{p.value}/rendered" for p in PopulationName},
        )
        self.assertEqual(manifest["load_plan_root"], "sources[<population>]")
        self.assertEqual(manifest["populations_required"], "all")
        self.assertEqual(
            manifest["serving"], f"answer_key/{eltbench.SOURCES_SERVING_MANIFEST}"
        )
        for pop, rel in manifest["sources"].items():
            root = self.task_root / rel
            self.assertTrue(root.is_dir(), pop)
            for table in self.task.tables:
                find_rendered_artifact(self.task, root, table.name)
        # The T manifest names no sources: its starting warehouse is provided.
        self.assertNotIn("sources", self.reward_json(TaskVariant.TRANSFORM))

    def test_t_reward_manifest_contract(self):
        manifest = self.reward_json(TaskVariant.TRANSFORM)
        self.assertEqual(manifest["variant"], "transform")
        self.assertEqual(
            manifest["evaluator"],
            "elt_taskgen.verification.upstream_eval.compare_mart",
        )
        self.assertEqual(manifest["mode"], "mart_fraction")
        self.assertEqual(
            manifest["gold"][P.PRIMARY.value][MART],
            f"answer_key/gold/primary/{MART}.csv",
        )
        gold_csv = (
            self.task_root / manifest["gold"][P.PRIMARY.value][MART]
        ).read_text()
        self.assertEqual(gold_csv, self.gold.stage2_csv[P.PRIMARY.value][MART])
        # The warehouse path is relative to the UNIT's own public bundle, and
        # it RESOLVES: the old 'task/warehouse/<pop>.duckdb' string resolved
        # under neither declared base — it named a directory that exists
        # nowhere, in a workspace or in a release.
        self.assertEqual(
            manifest["warehouse"][P.PRIMARY.value],
            f"{eltbench.WAREHOUSE_DIRNAME}/primary.duckdb",
        )
        self.assertIn("variants/transform/task/", manifest["warehouse_base"])
        unit_public = self.variants_root / TaskVariant.TRANSFORM.value / "task"
        for pop in PopulationName:
            self.assertTrue(
                (unit_public / manifest["warehouse"][pop.value]).is_file(),
                pop.value,
            )
        # The reward is over ALL graded populations, all-or-nothing.
        self.assertEqual(manifest["populations_required"], "all")

    def test_full_reward_manifest_composite(self):
        manifest = self.reward_json(TaskVariant.FULL)
        self.assertEqual(manifest["variant"], "full")
        self.assertEqual(manifest["task_id"], self.task.task_id)
        self.assertEqual(
            manifest["evaluator"], "elt_taskgen.verification.upstream_eval.evaluate"
        )
        self.assertEqual(manifest["composite"], ["stage1_gate", "mart_fraction"])
        # FULL emits ONLY the reward manifest: the public bundle stays the
        # unchanged parent task/ tree.
        full_dir = self.variants_root / TaskVariant.FULL.value
        self.assertEqual(
            sorted(p.name for p in full_dir.iterdir()), [eltbench.REWARD_MANIFEST]
        )

    def test_every_manifest_evaluator_resolves_to_a_real_callable(self):
        """The evaluator is a dotted import path, not decorative prose: a
        rename in upstream_eval must break here, not at grading time. It must
        also resolve INTO upstream_eval — the single reward implementation."""
        import importlib

        for variant in TaskVariant:
            ref = self.reward_json(variant)["evaluator"]
            module_name, _, attr = ref.rpartition(".")
            self.assertEqual(
                module_name,
                "elt_taskgen.verification.upstream_eval",
                msg=f"{variant.value} evaluator escapes THE reward module: {ref}",
            )
            fn = getattr(importlib.import_module(module_name), attr, None)
            self.assertTrue(callable(fn), msg=f"{ref} is not callable")
            self.assertIs(fn, getattr(upstream_eval, attr))

    def test_transform_without_populations_dir_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "populations_dir"):
            eltbench.emit_variant(
                self.task,
                self.gold,
                TaskVariant.TRANSFORM,
                self.workspace / "nowhere",
            )

    def test_stale_gold_fails_closed(self):
        stale = self.gold.model_copy(update={"task_content_hash": "0" * 64})
        with self.assertRaisesRegex(ValueError, "stale"):
            eltbench.emit_variant(
                self.task,
                stale,
                TaskVariant.EXTRACT_LOAD,
                self.workspace / "nowhere",
            )


# ---------------------------------------------------------------------------
# Leak guard extension: DuckDB warehouses in a public tree
# ---------------------------------------------------------------------------

class TestWarehouseLeakGuard(VariantPipelineBase):
    def _fake_public_tree(self, table_sql: str) -> Path:
        public = Path(tempfile.mkdtemp(dir=self.workspace, prefix="leak-"))
        self.addCleanup(shutil.rmtree, public, ignore_errors=True)
        db_path = public / eltbench.WAREHOUSE_DIRNAME / "primary.duckdb"
        db_path.parent.mkdir(parents=True)
        con = duckdb.connect(str(db_path))
        try:
            con.execute(table_sql)
        finally:
            con.close()
        return public

    def test_mart_table_in_public_warehouse_is_a_leak(self):
        public = self._fake_public_tree(
            f'create table "{MART}" (customer_id integer)'
        )
        with self.assertRaisesRegex(ValueError, "leak: mart table"):
            eltbench.assert_public_tree_clean(self.task, public)

    def test_unknown_table_in_public_warehouse_is_a_leak(self):
        public = self._fake_public_tree("create table scratch (x integer)")
        with self.assertRaisesRegex(ValueError, "leak"):
            eltbench.assert_public_tree_clean(self.task, public)

    def test_reward_manifest_name_forbidden_in_public_tree(self):
        public = Path(tempfile.mkdtemp(dir=self.workspace, prefix="leak-"))
        self.addCleanup(shutil.rmtree, public, ignore_errors=True)
        (public / eltbench.REWARD_MANIFEST).write_text("{}")
        with self.assertRaisesRegex(ValueError, "forbidden name"):
            eltbench.assert_public_tree_clean(self.task, public)

    def test_emitted_transform_tree_is_clean(self):
        eltbench.assert_public_tree_clean(
            self.task, self.variants_root / TaskVariant.TRANSFORM.value / "task"
        )

    # -- catalog objects: what a relation-name check cannot see -------------

    def _shipped_copy(self, *statements: str) -> Path:
        """A copy of the shipped primary warehouse, mutated, in a public tree."""
        public = Path(tempfile.mkdtemp(dir=self.workspace, prefix="catalog-"))
        self.addCleanup(shutil.rmtree, public, ignore_errors=True)
        db_path = public / eltbench.WAREHOUSE_DIRNAME / "primary.duckdb"
        db_path.parent.mkdir(parents=True)
        shutil.copy(
            self.variants_root / TaskVariant.TRANSFORM.value / "task"
            / eltbench.WAREHOUSE_DIRNAME / "primary.duckdb",
            db_path,
        )
        con = duckdb.connect(str(db_path))
        try:
            for sql in statements:
                con.execute(sql)
            con.execute("CHECKPOINT")
        finally:
            con.close()
        return public

    def test_macro_view_comment_or_sequence_in_a_public_warehouse_is_a_leak(self):
        """A macro whose body is the reference SQL hands the solver the answer
        while leaving the relation list — the only thing the old guard read —
        completely unchanged."""
        cases = {
            "macro": "CREATE MACRO gold() AS TABLE SELECT 1 AS x",
            "view": "CREATE VIEW peek AS SELECT * FROM customers",
            "comment": "COMMENT ON TABLE customers IS 'the answer'",
            "sequence": "CREATE SEQUENCE s",
            "type": "CREATE TYPE mood AS ENUM ('ok')",
        }
        for name, sql in cases.items():
            with self.subTest(case=name):
                public = self._shipped_copy(sql)
                with self.assertRaisesRegex(ValueError, "leak"):
                    eltbench.assert_public_tree_clean(self.task, public)

    def test_a_comment_carrying_the_reference_sql_is_named_as_such(self):
        """The marker scan runs over the CATALOG text too, so a leak that is
        the answer verbatim is reported as that, not as an anonymous extra
        catalog object."""
        reference_sql = self.task.reference.sql_by_mart[MART].replace("'", "''")
        public = self._shipped_copy(
            f"COMMENT ON TABLE customers IS '{reference_sql}'"
        )
        with self.assertRaisesRegex(ValueError, "reference-sql"):
            eltbench.assert_public_tree_clean(self.task, public)

    def test_a_macro_carrying_the_reference_sql_is_refused(self):
        """DuckDB re-renders a macro body, so the verbatim marker may not
        survive — the group ban is what makes the refusal unconditional, and
        the message carries the definition so a human can see what leaked."""
        reference_sql = self.task.reference.sql_by_mart[MART]
        public = self._shipped_copy(
            f"CREATE MACRO answer() AS TABLE ({reference_sql.rstrip().rstrip(';')})"
        )
        with self.assertRaisesRegex(ValueError, "macros") as caught:
            eltbench.assert_public_tree_clean(self.task, public)
        self.assertIn("completed_order_count", str(caught.exception))

    def test_a_hidden_schema_relation_in_a_public_warehouse_is_a_leak(self):
        public = self._shipped_copy(
            "CREATE SCHEMA h", "CREATE TABLE h.gold AS SELECT 1 AS x"
        )
        with self.assertRaisesRegex(ValueError, "leak"):
            eltbench.assert_public_tree_clean(self.task, public)

    def test_public_warehouse_column_shape_must_match_the_ir(self):
        """A retyped column keeps every row and every name, and BREAKS a
        correct query (`sum(VARCHAR)` binds to nothing)."""
        public = self._shipped_copy(
            "ALTER TABLE order_items ALTER unit_price TYPE VARCHAR"
        )
        with self.assertRaisesRegex(ValueError, "shape"):
            eltbench.assert_public_tree_clean(self.task, public)


# Release tests cover shipped, checksummed, leak-checked variants.
# Shared ledger doubles come from `tests/release_doubles.py`.
try:
    from release_doubles import (
        FakeEngine,
        FakeReport,
        FakeSelection,
        _census_evidence,
    )
except ImportError:  # running as tests.test_variants from the repo root
    from tests.release_doubles import (
        FakeEngine,
        FakeReport,
        FakeSelection,
        _census_evidence,
    )


class TestReleaseVariants(VariantPipelineBase):
    def _freeze(self, out_name: str):
        engine = FakeEngine(
            self.workspace,
            self.task,
            FakeReport("pass", self.task.content_hash()),
        )
        selection = FakeSelection(
            train=(self.task.task_id,),
            variants={
                self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)
            },
        )
        return release.freeze_release(
            engine, selection, self.workspace / "release" / out_name
        )

    def test_release_ships_and_checksums_variants(self):
        manifest = self._freeze("with-variants")
        tid = self.task.task_id
        out = self.workspace / "release" / "with-variants"
        self.assertEqual(
            manifest.variants, {tid: ("extract_load", "transform")}
        )
        self.assertEqual(manifest.schema_version, release.RELEASE_SCHEMA_VERSION)
        self.assertEqual(manifest.corpus_profile, release.COMBINED_CORPUS_PROFILE)
        self.assertEqual(manifest.public_layout, release.COMBINED_PUBLIC_LAYOUT)
        el_id, t_id = f"{tid}__el", f"{tid}__t"
        # The two batteries certify one end-to-end public parent task.
        public_task = out / "public" / tid
        self.assertTrue((public_task / "config.yaml").is_file())
        self.assertTrue((public_task / "data_model.yaml").is_file())
        self.assertFalse((out / "public" / el_id).exists())
        self.assertFalse((out / "public" / t_id).exists())
        self.assertFalse((public_task / "sources").exists())
        self.assertFalse(any(p.suffix == ".duckdb" for p in public_task.rglob("*")))
        warehouse_rel = f"private/{tid}/oracle/primary.duckdb"
        warehouse_path = out / warehouse_rel
        self.assertTrue(warehouse_path.is_file())
        # Warehouses use stable census digests rather than DuckDB file bytes.
        # Every non-warehouse release artifact remains byte-pinned.
        self.assertIn(warehouse_rel, manifest.checksums)
        self.assertEqual(
            manifest.checksum_kinds[warehouse_rel], release.WAREHOUSE_CHECKSUM_KIND
        )
        census = eltbench.warehouse_census(warehouse_path)
        self.assertEqual(manifest.checksums[warehouse_rel], census["census_digest"])
        record = manifest.warehouse_census[warehouse_rel]
        self.assertEqual(record.census_version, eltbench.CENSUS_VERSION)
        self.assertEqual(
            record.row_counts,
            {n: c["row_count"] for n, c in census["tables"].items()},
        )
        self.assertEqual(set(record.row_counts), SOURCE_TABLES)
        # The census digest is NOT the byte hash: pinning by bytes is exactly
        # the defect, so the two values must be distinguishable.
        self.assertNotEqual(
            hashlib.sha256(warehouse_path.read_bytes()).hexdigest(),
            manifest.checksums[warehouse_rel],
        )
        # The exemption is DuckDB-only: everything else is still byte-pinned.
        self.assertEqual(
            sorted(manifest.checksum_kinds),
            sorted(r for r in manifest.checksums if r.endswith(".duckdb")),
        )
        for rel, digest in manifest.checksums.items():
            if rel in manifest.checksum_kinds:
                continue
            self.assertEqual(
                hashlib.sha256((out / rel).read_bytes()).hexdigest(), digest, rel
            )
        # Exactly two private phase-reward records; FULL remains legacy/debug-only.
        for private_id in (el_id, t_id):
            self.assertTrue(
                (out / "private" / private_id / eltbench.REWARD_MANIFEST).is_file(),
                private_id,
            )
        el_reward = json.loads(
            (out / "private" / el_id / eltbench.REWARD_MANIFEST).read_text()
        )
        t_reward = json.loads(
            (out / "private" / t_id / eltbench.REWARD_MANIFEST).read_text()
        )
        self.assertEqual(el_reward["runtime"], "snowflake_warehouse_state")
        self.assertNotIn("load_plan_root", el_reward)
        self.assertNotIn("warehouse", t_reward)
        for pop, rel in t_reward["oracle"].items():
            self.assertTrue((out / "private" / tid / rel).is_file(), pop)
        self.assertFalse(
            (out / "private" / tid / eltbench.REWARD_MANIFEST).exists()
        )
        # The two rewards share the parent's frozen answer key while the parent
        # is the sole public task.
        self.assertTrue(
            (out / "private" / tid / "answer_key" / "manifest.json").is_file()
        )
        self.assertEqual(
            [record.variant for record in manifest.variant_acceptance[tid]],
            ["extract_load", "transform"],
        )
        self.assertTrue(
            all(record.shipped for record in manifest.variant_acceptance[tid])
        )
        # No answer-key content under any public variant tree.
        names = {p.name for p in (out / "public").rglob("*")}
        for forbidden in ("table.json", "sort_key.json", "gt", "gold",
                          eltbench.REWARD_MANIFEST):
            self.assertNotIn(forbidden, names)

    def test_release_refuses_unknown_variant_dir(self):
        bogus = self.variants_root / "bogus"
        bogus.mkdir()
        self.addCleanup(shutil.rmtree, bogus, ignore_errors=True)
        with self.assertRaisesRegex(ValueError, "unknown variant"):
            self._freeze("with-bogus")
        self.assertFalse((self.workspace / "release" / "with-bogus").exists())

    def test_release_refuses_leaked_combined_bundle(self):
        planted = self.task_root / "task" / "notes.txt"
        planted.write_text(demo_fixture.REFERENCE_SQL)
        self.addCleanup(planted.unlink)
        with self.assertRaisesRegex(ValueError, "leak"):
            self._freeze("with-leak")
        self.assertFalse((self.workspace / "release" / "with-leak").exists())

    def test_release_manifest_records_schema_rosters_and_el_sources(self):
        from elt_taskgen.verification import gates as gates_mod

        manifest = self._freeze("with-el-sources")
        out = self.workspace / "release" / "with-el-sources"
        tid = self.task.task_id
        self.assertEqual(manifest.schema_version, release.RELEASE_SCHEMA_VERSION)
        self.assertEqual(
            sorted(manifest.el_sources[tid]), sorted(p.value for p in PopulationName)
        )
        self.assertEqual(manifest.roster_digest, gates_mod.ROSTER_DIGEST)
        for variant in RLVR_TASK_VARIANTS:
            self.assertEqual(
                manifest.gate_rosters[variant.value],
                tuple(variant_gate_names_map[variant]),
            )
        # Every graded EL source root ships, byte-pinned, and its rendered
        # artifacts resolve exactly as the loader resolves them.
        for pop, rel in manifest.el_sources[tid].items():
            root = out / rel
            self.assertTrue(root.is_dir(), pop)
            for table in self.task.tables:
                artifact = find_rendered_artifact(self.task, root, table.name)
                self.assertTrue(artifact.exists())
            for path in root.rglob("*"):
                if path.is_file():
                    key = path.relative_to(out).as_posix()
                    self.assertIn(key, manifest.checksums)
                    self.assertNotIn(key, manifest.checksum_kinds)
        self.assertTrue(release.verify_release(out).ok)

    def test_release_refuses_a_warehouse_drifted_from_the_certified_census(self):
        """The freeze pin must be the CERTIFIED census, not a self-attestation.

        Before this, a warehouse edited AFTER its battery froze with a fresh
        pin and verified against itself — while the gate, the CLI stage and
        release.py all documented the opposite.
        """
        workspace = Path(tempfile.mkdtemp(prefix="elt-cert-census-"))
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        shutil.copytree(self.workspace, workspace, dirs_exist_ok=True)
        shutil.rmtree(workspace / "release", ignore_errors=True)
        victim = (
            workspace / "tasks" / self.task.task_id / "variants"
            / TaskVariant.TRANSFORM.value / "task" / eltbench.WAREHOUSE_DIRNAME
            / "primary.duckdb"
        )
        con = duckdb.connect(str(victim))
        try:
            con.execute(
                "DELETE FROM customers WHERE customer_id = "
                "(SELECT min(customer_id) FROM customers)"
            )
            con.execute("CHECKPOINT")
        finally:
            con.close()
        engine = FakeEngine(
            workspace, self.task, FakeReport("pass", self.task.content_hash())
        )
        selection = FakeSelection(
            train=(self.task.task_id,),
            variants={self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)},
        )
        with self.assertRaisesRegex(ValueError, "certified census"):
            release.freeze_release(engine, selection, workspace / "release" / "drift")
        self.assertFalse((workspace / "release" / "drift").exists())

    def test_release_refuses_a_certificate_not_bound_to_the_ledger(self):
        """Rewriting the evidence FILE to match a tampered warehouse is not a
        certification: the ledger battery evidence is the second leg."""
        workspace = Path(tempfile.mkdtemp(prefix="elt-cert-ledger-"))
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        shutil.copytree(self.workspace, workspace, dirs_exist_ok=True)
        shutil.rmtree(workspace / "release", ignore_errors=True)
        # The ledger double is built from the ORIGINAL record...
        engine = FakeEngine(
            workspace, self.task, FakeReport("pass", self.task.content_hash())
        )
        # ... and only then is the certificate rewritten.
        path = (
            workspace / "tasks" / self.task.task_id
            / eltbench.WAREHOUSE_CENSUS_EVIDENCE_REL
        )
        record = json.loads(path.read_text())
        record["populations"]["primary"]["census_digest"] = "f" * 64
        path.write_text(json.dumps(record, sort_keys=True))
        selection = FakeSelection(
            train=(self.task.task_id,),
            variants={self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)},
        )
        with self.assertRaisesRegex(ValueError, "ledger"):
            release.freeze_release(engine, selection, workspace / "release" / "unbound")

    def test_release_refuses_a_shipped_warehouse_without_a_certificate(self):
        workspace = Path(tempfile.mkdtemp(prefix="elt-cert-missing-"))
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        shutil.copytree(self.workspace, workspace, dirs_exist_ok=True)
        shutil.rmtree(workspace / "release", ignore_errors=True)
        engine = FakeEngine(
            workspace, self.task, FakeReport("pass", self.task.content_hash())
        )
        (
            workspace / "tasks" / self.task.task_id
            / eltbench.WAREHOUSE_CENSUS_EVIDENCE_REL
        ).unlink()
        selection = FakeSelection(
            train=(self.task.task_id,),
            variants={self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)},
        )
        with self.assertRaisesRegex(ValueError, "nobody certified"):
            release.freeze_release(engine, selection, workspace / "release" / "nocert")

    def test_release_refuses_a_t_unit_that_ships_no_warehouse_at_all(self):
        """Deleting EVERY warehouse after the battery must not freeze clean.

        The reconciliation used to run only when at least one .duckdb was
        found, so the one drift that removes all of them — the T unit ships
        with no starting warehouse and nothing objects — walked straight
        through the check written to stop it, and `verify_release` said ok
        because the manifest pinned nothing to disagree with. Missing evidence
        is a failure, never a pass.
        """
        workspace = Path(tempfile.mkdtemp(prefix="elt-cert-nowh-"))
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        shutil.copytree(self.workspace, workspace, dirs_exist_ok=True)
        shutil.rmtree(workspace / "release", ignore_errors=True)
        warehouse_dir = (
            workspace / "tasks" / self.task.task_id / "variants"
            / TaskVariant.TRANSFORM.value / "task" / eltbench.WAREHOUSE_DIRNAME
        )
        shipped = sorted(p.name for p in warehouse_dir.glob("*.duckdb"))
        self.assertTrue(shipped, "fixture must ship warehouses to begin with")
        for path in warehouse_dir.glob("*.duckdb"):
            path.unlink()
        engine = FakeEngine(
            workspace, self.task, FakeReport("pass", self.task.content_hash())
        )
        selection = FakeSelection(
            train=(self.task.task_id,),
            variants={self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)},
        )
        out = workspace / "release" / "nowarehouse"
        with self.assertRaises(ValueError) as caught:
            release.freeze_release(engine, selection, out)
        message = str(caught.exception)
        self.assertIn("ships no warehouse", message)
        self.assertIn("primary", message)
        self.assertFalse(out.exists())

    def test_release_refuses_one_missing_warehouse_of_a_certified_set(self):
        """The partial case: four of five ship, the certificate names five."""
        workspace = Path(tempfile.mkdtemp(prefix="elt-cert-partial-"))
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        shutil.copytree(self.workspace, workspace, dirs_exist_ok=True)
        shutil.rmtree(workspace / "release", ignore_errors=True)
        (
            workspace / "tasks" / self.task.task_id / "variants"
            / TaskVariant.TRANSFORM.value / "task" / eltbench.WAREHOUSE_DIRNAME
            / "stress.duckdb"
        ).unlink()
        engine = FakeEngine(
            workspace, self.task, FakeReport("pass", self.task.content_hash())
        )
        selection = FakeSelection(
            train=(self.task.task_id,),
            variants={self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)},
        )
        with self.assertRaisesRegex(ValueError, "stress"):
            release.freeze_release(
                engine, selection, workspace / "release" / "partial"
            )

    def _freeze_with_el_reward(self, name, mutate):
        """Freeze a private copy of the module workspace after `mutate` has
        rewritten the EL unit's reward manifest."""
        workspace = Path(tempfile.mkdtemp(prefix=f"elt-el-{name}-"))
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        shutil.copytree(self.workspace, workspace, dirs_exist_ok=True)
        shutil.rmtree(workspace / "release", ignore_errors=True)
        reward_path = (
            workspace / "tasks" / self.task.task_id / "variants"
            / TaskVariant.EXTRACT_LOAD.value / eltbench.REWARD_MANIFEST
        )
        payload = json.loads(reward_path.read_text())
        mutate(payload)
        reward_path.write_text(json.dumps(payload, sort_keys=True))
        engine = FakeEngine(
            workspace, self.task, FakeReport("pass", self.task.content_hash())
        )
        selection = FakeSelection(
            train=(self.task.task_id,),
            variants={self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)},
        )
        release.freeze_release(engine, selection, workspace / "release" / name)

    def test_release_refuses_an_el_reward_naming_no_population(self):
        """A reward manifest with no `expected` is an unscorable EL unit.

        It shipped zero graded source roots and `_verify_serving_surface`
        only checks the keys a manifest actually carries, so the release
        froze and verified while nothing could ever grade it. The frozen gold
        in the answer key is the certificate that says which populations must
        be gradable.
        """

        def strip_all(payload):
            payload.pop("expected", None)
            payload.pop("sources", None)

        with self.assertRaisesRegex(ValueError, "frozen gold for population"):
            self._freeze_with_el_reward("unscorable", strip_all)

    def test_release_refuses_an_el_reward_short_of_the_frozen_gold(self):
        """Naming SOME populations is not enough: every population the answer
        key can grade stage 1 on must ship a source root."""

        def drop_one(payload):
            payload["expected"].pop("stress", None)
            (payload.get("sources") or {}).pop("stress", None)

        with self.assertRaisesRegex(ValueError, "stress"):
            self._freeze_with_el_reward("short", drop_one)

    def test_release_accepts_legacy_prefix_only_ledger_evidence(self):
        """Every existing ledger carries only the 16-hex `census:<pop>` prefix;
        the full `census_digest:<pop>` key is new. Both must bind."""
        census = _census_evidence(self.workspace, self.task)
        legacy = {k: v for k, v in census.items() if k.startswith("census:")}
        engine = FakeEngine(
            self.workspace,
            self.task,
            FakeReport("pass", self.task.content_hash()),
            variants={
                TaskVariant.EXTRACT_LOAD: FakeReport(
                    "pass",
                    self.task.content_hash(),
                    gates=variant_gate_names_map[TaskVariant.EXTRACT_LOAD],
                    task_id=variant_task_id(
                        self.task.task_id, TaskVariant.EXTRACT_LOAD
                    ),
                ),
                TaskVariant.TRANSFORM: FakeReport(
                    "pass",
                    self.task.content_hash(),
                    gates=variant_gate_names_map[TaskVariant.TRANSFORM],
                    task_id=variant_task_id(self.task.task_id, TaskVariant.TRANSFORM),
                    evidence_by_gate={"warehouses-load": legacy},
                ),
            },
        )
        selection = FakeSelection(
            train=(self.task.task_id,),
            variants={self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)},
        )
        manifest = release.freeze_release(
            engine, selection, self.workspace / "release" / "legacy-evidence"
        )
        self.assertTrue(manifest.warehouse_census)

    def test_manifest_census_equals_the_certified_census(self):
        manifest = self._freeze("cert-equal")
        record = json.loads(
            (
                self.workspace / "tasks" / self.task.task_id
                / eltbench.WAREHOUSE_CENSUS_EVIDENCE_REL
            ).read_text()
        )
        for pop in PopulationName:
            rel = f"private/{self.task.task_id}/oracle/{pop.value}.duckdb"
            self.assertEqual(
                manifest.warehouse_census[rel].census_digest,
                record["populations"][pop.value]["census_digest"],
                pop.value,
            )
            self.assertTrue(manifest.warehouse_census[rel].catalog_digest)


# ---------------------------------------------------------------------------
# The T warehouse pin: census, not bytes
# ---------------------------------------------------------------------------

class TestWarehouseChecksumPin(VariantPipelineBase):
    """WHY THIS EXISTS

    A TRANSFORM unit SHIPS one DuckDB warehouse per population, and the release
    manifest used to pin those files by BYTE hash — asserting a reproducibility
    property DuckDB files do not have. `test_duckdb_bytes_are_not_the_warehouse`
    measures it directly: four rebuilds of one warehouse's exact data produce
    four different byte hashes and ONE census digest. Under a byte pin, a
    release re-cut from unchanged inputs would read as "changed", and the only
    way to make that alarm stop is to stop checking — the failure mode the
    warehouse-census gate already exists to avoid.

    So the pin is the census, for `.duckdb` files and nothing else, stated in
    the manifest (`checksum_kinds` + `warehouse_census`). These tests hold that
    exemption to its two obligations: it must still catch a tampered warehouse
    (one changed cell, one deleted row, one smuggled relation), and it must not
    reach back and re-judge the pre-refactor releases, which recorded byte pins
    for their warehouses and must keep verifying under exactly those rules.
    """

    #: Same freeze the release tests use: real bundles, accepted batteries.
    _freeze = TestReleaseVariants._freeze

    def _tmpdir(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix="elt-wh-pin-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def _shipped_warehouse(self, population: str = "primary") -> Path:
        return (
            self.variants_root
            / TaskVariant.TRANSFORM.value
            / "task"
            / eltbench.WAREHOUSE_DIRNAME
            / f"{population}.duckdb"
        )

    @staticmethod
    def _bytes(path: Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_duckdb_bytes_are_not_the_warehouse(self):
        """Same data, four rebuilds: four byte hashes, one census digest.

        The rebuilds are real materializations from the same frozen rendered
        artifacts — same rows, same typed columns, same counts. DuckDB is free
        to choose the physical layout (and its scans are parallel, so the row
        order it writes is not fixed), which is precisely why the bytes are not
        an identity for the DATA and the census is.

        The rebuild DECLARES each table exactly as the IR does and then copies
        the rows in. It is not a `CREATE TABLE ... AS SELECT *` copy, which
        this test used to make: CTAS drops the NOT NULL declarations, and
        census v2 pins each column as (name, type, nullability), so a CTAS copy
        is honestly a DIFFERENT warehouse — one where a correct query can no
        longer rely on the declared shape.
        """
        from elt_taskgen.reference.solution import create_table

        src = self._shipped_warehouse()
        tmp = self._tmpdir()
        shipped_census = eltbench.warehouse_census(src)["census_digest"]
        byte_hashes, censuses = set(), {shipped_census}
        for i in range(4):
            out = tmp / f"rebuild{i}.duckdb"
            con = duckdb.connect(str(out))
            try:
                con.execute(f"ATTACH '{src}' AS s (READ_ONLY)")
                for table in self.task.tables:
                    create_table(con, table)
                    con.execute(
                        f'INSERT INTO "{table.name}" SELECT * FROM s."{table.name}"'
                    )
                con.execute("DETACH s")
                con.execute("CHECKPOINT")
            finally:
                con.close()
            byte_hashes.add(self._bytes(out))
            censuses.add(eltbench.warehouse_census(out)["census_digest"])
        self.assertEqual(
            len(censuses),
            1,
            "census must be invariant across rebuilds of the same data",
        )
        self.assertGreater(
            len(byte_hashes),
            1,
            "DuckDB bytes were stable across rebuilds; if that ever becomes a "
            "guarantee, revisit the exemption — do not weaken this assertion",
        )

    def test_two_clean_builds_agree_on_the_census(self):
        """Two independent materializations of one population, same census."""
        tmp = self._tmpdir()
        rendered = self.populations_dir / "primary" / "rendered"
        digests = set()
        for name in ("build_a", "build_b"):
            path = tmp / name / "primary.duckdb"
            eltbench.materialize_warehouse(
                self.task, self.gold, "primary", rendered, path
            )
            digests.add(eltbench.warehouse_census(path)["census_digest"])
        self.assertEqual(len(digests), 1)
        self.assertEqual(
            digests.pop(),
            eltbench.warehouse_census(self._shipped_warehouse())["census_digest"],
        )

    def test_release_verifies_under_its_own_recorded_rules(self):
        manifest = self._freeze("verify-ok")
        out = self.workspace / "release" / "verify-ok"
        result = release.verify_release(out)
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(result.schema_version, release.RELEASE_SCHEMA_VERSION)
        self.assertEqual(result.corpus_profile, release.COMBINED_CORPUS_PROFILE)
        self.assertEqual(result.files_checked, len(manifest.checksums) + 1)
        self.assertEqual(result.census_pinned, len(manifest.checksum_kinds))
        self.assertEqual(
            result.byte_pinned + result.census_pinned, result.files_checked
        )
        self.assertEqual(result.unpinned_files, ())

    def test_tampered_warehouse_fails_verification(self):
        """One changed cell, one deleted row, one smuggled relation."""
        self._freeze("verify-tampered")
        out = self.workspace / "release" / "verify-tampered"
        rel = f"private/{self.task.task_id}/oracle/primary.duckdb"
        victim = out / rel
        victim.chmod(0o644)

        def mutate(sql: str):
            con = duckdb.connect(str(victim))
            try:
                con.execute(sql)
                con.execute("CHECKPOINT")
            finally:
                con.close()

        # (a) one cell changed; every row count is untouched, so a count-only
        #     witness would miss it and the row-multiset digest must not.
        mutate(
            "UPDATE customers SET customer_name = 'TAMPERED' "
            "WHERE customer_id = (SELECT min(customer_id) FROM customers)"
        )
        result = release.verify_release(out)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("census mismatch" in f and "customers" in f for f in result.failures),
            result.failures,
        )
        # (b) a deleted row and (c) an extra relation are caught too.
        mutate("DELETE FROM orders WHERE order_id = (SELECT min(order_id) FROM orders)")
        self.assertFalse(release.verify_release(out).ok)
        mutate("CREATE TABLE smuggled AS SELECT 1 AS x")
        failures = release.verify_release(out).failures
        self.assertTrue(any("smuggled" in f for f in failures), failures)

    def test_catalog_and_type_tampering_fails_verification(self):
        """(d) a macro, (e) a comment, (f) a retype, (g) NULL -> ''.

        Each of these left the v1 census digest EXACTLY as frozen — a macro
        carrying the gold, a comment carrying the reference SQL, a retyped
        column that breaks a correct query, and a NULL rewritten to '' that
        changes what one returns. Under v2 each one moves the pin.
        """
        rel = f"private/{self.task.task_id}/oracle/primary.duckdb"
        cases = (
            ("macro", "CREATE MACRO gold() AS TABLE SELECT 1 AS x"),
            ("comment", "COMMENT ON TABLE customers IS 'the answer'"),
            ("retype", "ALTER TABLE customers ALTER customer_id TYPE VARCHAR"),
            (
                "null",
                "UPDATE orders SET customer_id = NULL "
                "WHERE order_id = (SELECT min(order_id) FROM orders)",
            ),
        )
        for name, sql in cases:
            with self.subTest(case=name):
                out_name = f"verify-catalog-{name}"
                self._freeze(out_name)
                out = self.workspace / "release" / out_name
                self.assertTrue(release.verify_release(out).ok)
                victim = out / rel
                victim.chmod(0o644)
                con = duckdb.connect(str(victim))
                try:
                    con.execute(sql)
                    con.execute("CHECKPOINT")
                finally:
                    con.close()
                result = release.verify_release(out)
                self.assertFalse(result.ok, name)
                self.assertTrue(any(rel in f for f in result.failures), result.failures)
                if name in ("macro", "comment"):
                    self.assertTrue(
                        any("catalog objects" in f for f in result.failures),
                        result.failures,
                    )

    def test_corrupted_warehouse_is_reported_not_raised(self):
        """A file too damaged to census fails verification, loudly and safely."""
        manifest = self._freeze("verify-corrupt")
        out = self.workspace / "release" / "verify-corrupt"
        rel = next(r for r in manifest.checksums if r.endswith(".duckdb"))
        victim = out / rel
        victim.chmod(0o644)
        victim.write_bytes(b"not a database at all")
        result = release.verify_release(out)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("could not be censused" in f for f in result.failures),
            result.failures,
        )

    def test_legacy_full_release_still_verifies_by_bytes(self):
        """BACKWARD COMPATIBILITY (the four already-released pools).

        Their manifests predate `schema_version`/`corpus_profile`, so they read
        back as "1.0"/"legacy_full" with an empty `checksum_kinds` — i.e. every
        file byte-pinned, warehouses included. That is the rule they were frozen
        under and the rule they are verified under; the census exemption does
        not reach backwards.
        """
        self._freeze("verify-legacy")
        src = self.workspace / "release" / "verify-legacy"
        legacy = self._tmpdir() / "legacy"
        shutil.copytree(src, legacy)
        for path in legacy.rglob("*"):
            path.chmod(0o644 if path.is_file() else 0o755)

        manifest_path = legacy / "release_manifest.json"
        raw = json.loads(manifest_path.read_text())
        for key in ("schema_version", "corpus_profile", "public_layout",
                    "checksum_kinds", "warehouse_census"):
            raw.pop(key, None)
        warehouses = [r for r in raw["checksums"] if r.endswith(".duckdb")]
        self.assertTrue(warehouses)
        for rel in warehouses:  # re-pin them the way the old freezer did
            raw["checksums"][rel] = self._bytes(legacy / rel)
        manifest_path.write_text(json.dumps(raw, sort_keys=True) + "\n")
        lines = [f"{d}  {r}" for r, d in raw["checksums"].items()]
        lines.append(f"{self._bytes(manifest_path)}  release_manifest.json")
        (legacy / "checksums.sha256").write_text("\n".join(sorted(lines)) + "\n")

        result = release.verify_release(legacy)
        self.assertEqual(result.schema_version, "1.0")
        self.assertEqual(result.corpus_profile, "legacy_full")
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(result.census_pinned, 0)
        self.assertEqual(result.byte_pinned, result.files_checked)
        # ... and a legacy release IS still byte-sensitive on its warehouses.
        victim = legacy / warehouses[0]
        victim.write_bytes(victim.read_bytes() + b"\0")
        self.assertFalse(release.verify_release(legacy).ok)

    def test_el_t_manifest_may_not_byte_pin_a_warehouse(self):
        """The false claim must not creep back in under the new profile."""
        manifest = self._freeze("verify-bytepin")
        out = self.workspace / "release" / "verify-bytepin"
        rel = next(r for r in manifest.checksums if r.endswith(".duckdb"))
        raw = json.loads((out / "release_manifest.json").read_text())
        raw["checksum_kinds"].pop(rel)
        raw["warehouse_census"].pop(rel)
        raw["checksums"][rel] = self._bytes(out / rel)
        manifest_path = out / "release_manifest.json"
        manifest_path.chmod(0o644)
        manifest_path.write_text(json.dumps(raw, sort_keys=True) + "\n")
        lines = [
            f"{d}  {r}"
            for r, d in raw["checksums"].items()
            if r not in raw["checksum_kinds"]
        ]
        lines.append(f"{self._bytes(manifest_path)}  release_manifest.json")
        flat = out / "checksums.sha256"
        flat.chmod(0o644)
        flat.write_text("\n".join(sorted(lines)) + "\n")

        result = release.verify_release(out)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("must carry" in f and "census" in f for f in result.failures),
            result.failures,
        )

    def test_census_record_must_agree_with_its_own_rollup(self):
        """A manifest edited in one place but not the other is tampering."""
        manifest = self._freeze("verify-forged-census")
        out = self.workspace / "release" / "verify-forged-census"
        rel = next(r for r in manifest.checksums if r.endswith(".duckdb"))
        raw = json.loads((out / "release_manifest.json").read_text())
        record = raw["warehouse_census"][rel]
        table = sorted(record["row_counts"])[0]
        record["row_counts"][table] += 1  # detail no longer implies the digest
        manifest_path = out / "release_manifest.json"
        manifest_path.chmod(0o644)
        manifest_path.write_text(json.dumps(raw, sort_keys=True) + "\n")
        flat = out / "checksums.sha256"
        flat.chmod(0o644)
        text = flat.read_text().splitlines()
        flat.write_text(
            "\n".join(
                line
                if not line.endswith("  release_manifest.json")
                else f"{self._bytes(manifest_path)}  release_manifest.json"
                for line in text
            )
            + "\n"
        )
        result = release.verify_release(out)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("roll-up" in f for f in result.failures), result.failures
        )


# ---------------------------------------------------------------------------
# Scoring a SHIPPED release unit (export/serve.py)
# ---------------------------------------------------------------------------

class TestScoreReleaseUnit(VariantPipelineBase):
    """Schema-3 releases must never fall back to the DuckDB unit scorer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from elt_taskgen.export import serve as serve_mod

        cls.serve = serve_mod
        cls.out = cls.workspace / "release" / "for-scoring"
        if not cls.out.exists():
            engine = FakeEngine(
                cls.workspace, cls.task, FakeReport("pass", cls.task.content_hash())
            )
            selection = FakeSelection(
                train=(cls.task.task_id,),
                variants={
                    cls.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)
                },
            )
            release.freeze_release(engine, selection, cls.out)

    def test_combined_release_refuses_legacy_duckdb_unit_scorer(self):
        for variant in RLVR_TASK_VARIANTS:
            with self.subTest(variant=variant.value):
                with self.assertRaisesRegex(
                    ValueError, "legacy DuckDB scorer cannot score"
                ):
                    self.serve.load_release_unit(
                        self.out, variant_task_id(self.task.task_id, variant)
                    )

    def _semantic_package(self):
        from elt_taskgen.semantic import load_semantic_package

        return load_semantic_package(self.out, self.task.task_id)

    def _semantic_submission(self, package, *, plan=None, sql=None):
        from elt_taskgen.reference.solution import build_reference
        from elt_taskgen.semantic import SEMANTIC_SUBMISSION_SCHEMA_VERSION

        formats = {
            "postgres": "postgres_sql",
            "mongodb": "jsonl",
            "rest": "rest_pages",
            "s3": "s3_jsonl",
            "files": "csv",
        }
        root = package.source_root(P.PRIMARY)
        if plan is None:
            plan = {}
            for table in self.task.tables:
                artifact = find_rendered_artifact(self.task, root, table.name)
                backend = self.task.backend_for(table.name).backend.value
                plan[table.name] = {
                    "path": artifact.relative_to(root).as_posix(),
                    "format": formats[backend],
                }
        if sql is None:
            sql = build_reference(self.task).sql_by_mart
        return json.dumps(
            {
                "schema_version": SEMANTIC_SUBMISSION_SCHEMA_VERSION,
                "task_id": self.task.task_id,
                "load_plan": plan,
                "sql_by_mart": sql,
            }
        )

    def test_release_ships_only_a_private_hash_bound_semantic_task_ir(self):
        from elt_taskgen.models import task_from_json

        rel = release.SEMANTIC_TASK_IR_REL
        private = self.out / "private" / self.task.task_id / rel
        public = self.out / "public" / self.task.task_id / rel
        self.assertTrue(private.is_file())
        self.assertFalse(public.exists())
        restored = task_from_json(private.read_text(encoding="utf-8"))
        self.assertEqual(restored.task_id, self.task.task_id)
        self.assertEqual(restored.content_hash(), self.task.content_hash())
        manifest = json.loads((self.out / "release_manifest.json").read_text())
        key = private.relative_to(self.out).as_posix()
        self.assertIn(key, manifest["checksums"])

    def test_reference_combined_submission_scores_one_on_all_hidden_populations(self):
        from elt_taskgen.semantic import score_semantic_text
        from elt_taskgen.verification.gates import GRADED_POPULATIONS

        package = self._semantic_package()
        result = score_semantic_text(
            package, self._semantic_submission(package)
        )
        self.assertTrue(result.valid_submission)
        self.assertEqual(result.semantic_el_reward, 1.0)
        self.assertEqual(result.semantic_t_reward, 1.0)
        self.assertEqual(result.reward, 1.0)
        self.assertEqual(
            set(result.populations), {population.value for population in GRADED_POPULATIONS}
        )
        self.assertNotIn(P.DEVELOPMENT.value, result.populations)

    def test_explicit_development_run_is_diagnostic_and_never_aggregated(self):
        from elt_taskgen.semantic import score_semantic_text

        package = self._semantic_package()
        result = score_semantic_text(
            package,
            self._semantic_submission(package),
            populations=(P.DEVELOPMENT,),
        )
        diagnostic = result.populations[P.DEVELOPMENT.value]
        self.assertFalse(diagnostic.graded)
        self.assertEqual(diagnostic.reward, 1.0)
        self.assertEqual(result.graded_populations, ())
        self.assertEqual(result.reward, 0.0)

    def test_el_failure_preserves_the_independent_t_signal_but_gates_reward(self):
        from elt_taskgen.semantic import score_semantic_text

        package = self._semantic_package()
        payload = json.loads(self._semantic_submission(package))
        payload["load_plan"]["customers"]["path"] = "postgres/missing.sql"
        result = score_semantic_text(
            package, json.dumps(payload), populations=(P.PRIMARY,)
        )
        self.assertEqual(result.semantic_el_reward, 0.0)
        self.assertEqual(result.semantic_t_reward, 1.0)
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(
            result.populations[P.PRIMARY.value].el_error_code,
            "stage1_execution_error",
        )

    def test_t_failure_preserves_the_independent_el_signal(self):
        from elt_taskgen.semantic import score_semantic_text

        package = self._semantic_package()
        payload = json.loads(self._semantic_submission(package))
        payload["sql_by_mart"][MART] = (
            "SELECT customer_id, 0 AS completed_order_count, "
            "0 AS total_spend FROM customers WHERE 1=0"
        )
        result = score_semantic_text(
            package, json.dumps(payload), populations=(P.PRIMARY,)
        )
        self.assertEqual(result.semantic_el_reward, 1.0)
        self.assertEqual(result.semantic_t_reward, 0.0)
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(result.populations[P.PRIMARY.value].mart_scores, {MART: False})

    def test_malformed_and_duplicate_key_submissions_are_structured_zeros(self):
        from elt_taskgen.semantic import score_semantic_text

        package = self._semantic_package()
        for text in (
            "not json",
            '{"schema_version":"1.0","schema_version":"1.0"}',
            json.dumps(
                {
                    "schema_version": "999",
                    "task_id": self.task.task_id,
                    "load_plan": {},
                    "sql_by_mart": {},
                }
            ),
        ):
            with self.subTest(text=text):
                result = score_semantic_text(package, text)
                self.assertFalse(result.valid_submission)
                self.assertEqual(result.reward, 0.0)
                self.assertEqual(result.error_code, "invalid_submission")

    def test_multiple_sql_statements_are_rejected_without_state_mutation(self):
        from elt_taskgen.semantic import score_semantic_text

        package = self._semantic_package()
        payload = json.loads(self._semantic_submission(package))
        payload["sql_by_mart"][MART] = (
            "SELECT customer_id, 0 AS completed_order_count, 0 AS total_spend "
            "FROM customers; DROP TABLE customers"
        )
        result = score_semantic_text(
            package, json.dumps(payload), populations=(P.PRIMARY,)
        )
        score = result.populations[P.PRIMARY.value]
        self.assertEqual(score.el_reward, 1.0)
        self.assertEqual(score.t_reward, 0.0)
        self.assertEqual(score.t_error_codes, {MART: "invalid_query"})

    def test_private_paths_and_filesystem_reads_are_blocked_without_error_leaks(self):
        from elt_taskgen.semantic import score_semantic_text

        package = self._semantic_package()
        payload = json.loads(self._semantic_submission(package))
        private_gold = (
            self.out / "private" / self.task.task_id / "answer_key" / "manifest.json"
        )
        payload["load_plan"]["customers"]["path"] = private_gold.as_posix()
        payload["sql_by_mart"][MART] = (
            "SELECT * FROM read_csv_auto('**/answer_key/gold/**/*.csv')"
        )
        result = score_semantic_text(
            package, json.dumps(payload), populations=(P.PRIMARY,)
        )
        score = result.populations[P.PRIMARY.value]
        self.assertEqual(score.el_error_code, "stage1_execution_error")
        self.assertEqual(score.t_error_codes, {MART: "query_failed"})
        public_result = json.dumps(result.model_dump(mode="json"))
        self.assertNotIn("answer_key", public_result)
        self.assertNotIn(self.out.as_posix(), public_result)

    def test_semantic_package_rejects_nested_source_symlinks(self):
        from elt_taskgen.semantic import SemanticPackageError, load_semantic_package

        scratch = Path(tempfile.mkdtemp(prefix="semantic-symlink-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        copied = scratch / "release"
        shutil.copytree(self.out, copied)
        source = (
            copied
            / "private"
            / self.task.task_id
            / "populations"
            / P.PRIMARY.value
            / "rendered"
            / "files"
            / "order_items.csv"
        )
        target = source.with_name("safe.csv")
        target.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(target.name)
        with self.assertRaisesRegex(SemanticPackageError, "symlink"):
            load_semantic_package(copied, self.task.task_id, verify=False)

    def test_nonterminating_query_is_killed_and_returns_a_stable_zero(self):
        from elt_taskgen.semantic import SemanticLimits, score_semantic_text

        package = self._semantic_package()
        payload = json.loads(self._semantic_submission(package))
        payload["sql_by_mart"][MART] = (
            "WITH RECURSIVE loop(x) AS ("
            "SELECT 1 UNION ALL SELECT x + 1 FROM loop"
            ") SELECT max(x) AS customer_id, "
            "max(x) AS completed_order_count, max(x) AS total_spend FROM loop"
        )
        result = score_semantic_text(
            package,
            json.dumps(payload),
            populations=(P.PRIMARY,),
            limits=SemanticLimits(timeout_seconds=1.0, memory_limit_mb=128),
        )
        self.assertTrue(result.valid_submission)
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(result.error_code, "execution_timeout")

    def test_giant_cell_transform_is_a_streamed_output_limit_zero(self):
        from elt_taskgen.semantic import SemanticLimits, score_semantic_text

        package = self._semantic_package()
        payload = json.loads(self._semantic_submission(package))
        # execute_mart checks output column NAMES only, so huge VARCHAR cells
        # can masquerade as total_spend; the streamed byte budget must stop
        # the fetch instead of materializing every giant row first.  200 KB
        # cells dwarf the 64 KiB budget while one engine result chunk
        # (~1k customers) stays far inside the default worker envelope.
        payload["sql_by_mart"][MART] = (
            "SELECT customer_id, 0 AS completed_order_count, "
            "repeat('x', 200000) AS total_spend FROM customers"
        )
        timeout_seconds = 30.0
        start = time.monotonic()
        result = score_semantic_text(
            package,
            json.dumps(payload),
            populations=(P.PRIMARY,),
            limits=SemanticLimits(
                max_result_bytes_per_mart=65536,
                timeout_seconds=timeout_seconds,
            ),
        )
        elapsed = time.monotonic() - start
        self.assertTrue(result.valid_submission)
        score = result.populations[P.PRIMARY.value]
        self.assertEqual(score.t_error_codes, {MART: "output_limit"})
        self.assertEqual(score.el_reward, 1.0)
        self.assertEqual(result.reward, 0.0)
        # An early streamed abort, never a full materialization then timeout.
        self.assertLess(elapsed, timeout_seconds * 0.8)

    def test_adversarial_output_keeps_child_rss_bounded(self):
        import resource

        from elt_taskgen.semantic import SemanticLimits, score_semantic_text

        package = self._semantic_package()
        payload = json.loads(self._semantic_submission(package))
        # A cross join targeting ~2 GB of nominal output (1k customers x 1k
        # range rows x 2 KB cells): the streamed 64 KiB byte budget rejects
        # it a few rows into the fetch, and the reaped spawn worker's peak
        # RSS must stay inside the default whole-worker envelope.
        payload["sql_by_mart"][MART] = (
            "SELECT c.customer_id AS customer_id, "
            "r.range AS completed_order_count, "
            "repeat('x', 2000) AS total_spend "
            "FROM customers AS c CROSS JOIN range(1000) AS r"
        )
        # ru_maxrss is bytes on darwin, KiB on linux.
        scale = 1 if sys.platform == "darwin" else 1024
        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale
        result = score_semantic_text(
            package,
            json.dumps(payload),
            populations=(P.PRIMARY,),
            limits=SemanticLimits(
                max_result_bytes_per_mart=65536, timeout_seconds=60.0
            ),
        )
        after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale
        budget = SemanticLimits().worker_rss_limit_mb * 1024 * 1024
        self.assertLessEqual(after - before, budget)
        score = result.populations[P.PRIMARY.value]
        self.assertEqual(score.t_error_codes.get(MART), "output_limit")

    def test_worker_rss_watchdog_or_rlimit_yields_stable_memory_limit_zero(self):
        from elt_taskgen.semantic import SemanticLimits, score_semantic_text

        package = self._semantic_package()
        payload = json.loads(self._semantic_submission(package))
        # Stream about 2 GiB past generous row/byte caps so only the 256 MiB
        # worker envelope, via rlimit or RSS watchdog, can stop it.
        payload["sql_by_mart"][MART] = (
            "SELECT range AS customer_id, 0 AS completed_order_count, "
            "repeat('x', 1000) AS total_spend FROM range(2000000)"
        )
        timeout_seconds = 60.0
        start = time.monotonic()
        result = score_semantic_text(
            package,
            json.dumps(payload),
            populations=(P.PRIMARY,),
            limits=SemanticLimits(
                memory_limit_mb=1024,
                worker_rss_limit_mb=256,
                timeout_seconds=timeout_seconds,
                max_result_rows_per_mart=10_000_000,
                max_result_bytes_per_mart=1024 * 1024 * 1024,
            ),
        )
        elapsed = time.monotonic() - start
        self.assertTrue(result.valid_submission)
        self.assertEqual(result.reward, 0.0)
        # A kill, never a timeout.
        self.assertLess(elapsed, timeout_seconds * 0.5)
        if result.error_code:
            self.assertEqual(result.error_code, "memory_limit")
        else:
            self.assertEqual(
                result.populations[P.PRIMARY.value].t_error_codes.get(MART),
                "memory_limit",
            )

    def test_semantic_cli_emits_indented_machine_json(self):
        package = self._semantic_package()
        attempt = self.out.parent / "semantic-attempt.json"
        attempt.write_text(self._semantic_submission(package), encoding="utf-8")
        self.addCleanup(attempt.unlink, missing_ok=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(
                [
                    "semantic",
                    "score",
                    "--release", str(self.out),
                    "--task-id", self.task.task_id,
                    "--submission", str(attempt),
                    "--population", P.PRIMARY.value,
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertTrue(text.startswith("{\n  \"aggregation\""), text[:80])
        payload = json.loads(text)
        self.assertEqual(payload["reward"], 1.0)
        self.assertEqual(payload["semantic_el_reward"], 1.0)
        self.assertEqual(payload["semantic_t_reward"], 1.0)

    # -- IR-002: strict typed diagnostic over a real verified package --------

    @staticmethod
    def _strip_strict_fields(result) -> dict:
        payload = result.model_dump(mode="json")
        payload.pop("strict_diagnostic_ran")
        payload.pop("strict_diagnostic_version")
        for score in payload["populations"].values():
            score.pop("strict_marts")
            score.pop("strict_shadow_marts")
        return payload

    def _tolerance_abusing_submission(self, package) -> str:
        payload = json.loads(self._semantic_submission(package))
        payload["sql_by_mart"][MART] = (
            "SELECT customer_id, completed_order_count, "
            "total_spend * 1.005 AS total_spend FROM ("
            + build_reference(self.task).sql_by_mart[MART]
            + ") AS ref"
        )
        return json.dumps(payload)

    def test_strict_diagnostic_is_additive_to_the_frozen_reward(self):
        from elt_taskgen.semantic import score_semantic_text

        package = self._semantic_package()
        text = self._semantic_submission(package)
        baseline = score_semantic_text(package, text, populations=(P.PRIMARY,))
        strict = score_semantic_text(
            package, text, populations=(P.PRIMARY,), strict_diagnostic=True
        )
        # Every legacy field is identical, field for field: the reward is frozen.
        self.assertEqual(
            self._strip_strict_fields(baseline), self._strip_strict_fields(strict)
        )
        self.assertTrue(strict.strict_diagnostic_ran)
        self.assertEqual(strict.strict_diagnostic_version, "1.0")
        score = strict.populations[P.PRIMARY.value]
        self.assertEqual(set(score.strict_marts), {MART})
        diagnostic = score.strict_marts[MART]
        self.assertTrue(diagnostic.strict_match)
        self.assertEqual(diagnostic.mismatch_code, "")
        self.assertTrue(diagnostic.submission_columns)
        self.assertEqual(diagnostic.submission_columns, diagnostic.reference_columns)

    def test_strict_off_run_reports_defaults_and_new_schema_version(self):
        from elt_taskgen.semantic import score_semantic_text

        package = self._semantic_package()
        result = score_semantic_text(
            package, self._semantic_submission(package), populations=(P.PRIMARY,)
        )
        self.assertEqual(result.schema_version, "1.1")
        self.assertFalse(result.strict_diagnostic_ran)
        self.assertEqual(result.strict_diagnostic_version, "")
        score = result.populations[P.PRIMARY.value]
        self.assertEqual(score.strict_marts, {})
        self.assertEqual(score.strict_shadow_marts, {})

    def test_strict_diagnostic_flags_tolerance_abuse_the_reward_forgives(self):
        from elt_taskgen.semantic import score_semantic_text

        package = self._semantic_package()
        result = score_semantic_text(
            package,
            self._tolerance_abusing_submission(package),
            populations=(P.PRIMARY,),
            strict_diagnostic=True,
        )
        score = result.populations[P.PRIMARY.value]
        # The legacy comparator forgives the 0.5% inflation (1% tolerance) ...
        self.assertEqual(score.mart_scores, {MART: True})
        self.assertEqual(result.reward, 1.0)
        # ... while the strict diagnostic names the drifted column.
        diagnostic = score.strict_marts[MART]
        self.assertFalse(diagnostic.strict_match)
        self.assertEqual(diagnostic.mismatch_code, "values:total_spend")

    def test_strict_shadow_reports_the_typed_warehouse_the_legacy_load_widens(self):
        from elt_taskgen.semantic import score_semantic_text

        package = self._semantic_package()
        result = score_semantic_text(
            package,
            self._semantic_submission(package),
            populations=(P.PRIMARY,),
            strict_diagnostic=True,
            strict_shadow=True,
        )
        self.assertTrue(result.strict_diagnostic_ran)
        self.assertEqual(result.reward, 1.0)
        score = result.populations[P.PRIMARY.value]
        shadow = score.strict_shadow_marts[MART]
        self.assertTrue(shadow.strict_match)
        shadow_types = {f.name: f.declared_type for f in shadow.reference_columns}
        self.assertEqual(shadow_types["total_spend"], "DECIMAL(38,9)")
        tier1_types = {
            f.name: f.declared_type
            for f in score.strict_marts[MART].reference_columns
        }
        self.assertEqual(tier1_types["total_spend"], "DOUBLE")

    def test_semantic_cli_strict_flag_prints_diagnostics_and_exits_zero(self):
        package = self._semantic_package()
        attempt = self.out.parent / "semantic-strict-attempt.json"
        attempt.write_text(
            self._tolerance_abusing_submission(package), encoding="utf-8"
        )
        self.addCleanup(attempt.unlink, missing_ok=True)
        argv = [
            "semantic",
            "score",
            "--release", str(self.out),
            "--task-id", self.task.task_id,
            "--submission", str(attempt),
            "--population", P.PRIMARY.value,
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(argv + ["--strict-diagnostic"])
        text = output.getvalue()
        # A strict mismatch is a diagnostic, never an exit-code change.
        self.assertEqual(code, 0)
        self.assertIn(
            f"strict {P.PRIMARY.value}/{MART}: MISMATCH(values:total_spend)", text
        )
        self.assertIn(f"strict types {MART}: ", text)
        without_flag = io.StringIO()
        with contextlib.redirect_stdout(without_flag):
            code = cli.main(argv)
        self.assertEqual(code, 0)
        self.assertNotIn("strict", without_flag.getvalue())


# ---------------------------------------------------------------------------
# CLI: elt-taskgen export --variants full,el,t
# ---------------------------------------------------------------------------

class TestExportCli(VariantPipelineBase):
    def _record_variant_batteries(self, *variants):
        """Record a PASSING, roster-complete battery per variant.

        `export` no longer emits a subtask bundle nothing certified (rule R3:
        a bundle under variants/ with no passing battery makes freeze_release
        raise, so the CLI must not be able to create that state). The batteries
        themselves are gates.py's; here they only need to exist in the ledger.
        """
        from elt_taskgen.models import AcceptanceReport, GateResult
        from elt_taskgen.verification.gates import ROSTER_DIGEST, SCORER_VERSION

        engine = Engine(self.workspace)
        try:
            for variant in variants:
                report = AcceptanceReport.from_gates(
                    task_id=variant_task_id(self.task.task_id, variant),
                    revision=self.task.current_revision,
                    task_content_hash=self.task.content_hash(),
                    gates=[
                        GateResult(gate=name, passed=True)
                        for name in variant_gate_names_map[variant]
                    ],
                    scorer_version=SCORER_VERSION,
                    # A battery is evidence about ONE roster: without the
                    # stamp the record reads as unknown provenance, i.e.
                    # stale, and export refuses it with "re-run".
                    roster_digest=ROSTER_DIGEST,
                    roster=variant_gate_names_map[variant],
                )
                engine.record_report(
                    self.task, variant_gate_stage(variant).value, "pass", report
                )
        finally:
            engine.close()

    def test_export_refuses_a_variant_with_no_battery(self):
        # Own workspace: this class shares one, and the sibling test records
        # passing batteries into its ledger.
        bare = Path(tempfile.mkdtemp(prefix="elt-noBattery-"))
        self.addCleanup(shutil.rmtree, bare, ignore_errors=True)
        engine = Engine(bare)
        try:
            engine.register(self.task)
        finally:
            engine.close()
        code = cli.main(
            [
                "export",
                "--workspace",
                str(bare),
                "--task-id",
                self.task.task_id,
                "--variants",
                "el",
            ]
        )
        self.assertEqual(code, 2)

    def test_explicit_legacy_full_export_remains_available(self):
        self._record_variant_batteries(
            TaskVariant.EXTRACT_LOAD, TaskVariant.TRANSFORM
        )
        code = cli.main(
            [
                "export",
                "--workspace",
                str(self.workspace),
                "--task-id",
                self.task.task_id,
                "--variants",
                "full,el,t",
            ]
        )
        self.assertEqual(code, 0)
        for variant in TaskVariant:
            self.assertTrue(
                (
                    self.variants_root / variant.value / eltbench.REWARD_MANIFEST
                ).is_file(),
                variant.value,
            )
        # Round trip: the emitted artifacts still verify against frozen gold.
        gold = load_gold(self.answer_key_dir)
        self.assertEqual(gold.stage1, self.gold.stage1)

    def test_default_export_retargets_the_combined_parent_destination(self):
        self._record_variant_batteries(
            TaskVariant.EXTRACT_LOAD,
            TaskVariant.TRANSFORM,
        )
        code = cli.main(
            [
                "export",
                "--workspace",
                str(self.workspace),
                "--task-id",
                self.task.task_id,
                "--destination",
                "databricks",
            ]
        )
        self.assertEqual(code, 0)
        config = yaml.safe_load(
            (self.task_root / "task" / "config.yaml").read_text(encoding="utf-8")
        )
        self.assertIn("databricks", config)
        self.assertNotIn("snowflake", config)
        self.assertEqual(
            config["Airbyte"]["config"]["databricks_definition_id"],
            "072d5540-f236-4294-ba7c-ade8fd918496",
        )

    def test_export_rejects_unknown_variant_token(self):
        code = cli.main(
            [
                "export",
                "--workspace",
                str(self.workspace),
                "--task-id",
                self.task.task_id,
                "--variants",
                "full,bogus",
            ]
        )
        self.assertEqual(code, 2)

    def test_variant_token_parsing(self):
        self.assertEqual(
            cli._parse_variants("full,el,t"),
            (TaskVariant.FULL, TaskVariant.EXTRACT_LOAD, TaskVariant.TRANSFORM),
        )
        self.assertEqual(
            cli._parse_variants("extract_load, transform"),
            (TaskVariant.EXTRACT_LOAD, TaskVariant.TRANSFORM),
        )
        self.assertEqual(cli._parse_variants("el,el"), (TaskVariant.EXTRACT_LOAD,))
        with self.assertRaises(ValueError):
            cli._parse_variants("")
        with self.assertRaises(ValueError):
            cli._parse_variants("full,unknown")


class TestProcessRssHelper(unittest.TestCase):
    """OS RSS sampling used by the semantic scorer's parent watchdog."""

    def test_process_rss_helper_reads_own_process(self):
        from elt_taskgen.semantic import scoring as scoring_mod

        rss = scoring_mod._process_rss_bytes(os.getpid())
        self.assertIsNotNone(rss)
        self.assertGreater(rss, 10 * 1024 * 1024)
        # Unmeasurable pids degrade to None; the watchdog must never raise.
        self.assertIsNone(scoring_mod._process_rss_bytes(-1))


if __name__ == "__main__":
    unittest.main()

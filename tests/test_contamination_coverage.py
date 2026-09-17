"""Contamination COVERAGE: an unarmed index must not look like a firewall.

WHY THIS EXISTS
Measured on all six documented runs (docs/runs/README.md, demo.md §8.5): none
of those workspaces ran `measure-target`, so the index every green
`contamination_pre` / `contamination_post` / `contamination-clean` line was
measured against held ~120 fingerprints, ALL of them `family:` names and ZERO
of them structural — against 1232 when the pinned anchors are loaded. The
`is_armed()` predicate could not see the difference, because the pipeline
seeds the embedded deny lists itself and thereby satisfies it.

These tests pin the distinction: the grade, the per-kind census, the loud
summary, the refusal knob, and the fact that our OWN admitted tasks never
upgrade the grade.

SECOND DEFECT (C2): the workspaces that DID run `measure-target` held 1232
fingerprints, all TYPED (`schema:`/`schema-table:` hash name:type) against
anchors whose every column is TEXT — inert against any real-typed candidate,
yet graded ARMED. ARMED now requires the type-blind `shape:` namespace; a
typed-only store grades NAME_ONLY with a "re-run measure-target" remedy, and
re-arming upgrades the store in place (add_benchmark merges).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from elt_taskgen import demo_fixture
from elt_taskgen.verification import contamination as cont
from elt_taskgen.verification.contamination import (
    ContaminationIndex,
    CoverageLevel,
    coverage_failure,
    required_coverage_from_env,
)


def _seed_embedded(idx: ContaminationIndex) -> None:
    """Exactly what cli._seed_embedded_deny_lists writes."""
    for name, families in (
        ("eltbench", cont.ELTBENCH_FAMILIES),
        ("spider2_dbt", cont.SPIDER2_DBT_FAMILIES),
        ("ade_bench", cont.ADE_BENCH_FAMILIES),
    ):
        idx.add_benchmark(
            name, sorted({f"family:{cont.normalize_name(f)}" for f in families})
        )


class CoverageGradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.idx = ContaminationIndex(self.tmp / "contamination")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_index_is_unarmed(self):
        cov = self.idx.coverage()
        self.assertIs(cov.level, CoverageLevel.UNARMED)
        self.assertFalse(cov.is_firewall)
        self.assertIn("UNARMED", cov.summary())

    def test_seeding_the_deny_lists_does_not_arm_a_firewall(self):
        """THE defect: is_armed() goes true, coverage stays name-only."""
        _seed_embedded(self.idx)
        self.assertTrue(self.idx.is_armed())
        cov = self.idx.coverage()
        self.assertIs(cov.level, CoverageLevel.NAME_ONLY)
        self.assertFalse(cov.is_firewall)
        self.assertEqual(cov.structural_fingerprints, 0)
        self.assertEqual(sorted(cov.benchmark_by_kind), ["family"])
        # The deny lists total 121 entries and may only grow. This test primarily
        # pins name-only grading with no structural fingerprints.
        self.assertEqual(cov.benchmark_fingerprints, 121)

    def test_seeded_stores_are_flagged_as_seeded_only(self):
        _seed_embedded(self.idx)
        cov = self.idx.coverage()
        self.assertEqual(
            {s.name: s.embedded_only for s in cov.stores},
            {"eltbench": True, "spider2_dbt": True, "ade_bench": True},
        )

    def test_the_name_only_summary_says_what_a_clean_result_means(self):
        _seed_embedded(self.idx)
        text = self.idx.coverage().summary()
        self.assertIn("NOT ARMED", text)
        # Re-pinned "family=120" -> "family=121" with the SPIDER2_DBT_FAMILIES
        # "jira" addition (see test_seeding_the_deny_lists_does_not_arm_a_firewall).
        self.assertIn("family=121", text)
        self.assertIn("measure-target", text)

    def test_real_anchor_fingerprints_arm_it(self):
        _seed_embedded(self.idx)
        anchor = demo_fixture.demo_task().model_copy(
            update={"task_id": "eltbench__financial", "family_id": "eltbench__financial"}
        )
        self.idx.add_benchmark("eltbench", sorted(cont.task_fingerprints(anchor)))
        cov = self.idx.coverage()
        self.assertIs(cov.level, CoverageLevel.ARMED)
        self.assertTrue(cov.is_firewall)
        self.assertGreater(cov.structural_fingerprints, 0)
        self.assertIn("schema", cov.benchmark_by_kind)
        self.assertIn("ARMED", cov.summary())
        self.assertFalse(
            next(s for s in cov.stores if s.name == "eltbench").embedded_only
        )

    def test_real_anchor_fingerprints_carry_shape_namespace(self):
        _seed_embedded(self.idx)
        anchor = demo_fixture.demo_task().model_copy(
            update={"task_id": "eltbench__financial", "family_id": "eltbench__financial"}
        )
        self.idx.add_benchmark("eltbench", sorted(cont.task_fingerprints(anchor)))
        cov = self.idx.coverage()
        self.assertIn("shape", cov.benchmark_by_kind)
        self.assertIn("shape-table", cov.benchmark_by_kind)
        self.assertEqual(
            cov.shape_fingerprints,
            cov.benchmark_by_kind["shape"] + cov.benchmark_by_kind["shape-table"],
        )
        self.assertFalse(cov.typed_only_structural)
        self.assertIn("shape=", cov.summary())

    def _legacy_typed_store(self) -> None:
        """Exactly what every runs/* workspace held before shape fingerprints:
        family names + TYPED whole-schema and per-table hashes, no shape:."""
        _seed_embedded(self.idx)
        legacy = [
            fp
            for fp in cont.task_fingerprints(
                demo_fixture.demo_task().model_copy(
                    update={"task_id": "eltbench__financial", "family_id": "eltbench__financial"}
                )
            )
            if fp.startswith(("family:", "schema:", "schema-table:"))
        ]
        self.assertTrue(any(fp.startswith("schema:") for fp in legacy))
        self.assertFalse(any(fp.startswith(cont.ARMING_PREFIXES) for fp in legacy))
        self.idx.add_benchmark("eltbench", sorted(legacy))

    def test_typed_only_structural_store_is_not_armed(self):
        """A pre-shape (typed-only) store is NOT a firewall against TEXT anchors."""
        self._legacy_typed_store()
        cov = self.idx.coverage()
        self.assertIs(cov.level, CoverageLevel.NAME_ONLY)
        self.assertFalse(cov.is_firewall)
        self.assertGreater(cov.structural_fingerprints, 0)   # typed hashes ARE there
        self.assertEqual(cov.shape_fingerprints, 0)           # ...but no shape:
        self.assertTrue(cov.typed_only_structural)
        text = cov.summary()
        self.assertIn("NOT ARMED", text)
        self.assertIn("TYPED schema fingerprints (pre-shape format)", text)
        self.assertIn("re-run measure-target", text)
        # the refusal knob sees it the same way
        self.assertIsNotNone(coverage_failure(cov, CoverageLevel.ARMED))
        # ...and adding ONE whole-schema shape fingerprint arms it
        self.idx.add_benchmark("eltbench", ["shape:" + "0" * 64])
        armed = self.idx.coverage()
        self.assertIs(armed.level, CoverageLevel.ARMED)
        self.assertEqual(armed.shape_fingerprints, 1)
        self.assertIsNone(coverage_failure(armed, CoverageLevel.ARMED))

    def test_per_table_shape_only_does_not_arm(self):
        """`shape-table:` is borderline (audit) — it cannot reject, so it is
        counted as shape material but does not flip the grade on its own."""
        self._legacy_typed_store()
        self.idx.add_benchmark("eltbench", ["shape-table:" + "1" * 64])
        cov = self.idx.coverage()
        self.assertIs(cov.level, CoverageLevel.NAME_ONLY)
        self.assertEqual(cov.shape_fingerprints, 1)

    def test_rearming_upgrades_a_typed_only_store_in_place(self):
        """measure-target on an old workspace: add_benchmark MERGES, so the
        typed hashes survive, the shape hashes arrive, and the grade flips —
        no store rewrite, no migration."""
        self._legacy_typed_store()
        before = set(
            json.loads((self.tmp / "contamination" / "eltbench.json").read_text())["fingerprints"]
        )
        anchor = demo_fixture.demo_task().model_copy(
            update={"task_id": "eltbench__financial", "family_id": "eltbench__financial"}
        )
        self.idx.add_benchmark("eltbench", sorted(cont.task_fingerprints(anchor)))
        after = set(
            json.loads((self.tmp / "contamination" / "eltbench.json").read_text())["fingerprints"]
        )
        self.assertTrue(before <= after)
        self.assertTrue(any(fp.startswith("shape:") for fp in after - before))
        cov = self.idx.coverage()
        self.assertIs(cov.level, CoverageLevel.ARMED)
        self.assertFalse(cov.typed_only_structural)

    def test_our_own_admitted_tasks_never_upgrade_the_grade(self):
        """Admitted fingerprints are structural, but they are OUR output:
        coverage against self-duplication, none against a benchmark."""
        _seed_embedded(self.idx)
        self.idx.add_admitted_task(demo_fixture.demo_task())
        cov = self.idx.coverage()
        self.assertIs(cov.level, CoverageLevel.NAME_ONLY)
        self.assertEqual(cov.admitted_tasks, 1)
        self.assertGreater(cov.admitted_fingerprints, 0)
        self.assertIn("schema", cov.admitted_by_kind)
        self.assertEqual(cov.structural_fingerprints, 0)

    def test_coverage_is_a_pure_function_of_the_directory(self):
        _seed_embedded(self.idx)
        again = ContaminationIndex(self.tmp / "contamination").coverage()
        self.assertEqual(
            self.idx.coverage().model_dump(mode="json"),
            again.model_dump(mode="json"),
        )


class RefusalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.idx = ContaminationIndex(self.tmp / "contamination")
        _seed_embedded(self.idx)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_coverage_failure_is_an_ordinary_fatal_collision(self):
        c = coverage_failure(self.idx.coverage(), CoverageLevel.ARMED)
        self.assertIsNotNone(c)
        self.assertTrue(c.fatal)
        self.assertEqual(c.kind, "index")
        self.assertIn("name_only", c.detail)

    def test_no_failure_when_the_requirement_is_met(self):
        self.assertIsNone(
            coverage_failure(self.idx.coverage(), CoverageLevel.NAME_ONLY)
        )

    def test_scan_pre_records_coverage_and_can_refuse(self):
        task = demo_fixture.demo_task()
        warn = self.idx.scan_pre(task)
        self.assertIs(warn.coverage.level, CoverageLevel.NAME_ONLY)
        self.assertEqual(warn.fatal_count, 0)
        self.assertIn("NOT ARMED", warn.detail())

        refuse = self.idx.scan_pre(task, require=CoverageLevel.ARMED)
        self.assertEqual(refuse.fatal_count, 1)
        self.assertEqual(refuse.collisions[0].kind, "index")

    def test_scan_post_records_coverage(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "task").mkdir()
            (tmp / "key").mkdir()
            result = self.idx.scan_post(
                demo_fixture.demo_task(), tmp / "task", tmp / "key"
            )
            self.assertIs(result.coverage.level, CoverageLevel.NAME_ONLY)
            self.assertEqual(result.call_point, "post")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_env_var_default_is_warn_not_refuse(self):
        self.assertIsNone(required_coverage_from_env({}))
        self.assertIsNone(required_coverage_from_env({"ELT_TASKGEN_REQUIRE_FIREWALL": "0"}))

    def test_env_var_can_demand_a_real_firewall(self):
        self.assertIs(
            required_coverage_from_env({"ELT_TASKGEN_REQUIRE_FIREWALL": "1"}),
            CoverageLevel.ARMED,
        )
        self.assertIs(
            required_coverage_from_env({"ELT_TASKGEN_REQUIRE_FIREWALL": "name"}),
            CoverageLevel.NAME_ONLY,
        )


class LedgerRecordTest(unittest.TestCase):
    """The CLI stage payload and the gate's evidence file both carry it."""

    def test_contamination_payload_has_coverage_fields(self):
        from elt_taskgen.cli import ContaminationPayload

        # legacy default: a payload recorded before the coverage channel existed
        payload = ContaminationPayload(call_point="pre")
        self.assertEqual(payload.coverage_level, "")
        self.assertEqual(payload.coverage, {})

        # populated exactly the way cli.run_contamination_pre builds it, from a
        # real scan result: the grade AND the per-kind census reach the ledger.
        tmp = Path(tempfile.mkdtemp())
        try:
            idx = ContaminationIndex(tmp / "contamination")
            _seed_embedded(idx)
            result = idx.scan_pre(demo_fixture.demo_task())
            payload = ContaminationPayload(
                call_point="pre",
                collisions=tuple(c.model_dump(mode="json") for c in result.collisions),
                fatal_count=result.fatal_count,
                coverage_level=result.coverage.level.value,
                coverage=result.coverage.model_dump(mode="json"),
                detail=result.detail(),
            )
            self.assertEqual(payload.coverage_level, CoverageLevel.NAME_ONLY.value)
            self.assertEqual(payload.coverage["benchmark_by_kind"], {"family": 121})
            self.assertEqual(payload.fatal_count, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_arming_helper_seeds_then_reports(self):
        from elt_taskgen import cli

        tmp = Path(tempfile.mkdtemp())
        try:
            idx = ContaminationIndex(tmp / "contamination")
            cov = cli._arm_contamination_index(idx, announce=False)
            self.assertIs(cov.level, CoverageLevel.NAME_ONLY)
            self.assertTrue(idx.is_armed())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_evidence_json_shape_is_json_serializable(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            idx = ContaminationIndex(tmp / "contamination")
            _seed_embedded(idx)
            payload = idx.coverage().model_dump(mode="json")
            blob = json.dumps(payload)
            self.assertIn("name_only", blob)
            # evidence JSON shows the shape count explicitly (gates read it)
            self.assertEqual(payload["shape_fingerprints"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pre_shape_evidence_json_still_loads(self):
        """Coverage recorded before `shape_fingerprints` existed must load."""
        legacy = {
            "level": "armed",
            "index_dir": "/x/state/contamination",
            "stores": [{"name": "eltbench", "fingerprints": 1232,
                        "by_kind": {"family": 300, "schema": 100, "schema-table": 832}}],
            "benchmark_fingerprints": 1232,
            "benchmark_by_kind": {"family": 300, "schema": 100, "schema-table": 832},
            "admitted_tasks": 0,
            "admitted_fingerprints": 0,
            "admitted_by_kind": {},
        }
        cov = cont.IndexCoverage.model_validate(legacy)
        self.assertEqual(cov.shape_fingerprints, 0)
        self.assertTrue(cov.typed_only_structural)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

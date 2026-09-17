"""C2 end-to-end (group B): a REAL ELT-Bench anchor vs a real-typed copy.

tests/test_contamination.py pins the shape namespace on synthetic tables; this
file closes the loop on the actual instrument: import the pinned `retails`
anchor (== TPC-H, 8 tables, every column TEXT because the schema CSVs carry no
types), arm a scratch index with it exactly the way `measure-target` does, and
run a candidate that copies the schema with the types any adapter would infer
(INTEGER keys, DATE dates, DECIMAL money). Before C2 that candidate produced
0 hits; it must now be a FATAL whole-schema `shape:` collision, and the store
written before shape fingerprints existed must grade NAME_ONLY until re-armed.

Skips when the pinned checkout is absent (same guard as
tests/test_adapters_dlt_anchor.py).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from elt_taskgen.adapters import eltbench_anchor as anchor
from elt_taskgen.models import ColumnSpec, ColumnType, TableSpec
from elt_taskgen.verification import contamination as cont

BENCH_ROOT = Path("/Users/wesleylu/Projects/Research/kang-lab/ELT-Bench")

needs_bench = unittest.skipUnless(
    (BENCH_ROOT / "elt-bench" / "snowflake" / "retails").is_dir(),
    "pinned ELT-Bench checkout not available",
)


def _infer(col: str) -> ColumnType:
    """What a DDL/manifest adapter would infer for TPC-H column names."""
    n = col.lower()
    if n.endswith("key") or n in ("l_linenumber", "o_shippriority", "p_size", "ps_availqty", "l_quantity"):
        return ColumnType.INTEGER
    if n.endswith("date"):
        return ColumnType.DATE
    if n in (
        "l_discount", "l_extendedprice", "l_tax", "o_totalprice", "p_retailprice",
        "ps_supplycost", "s_acctbal", "c_acctbal",
    ):
        return ColumnType.DECIMAL
    return ColumnType.TEXT


def _typed_copy(tables) -> tuple[TableSpec, ...]:
    return tuple(
        TableSpec(
            name=t.name,
            columns=tuple(
                ColumnSpec(name=c.name, type=_infer(c.name), nullable=False)
                for c in t.columns
            ),
        )
        for t in tables
    )


@needs_bench
class RealAnchorShapeFirewallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.retails = anchor.import_anchor_task(BENCH_ROOT, "retails")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fix_b_"))
        self.idx = cont.ContaminationIndex(self.tmp / "contamination")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _candidate(self, tables):
        # Identity is repo-derived like a SchemaPile record, so no family:
        # alias can fire; relationships/reference cleared so ONLY the schema
        # can collide.
        return self.retails.model_copy(
            update={
                "task_id": "schemapile__github_com_x_tpch_0001",
                "family_id": "schemapile__github_com_x_tpch",
                "title": "x/tpch warehouse",
                "tables": tables,
                "relationships": (),
                "reference": None,
                "solver_prompt": "",
            }
        )

    def test_anchor_is_text_typed_and_typed_copy_is_fatal_by_shape(self):
        self.assertTrue(
            all(c.type is ColumnType.TEXT for t in self.retails.tables for c in t.columns)
        )
        self.idx.add_benchmark("eltbench", sorted(cont.task_fingerprints(self.retails)))
        cov = self.idx.coverage()
        self.assertIs(cov.level, cont.CoverageLevel.ARMED)
        self.assertEqual(cov.benchmark_by_kind["shape"], 1)
        self.assertEqual(cov.benchmark_by_kind["shape-table"], len(self.retails.tables))

        typed = _typed_copy(self.retails.tables)
        self.assertNotEqual(  # the copy really is retyped
            {c.type for t in typed for c in t.columns}, {ColumnType.TEXT}
        )
        collisions = self.idx.check_pre(self._candidate(typed))
        fatal = [c for c in collisions if c.fatal]
        self.assertTrue(fatal, msg=f"got: {collisions}")
        self.assertTrue(all(c.kind == "schema" and "shape:" in c.detail for c in fatal))
        # the typed namespaces stayed silent — exactly the pre-C2 blind spot
        self.assertFalse(any(" schema:" in c.detail for c in collisions))
        self.assertFalse(any(" schema-table:" in c.detail for c in collisions))
        self.assertEqual(
            sum("shape-table:" in c.detail for c in collisions), len(self.retails.tables)
        )

    def test_legacy_typed_only_store_grades_name_only_until_rearmed(self):
        """A store written by the pre-shape measure-target (typed hashes only)
        is NOT a firewall; re-arming merges the shape hashes in place."""
        legacy = sorted(
            fp for fp in cont.task_fingerprints(self.retails)
            if not fp.startswith(cont.ARMING_PREFIXES)
        )
        self.idx.add_benchmark("eltbench", legacy)
        before = self.idx.coverage()
        self.assertIs(before.level, cont.CoverageLevel.NAME_ONLY)
        self.assertTrue(before.typed_only_structural)
        # ...and the typed copy walks through it: the exact C2 defect
        typed = _typed_copy(self.retails.tables)
        self.assertEqual(self.idx.check_pre(self._candidate(typed)), [])
        # re-arm (what measure-target does again on the same workspace)
        self.idx.add_benchmark("eltbench", sorted(cont.task_fingerprints(self.retails)))
        store = json.loads((self.tmp / "contamination" / "eltbench.json").read_text())
        self.assertTrue(set(legacy) <= set(store["fingerprints"]))
        after = self.idx.coverage()
        self.assertIs(after.level, cont.CoverageLevel.ARMED)
        self.assertTrue(any(c.fatal for c in self.idx.check_pre(self._candidate(typed))))

if __name__ == "__main__":  # pragma: no cover
    unittest.main()

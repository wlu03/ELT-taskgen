"""verification/contamination.py tests — one service, two call points, fail closed.

Covers: armed-but-empty index is a failure (never a clean pass); the embedded
ELT-Bench deny list catches family-name reuse across pools; SQL normalization
catches whitespace/case-mangled copies of reference SQL; whole-schema hashes
catch structural clones; the TYPE-BLIND `shape:`/`shape-table:` namespace
catches a real-typed copy of a TEXT-typed ELT-Bench anchor (whole schema
fatal, per-table borderline) where the typed `schema:` hash cannot; admitted
tasks never collide with themselves but do catch near-duplicates; persistence
across index instances; post-generation scanning of emitted artifacts
(fixture/data file hashes, text overlap) with missing artifact trees failing
closed.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from elt_taskgen import demo_fixture
from elt_taskgen.demo_fixture import MART_NAME
from elt_taskgen.models import ColumnSpec, ColumnType, TableSpec
from elt_taskgen.verification.contamination import (
    ARMING_PREFIXES,
    ELTBENCH_FAMILIES,
    STRUCTURAL_PREFIXES,
    Collision,
    ContaminationIndex,
    normalize_name,
    normalize_sql,
    schema_fingerprints,
    sql_fingerprint,
    task_fingerprints,
    text_fingerprints,
)


def _variant(task, task_id: str, family_id: str, **updates):
    """A frozen-model copy with new identity (validators already ran on demo)."""
    return task.model_copy(
        update={"task_id": task_id, "family_id": family_id, **updates}
    )


#: A TPC-H-shaped 8-table schema (the ELT-Bench `retails` anchor is exactly
#: this table set), as (table, columns). Types are assigned per column name by
#: `_typed_tables`; `_text_tables` types everything TEXT the way
#: adapters/eltbench_anchor.py does.
_BENCH_SHAPE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("region", ("r_regionkey", "r_name", "r_comment")),
    ("nation", ("n_nationkey", "n_name", "n_regionkey", "n_comment")),
    ("supplier", ("s_suppkey", "s_name", "s_nationkey", "s_acctbal")),
    ("part", ("p_partkey", "p_name", "p_size", "p_retailprice")),
    ("partsupp", ("ps_partkey", "ps_suppkey", "ps_availqty", "ps_supplycost")),
    ("customer", ("c_custkey", "c_name", "c_nationkey", "c_acctbal")),
    ("orders", ("o_orderkey", "o_custkey", "o_orderdate", "o_totalprice")),
    ("lineitem", ("l_orderkey", "l_linenumber", "l_shipdate", "l_extendedprice")),
)


def _text_tables(shape=_BENCH_SHAPE) -> tuple[TableSpec, ...]:
    """Anchor-style: every column TEXT/nullable (no type info upstream)."""
    return tuple(
        TableSpec(
            name=t,
            columns=tuple(
                ColumnSpec(name=c, type=ColumnType.TEXT, nullable=True) for c in cols
            ),
        )
        for t, cols in shape
    )


def _typed_tables(shape=_BENCH_SHAPE) -> tuple[TableSpec, ...]:
    """Candidate-style: real types inferred from the names, NOT NULL keys."""

    def _type(col: str) -> ColumnType:
        if col.endswith("key") or col in ("l_linenumber", "p_size", "ps_availqty"):
            return ColumnType.INTEGER
        if col.endswith("date"):
            return ColumnType.DATE
        if col.endswith(("price", "cost", "acctbal")):
            return ColumnType.DECIMAL
        return ColumnType.TEXT

    return tuple(
        TableSpec(
            name=t,
            columns=tuple(
                ColumnSpec(name=c, type=_type(c), nullable=not c.endswith("key"))
                for c in cols
            ),
        )
        for t, cols in shape
    )


class FingerprintFunctionTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()

    def test_normalize_name(self):
        self.assertEqual(normalize_name("  Customer Summary! "), "customer_summary")
        self.assertEqual(normalize_name("A--B__c"), "a_b_c")

    def test_sql_normalization_ignores_case_and_whitespace(self):
        a = "SELECT  a,b FROM t WHERE x = 1"
        b = "select A, B\n  from T\nwhere X=1"
        self.assertEqual(normalize_sql(a), normalize_sql(b))
        self.assertEqual(sql_fingerprint(a), sql_fingerprint(b))

    def test_sql_normalization_distinguishes_semantics(self):
        self.assertNotEqual(
            sql_fingerprint("SELECT a FROM t"), sql_fingerprint("SELECT b FROM t")
        )

    def test_task_fingerprints_are_namespaced(self):
        fps = task_fingerprints(self.task)
        prefixes = {fp.split(":", 1)[0] for fp in fps}
        self.assertIn("family", prefixes)
        self.assertIn("schema", prefixes)
        self.assertIn("schema-table", prefixes)
        # type-blind twins of the typed namespaces (what arms the firewall)
        self.assertIn("shape", prefixes)
        self.assertIn("shape-table", prefixes)
        self.assertIn("sql", prefixes)
        self.assertIn("deps", prefixes)
        # pool-qualified AND base family aliases (normalized: '__' collapses)
        self.assertIn("family:demo_customer_summary", fps)
        self.assertIn("family:customer_summary", fps)
        # one whole-schema shape hash, one per-table shape hash per table
        self.assertEqual(sum(fp.startswith("shape:") for fp in fps), 1)
        self.assertEqual(
            sum(fp.startswith("shape-table:") for fp in fps), len(self.task.tables)
        )

    def test_shape_fingerprint_ignores_types_and_nullability(self):
        """Typed vs TEXT copies share EXACTLY the shape:/shape-table: subset."""
        typed = schema_fingerprints(_typed_tables())
        text = schema_fingerprints(_text_tables())
        shape_typed = {fp for fp in typed if fp.startswith(ARMING_PREFIXES)}
        shape_text = {fp for fp in text if fp.startswith(ARMING_PREFIXES)}
        self.assertEqual(shape_typed, shape_text)
        self.assertEqual(len(shape_typed), 1 + len(_BENCH_SHAPE))
        # ...and NOTHING else: the typed namespaces diverge completely.
        self.assertEqual(typed & text, shape_typed)
        self.assertTrue(any(fp.startswith("schema:") for fp in typed))
        self.assertTrue(any(fp.startswith("schema-table:") for fp in typed))
        # Renaming a column changes the shape; retyping does not.
        renamed = list(_BENCH_SHAPE)
        renamed[0] = ("region", ("r_regionkey", "region_name", "r_comment"))
        self.assertNotEqual(
            {fp for fp in schema_fingerprints(_text_tables(renamed)) if fp.startswith("shape:")},
            {fp for fp in text if fp.startswith("shape:")},
        )

    def test_shape_namespaces_are_structural_and_arming(self):
        for prefix in ARMING_PREFIXES:
            self.assertIn(prefix, STRUCTURAL_PREFIXES)
        self.assertEqual(ARMING_PREFIXES, ("shape:", "shape-table:"))

    def test_embedded_eltbench_list_is_complete(self):
        self.assertEqual(len(ELTBENCH_FAMILIES), 100)
        self.assertIn("california_schools", ELTBENCH_FAMILIES)
        self.assertIn("zuora", ELTBENCH_FAMILIES)

    def test_text_fingerprints_skip_short_fragments(self):
        fps = text_fingerprints("short.  " * 2)
        self.assertEqual(fps, set())
        fps = text_fingerprints(
            "Compute one row per customer including customers with no orders at all."
        )
        self.assertEqual(len(fps), 1)


class ContaminationIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="contamination_test_"))
        self.index_dir = self.tmpdir / "index"
        self.task = demo_fixture.demo_task()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _armed_index(self) -> ContaminationIndex:
        idx = ContaminationIndex(self.index_dir)
        idx.add_benchmark("spider2_dbt", ["family:__nonexistent_seed__"])
        return idx

    # -- armed-but-empty ----------------------------------------------------

    def test_empty_index_is_failure_not_clean_pass(self):
        idx = ContaminationIndex(self.index_dir)  # never armed
        collisions = idx.check_pre(self.task)
        self.assertTrue(any(c.fatal and c.kind == "index" for c in collisions))

    def test_armed_index_clean_task_passes(self):
        idx = self._armed_index()
        self.assertEqual(idx.check_pre(self.task), [])

    def test_arming_persists_across_instances(self):
        self._armed_index()
        reopened = ContaminationIndex(self.index_dir)
        self.assertTrue(reopened.is_armed())
        self.assertEqual(reopened.check_pre(self.task), [])

    # -- embedded deny lists ------------------------------------------------

    def test_embedded_eltbench_family_collision_is_fatal(self):
        idx = self._armed_index()
        candidate = _variant(
            self.task, "synsql__california_schools_0001", "synsql__california_schools"
        )
        collisions = idx.check_pre(candidate)
        fatal_family = [
            c for c in collisions
            if c.fatal and c.kind == "family" and c.against == "eltbench"
        ]
        self.assertTrue(fatal_family, msg=f"got: {collisions}")

    # -- benchmark fingerprints seeded at build time -------------------------

    def test_seeded_benchmark_sql_collision(self):
        idx = self._armed_index()
        # An anchor whose reference SQL matches ours modulo formatting.
        mangled = demo_fixture.REFERENCE_SQL.upper().replace("\n", "  ")
        idx.add_benchmark("eltbench", [sql_fingerprint(mangled)])
        collisions = idx.check_pre(self.task)
        self.assertTrue(
            any(c.fatal and c.kind == "sql" and c.against == "eltbench"
                for c in collisions),
            msg=f"got: {collisions}",
        )

    # -- type-blind shape namespace (the anchors are TEXT-typed) --------------

    def _index_text_anchor(self, idx: ContaminationIndex, shape=_BENCH_SHAPE) -> None:
        """What measure-target does with an anchor: index its fingerprints.

        Shaped like a real import (adapters/eltbench_anchor.py): TEXT-typed
        tables, no relationships, no reference SQL, no solver prompt — so the
        ONLY structural material the anchor contributes is its schema.
        """
        anchor = _variant(
            self.task, "eltbench__retails", "eltbench__retails",
            title="ELT-Bench anchor: retails", tables=_text_tables(shape),
            relationships=(), reference=None, solver_prompt="",
        )
        idx.add_benchmark("eltbench", sorted(task_fingerprints(anchor)))

    def _candidate(self, tables):
        return _variant(
            self.task, "schemapile__gh_com_x_y_0001", "schemapile__gh_com_x_y",
            title="Repo x/y warehouse", tables=tables,
        )

    def test_retyped_copy_of_benchmark_schema_is_fatal(self):
        """THE C2 defect: an all-TEXT anchor vs the same schema with real types.

        The typed `schema:` hash cannot match (INTEGER != TEXT), so before the
        shape namespace a verbatim copy of ELT-Bench `retails` with inferred
        types walked through the firewall with 0 hits. Now the whole-schema
        `shape:` hash is a fatal Collision(kind='schema').
        """
        idx = self._armed_index()
        self._index_text_anchor(idx)
        collisions = idx.check_pre(self._candidate(_typed_tables()))
        schema_hits = [c for c in collisions if c.kind == "schema" and c.against == "eltbench"]
        self.assertTrue(schema_hits, msg=f"got: {collisions}")
        fatal = [c for c in schema_hits if c.fatal]
        self.assertTrue(fatal, msg=f"got: {collisions}")
        self.assertTrue(all("shape:" in c.detail for c in fatal), msg=f"got: {fatal}")
        # the typed namespaces did NOT match — the shape namespace is the catch
        self.assertFalse(
            any(" schema:" in c.detail or " schema-table:" in c.detail for c in collisions),
            msg=f"typed hash matched a TEXT anchor?! {collisions}",
        )
        # every table matched by shape too (borderline per-table hits)
        self.assertEqual(
            sum("shape-table:" in c.detail for c in schema_hits), len(_BENCH_SHAPE)
        )

    def test_partial_table_shape_overlap_is_borderline(self):
        """7 of 8 tables identical by name -> non-fatal, kind schema, shape-table:."""
        idx = self._armed_index()
        self._index_text_anchor(idx)
        partial = list(_BENCH_SHAPE[:-1]) + [
            ("shipments", ("shipment_id", "l_orderkey", "carrier", "shipped_on")),
        ]
        collisions = idx.check_pre(self._candidate(_typed_tables(tuple(partial))))
        schema_hits = [c for c in collisions if c.kind == "schema"]
        self.assertEqual(len(schema_hits), len(_BENCH_SHAPE) - 1, msg=f"got: {collisions}")
        self.assertTrue(all(not c.fatal for c in schema_hits))
        self.assertTrue(all("shape-table:" in c.detail for c in schema_hits))
        self.assertFalse(any(c.fatal for c in collisions), msg=f"got: {collisions}")

    def test_unrelated_schema_has_no_shape_hits(self):
        idx = self._armed_index()
        self._index_text_anchor(idx)
        self.assertEqual(idx.check_pre(self.task), [])

    # -- admitted corpus -----------------------------------------------------

    def test_admitted_task_never_collides_with_itself(self):
        idx = self._armed_index()
        idx.add_admitted_task(self.task)
        self.assertEqual(idx.check_pre(self.task), [])

    def test_admitted_near_duplicate_is_caught(self):
        idx = self._armed_index()
        idx.add_admitted_task(self.task)
        clone = _variant(self.task, "synsql__orders_clone_01", "synsql__orders_clone")
        collisions = idx.check_pre(clone)
        kinds = {c.kind for c in collisions if c.against == "admitted"}
        self.assertIn("schema", kinds)   # identical schema shape
        self.assertIn("sql", kinds)      # identical reference SQL
        self.assertTrue(any(c.fatal for c in collisions))

    # -- post-generation call point ------------------------------------------

    def _emit_dirs(self) -> tuple[Path, Path]:
        task_dir = self.tmpdir / "task"
        answer_key = self.tmpdir / "answer_key"
        (task_dir / "sources" / "postgres").mkdir(parents=True)
        (task_dir / "sources" / "postgres" / "customers.sql").write_text(
            "INSERT INTO customers VALUES (1, 'C1');\n", encoding="utf-8"
        )
        (task_dir / "config.yaml").write_text("version: 1\n", encoding="utf-8")
        (answer_key / "gold" / "primary").mkdir(parents=True)
        (answer_key / "gold" / "primary" / f"{MART_NAME}.csv").write_text(
            "customer_id,completed_order_count,total_spend\n1,1,45.0\n",
            encoding="utf-8",
        )
        (answer_key / "reference").mkdir(parents=True)
        (answer_key / "reference" / f"{MART_NAME}.sql").write_text(
            demo_fixture.REFERENCE_SQL, encoding="utf-8"
        )
        return task_dir, answer_key

    def test_check_post_missing_dirs_fail_closed(self):
        idx = self._armed_index()
        collisions = idx.check_post(
            self.task, self.tmpdir / "nope_task", self.tmpdir / "nope_key"
        )
        self.assertTrue(any(c.fatal for c in collisions))

    def test_check_post_clean_artifacts_pass(self):
        idx = self._armed_index()
        task_dir, answer_key = self._emit_dirs()
        self.assertEqual(idx.check_post(self.task, task_dir, answer_key), [])

    def test_check_post_generated_data_collision_is_fatal(self):
        idx = self._armed_index()
        task_dir, answer_key = self._emit_dirs()
        fixture_bytes = (
            task_dir / "sources" / "postgres" / "customers.sql"
        ).read_bytes()
        idx.add_benchmark(
            "ade_bench", ["fixture:" + hashlib.sha256(fixture_bytes).hexdigest()]
        )
        collisions = idx.check_post(self.task, task_dir, answer_key)
        self.assertTrue(
            any(c.fatal and c.kind == "fixture" and c.against == "ade_bench"
                for c in collisions),
            msg=f"got: {collisions}",
        )

    def test_check_post_reference_sql_collision(self):
        idx = self._armed_index()
        task_dir, answer_key = self._emit_dirs()
        idx.add_benchmark("eltbench", [sql_fingerprint(demo_fixture.REFERENCE_SQL)])
        collisions = idx.check_post(self.task, task_dir, answer_key)
        self.assertTrue(any(c.fatal and c.kind == "sql" for c in collisions))

    def test_borderline_text_overlap_is_nonfatal(self):
        idx = self._armed_index()
        sentence = (
            "Include every customer in the output even when they have placed no orders."
        )
        idx.add_benchmark("ade_bench", text_fingerprints(sentence))
        prompted = self.task.model_copy(
            update={"solver_prompt": f"Overview. {sentence}"}
        )
        collisions = idx.check_pre(prompted)
        text_hits = [c for c in collisions if c.kind == "text"]
        self.assertTrue(text_hits)
        self.assertTrue(all(not c.fatal for c in text_hits))

    # -- misc ----------------------------------------------------------------

    def test_collision_model_is_frozen(self):
        c = Collision(kind="family", against="eltbench", detail="x", fatal=True)
        with self.assertRaises(Exception):
            c.fatal = False

    def test_corrupt_store_fails_closed(self):
        idx = self._armed_index()
        for store in self.index_dir.glob("*.json"):
            store.write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            idx.check_pre(self.task)


if __name__ == "__main__":
    unittest.main()

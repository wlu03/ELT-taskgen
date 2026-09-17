"""Tests for the TRANSFORM warehouse CENSUS (export/eltbench.py).

WHY THIS EXISTS
The TRANSFORM variant SHIPS an artifact — one DuckDB warehouse per population —
and until now the only thing that ever looked at it was an assertion inside
`materialize_warehouse`, at export time. Export is not a gate: it writes no
record, it is not bound to the ledger, and three TRANSFORM gates
(`warehouses-load`, `data-sensitivity`, `determinism`) need to make claims
about that artifact. So the check is promoted into recorded evidence,
reports/warehouse_census.json.

WHY A CENSUS AND NOT A FILE HASH — the claim these tests pin down. DuckDB files
are NOT byte-stable: two clean builds of identical data differ in bytes
(version header, storage page allocation). Measured on this fixture, two
materializations of the primary population produced files whose sha256 differed
(a5c23563…, 11a5a055…) while the data was identical. A byte digest would
therefore report the shipped warehouse as non-reproducible on every single run
— a permanent false alarm, and permanent false alarms get deleted. The witness
is instead the DATA: sorted (table, row_count) plus a per-table row-MULTISET
digest, which is invariant to physical row order (which DuckDB does not
promise) and moves whenever a value, a row, or a relation changes.

These tests assert exactly that pair of properties:
  * INVARIANT under a second clean build (census == rebuild census, and the
    whole record is byte-identical across two independent emissions), and
  * SENSITIVE to a perturbed warehouse — one changed cell with the row count
    untouched, one deleted row, and one smuggled-in extra relation.

Plus the binding contract every piece of gate evidence shares (task id,
content hash, kind) and the `warehouses-load` gate reading it end to end.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import duckdb

from elt_taskgen.export import eltbench
from elt_taskgen.models import PopulationName, TaskVariant
from elt_taskgen.verification import gates

try:  # `unittest discover -s tests` puts tests/ on sys.path; -m tests.x does not
    from test_independent import IndependentTestCase
except ImportError:  # pragma: no cover - depends on how the suite is invoked
    from tests.test_independent import IndependentTestCase

P = PopulationName


#: Module-level cache. One emission builds TEN warehouses (five shipped, five
#: rebuilt) over five populations including the stress split, so it is done
#: once and copied per test — the tests that mutate a warehouse need their own
#: bytes, not their own build.
_EMITTED: dict = {}


def _emit_once(test: IndependentTestCase) -> Path:
    if not _EMITTED:
        workspace = test.fresh_workspace()
        task_dir = workspace / "tasks" / test.task.task_id
        eltbench.emit_variant(
            test.task,
            test.gold,
            TaskVariant.TRANSFORM,
            task_dir / "variants" / "transform",
            populations_dir=task_dir / "populations",
        )
        # fresh_workspace() is cleaned up per test; keep our own copy alive.
        keep = tempfile.TemporaryDirectory()
        base = Path(keep.name) / "emitted"
        shutil.copytree(workspace, base)
        _EMITTED.update({"tmp": keep, "base": base})
    return _EMITTED["base"]


class WarehouseCensusTestCase(IndependentTestCase):
    """One emitted TRANSFORM bundle, copied per test."""

    def emitted(self) -> tuple[Path, Path, dict]:
        """(workspace, warehouse_dir, recorded census) after one emission."""
        base = _emit_once(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name) / "taskgen-workspace"
        shutil.copytree(base, workspace)
        task_dir = workspace / "tasks" / self.task.task_id
        record = json.loads(
            (task_dir / eltbench.WAREHOUSE_CENSUS_EVIDENCE_REL).read_text(
                encoding="utf-8"
            )
        )
        warehouse_dir = (
            task_dir / "variants" / "transform" / "task" / eltbench.WAREHOUSE_DIRNAME
        )
        return workspace, warehouse_dir, record


class TestRecordedCensus(WarehouseCensusTestCase):
    def test_emitting_the_variant_records_the_census(self) -> None:
        workspace, _, record = self.emitted()
        path = (
            workspace / "tasks" / self.task.task_id
            / gates.WAREHOUSE_CENSUS_EVIDENCE_REL
        )
        self.assertTrue(path.is_file(), "the gates read exactly this path")
        self.assertEqual(record["kind"], gates.WAREHOUSE_CENSUS_KIND)
        self.assertEqual(record["task_id"], self.task.task_id)
        self.assertEqual(record["task_content_hash"], self.task.content_hash())
        self.assertEqual(
            sorted(record["populations"]), sorted(p.value for p in P)
        )

    def test_census_names_the_shipped_artifact_and_the_frozen_counts(self) -> None:
        _, _, record = self.emitted()
        expected_tables = sorted(t.name for t in self.task.tables)
        for pop in P:
            entry = record["populations"][pop.value]
            self.assertEqual(
                entry["path"],
                f"variants/transform/task/warehouse/{pop.value}.duckdb",
            )
            self.assertEqual(sorted(entry["tables"]), expected_tables)
            for table, cell in entry["tables"].items():
                self.assertEqual(
                    cell["row_count"], self.gold.stage1[pop.value][table]
                )
                self.assertEqual(len(cell["row_digest"]), 64)
            self.assertEqual(len(entry["census_digest"]), 64)

    def test_two_clean_builds_agree(self) -> None:
        """`rebuild_census_digest` comes from a SECOND materialization into a
        throwaway file — one build is a claim, two are evidence."""
        _, _, record = self.emitted()
        for pop in P:
            entry = record["populations"][pop.value]
            self.assertEqual(
                entry["census_digest"],
                entry["rebuild_census_digest"],
                f"{pop.value}: the shipped warehouse is not reproducible",
            )

    def test_the_whole_record_is_deterministic_across_emissions(self) -> None:
        """A re-emission from scratch reproduces the record BYTE for byte —
        while the .duckdb files it censuses do not (see the module docstring)."""
        workspace, _, _ = self.emitted()
        task_dir = workspace / "tasks" / self.task.task_id
        out = task_dir / "variants" / "transform"
        path = task_dir / eltbench.WAREHOUSE_CENSUS_EVIDENCE_REL
        first = path.read_bytes()
        shutil.rmtree(out)
        path.unlink()
        eltbench.emit_variant(
            self.task,
            self.gold,
            TaskVariant.TRANSFORM,
            out,
            populations_dir=task_dir / "populations",
        )
        self.assertEqual(first, path.read_bytes())

    def test_census_is_order_independent_but_value_sensitive(self) -> None:
        """The multiset digest, stated as two halves.

        Physical row order must not matter (DuckDB does not promise it), but a
        changed VALUE must move the digest even when the row count does not.
        """
        _, warehouse_dir, _ = self.emitted()
        db = warehouse_dir / f"{P.PRIMARY.value}.duckdb"
        before = eltbench.warehouse_census(db)

        con = duckdb.connect(str(db))
        con.execute("CREATE TABLE _shuffled AS SELECT * FROM customers ORDER BY random()")
        con.execute("DELETE FROM customers")
        con.execute("INSERT INTO customers SELECT * FROM _shuffled")
        con.execute("DROP TABLE _shuffled")
        con.execute("CHECKPOINT")
        con.close()
        self.assertEqual(
            before["tables"]["customers"]["row_digest"],
            eltbench.warehouse_census(db)["tables"]["customers"]["row_digest"],
            "row ORDER must not move the digest",
        )

        con = duckdb.connect(str(db))
        con.execute(
            "UPDATE customers SET customer_name = 'PERTURBED' "
            "WHERE customer_id = (SELECT MIN(customer_id) FROM customers)"
        )
        con.execute("CHECKPOINT")
        con.close()
        after = eltbench.warehouse_census(db)
        self.assertEqual(
            before["tables"]["customers"]["row_count"],
            after["tables"]["customers"]["row_count"],
        )
        self.assertNotEqual(
            before["tables"]["customers"]["row_digest"],
            after["tables"]["customers"]["row_digest"],
        )
        self.assertNotEqual(before["census_digest"], after["census_digest"])

    def test_a_dropped_row_moves_the_census(self) -> None:
        _, warehouse_dir, _ = self.emitted()
        db = warehouse_dir / f"{P.PRIMARY.value}.duckdb"
        before = eltbench.warehouse_census(db)
        con = duckdb.connect(str(db))
        con.execute(
            "DELETE FROM customers WHERE customer_id = "
            "(SELECT MIN(customer_id) FROM customers)"
        )
        con.execute("CHECKPOINT")
        con.close()
        after = eltbench.warehouse_census(db)
        self.assertEqual(
            after["tables"]["customers"]["row_count"],
            before["tables"]["customers"]["row_count"] - 1,
        )
        self.assertNotEqual(before["census_digest"], after["census_digest"])

    def test_a_smuggled_relation_is_visible_in_the_census(self) -> None:
        """The census covers EVERY relation, not just the declared tables —
        an `answer_key`-shaped table must not be able to hide behind a filter."""
        _, warehouse_dir, _ = self.emitted()
        db = warehouse_dir / f"{P.PRIMARY.value}.duckdb"
        before = eltbench.warehouse_census(db)
        con = duckdb.connect(str(db))
        con.execute("CREATE TABLE answer_key AS SELECT 1 AS x")
        con.execute("CHECKPOINT")
        con.close()
        after = eltbench.warehouse_census(db)
        self.assertIn("answer_key", after["tables"])
        self.assertNotEqual(before["census_digest"], after["census_digest"])

    def test_missing_warehouse_fails_closed(self) -> None:
        _, warehouse_dir, _ = self.emitted()
        db = warehouse_dir / f"{P.PRIMARY.value}.duckdb"
        db.unlink()
        with self.assertRaises(FileNotFoundError):
            eltbench.warehouse_census(db)


class TestCensusSeesTheWholeArtifact(WarehouseCensusTestCase):
    """CENSUS v2: what v1 could not see (and therefore could not pin).

    v1 hashed relation NAMES, column NAMES and `str(cell)`. Measured on a real
    released warehouse, each of the following left its digest completely
    unmoved while changing what the warehouse IS:

      * a macro whose body is the reference SQL (`CREATE MACRO gold() AS
        TABLE read_csv_auto(<the gold csv>)`) — the answer, in the artifact;
      * a table COMMENT carrying the reference SQL;
      * `ALTER ... TYPE VARCHAR` on a column the reference sums — after which
        the reference query FAILS on the shipped warehouse;
      * rewriting NULL to '' — after which a correct query returns different
        rows (12 of 63 on the measured task).

    A pin blind to all of that is not a pin of the artifact, so v2 covers
    typed columns, NULL-distinct cells and the catalog.
    """

    def _db(self) -> Path:
        _, warehouse_dir, _ = self.emitted()
        return warehouse_dir / f"{P.PRIMARY.value}.duckdb"

    @staticmethod
    def _mutate(db: Path, *statements: str) -> None:
        con = duckdb.connect(str(db))
        try:
            for sql in statements:
                con.execute(sql)
            con.execute("CHECKPOINT")
        finally:
            con.close()

    def test_a_macro_moves_the_census(self) -> None:
        db = self._db()
        before = eltbench.warehouse_census(db)
        self._mutate(db, "CREATE MACRO gold() AS TABLE SELECT 1 AS x")
        after = eltbench.warehouse_census(db)
        self.assertNotEqual(before["catalog_digest"], after["catalog_digest"])
        self.assertNotEqual(before["census_digest"], after["census_digest"])
        # ... while every ROW digest is untouched: the change is catalog-only,
        # which is exactly why a row-only census could not see it.
        self.assertEqual(
            {n: t["row_digest"] for n, t in before["tables"].items()},
            {n: t["row_digest"] for n, t in after["tables"].items()},
        )
        self.assertTrue(after["catalog"]["macros"])

    def test_a_comment_or_sequence_moves_the_census(self) -> None:
        db = self._db()
        before = eltbench.warehouse_census(db)
        self._mutate(db, "COMMENT ON TABLE customers IS 'select * from gold'")
        commented = eltbench.warehouse_census(db)
        self.assertNotEqual(before["census_digest"], commented["census_digest"])
        self._mutate(db, "CREATE SEQUENCE leak_seq")
        after = eltbench.warehouse_census(db)
        self.assertNotEqual(commented["census_digest"], after["census_digest"])
        self.assertTrue(after["catalog"]["sequences"])

    def test_null_and_empty_string_are_different_cells(self) -> None:
        """v1 hashed `str(cell)` with None -> '', so rewriting every NULL to ''
        left the census IDENTICAL — while `IS NULL`, `COALESCE` and `COUNT(col)`
        all changed their answer. Starting-state identity is not reward
        equivalence, so the two are distinct cells here."""
        tmp = Path(tempfile.mkdtemp(prefix="elt-census-null-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        digests = []
        for name, value in (("null", "NULL"), ("empty", "''")):
            db = tmp / f"{name}.duckdb"
            con = duckdb.connect(str(db))
            try:
                con.execute('CREATE TABLE t ("id" INTEGER NOT NULL, "note" VARCHAR)')
                con.execute(f"INSERT INTO t VALUES (1, {value})")
                con.execute("CHECKPOINT")
            finally:
                con.close()
            census = eltbench.warehouse_census(db)
            self.assertEqual(census["tables"]["t"]["row_count"], 1)
            digests.append(census["tables"]["t"]["row_digest"])
        self.assertNotEqual(digests[0], digests[1])

    def test_a_retyped_column_moves_the_census(self) -> None:
        db = self._db()
        before = eltbench.warehouse_census(db)
        self._mutate(db, "ALTER TABLE customers ALTER customer_id TYPE VARCHAR")
        after = eltbench.warehouse_census(db)
        self.assertNotEqual(
            before["tables"]["customers"]["row_digest"],
            after["tables"]["customers"]["row_digest"],
        )
        self.assertNotEqual(before["census_digest"], after["census_digest"])

    def test_a_hidden_schema_relation_is_censused_not_crashed(self) -> None:
        db = self._db()
        before = eltbench.warehouse_census(db)
        self._mutate(
            db,
            "CREATE SCHEMA h",
            "CREATE TABLE h.customers AS SELECT 1 AS x",
        )
        after = eltbench.warehouse_census(db)  # v1 crashed here (name collision)
        self.assertIn("h.customers", after["tables"])
        self.assertIn("customers", after["tables"])
        self.assertNotEqual(before["census_digest"], after["census_digest"])

    def test_a_clean_warehouse_has_an_empty_forbidden_catalog(self) -> None:
        """The leak guard's premise: a source-only warehouse carries no macros,
        views, sequences, user types, indexes, extra schemas or comments."""
        catalog = eltbench.warehouse_census(self._db())["catalog"]
        for group in eltbench._FORBIDDEN_CATALOG_GROUPS:
            self.assertEqual(catalog[group], [], group)
        # NOT NULL constraints DO exist (the loader declares them) and are
        # pinned rather than refused.
        self.assertTrue(catalog["constraints"])


class TestCensusFeedsTheGates(WarehouseCensusTestCase):
    def test_warehouses_load_gate_passes_on_the_real_record(self) -> None:
        workspace, _, _ = self.emitted()
        gate = gates._gate_t_warehouses_load(self.task, workspace, self.gold)
        self.assertTrue(gate.passed, gate.details)
        for pop in P:
            self.assertEqual(
                gate.evidence[f"{pop.value}:customers"],
                f"warehouse={self.gold.stage1[pop.value]['customers']},"
                f"gold={self.gold.stage1[pop.value]['customers']}",
            )

    def test_absent_census_is_red_never_a_skip(self) -> None:
        workspace, _, _ = self.emitted()
        (
            workspace / "tasks" / self.task.task_id
            / gates.WAREHOUSE_CENSUS_EVIDENCE_REL
        ).unlink()
        gate = gates._gate_t_warehouses_load(self.task, workspace, self.gold)
        self.assertFalse(gate.passed)

    def test_stale_census_is_red(self) -> None:
        workspace, _, _ = self.emitted()
        moved = self.task.model_copy(update={"title": "a different task"})
        gate = gates._gate_t_warehouses_load(moved, workspace, self.gold)
        self.assertFalse(gate.passed)

    def test_recorded_counts_are_checked_against_the_frozen_gold(self) -> None:
        """The record is evidence, not testimony: a doctored count is caught."""
        workspace, _, _ = self.emitted()
        path = (
            workspace / "tasks" / self.task.task_id
            / gates.WAREHOUSE_CENSUS_EVIDENCE_REL
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        record["populations"][P.PRIMARY.value]["tables"]["customers"][
            "row_count"
        ] += 1
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        gate = gates._gate_t_warehouses_load(self.task, workspace, self.gold)
        self.assertFalse(gate.passed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

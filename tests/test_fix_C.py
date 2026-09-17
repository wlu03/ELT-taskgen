"""Group C fixes that do not belong to one existing test module.

WHY THIS EXISTS
Three of the export-side fixes are about things a bundle SAYS rather than
things it contains, and each of them shipped wrong once already:

  * PROVENANCE (P1). Every "byte-identical" and "deterministic" claim in this
    codebase is a same-environment claim — three rebuilds in one process, a
    repair fingerprint over one machine's artifacts — and no release artifact
    recorded which environment that was. `release.runtime_versions()` /
    `environment_drift()` make the record, and a drifting environment is a
    refusal unless the operator says otherwise.
  * THE COLLECTORS (X3). `export/serve.py` is the ONE grading client; its two
    collectors decide what "the solver did not build that" looks like to the
    reward, and the difference between "absent" and "zero rows" is the
    difference between "table not found" and a count mismatch.
  * THE DOCSTRINGS (X5, C3). `eltbench.py` claimed "THE LAYOUT IS THE REAL
    UPSTREAM ONE" for a private tree that is a per-task CONSOLIDATION of
    upstream's shared evaluation dirs, and documented no bound on the derived
    database/bucket names — the two claims that let un-provisionable bundles
    ship. A docstring that is load-bearing is worth a test.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import duckdb

from elt_taskgen.export import eltbench, release, serve


class TestEnvironmentProvenance(unittest.TestCase):
    def test_runtime_versions_names_every_pinned_dependency(self):
        versions = release.runtime_versions()
        for key in (
            "python",
            "platform",
            "duckdb",
            "sqlglot",
            "pydantic",
            "pyyaml",
            "python-hcl2",
            "lark",
        ):
            self.assertIn(key, versions)
            self.assertTrue(versions[key])
        import duckdb as duckdb_mod

        self.assertEqual(versions["duckdb"], duckdb_mod.__version__)

    def test_locked_versions_parses_the_repo_lock(self):
        lock = release.repo_lock_path()
        if not lock.is_file():
            self.skipTest("uv.lock is not present in this checkout")
        locked = release.locked_versions(lock)
        self.assertEqual(
            sorted(locked),
            ["duckdb", "lark", "pydantic", "python-hcl2", "pyyaml", "sqlglot"],
        )

    def test_a_missing_lock_is_never_reported_as_clean(self):
        tmp = Path(tempfile.mkdtemp(prefix="elt-nolock-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        drift = release.environment_drift(tmp / "uv.lock")
        self.assertEqual(drift, {"uv.lock": ("missing", "")})

    def test_drift_is_empty_for_a_lock_matching_the_installed_versions(self):
        tmp = Path(tempfile.mkdtemp(prefix="elt-lock-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        installed = release.runtime_versions()
        lock = tmp / "uv.lock"
        lock.write_text(
            "\n".join(
                f'[[package]]\nname = "{name}"\nversion = "{installed[name]}"\n'
                for name in (
                    "duckdb",
                    "sqlglot",
                    "pydantic",
                    "pyyaml",
                    "python-hcl2",
                    "lark",
                )
            ),
            encoding="utf-8",
        )
        self.assertEqual(release.environment_drift(lock), {})
        # ... and a single moved pin is reported with both sides.
        lock.write_text(
            lock.read_text().replace(installed["duckdb"], "0.0.1"), encoding="utf-8"
        )
        self.assertEqual(
            release.environment_drift(lock),
            {"duckdb": (installed["duckdb"], "0.0.1")},
        )


class TestServeCollectors(unittest.TestCase):
    """A missing relation is ABSENT, not zero: `compare_stage1` must be able
    to say "table not found" rather than "expected 3 rows, got 0"."""

    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.execute('CREATE TABLE "customers" (id INTEGER)')
        self.con.execute('INSERT INTO "customers" VALUES (1), (2), (3)')

    def test_stage1_counts_omit_a_table_that_was_never_loaded(self):
        counts = serve.collect_stage1_counts(self.con, ["customers", "orders"])
        self.assertEqual(counts, {"customers": 3})

    def test_stage1_counts_read_the_named_schema(self):
        self.con.execute('CREATE SCHEMA "other"')
        self.con.execute('CREATE TABLE "other"."customers" (id INTEGER)')
        self.assertEqual(
            serve.collect_stage1_counts(
                self.con, ["customers"], source_schema="other"
            ),
            {"customers": 0},
        )

    def test_mart_rows_skip_an_unbuilt_mart_and_key_by_column(self):
        mart = serve.MartView(name="m", key_columns=("k",), columns=("k", "v"))
        missing = serve.MartView(name="gone", key_columns=("k",))
        self.con.execute('CREATE SCHEMA "main_db_dev"')
        self.con.execute('CREATE TABLE "main_db_dev"."m" (k INTEGER, v VARCHAR)')
        self.con.execute("INSERT INTO \"main_db_dev\".\"m\" VALUES (1, 'a')")
        rows = serve.collect_mart_rows(self.con, [mart, missing], "main_db_dev")
        self.assertEqual(rows, {"m": [{"k": 1, "v": "a"}]})
        self.assertNotIn("gone", rows)

    def test_identifiers_are_quote_escaped(self):
        self.assertEqual(serve._ident('we"ird'), 'we""ird')

    def test_the_default_mart_schema_is_the_dbt_duckdb_convention(self):
        self.assertEqual(
            serve.MART_SCHEMA_TEMPLATE.format(db="demo__x"), "main_demo__x_dev"
        )


class TestLoadBearingDocstrings(unittest.TestCase):
    def test_eltbench_states_the_upstream_path_map_not_an_overclaim(self):
        doc = eltbench.__doc__ or ""
        self.assertNotIn("THE LAYOUT IS THE REAL UPSTREAM ONE", doc)
        self.assertIn("PATH MAP", doc)
        for upstream_path in (
            "elt-bench/snowflake/<db>/",
            "elt-bench/schemas/<db>/<table>.csv",
            "evaluation/table.json[<db>]",
            "evaluation/sql/<db>/<mart>.sql",
            "agent_results/gt_<warehouse>/<db>/",
        ):
            self.assertIn(upstream_path, doc)

    def test_eltbench_documents_the_identifier_bound(self):
        doc = eltbench.__doc__ or ""
        self.assertIn("NAMING", doc)
        self.assertIn("DATABASE_NAME_MAX_LEN", doc)
        self.assertIn("config.yaml is therefore the AUTHORITATIVE", doc)
        # The constants the prose quotes are the constants in force.
        self.assertEqual(eltbench.DATABASE_NAME_MAX_LEN, 56)
        self.assertEqual(eltbench.S3_BUCKET_MAX_LEN, 63)
        self.assertEqual(
            eltbench.DATABASE_NAME_MAX_LEN,
            eltbench.S3_BUCKET_MAX_LEN - len("-bucket"),
        )

    def test_release_documents_the_certified_census_and_el_sources(self):
        doc = release.__doc__ or ""
        self.assertIn("BATTERY-CERTIFIED census", doc)
        self.assertIn("populations/<population>/rendered/", doc)
        self.assertIn("allow_unlocked_env", doc)

    def test_verify_release_docstring_no_longer_overstates_the_pin(self):
        doc = release.verify_release.__doc__ or ""
        self.assertNotIn("Every value, row, column and relation is still pinned", doc)
        self.assertIn("NULL distinct from ''", doc)

    def test_the_s3_single_object_parity_contract_is_recorded(self):
        """Public S3 paths match upstream; chunk assembly remains private."""
        doc = eltbench.__doc__ or ""
        self.assertIn("S3 SHAPE", doc)
        self.assertIn("s3://<bucket>/<table>.jsonl", doc)
        self.assertIn("concatenate every part", doc)


class TestVersionPins(unittest.TestCase):
    """Literal pins for the two versions that invalidate frozen artifacts.

    Every other owned test references these constants, so an accidental edit
    would move the whole suite with it and nothing would go red — while in the
    field the bump means "every existing release stops verifying" (census) or
    "the recorded rules a release is judged by changed" (schema). A pin is the
    only thing that turns such a change into a deliberate one.
    """

    def test_census_version_is_pinned(self):
        self.assertEqual(
            eltbench.CENSUS_VERSION,
            "2",
            "census algorithm changed: bump CENSUS_VERSION deliberately, "
            "update this pin, and expect every frozen release to need "
            "re-freezing (digests are per-version)",
        )
        self.assertEqual(release.WAREHOUSE_CHECKSUM_KIND, "duckdb-census/2")

    def test_release_schema_version_is_pinned(self):
        self.assertEqual(
            release.RELEASE_SCHEMA_VERSION,
            "3.5",
            "release manifest schema changed: update this pin and decide, "
            "explicitly, which verification floors move with it",
        )
        from elt_taskgen.semantic_contract import SEMANTIC_SCORER_VERSION

        self.assertEqual(
            SEMANTIC_SCORER_VERSION,
            "1.0.0",
            "semantic scorer contract changed: bump deliberately and retain "
            "a backward reader for releases that record the previous version",
        )

    def test_the_serving_surface_floor_is_not_the_writers_version(self):
        """Gating the serving-surface checks on `>= RELEASE_SCHEMA_VERSION`
        means the next bump silently stops checking every already-frozen
        release — verification shrinks instead of failing. The floor is
        pinned separately and compared numerically."""
        self.assertEqual(release._SERVING_SURFACE_MIN_SCHEMA, "2.1")
        # A future writer version must still be judged by the 2.1 floor.
        self.assertGreaterEqual(
            release._schema_tuple("2.2"),
            release._schema_tuple(release._SERVING_SURFACE_MIN_SCHEMA),
        )
        # ... and dotted versions compare as NUMBERS, not as strings.
        self.assertGreater(
            release._schema_tuple("2.10"), release._schema_tuple("2.9")
        )
        self.assertLess("2.10", "2.9")  # the trap this avoids
        self.assertLess(
            release._schema_tuple("2.0"),
            release._schema_tuple(release._SERVING_SURFACE_MIN_SCHEMA),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

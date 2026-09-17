"""WHY THIS EXISTS

Round 4 makes every vendored source runnable, which needed two additive edits
to the frozen shared IR (`models.py`) plus one new config surface:

  1. `Origin.SCHEMAPILE` / `Origin.WIKIDBS` — two new pools. Adding an enum
     MEMBER is the classic "obviously additive" change that is only additive
     until someone reorders the enum, gives a member a different value, or
     lets one leak into a serialized default. So: every Origin round-trips by
     VALUE (the wire format), the pre-existing members keep their exact
     spellings, and the pinned demo task content hash is re-asserted HERE as
     well as in test_models_round3.py — evidence binding across the whole
     corpus rests on that literal not moving.

  2. `PoolSelection` + `slugify_family` — the one contract the five source
     adapters build TaskIR identity from. Its validators are the licensing and
     namespacing gates (no blank license; no family id smuggled into another
     pool's namespace), so each one gets a test that proves it rejects.

  3. `catalog.py` + `config/sources.yaml` — pool roots/licenses/exclusions as
     data. The gates that matter are the fail-closed ones: an excluded record
     cannot be selected, and a per-record-license pool (SchemaPile) cannot
     produce a selection unless the caller states the record's own license.
"""

import unittest
from pathlib import Path

from pydantic import ValidationError

from elt_taskgen import catalog as catalog_mod
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.models import (
    FAMILY_SLUG_MAX_LEN,
    Origin,
    PoolSelection,
    TaskIR,
    slugify_family,
    task_from_json,
    task_to_json,
)

# Independent demo-identity pin. Update it with test_models_round3.py only
# after an intentional contract change.
DEMO_TASK_CONTENT_HASH = (
    "c2e0744cfc85657f6b1680600cc35ff9bfaf67a8548b21d9d8c57ea65d56ee44"
)


class TestOrigin(unittest.TestCase):
    def test_new_origins_exist_with_expected_values(self):
        self.assertEqual(Origin.SCHEMAPILE.value, "schemapile")
        self.assertEqual(Origin.WIKIDBS.value, "wikidbs")

    def test_pre_round4_origin_values_unchanged(self):
        """The wire spelling of every pre-existing pool is frozen."""
        self.assertEqual(
            {
                Origin.DBT.value,
                Origin.SYNSQL.value,
                Origin.DLT.value,
                Origin.ELTBENCH_ANCHOR.value,
                Origin.SYNTHETIC.value,
                Origin.DEMO.value,
            },
            {"dbt", "synsql", "dlt", "eltbench_anchor", "synthetic", "demo"},
        )

    # (An enum value-round-trip test was removed: assertIs(
    # Origin(o.value), o) holds for ANY str-Enum with no _missing_ override —
    # demonstrated true on a deliberately broken throwaway enum. The renaming
    # hazard the class docstring names is pinned by the member tests above.)

    def test_task_ir_round_trips_through_a_new_origin(self):
        task = demo_task().model_copy(
            update={"origin": Origin.WIKIDBS, "family_id": "wikidbs__some_db"}
        )
        restored = task_from_json(task_to_json(task))
        self.assertIs(restored.origin, Origin.WIKIDBS)
        self.assertEqual(restored.content_hash(), task.content_hash())


class TestHashStabilityRound4(unittest.TestCase):
    """Two new enum members and a new value object must not move identity."""

    def test_demo_task_content_hash_unchanged(self):
        self.assertEqual(demo_task().content_hash(), DEMO_TASK_CONTENT_HASH)

    def test_a_different_origin_is_a_different_hash(self):
        """Origin is semantic: it participates in the content hash."""
        task = demo_task()
        moved = task.model_copy(update={"origin": Origin.SCHEMAPILE})
        self.assertNotEqual(task.content_hash(), moved.content_hash())


class TestSlugifyFamily(unittest.TestCase):
    def test_real_record_names(self):
        for raw, expected in (
            ("dbt_netsuite", "dbt_netsuite"),
            ("dlt_google_analytics", "dlt_google_analytics"),
            ("000002_create_tables.sql", "000002_create_tables_sql"),
            ("00042 Some Db Name", "00042_some_db_name"),
            ("  Trailing/Slashes//  ", "trailing_slashes"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(slugify_family(raw), expected)

    def test_slug_never_contains_the_family_separator(self):
        """'__' inside a segment would break pool__family parsing."""
        self.assertNotIn("__", slugify_family("a -- b // c"))

    def test_non_latin_name_gets_a_deterministic_token(self):
        slug = slugify_family("数据库")
        self.assertTrue(slug.startswith("x"))
        self.assertEqual(slug, slugify_family("数据库"))
        self.assertNotEqual(slug, slugify_family("其他"))

    def test_long_name_is_truncated_but_stays_distinguishable(self):
        a = slugify_family("x" * 200 + " alpha")
        b = slugify_family("x" * 200 + " beta")
        self.assertLessEqual(len(a), FAMILY_SLUG_MAX_LEN)
        self.assertNotEqual(a, b)

    def test_slug_is_always_a_valid_family_segment(self):
        for raw in ("dbt_netsuite", "00042 Some Db", "数据库", "-- ..", "x" * 300):
            with self.subTest(raw=raw):
                sel = PoolSelection(
                    pool="wikidbs",
                    selector=raw,
                    origin=Origin.WIKIDBS,
                    family_id=f"wikidbs__{slugify_family(raw)}",
                    license="CC-BY-4.0",
                )
                self.assertTrue(sel.family_id.startswith("wikidbs__"))


class TestPoolSelection(unittest.TestCase):
    def _sel(self, **over) -> PoolSelection:
        kw = dict(
            pool="schemapile",
            selector="000002_create_tables.sql",
            origin=Origin.SCHEMAPILE,
            license="apache-2.0",
            attribution="SchemaPile (permissive subset)",
        )
        kw.update(over)
        return PoolSelection.for_record(**kw)

    def test_round_trip(self):
        sel = self._sel()
        self.assertEqual(PoolSelection.model_validate_json(sel.model_dump_json()), sel)

    def test_family_id_derived_from_selector(self):
        self.assertEqual(self._sel().family_id, "schemapile__000002_create_tables_sql")

    def test_explicit_family_groups_records(self):
        a = self._sel(selector="record_a.sql", family="shared pool family")
        b = self._sel(selector="record_b.sql", family="shared pool family")
        self.assertEqual(a.family_id, b.family_id)
        self.assertNotEqual(a.selector, b.selector)

    def test_selector_is_kept_verbatim(self):
        raw = "00042 Some Db Name"
        self.assertEqual(self._sel(selector=raw).selector, raw)

    def test_blank_license_rejected(self):
        """Unlicensed material never enters the corpus by omission."""
        with self.assertRaises(ValidationError) as ctx:
            self._sel(license="   ")
        self.assertIn("license", str(ctx.exception))

    def test_family_id_outside_the_pool_namespace_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            PoolSelection(
                pool="wikidbs",
                selector="rec",
                origin=Origin.WIKIDBS,
                family_id="dbt__netsuite",  # another pool's namespace
                license="CC-BY-4.0",
            )
        self.assertIn("namespace", str(ctx.exception))

    def test_unnamespaced_family_id_rejected(self):
        with self.assertRaises(ValidationError):
            PoolSelection(
                pool="wikidbs",
                selector="rec",
                origin=Origin.WIKIDBS,
                family_id="wikidbs_rec",  # single underscore: not namespaced
                license="CC-BY-4.0",
            )

    def test_bad_pool_token_rejected(self):
        for bad in ("WikiDBs", "wiki dbs", "_leading", ""):
            with self.subTest(pool=bad):
                with self.assertRaises(ValidationError):
                    PoolSelection(
                        pool=bad,
                        selector="rec",
                        origin=Origin.WIKIDBS,
                        family_id="wikidbs__rec",
                        license="CC-BY-4.0",
                    )

    def test_pool_token_may_not_contain_the_separator(self):
        """'a__b' would make 'pool__family' ambiguous."""
        with self.assertRaises(ValidationError) as ctx:
            PoolSelection(
                pool="a__b",
                selector="rec",
                origin=Origin.WIKIDBS,
                family_id="a__b__rec",
                license="CC-BY-4.0",
            )
        self.assertIn("pool", str(ctx.exception))

    def test_ir_identity_builds_a_valid_task(self):
        """The one bridge into the IR: kwargs an adapter splices into TaskIR."""
        sel = self._sel()
        demo = demo_task()
        task = TaskIR(
            task_id="schemapile__t1",
            cluster_id="schemapile__c1",
            **sel.ir_identity(),
            tables=demo.tables,
            relationships=demo.relationships,
            backends=demo.backends,
            marts=demo.marts,
        )
        self.assertEqual(task.family_id, sel.family_id)
        self.assertIs(task.origin, Origin.SCHEMAPILE)
        self.assertEqual(task.license, "apache-2.0")
        self.assertEqual(task.attribution, sel.attribution)


class TestSourceCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = catalog_mod.load_source_catalog()

    def test_repo_catalog_covers_every_ingestible_pool(self):
        self.assertEqual(
            set(self.catalog.pool_names()),
            {"dbt", "dlt", "synsql", "schemapile", "wikidbs", "eltbench"},
        )

    def test_catalog_pool_keys_match_the_family_namespaces_in_use(self):
        """The pool key IS the family-id namespace of the adapter that owns it.

        adapters/eltbench_anchor.py emits 'eltbench__<db>' (Origin is
        ELTBENCH_ANCHOR, the namespace is not) — a catalog key of
        'eltbench_anchor' would silently move 100 anchor family ids and every
        contamination fingerprint derived from them.
        """
        anchors = self.catalog.pool("eltbench")
        self.assertIs(anchors.origin, Origin.ELTBENCH_ANCHOR)
        self.assertEqual(anchors.selection("flight").family_id, "eltbench__flight")
        for pool, expected in (("dbt", "dbt"), ("dlt", "dlt"), ("synsql", "synsql")):
            with self.subTest(pool=pool):
                self.assertTrue(
                    self.catalog.pool(pool).selection("rec").family_id.startswith(
                        f"{expected}__"
                    )
                )

    # (A "row names a real Origin" test was removed:
    # PoolSource.origin is a validated pydantic field and the loader raises
    # "names unknown origin", so no LOADED row can hold a non-member; the
    # fail-closed path is pinned by test_unknown_origin_in_config_fails_closed.)

    def test_licensed_pools_produce_a_selection(self):
        sel = self.catalog.pool("wikidbs").selection("00042 Some Db Name")
        self.assertIs(sel.origin, Origin.WIKIDBS)
        self.assertEqual(sel.license, "CC-BY-4.0")
        self.assertTrue(sel.family_id.startswith("wikidbs__"))

    def test_per_record_license_pool_refuses_a_bare_selection(self):
        with self.assertRaises(ValueError) as ctx:
            self.catalog.pool("schemapile").selection("000002_create_tables.sql")
        self.assertIn("per-record", str(ctx.exception))

    def test_per_record_license_pool_accepts_the_records_license(self):
        sel = self.catalog.pool("schemapile").selection(
            "000002_create_tables.sql", license="apache-2.0"
        )
        self.assertEqual(sel.license, "apache-2.0")

    def test_excluded_record_cannot_be_ingested(self):
        """The `excluded:` machinery, exercised on dlt — the pool that still
        uses it. dbt's carve-out was retired; see the companion
        test below and ContaminationBlocksStripeAndZendeskTest for the guard
        that replaced it."""
        for name in ("dlt_shopify", "dlt_salesforce"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as ctx:
                    self.catalog.pool("dlt").selection(name)
                self.assertIn("excluded", str(ctx.exception))

    def test_dbt_pool_no_longer_carves_out_stripe_and_zendesk(self):
        """Retiring the carve-out must not silently re-add itself."""
        self.assertEqual(self.catalog.pool("dbt").excluded, ())

    def test_unknown_pool_and_instrument_fail_closed(self):
        with self.assertRaises(KeyError):
            self.catalog.pool("nope")
        with self.assertRaises(KeyError):
            self.catalog.instrument("nope")

    def test_instruments_are_paths(self):
        self.assertIsInstance(self.catalog.instrument("wikidbgraph_edges"), Path)

    def test_missing_config_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            catalog_mod.load_source_catalog(Path("/nonexistent/sources.yaml"))

    def test_unknown_pool_key_fails_closed(self):
        """A typo'd `excluded:`/`license_per_record:` would fail OPEN if dropped."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.yaml"
            path.write_text(
                "pools:\n  wikidbs:\n    origin: wikidbs\n    root: /tmp\n"
                "    license: CC-BY-4.0\n    exclude: [a]\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                catalog_mod.load_source_catalog(path)
            self.assertIn("exclude", str(ctx.exception))

    def test_unknown_origin_in_config_fails_closed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.yaml"
            path.write_text(
                "pools:\n  nope:\n    origin: not_a_pool\n    root: /tmp\n"
                "    license: X\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                catalog_mod.load_source_catalog(path)
            self.assertIn("unknown origin", str(ctx.exception))

    def test_env_var_overrides_the_pinned_root(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.yaml"
            path.write_text(
                "pools:\n  wikidbs:\n    origin: wikidbs\n"
                "    root: ${ELT_TASKGEN_DATA_ROOT}/WikiDBs\n"
                "    license: CC-BY-4.0\n",
                encoding="utf-8",
            )
            prior = os.environ.get("ELT_TASKGEN_DATA_ROOT")
            os.environ["ELT_TASKGEN_DATA_ROOT"] = "/elsewhere"
            try:
                cat = catalog_mod.load_source_catalog(path)
            finally:
                if prior is None:
                    del os.environ["ELT_TASKGEN_DATA_ROOT"]
                else:
                    os.environ["ELT_TASKGEN_DATA_ROOT"] = prior
            self.assertEqual(cat.pool("wikidbs").root, "/elsewhere/WikiDBs")

    def test_pool_row_rejects_contradictory_license_terms(self):
        with self.assertRaises(ValidationError):
            catalog_mod.PoolSource(
                pool="p", origin=Origin.WIKIDBS, root="/tmp", license_per_record=True,
                license="CC-BY-4.0",
            )
        with self.assertRaises(ValidationError):
            catalog_mod.PoolSource(pool="p", origin=Origin.WIKIDBS, root="/tmp")

    def test_selection_for_helper(self):
        sel = catalog_mod.selection_for(
            "synsql", "retail_orders_0042", catalog=self.catalog
        )
        self.assertEqual(sel.family_id, "synsql__retail_orders_0042")
        self.assertEqual(sel.license, "Apache-2.0")

    def test_attribution_names_the_record(self):
        text = self.catalog.pool("dlt").attribution_for("dlt_chess")
        self.assertIn("dlt_chess", text)


if __name__ == "__main__":
    unittest.main()

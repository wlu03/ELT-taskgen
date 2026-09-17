"""Tests for tools/extract_dlt_manifest.py and the real dlt manifests it emits.

WHY THIS EXISTS
The dlt pool's material is Python source, and the extractor is the only thing
standing between third-party connector code and the corpus. Three properties
have to hold or the pool is untrustworthy:

  1. the extractor reads code as DATA — a connector whose module body raises on
     import must still extract, which is the executable proof that nothing is
     imported (the fixture connector below raises at module scope on purpose);
  2. what it resolves is what dlt would do — resource names created in a loop
     over a settings constant, transformer parent edges (both `data_from=` and
     the `parent | dlt.transformer(...)` pipe form), incremental cursors, and
     `selected=False` resources that are extracted but never loaded;
  3. it fabricates nothing — a keyless `merge` is downgraded with a recorded
     note instead of getting an invented key, and a connector whose resources
     are runtime-defined comes out empty and fails closed at ingest.

The committed manifests under config/dlt_connectors/ are tested against the
same contract so a re-extraction that silently loses a connector is caught.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from elt_taskgen.adapters import dlt as dlt_adapter
from elt_taskgen.catalog import load_source_catalog
from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.generation import populations as pops
from elt_taskgen.generation.difficulty_profiles import (
    CHALLENGING_DIFFICULTY_PROFILE,
)
from elt_taskgen.models import Backend, ColumnType, Origin, PopulationName
from elt_taskgen.verification import contamination as cont

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = REPO_ROOT / "config" / "dlt_connectors"
TOOL_PATH = REPO_ROOT / "tools" / "extract_dlt_manifest.py"


def _load_tool():
    """Import the tool by path (tools/ is not a package on sys.path)."""
    spec = importlib.util.spec_from_file_location("extract_dlt_manifest", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves types via sys.modules
    spec.loader.exec_module(module)
    return module


extract_tool = _load_tool()


# A connector that EXPLODES at import time on purpose: the extractor must read
# it anyway. It exercises every construct used by the task-manifest subset.
FIXTURE_SETTINGS = '''\
DEFAULT_ENDPOINTS = ["widgets", "gadgets"]
DETAIL_ENDPOINTS = {"orders": ("lines", "shipments")}
'''

FIXTURE_INIT = '''\
"""Fixture connector: never imported, only parsed."""
import dlt

from .settings import DEFAULT_ENDPOINTS, DETAIL_ENDPOINTS

raise RuntimeError("importing this connector must never happen")


@dlt.source(name="fixture")
def fixture_source(
    api_key: str = dlt.secrets.value,
    domain: str = dlt.config.value,
    load_details: bool = False,
):
    resources = {}
    for endpoint in DEFAULT_ENDPOINTS:

        @dlt.resource(name=endpoint, write_disposition="replace", primary_key="id")
        def endpoint_resource(endpoint: str = endpoint):
            yield from client.get_pages(endpoint=endpoint, per_page=100)

        resources[endpoint] = endpoint_resource
        yield endpoint_resource

    @dlt.resource(name="orders", primary_key="id", write_disposition="merge")
    def orders(
        updated_at=dlt.sources.incremental("updated_at", initial_value="2020-01-01"),
    ):
        yield from client.get_pages(endpoint="orders", per_page=100)

    yield orders

    order_resources = {"orders": orders}
    if load_details:
        for sub in DETAIL_ENDPOINTS["orders"]:
            yield order_resources["orders"] | dlt.transformer(
                name=f"orders_{sub}", write_disposition="append"
            )(_get_details)("orders", sub)

    yield accounts_resource
    yield account_notes
    yield snapshot
    yield internal_state


def _get_details(page, main, sub):
    yield page


@dlt.resource(name="accounts", primary_key="id", write_disposition="replace")
def accounts_resource():
    yield from client.get_pages(endpoint="accounts", per_page=50)


@dlt.transformer(
    data_from=accounts_resource,
    name="account_notes",
    write_disposition="append",
    primary_key="id",
    columns={"body": {"data_type": "json"}},
)
def account_notes(page):
    yield page


@dlt.resource(write_disposition="merge")
def snapshot():
    yield []


@dlt.resource(selected=False, write_disposition="replace")
def internal_state():
    yield {}
'''


def _write_fixture(root: Path) -> Path:
    source = root / "dlt_fixture" / "source"
    source.mkdir(parents=True)
    (source / "settings.py").write_text(FIXTURE_SETTINGS, encoding="utf-8")
    (source / "__init__.py").write_text(FIXTURE_INIT, encoding="utf-8")
    tests = root / "dlt_fixture" / "tests"
    tests.mkdir()
    # A tests/ tree must be ignored: it also defines dlt resources.
    (tests / "test_fixture.py").write_text(
        "import dlt\n\n\n@dlt.resource(name='never_a_task')\ndef fake():\n    yield 1\n",
        encoding="utf-8",
    )
    return source


class TestAstExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        source = _write_fixture(root)
        cls.extract = extract_tool.extract_connector("dlt_fixture", source)
        cls.by_name = {e.name: e for e in cls.extract.endpoints}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_connector_is_parsed_not_imported(self):
        # The fixture raises at module scope; extraction still succeeded, and
        # the module never entered sys.modules.
        self.assertNotIn("dlt_fixture", sys.modules)
        self.assertTrue(self.extract.endpoints)

    def test_loop_over_settings_constant_expands_to_named_resources(self):
        self.assertIn("widgets", self.by_name)
        self.assertIn("gadgets", self.by_name)
        self.assertEqual(self.by_name["widgets"].write_disposition, "replace")
        self.assertEqual(self.by_name["widgets"].primary_key, ("id",))

    def test_fstring_names_expand_from_a_dict_constant(self):
        self.assertIn("orders_lines", self.by_name)
        self.assertIn("orders_shipments", self.by_name)

    def test_pipe_transformer_takes_its_parent(self):
        for child in ("orders_lines", "orders_shipments"):
            self.assertEqual(self.by_name[child].parent, "orders")
            self.assertEqual(self.by_name[child].kind, "transformer")

    def test_data_from_transformer_takes_its_parent(self):
        notes = self.by_name["account_notes"]
        self.assertEqual(notes.parent, "accounts")
        self.assertEqual(notes.kind, "transformer")
        self.assertEqual(notes.column_hints, {"body": "json"})

    def test_incremental_cursor_from_parameter_default(self):
        self.assertEqual(self.by_name["orders"].cursor, "updated_at")
        self.assertEqual(self.by_name["orders"].write_disposition, "merge")

    def test_keyless_merge_is_downgraded_not_invented(self):
        snap = self.by_name["snapshot"]
        self.assertEqual(snap.primary_key, ())
        self.assertEqual(snap.write_disposition, "append")
        self.assertEqual(snap.declared_write_disposition, "merge")
        self.assertTrue(any("keyless merge" in n for n in snap.notes))

    def test_unselected_resource_is_kept_and_flagged(self):
        self.assertFalse(self.by_name["internal_state"].selected)

    def test_guarded_resources_record_their_guard(self):
        """Resources defined under `if load_details:` (workable's pattern) are
        recorded WITH the guard and a note naming the source parameter's
        default; unconditional resources carry no guard."""
        lines = self.by_name["orders_lines"]
        self.assertEqual(lines.guard, "load_details")
        self.assertTrue(
            any("default is False" in n and "does not load" in n for n in lines.notes),
            lines.notes,
        )
        self.assertEqual(self.by_name["orders_shipments"].guard, "load_details")
        self.assertEqual(self.by_name["orders"].guard, "")
        self.assertEqual(self.by_name["accounts"].guard, "")
        # The guard reaches the rendered manifest.
        pool = load_source_catalog().pool("dlt")
        doc = extract_tool.manifest_document(self.extract, pool)
        rows = {e["name"]: e for e in doc["endpoints"]}
        self.assertEqual(rows["orders_lines"]["guard"], "load_details")
        self.assertNotIn("guard", rows["orders"])
        self.assertEqual(doc["extractor_version"], "3")

    def test_duplicate_resource_name_is_recorded(self):
        """A second `@dlt.resource` under an existing name is not silently
        dropped: first definition wins and BOTH the kept endpoint's notes and
        `unresolved` say so (jira defines `issues` twice)."""
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "source"
            source.mkdir()
            (source / "__init__.py").write_text(
                "import dlt\n\n\n"
                "@dlt.source\n"
                "def one():\n"
                "    yield issues\n\n\n"
                "@dlt.resource(name='issues', primary_key='id')\n"
                "def issues():\n"
                "    yield {}\n\n\n"
                "@dlt.resource(name='issues', primary_key='key')\n"
                "def issues_again():\n"
                "    yield {}\n",
                encoding="utf-8",
            )
            extract = extract_tool.extract_connector("dlt_dup", source)
        by_name = {e.name: e for e in extract.endpoints}
        self.assertEqual(by_name["issues"].primary_key, ("id",))
        self.assertTrue(
            any("duplicate resource name 'issues'" in n for n in by_name["issues"].notes),
            by_name["issues"].notes,
        )
        self.assertTrue(
            any("duplicate resource name 'issues'" in u.reason for u in extract.unresolved)
        )

    def test_pagination_and_path_signals(self):
        self.assertTrue(self.by_name["widgets"].paginated)
        self.assertEqual(self.by_name["accounts"].path, "/accounts")
        self.assertEqual(self.by_name["accounts"].path_source, "literal")

    def test_auth_names_extracted(self):
        self.assertEqual(self.extract.secrets, ["api_key"])
        self.assertEqual(self.extract.config, ["domain"])

    def test_tests_directory_is_never_read(self):
        self.assertNotIn("never_a_task", self.by_name)
        self.assertTrue(all("tests" not in f for f in self.extract.files))

    def test_extraction_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            source = _write_fixture(Path(d))
            again = extract_tool.extract_connector("dlt_fixture", source)
        pool = load_source_catalog().pool("dlt")
        first = extract_tool.render_manifest(
            extract_tool.manifest_document(self.extract, pool)
        )
        second = extract_tool.render_manifest(
            extract_tool.manifest_document(again, pool)
        )
        self.assertEqual(first, second)

    def test_unparseable_connector_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "source"
            source.mkdir()
            (source / "__init__.py").write_text("def (:\n", encoding="utf-8")
            with self.assertRaises(extract_tool.ExtractionError):
                extract_tool.extract_connector("dlt_broken", source)

    def test_missing_source_dir_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(extract_tool.ExtractionError):
                extract_tool.extract_connector("dlt_gone", Path(d) / "nope")


class TestFixtureManifestToTaskIr(unittest.TestCase):
    """The fixture's extracted manifest must round-trip into a valid TaskIR."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        source = _write_fixture(root)
        extract = extract_tool.extract_connector("dlt_fixture", source)
        pool = load_source_catalog().pool("dlt")
        doc = extract_tool.manifest_document(extract, pool)
        path = root / "fixture.yaml"
        path.write_text(extract_tool.render_manifest(doc), encoding="utf-8")
        cls.manifest = dlt_adapter.load_connector(path)
        cls.task = dlt_adapter.to_task_ir(cls.manifest)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_identity_license_and_origin(self):
        self.assertIs(self.task.origin, Origin.DLT)
        self.assertEqual(self.task.family_id, "dlt__fixture")
        self.assertEqual(self.task.license, "Apache-2.0")
        self.assertIn("dlt", self.task.attribution)

    def test_unselected_resource_is_not_a_source_table(self):
        names = {t.name for t in self.task.tables}
        self.assertNotIn("internal_state", names)
        self.assertEqual(
            names,
            {
                "widgets", "gadgets", "orders", "orders_lines", "orders_shipments",
                "accounts", "account_notes", "snapshot",
            },
        )

    def test_backend_rotation_spreads_pagination_but_keeps_rest_represented(self):
        # Paginated resources may rotate, but at least one stays on REST.
        # Non-paginated resources never use REST, and assignment is deterministic.
        paginated = {
            e.name for e in self.manifest.loadable() if e.paginated
        }
        by_table = {b.table: b.backend for b in self.task.backends}
        self.assertTrue(
            any(by_table[name] is Backend.REST for name in paginated)
        )
        self.assertIn(
            self.task.backend_for("snapshot").backend, dlt_adapter._NON_REST_BACKENDS
        )
        self.assertIsNot(self.task.backend_for("snapshot").backend, Backend.REST)
        again = dlt_adapter.to_task_ir(self.manifest)
        self.assertEqual(
            by_table, {b.table: b.backend for b in again.backends}
        )
        opts = self.task.backend_for("orders").options
        self.assertEqual(opts["cursor"], "updated_at")
        self.assertEqual(opts["write_disposition"], "merge")
        self.assertEqual(opts["auth_secrets"], "api_key")

    def test_parent_edges_become_foreign_keys_with_synthesized_link(self):
        edges = {
            (r.child_table, r.child_columns[0], r.parent_table)
            for r in self.task.relationships
        }
        self.assertIn(("orders_lines", "_orders_id", "orders"), edges)
        self.assertIn(("account_notes", "_accounts_id", "accounts"), edges)
        self.assertTrue(all(r.required for r in self.task.relationships))
        link = self.task.table("orders_lines").column("_orders_id")
        self.assertIs(link.type, ColumnType.BIGINT)
        self.assertFalse(link.nullable)

    def test_marts_come_from_the_entity_graph(self):
        # No extraction_summary: a count-echo mart duplicates the extract-load
        # reward and is CONSTANT across the memorization pair by design, so
        # the data-sensitivity gate rejects any task carrying it (measured
        # live on personio). Only real transform marts are graded.
        names = [m.name for m in self.task.marts]
        self.assertEqual(names, ["dim_orders", "orders_activity"])
        dim = self.task.mart("dim_orders")
        self.assertEqual(dim.key_columns, ("id",))
        cols = {c.name for c in dim.columns}
        self.assertIn("orders_lines_count", cols)
        self.assertIn("last_updated_at", cols)
        activity = self.task.mart("orders_activity")
        self.assertEqual(activity.key_columns, ("activity_date",))

    def test_challenging_profile_emits_no_thin_legacy_mart(self):
        task = dlt_adapter.to_task_ir(
            self.manifest,
            difficulty_profile=CHALLENGING_DIFFICULTY_PROFILE,
        )
        self.assertTrue(task.marts)
        self.assertTrue(all(len(mart.columns) >= 6 for mart in task.marts))
        # This fixture's child has only its link plus JSON payload, so the
        # optional dimension is omitted instead of being padded to six.
        self.assertNotIn("dim_orders", {mart.name for mart in task.marts})
        activity = task.mart("orders_activity")
        self.assertIn("distinct_record_count", {c.name for c in activity.columns})

    def test_column_hint_lands_in_the_synthesized_schema(self):
        body = self.task.table("account_notes").column("body")
        self.assertIs(body.type, ColumnType.JSON)


class TestExcludedConnectorGuard(unittest.TestCase):
    """Vendored held-out families must remain source-only audit material."""

    EXCLUDED = (
        "asana", "facebook_ads", "github", "google_ads", "hubspot", "jira",
        "salesforce", "shopify", "stripe", "zendesk",
    )

    def test_vendored_set_contains_no_excluded_connector(self):
        stems = {p.stem for p in dlt_adapter.list_manifests(MANIFEST_DIR)}
        self.assertTrue(stems.isdisjoint(set(self.EXCLUDED)))

    def test_extractor_refuses_a_denylisted_connector_name(self):
        for name in self.EXCLUDED:
            with self.subTest(name=name):
                with self.assertRaises(extract_tool.ExtractionError):
                    extract_tool.assert_not_denylisted(name)
        extract_tool.assert_not_denylisted("freshdesk")  # must NOT raise

    def test_upstream_packaging_aliases_cannot_bypass_family_guard(self):
        aliases = {
            "dlt_asana_dlt": "asana",
            "dlt_shopify_dlt": "shopify",
            "dlt_stripe_analytics": "stripe",
        }
        for record, expected in aliases.items():
            with self.subTest(record=record):
                slug = extract_tool.connector_slug(record)
                self.assertEqual(slug, expected)
                with self.assertRaises(extract_tool.ExtractionError):
                    extract_tool.assert_not_denylisted(slug)

    def test_targeted_catalog_exclusion_does_not_hide_contamination(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = extract_tool.main(["--connector", "dlt_zendesk", "--dry-run"])
        self.assertEqual(rc, 2)
        self.assertIn("contamination deny lists", stderr.getvalue())

    def test_clean_excluded_manifest_can_be_regenerated_for_review(self):
        with tempfile.TemporaryDirectory() as d:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = extract_tool.main(
                    [
                        "--connector", "dlt_freshdesk", "--include-excluded",
                        "--out", d, "--quiet",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertTrue((Path(d) / "freshdesk.yaml").is_file())
            self.assertNotIn("excluded by the source catalog", stderr.getvalue())

    def test_contamination_pre_check_rejects_a_denylisted_manifest(self):
        raw = {
            "connector": "zendesk",
            "license": "Apache-2.0",
            "endpoints": [
                {
                    "name": "tickets",
                    "path": "/tickets",
                    "primary_key": ["id"],
                    "cursor": "updated_at",
                    "write_disposition": "merge",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "zendesk.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            task = dlt_adapter.to_task_ir(dlt_adapter.load_connector(path))
            index_dir = Path(d) / "index"
            idx = cont.ContaminationIndex(index_dir)
            idx.add_benchmark(
                "spider2_dbt",
                sorted(
                    f"family:{cont.normalize_name(f)}"
                    for f in cont.SPIDER2_DBT_FAMILIES
                ),
            )
            with self.assertRaises(ValueError) as ctx:
                dlt_adapter.assert_uncontaminated(task, index_dir)
            self.assertIn("family:zendesk", str(ctx.exception))

    def test_unarmed_index_is_a_failure_not_a_pass(self):
        raw = {
            "connector": "brand_new",
            "license": "Apache-2.0",
            # cursor funds the activity mart: a graded task must carry a REAL
            # transform mart (extraction_summary is no longer emitted).
            "endpoints": [
                {
                    "name": "things",
                    "path": "/things",
                    "primary_key": ["id"],
                    "cursor": "updated_at",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            task = dlt_adapter.to_task_ir(dlt_adapter.load_connector(path))
            with self.assertRaises(ValueError):
                dlt_adapter.assert_uncontaminated(task, Path(d) / "empty_index")


class TestCommittedManifests(unittest.TestCase):
    """The 12 task manifests, against the full 29-source vendored pool."""

    EXPECTED = {
        "airtable", "chess", "freshdesk", "google_analytics", "matomo", "mux",
        "notion", "personio", "pipedrive", "slack", "strapi", "workable",
    }
    #: Resources are defined at runtime; these must fail closed, not guess.
    RUNTIME_DEFINED = {"airtable", "strapi"}

    def test_every_vendored_connector_has_a_manifest(self):
        stems = {p.stem for p in dlt_adapter.list_manifests(MANIFEST_DIR)}
        self.assertEqual(stems, self.EXPECTED)

    def test_runtime_defined_connectors_fail_closed(self):
        for name in sorted(self.RUNTIME_DEFINED):
            with self.subTest(name=name):
                with self.assertRaises(dlt_adapter.NoStaticResources) as ctx:
                    dlt_adapter.load_connector(MANIFEST_DIR / f"{name}.yaml")
                self.assertIn("declares no endpoints", str(ctx.exception))

    #: Blocker-2 X1: EL-unrewardable connectors the source catalog refuses.
    #: Their manifests stay loadable (extractor regression fixtures); the
    #: catalog is the single enforcement point, so this test asserts BOTH
    #: halves — the nine refuse, the three survivors still build a TaskIR.
    INGESTIBLE = {"personio", "pipedrive", "workable"}

    def test_task_extraction_and_raw_inventory_have_separate_discovery_scopes(self):
        pool = load_source_catalog().pool("dlt")
        self.assertEqual(
            extract_tool.discover_records(pool),
            [f"dlt_{name}" for name in sorted(self.INGESTIBLE)],
        )
        all_vendored = extract_tool.discover_records(pool, include_excluded=True)
        self.assertEqual(len(all_vendored), 29)
        self.assertEqual(set(all_vendored), {p.name for p in pool.root_path().glob("dlt_*")})

    def test_every_other_connector_becomes_a_valid_task(self):
        pool = load_source_catalog().pool("dlt")
        loaded = dlt_adapter.load_all_connectors(MANIFEST_DIR)
        self.assertEqual(
            {m.connector for _, m in loaded}, self.EXPECTED - self.RUNTIME_DEFINED
        )
        for _, manifest in loaded:
            with self.subTest(connector=manifest.connector):
                if manifest.connector not in self.INGESTIBLE:
                    with self.assertRaises(ValueError) as ctx:
                        pool.selection(
                            manifest.selector,
                            attribution=manifest.attribution,
                            family=manifest.connector,
                        )
                    self.assertIn("excluded", str(ctx.exception))
                    continue
                selection = pool.selection(
                    manifest.selector,
                    attribution=manifest.attribution,
                    family=manifest.connector,
                )
                task = dlt_adapter.to_task_ir(manifest, selection=selection)
                self.assertIs(task.origin, Origin.DLT)
                self.assertEqual(task.family_id, f"dlt__{manifest.connector}")
                self.assertEqual(task.license, "Apache-2.0")
                self.assertEqual(len(task.backends), len(task.tables))
                # Every ingestible connector must fund at least one REAL
                # transform mart (count-echo summaries are no longer graded).
                self.assertTrue(task.marts)
                self.assertNotIn(
                    "extraction_summary", {m.name for m in task.marts}
                )

    def test_excluded_records_cover_the_whole_denylist(self):
        """The catalog's denylist and this suite's INGESTIBLE set must agree —
        a connector added to one and not the other would silently change which
        tasks the corpus admits."""
        pool = load_source_catalog().pool("dlt")
        manifested_records = {f"dlt_{c}" for c in self.EXPECTED}
        self.assertEqual(
            set(pool.excluded) & manifested_records,
            {f"dlt_{c}" for c in self.EXPECTED - self.INGESTIBLE},
        )

    def test_manifests_carry_pinned_provenance(self):
        for _, manifest in dlt_adapter.load_all_connectors(MANIFEST_DIR):
            with self.subTest(connector=manifest.connector):
                self.assertTrue(manifest.record.startswith("dlt_"))
                self.assertEqual(len(manifest.commit), 40)
                self.assertIn("dlt-hub/verified-sources", manifest.upstream)
                self.assertEqual(
                    manifest.source_dir, f"{manifest.record}/source"
                )

    def test_freshdesk_shape(self):
        m = dlt_adapter.load_connector(MANIFEST_DIR / "freshdesk.yaml")
        self.assertEqual(
            [e.name for e in m.endpoints],
            ["agents", "companies", "contacts", "groups", "roles", "tickets"],
        )
        for ep in m.endpoints:
            self.assertEqual(ep.primary_key, ("id",))
            self.assertEqual(ep.cursor, "updated_at")
            self.assertEqual(ep.write_disposition, "merge")
            self.assertTrue(ep.paginated)
        self.assertEqual(m.auth.secrets, ("api_secret_key", "domain"))
        task = dlt_adapter.to_task_ir(m)
        self.assertEqual(len(task.tables), 6)
        # All six endpoints are paginated; the rotation spreads them over the
        # backends but must keep `rest` represented (the page under-count
        # hazard is the pool's one silent Layer-2 hazard).
        self.assertIn(Backend.REST, {b.backend for b in task.backends})

    def test_pipedrive_shape(self):
        m = dlt_adapter.load_connector(MANIFEST_DIR / "pipedrive.yaml")
        names = {e.name for e in m.endpoints}
        self.assertIn("deals", names)
        self.assertIn("deals_flow", names)
        self.assertIn("deals_participants", names)
        self.assertEqual(m.endpoint("deals_flow").parent, "deals")
        self.assertEqual(m.endpoint("deals").cursor, "update_time|modified")
        self.assertEqual(m.endpoint("deals").cursor_column, "update_time")
        self.assertFalse(m.endpoint("create_state").selected)
        task = dlt_adapter.to_task_ir(m)
        self.assertNotIn("create_state", {t.name for t in task.tables})
        self.assertEqual(
            {(r.child_table, r.parent_table) for r in task.relationships},
            {("deals_flow", "deals"), ("deals_participants", "deals")},
        )
        # Blocker-2 T3: the curated columns on deals / deals_flow /
        # deals_participants funds two semantically distinct library programs:
        # a filtered measure-state distribution and an argmax profile.  Both
        # lead the hand-built entity/activity marts.
        self.assertEqual(
            [mart.name for mart in task.marts],
            [
                "deals_participants_distribution",
                "deals_participants_top",
                "dim_deals",
                "deals_activity",
            ],
        )


class TestIngestDltCommand(unittest.TestCase):
    """`elt-taskgen ingest-dlt` against the committed manifests, end to end."""

    def _run(self, *argv: str) -> int:
        from elt_taskgen import cli

        return cli.main(list(argv))

    def test_list_does_not_touch_the_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            rc = self._run("ingest-dlt", "--workspace", d, "--list")
        self.assertEqual(rc, 0)

    def test_excluded_connector_registers_nothing_and_pipedrive_still_lands(self):
        """Blocker-2 X1: freshdesk is on the catalog denylist, so a targeted
        ingest registers zero tasks and exits 2 (the INGEST EXIT-CODE
        CONTRACT: nothing registered is a failed ingest, even a deliberate
        one). An ingestible connector is unaffected."""
        from elt_taskgen.engine import Engine

        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                self._run("ingest-dlt", "--workspace", d, "--connector", "dlt_freshdesk"),
                2,
            )
            self.assertEqual(
                self._run("ingest-dlt", "--workspace", d, "--connector", "pipedrive"), 0
            )
            engine = Engine(Path(d))
            try:
                self.assertNotIn(
                    "dlt__freshdesk",
                    [t.task_id for t in engine.list_tasks()]
                    if hasattr(engine, "list_tasks")
                    else [p.name for p in (Path(d) / "tasks").iterdir()],
                )
                pipedrive = engine.load_task("dlt__pipedrive")
            finally:
                engine.close()
        self.assertEqual(len(pipedrive.tables), 18)
        self.assertEqual(len(pipedrive.relationships), 2)
        self.assertIs(pipedrive.origin, Origin.DLT)
        self.assertEqual(pipedrive.license, "Apache-2.0")
        self.assertIn("verified-sources", pipedrive.attribution)

    def test_all_ingests_every_ingestible_connector(self):
        with tempfile.TemporaryDirectory() as d:
            rc = self._run("ingest-dlt", "--workspace", d, "--all")
            self.assertEqual(rc, 0)
            registered = sorted(p.name for p in (Path(d) / "tasks").iterdir())
        # Blocker-2 X1: the nine denylisted connectors are SKIPPED by the
        # catalog; only the three EL-rewardable ones may register.
        expected = sorted(f"dlt__{c}" for c in TestCommittedManifests.INGESTIBLE)
        self.assertEqual(registered, expected)

    def test_unknown_connector_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            rc = self._run("ingest-dlt", "--workspace", d, "--connector", "nope")
        self.assertEqual(rc, 2)

    def test_contamination_refusal_exits_2_and_is_not_a_skip(self):
        """A contaminated connector is a REFUSAL, not a benign skip.

        `--all` treats a plain ValueError as a per-connector skip (the
        "declares no endpoints" case) and still exits 0. A contamination
        refusal used to land in that bucket, so a wrapper testing
        `$? -eq 2` — the INGEST EXIT-CODE CONTRACT in cli.py — read a
        contaminated candidate as a clean run.
        """
        from elt_taskgen.adapters import dlt as dlt_adapter
        from elt_taskgen.verification import contamination as cont

        with tempfile.TemporaryDirectory() as d:
            index_dir = Path(d) / "state" / "contamination"
            index_dir.mkdir(parents=True)
            # personio, not slack: the probe connector must be one the catalog
            # still admits, or the exclusion skip fires before the
            # contamination check this test exists to exercise.
            task = dlt_adapter.to_task_ir(
                dlt_adapter.load_connector(
                    MANIFEST_DIR / "personio.yaml"
                )
            )
            cont.ContaminationIndex(index_dir).add_benchmark(
                "verifier_probe",
                [f for f in cont.task_fingerprints(task) if f.startswith("family:")],
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self._run("ingest-dlt", "--workspace", d, "--all", "--dry-run")
            out = buf.getvalue()

        self.assertEqual(rc, 2, out)
        self.assertIn("contamination FATAL [family/verifier_probe]", out)
        self.assertIn("REFUSED:", out)
        self.assertIn("dlt__personio was NOT registered", out)
        # It must NOT be filed beside the benign "no static resources" skips.
        self.assertNotIn("SKIPPED personio:", out)

    def test_the_contamination_gate_raises_its_own_type(self):
        """The dedicated type is what the CLI branches on; a bare ValueError
        is indistinguishable from a benign per-connector skip."""
        from elt_taskgen.adapters import dlt as dlt_adapter

        self.assertTrue(
            issubclass(dlt_adapter.DltContaminationError, ValueError),
            "must stay a ValueError subclass so existing callers keep working",
        )
        with tempfile.TemporaryDirectory() as d:
            task = dlt_adapter.to_task_ir(
                dlt_adapter.load_connector(
                    MANIFEST_DIR / "slack.yaml"
                )
            )
            # An unarmed index is a fatal collision too.
            with self.assertRaises(dlt_adapter.DltContaminationError) as ctx:
                dlt_adapter.assert_uncontaminated(task, Path(d) / "empty")
        self.assertTrue(ctx.exception.collisions)


class TestCuratedColumns(unittest.TestCase):
    """Blocker-2 T1/T3: curator-authored schema and its provenance contract."""

    def _column(self, **kw):
        base = dict(name="thing_label", type="text", source="curator-invented")
        base.update(kw)
        return dlt_adapter.DltCuratedColumn(**base)

    def test_source_is_required_and_enumerated(self):
        with self.assertRaises(ValueError):
            dlt_adapter.DltCuratedColumn(name="c", type="text")  # no source
        for bad in ("", "vendor-docs", "fixture:", "readme:", "ast:", "invented"):
            with self.subTest(source=bad):
                with self.assertRaises(ValueError):
                    self._column(source=bad)
        for good in (
            "curator-invented",
            "fixture:tests/recents_response_with_null.json#title",
            "readme:README.md:100",
            "ast:__init__.py:12",
        ):
            with self.subTest(source=good):
                self._column(source=good)  # must not raise

    def test_collisions_with_pk_link_and_cursor_are_refused(self):
        with self.assertRaises(ValueError):
            dlt_adapter.DltEndpoint(
                name="t", path="/t", primary_key=("id",),
                curated_columns=(self._column(name="id"),),
            )
        with self.assertRaises(ValueError):
            dlt_adapter.DltEndpoint(
                name="t", path="/t", cursor="updated_at",
                curated_columns=(self._column(name="updated_at"),),
            )
        with self.assertRaises(ValueError):
            dlt_adapter.DltManifest(
                connector="c",
                endpoints=(
                    dlt_adapter.DltEndpoint(name="p", path="/p", primary_key=("id",)),
                    dlt_adapter.DltEndpoint(
                        name="t", path="/t", parent="p",
                        curated_columns=(self._column(name="_p_id"),),
                    ),
                ),
            )
        with self.assertRaises(ValueError):
            dlt_adapter.DltEndpoint(
                name="t", path="/t",
                curated_columns=(self._column(), self._column()),
            )

    def test_curated_alongside_wholesale_columns_fails_closed(self):
        ep = dlt_adapter.DltEndpoint(
            name="t", path="/t",
            columns=(dlt_adapter.DltColumn(name="a", type="text"),),
            curated_columns=(self._column(),),
        )
        with self.assertRaises(ValueError):
            dlt_adapter._endpoint_columns(ep)

    def test_curated_columns_land_in_the_schema_with_printed_provenance(self):
        m = dlt_adapter.DltManifest(
            connector="tiny",
            license="Apache-2.0",
            endpoints=(
                dlt_adapter.DltEndpoint(
                    name="things", path="/things", primary_key=("id",),
                    cursor="updated_at",  # funds the activity mart
                    curated_columns=(self._column(),),
                ),
            ),
        )
        task = dlt_adapter.to_task_ir(m)
        col = task.table("things").column("thing_label")
        self.assertIs(col.type, ColumnType.TEXT)
        self.assertIn("curator-authored", col.description)
        self.assertIn("curator-invented", col.description)
        # Merged AHEAD of the payload JSON column, after the graph columns
        # (id + the cursor column the mart-funding fixture declares).
        names = [c.name for c in task.table("things").columns]
        self.assertEqual(names, ["id", "updated_at", "thing_label", "payload"])

    def test_committed_manifests_carry_the_17_invented_columns(self):
        """Blocker-2 T3's enumeration, pinned: 5 + 6 + 6 columns, all of them
        honestly `curator-invented` (docs/plans/blocker2_dlt.md §5)."""
        expected = {"pipedrive": 5, "personio": 6, "workable": 6}
        for connector, count in sorted(expected.items()):
            with self.subTest(connector=connector):
                m = dlt_adapter.load_connector(MANIFEST_DIR / f"{connector}.yaml")
                curated = [
                    c for e in m.endpoints for c in e.curated_columns
                ]
                self.assertEqual(len(curated), count)
                self.assertTrue(
                    all(c.source == "curator-invented" for c in curated)
                )

    def test_curated_columns_fund_library_chains(self):
        """The T3 unlock, measured at TaskIR level: chain counts match the
        plan (§4.3: pipedrive 2, personio 2, workable 9 after D4) and every
        kept task now emits an `argmax_profile` library mart."""
        from elt_taskgen.adapters.evidence import chain_candidates

        expected_chains = {"personio": 2, "pipedrive": 2, "workable": 9}
        for connector, chains in sorted(expected_chains.items()):
            with self.subTest(connector=connector):
                m = dlt_adapter.load_connector(MANIFEST_DIR / f"{connector}.yaml")
                task = dlt_adapter.to_task_ir(m)
                self.assertEqual(
                    len(chain_candidates(task.tables, task.relationships)),
                    chains,
                )
                self.assertTrue(
                    any(mart.name.endswith("_top") for mart in task.marts)
                )


class TestSurrogateParentKey(unittest.TestCase):
    """Blocker-2 D4: a PK-less parent referenced by a transformer gets a
    deterministic surrogate key instead of dropping every FK edge onto it."""

    def test_workable_jobs_children_become_relationships(self):
        m = dlt_adapter.load_connector(MANIFEST_DIR / "workable.yaml")
        task = dlt_adapter.to_task_ir(m)
        self.assertEqual(len(task.relationships), 9)
        jobs = task.table("jobs")
        self.assertEqual(jobs.primary_key, ("_jobs_surrogate_id",))
        pk = jobs.column("_jobs_surrogate_id")
        self.assertIs(pk.type, ColumnType.BIGINT)
        self.assertFalse(pk.nullable)
        self.assertIn("surrogate", pk.description)
        self.assertIn("synthesized", pk.description)
        # FK type agreement: every jobs child links BIGINT -> BIGINT.
        for rel in task.relationships:
            if rel.parent_table != "jobs":
                continue
            child_col = task.table(rel.child_table).column(rel.child_columns[0])
            self.assertIs(child_col.type, pk.type)

        # Workable mixes catalogue-backed marts with this legacy entity star.
        # Its existing childless jobs must therefore remain public, scoped
        # evidence for dim_jobs rather than disappearing behind the modern
        # shape's non-empty condition catalogue.
        counterfactual = task.population(PopulationName.COUNTERFACTUAL)
        self.assertIn(
            "WITNESS SCOPE [mart=dim_jobs; shape=star; anchor=jobs; "
            "bridge=jobs_activities; child=(none)]: "
            + pops._LEGACY_CHILDLESS_PROSE,
            counterfactual.conditions,
        )
        jobs_ids = {
            row["_jobs_surrogate_id"]
            for row in counterfactual.literal_rows["jobs"]
        }
        activity_jobs_ids = {
            row["_jobs_id"]
            for row in counterfactual.literal_rows["jobs_activities"]
        }
        self.assertTrue(jobs_ids - activity_jobs_ids)

    def test_a_pkless_parent_without_children_is_left_alone(self):
        m = dlt_adapter.DltManifest(
            connector="tiny",
            license="Apache-2.0",
            endpoints=(
                dlt_adapter.DltEndpoint(name="loose", path="/loose"),
                # cursor funds the activity mart (a graded task needs a real
                # transform mart now that extraction_summary is gone).
                dlt_adapter.DltEndpoint(
                    name="things", path="/things", primary_key=("id",),
                    cursor="updated_at",
                ),
            ),
        )
        task = dlt_adapter.to_task_ir(m)
        self.assertEqual(task.table("loose").primary_key, ())


class TestLinkColumnTyping(unittest.TestCase):
    """I3-A: a synthesized `_<parent>_id` link carries its PARENT KEY's type,
    and a relationship whose two sides disagree fails closed at ingest.

    `_infer_type` typed every link BIGINT by name; matomo's `visits.idVisit` is
    TEXT, so the generator copied 'visits_10000' into a BIGINT column and the
    reference loader raised ConversionException — one stage late."""

    def _manifest(self, **overrides):
        parent = dict(name="visits", path="/visits", primary_key=("idVisit",))
        parent.update(overrides.get("parent", {}))
        child = dict(name="visitors", path="/visitors", parent="visits")
        child.update(overrides.get("child", {}))
        return dlt_adapter.DltManifest(
            connector="typed_links",
            license="Apache-2.0",
            endpoints=(
                dlt_adapter.DltEndpoint(**parent),
                dlt_adapter.DltEndpoint(**child),
            ),
        )

    def _rel_types(self, task):
        return [
            (
                task.table(r.child_table).column(r.child_columns[0]).type,
                task.table(r.parent_table).column(r.parent_columns[0]).type,
            )
            for r in task.relationships
        ]

    def test_link_column_is_typed_after_the_parent_key(self):
        # `idVisit` -> `_infer_type` says TEXT (no `_id` suffix), so the link
        # must be TEXT too, and its description says where the type came from.
        task = dlt_adapter.to_task_ir(self._manifest())
        link = task.table("visitors").column("_visits_id")
        self.assertIs(link.type, ColumnType.TEXT)
        self.assertIn("typed after visits.idVisit", link.description)
        self.assertTrue(all(ct is pt for ct, pt in self._rel_types(task)))
        # Same when the parent DECLARES its columns.
        declared = self._manifest(
            parent={
                "primary_key": ("code",),
                "columns": (
                    dlt_adapter.DltColumn(name="code", type="text", nullable=False),
                ),
            }
        )
        task = dlt_adapter.to_task_ir(declared)
        self.assertIs(task.table("visitors").column("_visits_id").type, ColumnType.TEXT)
        self.assertTrue(all(ct is pt for ct, pt in self._rel_types(task)))

    def test_bigint_parent_key_leaves_the_link_and_its_description_unchanged(self):
        # No `typed after` note when the parent key type is what the name
        # heuristic already says: admitted connectors' hashes do not move.
        task = dlt_adapter.to_task_ir(
            self._manifest(parent={"primary_key": ("id",)})
        )
        link = task.table("visitors").column("_visits_id")
        self.assertIs(link.type, ColumnType.BIGINT)
        self.assertNotIn("typed after", link.description)

    def test_fk_type_mismatch_fails_closed_at_ingest(self):
        # The child declares its schema wholesale with a BIGINT link while the
        # parent key is TEXT: refused here, not at the reference load.
        manifest = self._manifest(
            parent={
                "primary_key": ("code",),
                "columns": (
                    dlt_adapter.DltColumn(name="code", type="text", nullable=False),
                ),
            },
            child={
                "parent_key": "code_ref",
                "columns": (
                    dlt_adapter.DltColumn(name="code_ref", type="bigint", nullable=False),
                    dlt_adapter.DltColumn(name="note", type="text"),
                ),
            },
        )
        with self.assertRaises(ValueError) as ctx:
            dlt_adapter.to_task_ir(manifest)
        self.assertIn("does not match", str(ctx.exception))
        self.assertIn("visitors.code_ref", str(ctx.exception))

    def test_matomo_manifest_loads_end_to_end_offline(self):
        """The real TEXT-keyed connector: generate, render and trusted-load
        the primary population without a ConversionException."""
        from elt_taskgen.generation.source_data import generate_rows, render_population
        from elt_taskgen.models import PopulationName
        from elt_taskgen.reference.solution import load_sources_duckdb
        import duckdb

        task = dlt_adapter.to_task_ir(
            dlt_adapter.load_connector(MANIFEST_DIR / "matomo.yaml")
        )
        self.assertTrue(all(ct is pt for ct, pt in self._rel_types(task)))
        rows = generate_rows(task, PopulationName.PRIMARY)
        with tempfile.TemporaryDirectory() as d:
            render_population(task, PopulationName.PRIMARY, rows, Path(d))
            con = duckdb.connect(":memory:")
            try:
                loaded = load_sources_duckdb(task, Path(d), con)
            finally:
                con.close()
        self.assertEqual(loaded.counts["visitors"], len(rows["visitors"]))

    def test_admitted_connectors_keep_bigint_links(self):
        for name in ("personio", "pipedrive", "workable"):
            with self.subTest(connector=name):
                task = dlt_adapter.to_task_ir(
                    dlt_adapter.load_connector(MANIFEST_DIR / f"{name}.yaml")
                )
                for rel in task.relationships:
                    self.assertIs(
                        task.table(rel.child_table).column(rel.child_columns[0]).type,
                        ColumnType.BIGINT,
                    )


class TestGuardedResources(unittest.TestCase):
    """I3-C: flag-gated resources are SERVED (the task never runs the
    connector) but presented honestly as opt-in."""

    def test_workable_manifest_marks_load_details_children(self):
        manifest = dlt_adapter.load_connector(MANIFEST_DIR / "workable.yaml")
        gated = [
            e for e in manifest.endpoints
            if e.name.startswith(("jobs_", "candidates_"))
        ]
        self.assertEqual(len(gated), 9)
        for ep in gated:
            self.assertEqual(ep.guard, "load_details", ep.name)
        self.assertEqual(manifest.endpoint("jobs").guard, "")
        self.assertEqual(manifest.extractor_version, extract_tool.EXTRACTOR_VERSION)
        task = dlt_adapter.to_task_ir(manifest)
        for ep in gated:
            self.assertIn("opt-in", task.table(ep.name).description, ep.name)
            self.assertIn("load_details", task.table(ep.name).description)
        self.assertNotIn("opt-in", task.table("jobs").description)

    def test_a_manifest_without_guards_still_loads(self):
        # `guard` defaults to "" so hand-written and older manifests load.
        m = dlt_adapter.DltManifest(
            connector="plain",
            license="Apache-2.0",
            endpoints=(dlt_adapter.DltEndpoint(name="things", path="/things"),),
        )
        self.assertEqual(m.endpoints[0].guard, "")


class TestBackendRotationInvariant(unittest.TestCase):
    """Blocker-2 EL diversity: pagination must not monopolize the backend."""

    def test_personio_exceeds_one_backend_and_rest_stays_represented(self):
        # personio is ALL-paginated and used to be a 1-backend task — the
        # audit's canonical witness of the old rest pin.
        for connector in ("personio", "pipedrive", "workable"):
            with self.subTest(connector=connector):
                m = dlt_adapter.load_connector(MANIFEST_DIR / f"{connector}.yaml")
                task = dlt_adapter.to_task_ir(m)
                kinds = {b.backend for b in task.backends}
                self.assertGreater(len(kinds), 1)
                self.assertIn(Backend.REST, kinds)
                paginated = {e.name for e in m.loadable() if e.paginated}
                by_table = {b.table: b.backend for b in task.backends}
                self.assertTrue(
                    any(by_table[n] is Backend.REST for n in paginated)
                )
                for name, backend in by_table.items():
                    if name not in paginated:
                        self.assertIsNot(backend, Backend.REST)

    def test_rest_floor_is_pinned_when_the_rotation_misses_rest(self):
        # Directly on `_assign_backends`: whatever the hash draws, a manifest
        # with paginated endpoints always keeps at least one on `rest`.
        eps = tuple(
            dlt_adapter.DltEndpoint(name=f"r{i}", path=f"/r{i}") for i in range(3)
        )
        for family in ("dlt__a", "dlt__b", "dlt__c", "dlt__d", "dlt__e"):
            with self.subTest(family=family):
                assigned = dlt_adapter._assign_backends(eps, family)
                self.assertIn(Backend.REST, set(assigned.values()))


class TestCuratedBlockPreservation(unittest.TestCase):
    """Blocker-2 T2: regeneration must not destroy curation."""

    CURATED = [{"name": "order_weight", "source": "curator-invented", "type": "decimal"}]

    _generation = 0

    def _extract_doc(self, root: Path):
        # A fresh subtree per extraction: regeneration re-reads the SOURCE,
        # exactly as `main()` does on every run.
        type(self)._generation += 1
        source = _write_fixture(root / f"gen{self._generation}")
        extract = extract_tool.extract_connector("dlt_fixture", source)
        pool = load_source_catalog().pool("dlt")
        return extract_tool.manifest_document(extract, pool)

    def test_curated_blocks_survive_a_regeneration_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            doc = self._extract_doc(root)
            target = root / "fixture.yaml"
            # First generation: no curation yet.
            extract_tool.inject_curated_blocks(
                doc, extract_tool.read_curated_blocks(target)
            )
            target.write_text(extract_tool.render_manifest(doc), encoding="utf-8")
            # Curator edits the committed manifest.
            raw = yaml.safe_load(target.read_text(encoding="utf-8"))
            next(
                e for e in raw["endpoints"] if e["name"] == "orders"
            )["curated_columns"] = self.CURATED
            target.write_text(yaml.safe_dump(raw, sort_keys=True), encoding="utf-8")
            # Regeneration: fresh AST extract + preservation, as main() does.
            doc = self._extract_doc(root)
            extract_tool.inject_curated_blocks(
                doc, extract_tool.read_curated_blocks(target)
            )
            text = extract_tool.render_manifest(doc)
            self.assertIn("# CURATOR-AUTHORED", text)
            target.write_text(text, encoding="utf-8")
            manifest = dlt_adapter.load_connector(target)
            curated = manifest.endpoint("orders").curated_columns
            self.assertEqual(
                [(c.name, c.source, c.type) for c in curated],
                [("order_weight", "curator-invented", "decimal")],
            )
            # And the preserved column reaches the emitted schema.
            task = dlt_adapter.to_task_ir(manifest)
            self.assertIn(
                "order_weight",
                [c.name for c in task.table("orders").columns],
            )

    def test_an_orphaned_curated_block_is_a_hard_failure(self):
        with tempfile.TemporaryDirectory() as d:
            doc = self._extract_doc(Path(d))
            with self.assertRaises(extract_tool.ExtractionError) as ctx:
                extract_tool.inject_curated_blocks(
                    doc, {"ghost_endpoint": self.CURATED}
                )
            self.assertIn("ghost_endpoint", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

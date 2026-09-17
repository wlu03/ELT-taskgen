"""Focused contract tests for the read-only, all-record dlt inventory."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from elt_taskgen.catalog import load_source_catalog


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "tools" / "audit_dlt_inventory.py"
SOURCES_CONFIG = REPO_ROOT / "config" / "sources.yaml"
DLT_MANIFEST_DIR = REPO_ROOT / "config" / "dlt_connectors"
COMMITTED_INVENTORY = REPO_ROOT / "config" / "dlt_source_inventory.yaml"
PINNED_COMMIT = "3957506893a7da821dbcc6acd51c7ca4475d1f53"


def _load_tool():
    spec = importlib.util.spec_from_file_location("audit_dlt_inventory_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit_tool = _load_tool()


EXPECTED_BY_STATUS = {
    "admitted": {"dlt_personio", "dlt_pipedrive", "dlt_workable"},
    "buildable": {"dlt_freshdesk", "dlt_matomo", "dlt_slack"},
    "contamination-blocked": {
        "dlt_asana",
        "dlt_facebook_ads",
        "dlt_github",
        "dlt_google_ads",
        "dlt_hubspot",
        "dlt_jira",
        "dlt_salesforce",
        "dlt_shopify",
        "dlt_stripe",
        "dlt_zendesk",
    },
    "runtime-defined": {
        "dlt_airtable",
        "dlt_kinesis",
        "dlt_mongodb",
        "dlt_pg_replication",
        "dlt_scrapy",
        "dlt_strapi",
    },
    "no-transform-mart": {
        "dlt_chess",
        "dlt_google_analytics",
        "dlt_google_sheets",
        "dlt_inbox",
        "dlt_kafka",
        "dlt_mux",
        "dlt_notion",
    },
}


class TestRealDltInventory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = audit_tool.build_inventory()
        cls.rows = {row["record"]: row for row in cls.inventory["records"]}

    def test_scans_all_29_records_including_every_catalog_exclusion(self):
        expected = set().union(*EXPECTED_BY_STATUS.values())
        self.assertEqual(set(self.rows), expected)
        self.assertEqual(len(self.rows), 29)
        self.assertEqual(self.inventory["summary"]["vendored_records"], 29)
        self.assertEqual(self.inventory["summary"]["catalog_excluded"], 26)
        self.assertEqual(
            {name for name, row in self.rows.items() if row["catalog_excluded"]},
            expected - EXPECTED_BY_STATUS["admitted"],
        )

    def test_current_five_way_classification_is_computed_exactly(self):
        for status, expected in EXPECTED_BY_STATUS.items():
            actual = {
                record for record, row in self.rows.items() if row["status"] == status
            }
            self.assertEqual(actual, expected, status)
            self.assertEqual(
                self.inventory["summary"]["status_counts"][status], len(expected)
            )
        self.assertEqual(
            self.inventory["summary"]["status_counts"]["not-buildable"], 0
        )
        self.assertEqual(
            self.inventory["summary"]["status_counts"]["extraction-error"], 0
        )

    def test_provenance_tier_files_and_resource_details_are_reported(self):
        for row in self.rows.values():
            self.assertNotEqual(row["tier"], "unclassified")
            self.assertEqual(row["provenance"]["commit"], PINNED_COMMIT)
            self.assertTrue(row["provenance"]["upstream"].startswith("https://"))
            self.assertEqual(row["source_summary"]["directory"], f"{row['record']}/source")
            self.assertEqual(
                row["source_summary"]["files"], sorted(row["source_summary"]["files"])
            )
            self.assertTrue(
                all(not Path(name).is_absolute() for name in row["source_summary"]["files"])
            )
            resources = row["resource_summary"]
            self.assertEqual(resources["names"], sorted(resources["names"]))
            self.assertEqual(resources["resources"], len(resources["names"]))
            self.assertEqual(resources["loadable"], len(resources["loadable_names"]))

        freshdesk = self.rows["dlt_freshdesk"]["resource_summary"]
        self.assertEqual(
            freshdesk["names"],
            ["agents", "companies", "contacts", "groups", "roles", "tickets"],
        )
        self.assertEqual(freshdesk["with_primary_key"], 6)
        self.assertEqual(freshdesk["with_cursor"], 6)
        self.assertEqual(freshdesk["paginated"], 6)

    def test_inventory_does_not_weaken_or_mutate_catalog_admission(self):
        catalog_before = SOURCES_CONFIG.read_bytes()
        manifests_before = {
            path.name: path.read_bytes() for path in sorted(DLT_MANIFEST_DIR.glob("*.yaml"))
        }

        audit_tool.build_inventory()

        self.assertEqual(SOURCES_CONFIG.read_bytes(), catalog_before)
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in sorted(DLT_MANIFEST_DIR.glob("*.yaml"))
            },
            manifests_before,
        )
        pool = load_source_catalog().pool("dlt")
        with self.assertRaisesRegex(ValueError, "excluded by the source catalog"):
            pool.selection("dlt_freshdesk")
        self.assertTrue(self.rows["dlt_freshdesk"]["catalog_excluded"])
        self.assertEqual(self.rows["dlt_freshdesk"]["status"], "buildable")

    def test_render_and_out_file_are_byte_stable(self):
        expected = audit_tool.render_inventory(self.inventory)
        self.assertEqual(expected, audit_tool.render_inventory(self.inventory))
        self.assertNotIn(str(load_source_catalog().pool("dlt").root_path()), expected)
        self.assertEqual(COMMITTED_INVENTORY.read_text(encoding="utf-8"), expected)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "inventory.yaml"
            self.assertEqual(audit_tool.main(["--out", str(out)]), 0)
            first = out.read_bytes()
            self.assertEqual(first, expected.encode("utf-8"))
            self.assertEqual(audit_tool.main(["--out", str(out)]), 0)
            self.assertEqual(out.read_bytes(), first)


class TestNoConnectorImport(unittest.TestCase):
    def test_module_body_that_raises_is_ast_scanned_not_imported(self):
        source_text = '''\
import dlt

raise RuntimeError("connector module was imported")


@dlt.resource(name="events", primary_key="id", write_disposition="merge")
def events(updated_at=dlt.sources.incremental("updated_at")):
    yield from client.get_pages(endpoint="events", per_page=100)


@dlt.source(name="poison")
def poison_source():
    yield events
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "raw" / "dlt_poison" / "source"
            source.mkdir(parents=True)
            (source / "__init__.py").write_text(source_text, encoding="utf-8")

            provenance = root / "MANIFEST.json"
            provenance.write_text(
                """{
  "sources": [{
    "name": "dlt_poison",
    "kind": "dlt-verified-source",
    "upstream": "https://example.invalid/dlt_poison",
    "commit": "abc123",
    "tier": "can_use",
    "path": "dlt/dlt_poison"
  }]
}\n""",
                encoding="utf-8",
            )
            catalog = root / "sources.yaml"
            catalog.write_text(
                yaml.safe_dump(
                    {
                        "pools": {
                            "dlt": {
                                "origin": "dlt",
                                "root": str(root / "raw"),
                                "license": "Apache-2.0",
                                "attribution": "fixture",
                                "provenance_manifest": str(provenance),
                                "excluded": [],
                            }
                        }
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            empty_manifests = root / "manifests"
            empty_manifests.mkdir()

            self.assertNotIn("dlt_poison", sys.modules)
            inventory = audit_tool.build_inventory(
                catalog_path=catalog, manifest_dir=empty_manifests
            )

            self.assertNotIn("dlt_poison", sys.modules)
            self.assertEqual(inventory["summary"]["vendored_records"], 1)
            row = inventory["records"][0]
            self.assertEqual(row["record"], "dlt_poison")
            self.assertEqual(row["resource_summary"]["names"], ["events"])
            self.assertEqual(row["status"], "admitted")


if __name__ == "__main__":
    unittest.main()

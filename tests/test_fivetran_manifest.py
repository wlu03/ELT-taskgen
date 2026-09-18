"""Tests for tools/build_dbt_manifest.py and the turnkey `ingest-dbt --package`.

WHAT IS PINNED HERE
  * the manifest build is IDEMPOTENT: two builds of the same package produce
    the same semantic fingerprint and the same TaskIR content hash (dbt stamps
    a fresh clock into every manifest, so only the fingerprint may be compared);
  * the build NEVER writes into the vendored tree (sha256 snapshot before/after,
    which is exactly what `assert_vendored_untouched` enforces in production);
  * the one-command path really produces valid, registered TaskIRs;
  * stripe / zendesk cannot become tasks — no longer via a catalog carve-out
    (removed) but via the contamination firewall, which is the guard
    that actually decides;
  * `extract_candidates` skips an unusable cut and REPORTS it, while the pinned
    `extract_tasks` keeps raising on the same manifest.

These tests build a SYNTHETIC dbt package with real dbt (no network: its only
dependency is the local package itself), so they exercise the true
deps+parse+mirror machinery without depending on the 213GB vendored corpus.
They skip only when no dbt interpreter is available on this machine.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen.adapters import dbt as dbt_adapter
from elt_taskgen.models import ColumnType, task_from_json
from elt_taskgen.verification import contamination as cont

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOL_PATH = _REPO_ROOT / "tools" / "build_dbt_manifest.py"


def _load_tool():
    import sys

    name = "elt_taskgen_build_dbt_manifest"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_tool()


def _dbt_available() -> bool:
    try:
        builder._dbt_python(None)
    except builder.ManifestBuildError:
        return False
    return True


DBT_AVAILABLE = _dbt_available()

# ---------------------------------------------------------------------------
# Synthetic vendored package: two sources feeding one mart (a usable cut) plus
# an isolated single-source chain (a cut that must be SKIPPED, not fatal).
# ---------------------------------------------------------------------------

_PROJECT_YML = """
name: 'fakepkg'
version: '1.0.0'
config-version: 2

models:
  fakepkg:
    +materialized: table
"""

_IT_PROJECT_YML = """
name: 'fakepkg_integration_tests'
version: '1.0.0'
config-version: 2
profile: 'integration_tests'
"""

_IT_PACKAGES_YML = """
packages:
  - local: ../
"""

_SRC_YML = """
version: 2

sources:
  - name: fake
    schema: "{{ var('fake_schema', 'fake') }}"
    tables:
      - name: customers
        description: Customers of the fake shop.
        columns:
          - name: customer_id
            data_type: integer
            description: Customer key.
            tests: [unique, not_null]
          - name: customer_name
            data_type: varchar
            description: Customer display name.
      - name: orders
        description: Orders placed by customers.
        columns:
          - name: order_id
            data_type: bigint
            description: Order key.
            tests: [unique, not_null]
          - name: customer_id
            data_type: integer
            description: Customer the order belongs to.
            tests:
              - not_null
              - relationships:
                  to: source('fake', 'customers')
                  field: customer_id
          - name: amount
            data_type: numeric
            description: Order amount.
      - name: lonely
        description: A source nothing joins to.
        columns:
          - name: lonely_id
            data_type: integer
            description: Lonely key.

models:
  - name: fake__customer_summary
    description: One row per customer with order totals.
    columns:
      - name: customer_id
        data_type: integer
        description: Customer key.
        tests: [unique, not_null]
      - name: order_count
        data_type: integer
        description: Number of orders for the customer.
      - name: total_amount
        data_type: numeric
        description: Total order amount for the customer.
  - name: fake__lonely_copy
    description: Passthrough of the lonely source.
    columns:
      - name: lonely_id
        data_type: integer
        description: Lonely key.
"""

_SUMMARY_SQL = """
select
    c.customer_id,
    count(o.order_id) as order_count,
    sum(o.amount) as total_amount
from {{ source('fake', 'customers') }} as c
left join {{ source('fake', 'orders') }} as o
    on c.customer_id = o.customer_id
group by 1
"""

_LONELY_SQL = "select lonely_id from {{ source('fake', 'lonely') }}\n"


def write_fake_package(root: Path, name: str = "dbt_fake") -> Path:
    pkg = root / name
    (pkg / "models").mkdir(parents=True, exist_ok=True)
    (pkg / "integration_tests").mkdir(parents=True, exist_ok=True)
    (pkg / "dbt_project.yml").write_text(_PROJECT_YML, encoding="utf-8")
    (pkg / "models" / "src_fake.yml").write_text(_SRC_YML, encoding="utf-8")
    (pkg / "models" / "fake__customer_summary.sql").write_text(_SUMMARY_SQL, encoding="utf-8")
    (pkg / "models" / "fake__lonely_copy.sql").write_text(_LONELY_SQL, encoding="utf-8")
    (pkg / "integration_tests" / "dbt_project.yml").write_text(_IT_PROJECT_YML, encoding="utf-8")
    (pkg / "integration_tests" / "packages.yml").write_text(_IT_PACKAGES_YML, encoding="utf-8")
    return pkg


# ---------------------------------------------------------------------------
# Catalog gating (no dbt needed)
# ---------------------------------------------------------------------------

class ContaminationBlocksStripeAndZendeskTest(unittest.TestCase):
    """THE COVERAGE THAT MOVED. stripe/zendesk used to be refused by a hand-kept
    `excluded:` list in config/sources.yaml; that carve-out is gone. They are
    still unreachable because both are Spider2-DBT families, so a candidate
    carrying either name collides at contamination_pre — a measured guard rather
    than a list someone has to remember to maintain.
    """

    def test_the_family_collides_before_generation(self) -> None:
        from elt_taskgen import cli, demo_fixture

        for family in ("stripe", "zendesk"):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as tmp:
                self.assertIn(family, cont.SPIDER2_DBT_FAMILIES)
                index = cont.ContaminationIndex(Path(tmp))
                cli._seed_embedded_deny_lists(index)
                task = demo_fixture.demo_task().model_copy(
                    update={"family_id": f"dbt__{family}"}
                )
                hits = [
                    c
                    for c in index.check_pre(task)
                    if c.kind == "family" and family in c.detail
                ]
                self.assertTrue(hits, f"{family} must collide at contamination_pre")
                self.assertEqual({c.against for c in hits}, {"spider2_dbt"})

    def test_a_cleared_family_does_not_collide(self) -> None:
        """The discriminating half: the guard must not refuse everything."""
        from elt_taskgen import cli, demo_fixture

        with tempfile.TemporaryDirectory() as tmp:
            index = cont.ContaminationIndex(Path(tmp))
            cli._seed_embedded_deny_lists(index)
            task = demo_fixture.demo_task().model_copy(
                update={"family_id": "dbt__workable"}
            )
            self.assertEqual(
                [c for c in index.check_pre(task) if c.kind == "family"], []
            )

    def test_missing_package_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(builder.ManifestBuildError) as ctx:
                builder.resolve_package_dir("dbt_nope", root=Path(tmp))
            self.assertIn("dbt_nope", str(ctx.exception))


# ---------------------------------------------------------------------------
# Adapter failure policy (no dbt needed)
# ---------------------------------------------------------------------------

def _mixed_manifest() -> dict:
    def col(name: str, dtype: str) -> dict:
        return {"name": name, "description": "", "data_type": dtype}

    return {
        "metadata": {"project_name": "mixed_pkg"},
        "sources": {
            "source.mixed_pkg.s.customers": {
                "name": "customers",
                "package_name": "mixed_pkg",
                "columns": {"customer_id": col("customer_id", "integer")},
            },
            "source.mixed_pkg.s.orders": {
                "name": "orders",
                "package_name": "mixed_pkg",
                "columns": {
                    "order_id": col("order_id", "bigint"),
                    "customer_id": col("customer_id", "integer"),
                },
            },
            "source.mixed_pkg.s.lonely": {
                "name": "lonely",
                "package_name": "mixed_pkg",
                "columns": {"lonely_id": col("lonely_id", "integer")},
            },
        },
        "nodes": {
            "model.mixed_pkg.summary": {
                "resource_type": "model",
                "name": "summary",
                "package_name": "mixed_pkg",
                "depends_on": {
                    "nodes": [
                        "source.mixed_pkg.s.customers",
                        "source.mixed_pkg.s.orders",
                    ]
                },
        # Give the CUT-policy fixture a real aggregate measure; trivial
        # COUNT(*)-by-full-grain marts are discarded at ingest.
                "compiled_code": (
                    "select customer_id, count(order_id) as order_count "
                    "from orders group by 1"
                ),
                "columns": {
                    "customer_id": col("customer_id", "integer"),
                    "order_count": col("order_count", "integer"),
                },
            },
            "model.mixed_pkg.lonely_copy": {
                "resource_type": "model",
                "name": "lonely_copy",
                "package_name": "mixed_pkg",
                "depends_on": {"nodes": ["source.mixed_pkg.s.lonely"]},
                "columns": {"lonely_id": col("lonely_id", "integer")},
            },
            # The mart's declared grain. Without key evidence a mart has no
            # defensible `key_columns` and the whole cut is skipped
            # (adapters/dbt.py::mart_key_columns) — which would make this
            # fixture test the WRONG skip reason.
            "test.mixed_pkg.not_null_summary_customer_id": {
                "resource_type": "test",
                "test_metadata": {
                    "name": "not_null",
                    "kwargs": {"column_name": "customer_id"},
                },
                "attached_node": "model.mixed_pkg.summary",
                "depends_on": {"nodes": ["model.mixed_pkg.summary"]},
            },
        },
    }


class ExtractionPolicyTest(unittest.TestCase):
    """A real package is a MIX: one rich cut plus isolated staging chains."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "manifest.json"
        self.path.write_text(json.dumps(_mixed_manifest()), encoding="utf-8")
        self.spec = dbt_adapter.load_manifest(self.path)

    def test_candidates_skip_the_trivial_cut_and_report_it(self) -> None:
        result = dbt_adapter.extract_candidates(self.spec)
        self.assertEqual(len(result.tasks), 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("no join surface", result.skipped[0].reason)
        self.assertIn("source.mixed_pkg.s.lonely", result.skipped[0].members)
        task = result.tasks[0]
        self.assertEqual(task.family_id, "dbt__mixed_pkg")
        self.assertEqual([t.name for t in task.tables], ["customers", "orders"])

    def test_strict_entry_point_still_raises(self) -> None:
        with self.assertRaises(ValueError):
            dbt_adapter.extract_tasks(self.spec)

    def test_candidates_and_strict_agree_when_nothing_is_skipped(self) -> None:
        doc = _mixed_manifest()
        del doc["sources"]["source.mixed_pkg.s.lonely"]
        del doc["nodes"]["model.mixed_pkg.lonely_copy"]
        path = Path(self.tmp.name) / "clean.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        spec = dbt_adapter.load_manifest(path)
        result = dbt_adapter.extract_candidates(spec)
        strict = dbt_adapter.extract_tasks(spec)
        self.assertEqual(len(result.skipped), 0)
        self.assertEqual(
            [t.content_hash() for t in result.tasks],
            [t.content_hash() for t in strict],
        )


# ---------------------------------------------------------------------------
# Real dbt build against a synthetic vendored package (offline)
# ---------------------------------------------------------------------------

class BuildRootHoldsAnotherPackageTest(unittest.TestCase):
    """`--build-root` is PER PACKAGE. Pointed at a per-POOL directory, every
    package mirrors onto the same `package/` dir and the reuse shortcut serves
    whichever package got there first.

    Measured: `--package dbt_mixpanel --build-root <pool dir>` (the dir already
    holding reddit_ads) returned reddit_ads' manifest and registered
    `dbt__mixpanel__reddit_ads__account_report_...` — a task whose family,
    license and attribution said mixpanel while every table, mart and row was
    reddit_ads. Needs no dbt: the guard fires before any dbt process.
    """

    def test_refuses_a_build_root_mirrored_from_a_different_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vendored"
            root.mkdir(parents=True)
            write_fake_package(root)

            build_root = Path(tmp) / "shared_pool_root"
            occupied = build_root / "package"
            occupied.mkdir(parents=True)
            (occupied / "dbt_project.yml").write_text(
                "name: 'someoneelse'\nversion: '1.0.0'\nconfig-version: 2\n",
                encoding="utf-8",
            )

            with self.assertRaises(builder.ManifestBuildError) as ctx:
                builder.build_manifest("dbt_fake", build_root=build_root, root=root)
            msg = str(ctx.exception)
            self.assertIn("someoneelse", msg)
            self.assertIn("fakepkg", msg)
            self.assertIn("PER PACKAGE", msg)

    def test_refuses_cached_manifest_from_another_release_of_same_package(self) -> None:
        """A shared project name cannot authorize reuse across source bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vendored"
            root.mkdir(parents=True)
            pkg = write_fake_package(root)
            build_root = Path(tmp) / "build"
            copy_dir = build_root / "package"
            shutil.copytree(pkg, copy_dir)
            manifest = copy_dir / "integration_tests" / "target" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}", encoding="utf-8")

            # Simulate a newer checkout under the same dbt project name. The
            # old shortcut checked only that name and would reuse the manifest.
            (pkg / "models" / "fake__customer_summary.sql").write_text(
                _SUMMARY_SQL + "\n-- newer release\n", encoding="utf-8"
            )
            with mock.patch.object(builder, "manifest_is_compiled", return_value=True):
                with self.assertRaises(builder.ManifestBuildError) as ctx:
                    builder.build_manifest(
                        "dbt_fake", build_root=build_root, root=root
                    )
            self.assertIn("different source bytes", str(ctx.exception))
            self.assertIn("--rebuild-manifest", str(ctx.exception))


class DbtCustomRootProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.catalog_root = base / "catalog-root"
        self.custom_root = base / "custom-root"
        self.catalog_root.mkdir()
        self.custom_root.mkdir()
        provenance = base / "MANIFEST.json"
        provenance.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "name": "dbt_fake",
                            "upstream": "https://example.test/dbt_fake",
                            "commit": "a" * 40,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.config = base / "sources.yaml"
        self.config.write_text(
            "pools:\n"
            "  dbt:\n"
            "    origin: dbt\n"
            f"    root: {self.catalog_root}\n"
            "    license: Apache-2.0\n"
            "    attribution: Test dbt package\n"
            f"    provenance_manifest: {provenance}\n",
            encoding="utf-8",
        )

    def _args(self, **updates):
        values = {
            "pool": "dbt",
            "package": "dbt_fake",
            "license": None,
            "root": str(self.custom_root),
            "sources_config": str(self.config),
            "source_commit": None,
            "source_upstream": None,
        }
        values.update(updates)
        return SimpleNamespace(**values)

    def test_custom_root_cannot_inherit_catalog_commit(self) -> None:
        from elt_taskgen import cli

        with self.assertRaises(ValueError) as ctx:
            cli._dbt_ingest_identity(self._args(), "fakepkg")
        self.assertIn("does not attest those bytes", str(ctx.exception))
        self.assertIn("--source-commit", str(ctx.exception))

    def test_cli_refuses_unbound_root_before_loading_the_builder(self) -> None:
        from elt_taskgen import cli

        with mock.patch.object(cli, "_load_manifest_builder") as load_builder:
            code = cli.main(
                [
                    "ingest-dbt",
                    "--workspace",
                    str(Path(self.tmp.name) / "ws"),
                    "--package",
                    "dbt_fake",
                    "--root",
                    str(self.custom_root),
                    "--sources-config",
                    str(self.config),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 2)
        load_builder.assert_not_called()

    def test_verified_custom_commit_replaces_catalog_commit(self) -> None:
        from elt_taskgen import cli

        selection = cli._dbt_ingest_identity(
            self._args(source_commit="b" * 40), "fakepkg"
        )
        self.assertEqual(selection.family_id, "dbt__fakepkg")
        self.assertIn("https://example.test/dbt_fake", selection.attribution)
        self.assertIn("@ bbbbbbbbbbbb", selection.attribution)
        self.assertNotIn("aaaaaaaaaaaa", selection.attribution)

    def test_upstream_without_commit_is_refused(self) -> None:
        from elt_taskgen import cli

        with self.assertRaisesRegex(ValueError, "requires --source-commit"):
            cli._dbt_ingest_identity(
                self._args(source_upstream="https://example.test/other"),
                "fakepkg",
            )


@unittest.skipUnless(
    DBT_AVAILABLE,
    f"no dbt interpreter (set ${builder.DBT_PYTHON_ENV} to a python with dbt-core)",
)
class ManifestBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vendored"
        self.root.mkdir(parents=True)
        self.pkg = write_fake_package(self.root)
        self.build_root = Path(self.tmp.name) / "build" / "dbt_fake"

    def _build(self, **kw):
        return builder.build_manifest(
            "dbt_fake", build_root=self.build_root, root=self.root, **kw
        )

    def test_build_is_idempotent_and_never_touches_the_vendored_tree(self) -> None:
        before = builder.vendored_snapshot(self.pkg)

        first = self._build()
        self.assertTrue(first.rebuilt)
        self.assertEqual(first.project_choice, builder.ProjectChoice.INTEGRATION_TESTS)
        self.assertEqual(first.package_project_name, "fakepkg")
        self.assertTrue(Path(first.manifest_path).is_file())

        # 1) reuse: no dbt process, same manifest, same fingerprint
        second = self._build()
        self.assertFalse(second.rebuilt)
        self.assertEqual(second.manifest_path, first.manifest_path)
        self.assertEqual(second.fingerprint, first.fingerprint)

        # 2) forced rebuild: dbt runs again, SEMANTIC content is unchanged
        third = self._build(force=True)
        self.assertTrue(third.rebuilt)
        self.assertEqual(third.fingerprint, first.fingerprint)

        # 3) the vendored tree is byte-identical (build droppings all landed in
        #    the build root: package-lock.yml, dbt_packages/, target/, logs/)
        self.assertEqual(builder.vendored_snapshot(self.pkg), before)
        builder.assert_vendored_untouched(self.pkg, before)
        self.assertFalse((self.pkg / "integration_tests" / "target").exists())
        self.assertFalse((self.pkg / "integration_tests" / "dbt_packages").exists())
        self.assertTrue(
            (Path(third.project_dir) / "target" / "manifest.json").is_file()
        )

        # 4) rebuilding also does not move the TaskIRs the manifest yields
        def hashes(path: str) -> list[str]:
            spec = dbt_adapter.load_manifest(Path(path)).model_copy(
                update={"package_name": "fakepkg"}
            )
            return [t.content_hash() for t in dbt_adapter.extract_candidates(spec).tasks]

        self.assertEqual(hashes(third.manifest_path), hashes(first.manifest_path))

    def test_compile_runs_and_the_manifest_carries_rendered_sql(self) -> None:
        """`dbt parse` leaves `compiled_code` null and a Fivetran model's
        `raw_code` is Jinja, so a parse-only manifest recovers NOTHING —
        measured, 0 aggregate expressions from 67 of 67 real mart models. The
        build runs `dbt compile` so recovery reads real SQL."""
        build = self._build()
        self.assertTrue(build.compiled)
        self.assertGreater(build.model_count, 0)
        self.assertEqual(build.compiled_model_count, build.model_count)
        self.assertEqual(build.compile_error, "")
        self.assertTrue(builder.manifest_is_compiled(Path(build.manifest_path)))
        node = next(
            m
            for m in dbt_adapter.load_manifest(Path(build.manifest_path)).models
            if m.name == "fake__customer_summary"
        )
        self.assertTrue(node.compiled_code)
        self.assertEqual(node.sql_text, node.compiled_code)
        self.assertNotIn("{{", node.sql_text)

    def test_no_compile_leaves_a_parse_only_manifest_and_says_so(self) -> None:
        build = self._build(run_compile=False)
        self.assertFalse(build.compiled)
        self.assertEqual(build.compiled_model_count, 0)
        self.assertFalse(builder.manifest_is_compiled(Path(build.manifest_path)))

    def test_a_parse_only_manifest_is_not_reused_when_compile_is_wanted(self) -> None:
        """Every workspace built before this change holds a parse-only
        manifest; reusing it would keep serving Jinja to the recovery pass."""
        first = self._build(run_compile=False)
        self.assertFalse(first.compiled)
        second = self._build()          # force=False, but compile is required
        self.assertTrue(second.rebuilt)
        self.assertTrue(second.compiled)
        third = self._build()           # now the reuse shortcut applies
        self.assertFalse(third.rebuilt)
        self.assertTrue(third.compiled)

    def test_assert_vendored_untouched_detects_a_mutation(self) -> None:
        before = builder.vendored_snapshot(self.pkg)
        (self.pkg / "target").mkdir()
        (self.pkg / "target" / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(builder.ManifestBuildError):
            builder.assert_vendored_untouched(self.pkg, before)

    def test_manifest_yields_a_valid_taskir(self) -> None:
        build = self._build()
        spec = dbt_adapter.load_manifest(Path(build.manifest_path)).model_copy(
            update={"package_name": build.package_project_name, "license": "Apache-2.0"}
        )
        result = dbt_adapter.extract_candidates(spec)
        self.assertEqual(len(result.tasks), 1, result.skipped)
        self.assertEqual(len(result.skipped), 1)  # the lonely chain
        task = result.tasks[0]
        self.assertEqual(task.family_id, "dbt__fakepkg")
        self.assertEqual([t.name for t in task.tables], ["customers", "orders"])
        self.assertEqual([m.name for m in task.marts], ["fake__customer_summary"])
        self.assertEqual(
            {b.table for b in task.backends}, {t.name for t in task.tables}
        )
        # the relationships test on orders.customer_id survived the round trip
        self.assertEqual(len(task.relationships), 1)
        rel = task.relationships[0]
        self.assertEqual((rel.child_table, rel.parent_table), ("orders", "customers"))


@unittest.skipUnless(
    DBT_AVAILABLE,
    f"no dbt interpreter (set ${builder.DBT_PYTHON_ENV} to a python with dbt-core)",
)
class IngestDbtPackageCliTest(unittest.TestCase):
    """`ingest-dbt --package` end to end: build, extract, register."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vendored"
        self.root.mkdir(parents=True)
        write_fake_package(self.root)
        self.workspace = Path(self.tmp.name) / "ws"
        self.build_root = Path(self.tmp.name) / "build"

    def _run(self, *extra: str) -> int:
        from elt_taskgen import cli

        return cli.main(
            [
                "ingest-dbt",
                "--workspace", str(self.workspace),
                "--package", "dbt_fake",
                "--root", str(self.root),
                "--build-root", str(self.build_root),
                *extra,
            ]
        )

    def test_one_command_registers_valid_taskirs(self) -> None:
        self.assertEqual(self._run(), 0)
        tasks = sorted((self.workspace / "tasks").iterdir())
        self.assertEqual(len(tasks), 1)
        task = task_from_json((tasks[0] / "task_ir.json").read_text(encoding="utf-8"))
        self.assertEqual(task.origin.value, "dbt")
        self.assertEqual(task.family_id, "dbt__fakepkg")
        self.assertEqual(task.cluster_id, "dbt__fakepkg")
        # license + attribution come from the source catalog, not the adapter
        self.assertEqual(task.license, "Apache-2.0")
        self.assertIn("dbt_fake", task.attribution)
        self.assertGreaterEqual(len(task.tables), 2)
        self.assertGreaterEqual(len(task.marts), 1)

        # re-running is idempotent: same task id, same content hash
        before = task.content_hash()
        self.assertEqual(self._run(), 0)
        again = task_from_json((tasks[0] / "task_ir.json").read_text(encoding="utf-8"))
        self.assertEqual(again.content_hash(), before)

    def test_strict_flag_fails_on_a_skipped_cut(self) -> None:
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertEqual(self._run("--strict", "--dry-run"), 2)

    def test_dry_run_registers_nothing(self) -> None:
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertFalse((self.workspace / "tasks").exists())



class JoinRecoveryTest(unittest.TestCase):
    """§C1+§C2 (blocker1 plan) against the REAL reddit_ads compiled manifest
    kept in the canonical drive. Every number below is the plan's measured
    value, reproduced by this implementation before it landed:

      * rungs 1-2 recover 10 relationships; the co-grain rung adds 4 distinct
        (5 clauses, url_report's deduping with ad_report's) -> 14 total;
      * campaign_country is REFUSED — both sides aggregated, undecidable;
      * every co-grain child is a *_conversions_report aggregated TO the key;
      * no relationship is ever `required` — a compiled join is evidence of a
        join surface, not a declared constraint.
    """

    MANIFEST = (
        _REPO_ROOT
        / "runs/dbt_elt/dbt_builds/dbt_reddit_ads/package/integration_tests"
        / "target/manifest.json"
    )

    @classmethod
    def setUpClass(cls) -> None:
        if not cls.MANIFEST.is_file():
            raise unittest.SkipTest("reddit_ads compiled manifest not on disk")
        cls.spec = dbt_adapter.load_manifest(str(cls.MANIFEST))
        cls.recovered = dbt_adapter.recover_join_relationships(cls.spec)

    def test_the_measured_reddit_yield_is_reproduced(self) -> None:
        self.assertEqual(len(self.recovered), 14)
        keys = {
            (r.child_table, r.child_columns, r.parent_table, r.parent_columns)
            for r in self.recovered
        }
        # rung 1: the dimension owns `id`, the report is the child.
        self.assertIn(
            ("account_report", ("account_id",), "business_account", ("id",)), keys
        )
        self.assertIn(("ad", ("ad_group_id",), "ad_group", ("id",)), keys)
        # co-grain: the aggregated conversions side is the CHILD.
        self.assertIn(
            (
                "account_conversions_report",
                ("date", "account_id"),
                "account_report",
                ("date", "account_id"),
            ),
            keys,
        )
        # the double-emission a naive same-name rule fabricates must be absent.
        self.assertNotIn(
            (
                "account_report",
                ("date", "account_id"),
                "account_conversions_report",
                ("date", "account_id"),
            ),
            keys,
        )
        # campaign_country: both sides aggregated -> refused, so NO recovered
        # relationship touches campaign_country_conversions_report.
        touching = [
            r
            for r in self.recovered
            if "campaign_country" in (r.child_table, r.parent_table)
            or r.child_table.startswith("campaign_country")
            or r.parent_table.startswith("campaign_country")
        ]
        self.assertEqual(touching, [])

    def test_recovered_relationships_are_never_required(self) -> None:
        for r in self.recovered:
            self.assertFalse(r.required, r)

    def test_end_to_end_the_task_keeps_the_relationships_and_declines_the_fan_out(self) -> None:
        """RE-MEASURED after the C1 lineage fix + I4(b)/I6-A (numbers below
        are the current adapter's, with the reason for each move):

          * 14 relationships still recovered and shipped (unchanged);
          * 6 -> 5 marts: `url_report`'s grain (ad_id, base_url, date_day)
            grounds only partially (base_url is a URL split of a column no
            source declares) — refused whole rather than shipped as the
            byte-identical clone of `ad_report` it was (I6-A);
          * 5 -> 0 JOIN marts: every measure the co-grain hop could carry
            (`sum(report.clicks|impressions|spend)`) is an ADDITIVE aggregate
            over the LOOKUP table, which fans out once per conversions row
            (`_fans_out`); with nothing to carry the hop is not taken. Before
            the lineage fix those measures were silently bound to the
            conversions table's same-named columns instead (C1) — the join
            existed only because of the mis-binding;
          * `inner_join` stays undeclared (nothing to witness); the
            source-side cases still ride along.
        """
        result = dbt_adapter.extract_candidates(self.spec)
        self.assertEqual(len(result.tasks), 1)
        task = result.tasks[0]
        self.assertEqual(len(task.relationships), 14)
        join_marts = sum(
            1
            for m in task.marts
            if any(op.kind.value == "join" for op in m.plan.ops)
        )
        self.assertEqual(join_marts, 0)
        self.assertEqual(len(task.marts), 5)
        self.assertNotIn("reddit_ads__url_report", {m.name for m in task.marts})
        dropped = [s for s in result.skipped if s.scope == dbt_adapter.MART_SCOPE]
        self.assertEqual(len(dropped), 1)
        self.assertIn("only partially grounds", dropped[0].reason)
        self.assertIn("base_url", dropped[0].reason)
        names = {c.name for c in task.attack_cases}
        self.assertNotIn("inner_join", names)
        self.assertIn("dropped_filter", names)  # Phase A rides along

    def test_manifest_project_name_strips_integration_tests(self) -> None:
        """I6-D: the CI harness project `reddit_ads_integration_tests` is the
        package `reddit_ads`; a `--manifest` ingest lands in the same family
        as `--package` (and cannot evade the contamination family check)."""
        self.assertEqual(self.spec.package_name, "reddit_ads")
        result = dbt_adapter.extract_candidates(self.spec)
        self.assertEqual(result.tasks[0].family_id, "dbt__reddit_ads")

    def test_clicks_is_dropped_from_account_report_and_the_pivots_survive(self) -> None:
        """C1 regression on the real manifest: `clicks` (vendor:
        `sum(report.clicks)`, lineage account_report) is no longer bound to
        account_conversions_report.clicks; the conversions pivots survive;
        no misplaced BIGINT lands on account_report.clicks — it takes the
        staging INTEGER instead (I4a)."""
        task = dbt_adapter.extract_candidates(self.spec).tasks[0]
        mart = next(m for m in task.marts if m.name == "reddit_ads__account_report")
        names = {c.name for c in mart.columns}
        for gone in ("clicks", "impressions", "spend"):
            self.assertNotIn(gone, names)
        for kept in ("conversions", "lead_conversions", "purchase_value", "total_items"):
            self.assertIn(kept, names)
        self.assertIn("clicks:", mart.plan.notes)
        self.assertIn("account_report", mart.plan.notes.split("clicks:")[1][:400])
        by_name = {t.name: t for t in task.tables}
        self.assertEqual(by_name["account_report"].column("clicks").type, ColumnType.INTEGER)
        self.assertEqual(by_name["account_conversions_report"].column("date").type, ColumnType.DATE)
        self.assertEqual(
            by_name["account_conversions_report"].column("_fivetran_synced").type,
            ColumnType.TIMESTAMP,
        )
        date_day = next(c for c in mart.columns if c.name == "date_day")
        self.assertEqual(date_day.type, ColumnType.DATE)

    def test_no_two_marts_share_a_plan_in_reddit_ads(self) -> None:
        """I6-A on the real manifest: no clone marts remain."""
        task = dbt_adapter.extract_candidates(self.spec).tasks[0]
        signatures = set()
        for mart in task.marts:
            signature = (
                tuple(mart.key_columns),
                tuple((c.name, c.type.value) for c in mart.columns),
                tuple(
                    (op.kind.value, tuple(sorted(op.details.items())))
                    for op in mart.plan.ops
                ),
            )
            self.assertNotIn(signature, signatures, mart.name)
            signatures.add(signature)

    def test_stress_rows_never_duplicate_a_lookup_parent_key(self) -> None:
        """The suite's only generate_rows run on the REAL reddit_ads task:
        every table must yield rows and every relationship key column the
        generator mints must be non-NULL. The lookup-hop loop below is dead
        today (join_marts == 0, pinned above) but arms itself if a hop ever
        survives; tests/test_fix_A.py exercises it on a live hop."""
        from elt_taskgen.generation.source_data import generate_rows
        from elt_taskgen.models import PopulationName

        task = dbt_adapter.extract_candidates(self.spec).tasks[0]
        rows = generate_rows(task, PopulationName.STRESS)
        for table in task.tables:
            self.assertTrue(rows[table.name], table.name)
        key_columns: dict[str, set[str]] = {}
        for rel in task.relationships:
            key_columns.setdefault(rel.child_table, set()).update(rel.child_columns)
            key_columns.setdefault(rel.parent_table, set()).update(rel.parent_columns)
        for table, cols in key_columns.items():
            for col in cols:
                self.assertTrue(
                    all(r.get(col) is not None for r in rows[table]),
                    (table, col),
                )
        by_name = {t.name: t for t in task.tables}
        for mart in task.marts:
            for table, columns in dbt_adapter._lookup_hops(
                mart, task.relationships, set(by_name)
            ):
                keys = [tuple(r.get(c) for c in columns) for r in rows[table]]
                self.assertEqual(len(keys), len(set(keys)), (mart.name, table))

    def test_a_join_with_aliased_tables_is_recovered(self) -> None:
        """I4(d): `from stg_a as a left join stg_b as b on a.b_id = b.id`
        resolves through the SELECT's table aliases (measured: 22% of pool
        joins were dropped as `?alias`)."""
        def col(n):
            return dbt_adapter.DbtColumn(name=n, description=f"{n}.")

        sources = (
            dbt_adapter.DbtNode(
                unique_id="source.p.x.a", name="a", resource_type="source",
                columns=(col("id"), col("b_id"), col("v"))),
            dbt_adapter.DbtNode(
                unique_id="source.p.x.b", name="b", resource_type="source",
                columns=(col("id"), col("name"))),
        )
        stg_a = dbt_adapter.DbtNode(
            unique_id="model.p.stg_a", name="stg_a", resource_type="model",
            depends_on=("source.p.x.a",),
            compiled_code='select id, b_id, v from "db"."x"."a"')
        stg_b = dbt_adapter.DbtNode(
            unique_id="model.p.stg_b", name="stg_b", resource_type="model",
            depends_on=("source.p.x.b",),
            compiled_code='select id, name from "db"."x"."b"')
        mart = dbt_adapter.DbtNode(
            unique_id="model.p.mart", name="p__mart", resource_type="model",
            depends_on=("model.p.stg_a", "model.p.stg_b"),
            compiled_code=(
                'select a.id, b.name, a.v from "db"."x"."stg_a" as a '
                'left join "db"."x"."stg_b" as b on a.b_id = b.id'))
        spec = dbt_adapter.CandidateSpec(
            package_name="p", sources=sources, models=(stg_a, stg_b, mart))
        recovered = dbt_adapter.recover_join_relationships(spec)
        self.assertEqual(
            [(r.child_table, r.child_columns, r.parent_table, r.parent_columns) for r in recovered],
            [("a", ("b_id",), "b", ("id",))],
        )


class MartLevelAliasLineageTest(unittest.TestCase):
    """§C3 (blocker1 plan). `staging_alias_map` read only single-source
    models, so a MART-level rename (`resolver.email as resolver_email`)
    scored 0 hits despite the mart declaring the column. The extension
    recovers `<join_alias>.<col> AS <mart_col>` where the alias resolves,
    recursively through the model's own CTEs, to exactly one source table.
    Measured: +59 declared columns across 10 marts; single-source packages
    (reddit) gain exactly nothing.
    """

    def _spec(self):
        def col(n):
            return dbt_adapter.DbtColumn(name=n, description=f"{n}.", data_type="text")

        sources = (
            dbt_adapter.DbtNode(
                unique_id="source.p.x.incident", name="incident",
                resource_type="source",
                columns=(col("sys_id"), col("caller_value"))),
            dbt_adapter.DbtNode(
                unique_id="source.p.x.sys_user", name="sys_user",
                resource_type="source",
                columns=(col("sys_id"), col("email"), col("department_value"))),
        )
        stg_user = dbt_adapter.DbtNode(
            unique_id="model.p.stg_x__sys_user", name="stg_x__sys_user",
            resource_type="model", depends_on=("source.p.x.sys_user",),
            compiled_code=(
                'select sys_id as user_id, email, department_value '
                'from "db"."x"."sys_user"'))
        stg_inc = dbt_adapter.DbtNode(
            unique_id="model.p.stg_x__incident", name="stg_x__incident",
            resource_type="model", depends_on=("source.p.x.incident",),
            compiled_code=(
                'select sys_id as incident_id, caller_value '
                'from "db"."x"."incident"'))
        mart = dbt_adapter.DbtNode(
            unique_id="model.p.x__incident_enhanced", name="x__incident_enhanced",
            resource_type="model",
            depends_on=("model.p.stg_x__incident", "model.p.stg_x__sys_user"),
            compiled_code="""
with incident as (select * from "db"."x"."stg_x__incident"),
resolver as (select * from "db"."x"."stg_x__sys_user")
select
    incident.incident_id,
    resolver.email as resolver_email,
    resolver.department_value as dv_resolver_department
from incident
left join resolver on incident.caller_value = resolver.user_id
""")
        return dbt_adapter.CandidateSpec(
            package_name="p", sources=sources,
            models=(stg_user, stg_inc, mart))

    def test_a_mart_level_rename_resolves_through_the_joined_cte(self) -> None:
        amap = dbt_adapter.staging_alias_map(self._spec())
        self.assertEqual(amap.get("resolver_email"), {"sys_user": "email"})
        self.assertEqual(
            amap.get("dv_resolver_department"), {"sys_user": "department_value"}
        )
        # the single-source staging read is untouched
        self.assertEqual(amap.get("user_id"), {"sys_user": "sys_id"})


class DataflowGrainRescueTest(unittest.TestCase):
    """§C4 (blocker1 plan). The inner GROUP BY as a rescue rung BELOW every
    test-backed rule — admitted only by the DATA-FLOW walk (pure pass-through
    from the root to the grouping select), width < 50%, `source_relation` and
    load metadata stripped. The naive walk keyed 6 of 6 reddit marts on a
    SUB-ROLLUP's grain; the join on the path is what refuses that here.
    """

    @staticmethod
    def _model(sql, cols):
        def col(n):
            return dbt_adapter.DbtColumn(name=n, description=f"{n}.", data_type="text")

        return dbt_adapter.DbtNode(
            unique_id="model.p.m", name="p__m", resource_type="model",
            columns=tuple(col(c) for c in cols), compiled_code=sql)

    def test_a_pass_through_chain_admits_the_inner_grain(self) -> None:
        m = self._model("""
with agg as (
  select source_relation, entity_id, date_day, sum(v) as total,
         sum(c) as clicks, sum(s) as spend, sum(i) as impressions
  from "db"."x"."stg_base" group by 1, 2, 3
),
final as (select * from agg)
select * from final
""", ["entity_id", "date_day", "total", "clicks", "spend", "impressions"])
        self.assertEqual(
            dbt_adapter._dataflow_group_by_grain(m, {c.name for c in m.columns}),
            ["entity_id", "date_day"],
        )

    def test_a_join_on_the_path_refuses_the_sub_rollup_grain(self) -> None:
        m = self._model("""
with rollup_conv as (
  select entity_id, date_day, sum(v) as total
  from "db"."x"."stg_conv" group by 1, 2
),
report as (select * from "db"."x"."stg_report")
select report.entity_id, report.date_day, rollup_conv.total,
       report.clicks, report.spend, report.impressions
from report
left join rollup_conv on report.entity_id = rollup_conv.entity_id
""", ["entity_id", "date_day", "total", "clicks", "spend", "impressions"])
        self.assertEqual(
            dbt_adapter._dataflow_group_by_grain(m, {c.name for c in m.columns}),
            [],
        )

    def test_a_wide_grain_is_refused(self) -> None:
        m = self._model("""
with agg as (select a, b, c, sum(v) as total from "db"."x"."stg" group by 1,2,3)
select * from agg
""", ["a", "b", "c", "total"])
        self.assertEqual(
            dbt_adapter._dataflow_group_by_grain(m, {c.name for c in m.columns}),
            [],
        )

    def test_an_aggregating_root_refuses_the_inner_grain(self) -> None:
        """Found adversarially: with the ROOT itself aggregating
        (rung 3's claim, here made unresolvable by a computed GROUP BY), the
        walk stepped PAST it and named the inner grain — a SUPERSET partition
        whose tuples stay distinct in the output, so even mart-key-unique
        cannot catch the misdeclaration. The walk must refuse outright."""
        m = self._model("""
with agg as (
  select entity_id, date_day, sum(v) as total, max(a) as x, max(b) as y,
         max(c) as z
  from "db"."x"."stg" group by 1, 2
)
select entity_id, max(date_day) as date_day, sum(total) as total,
       max(x) as x, max(y) as y, max(z) as z
from agg group by entity_id || 'suffix'
""", ["entity_id", "date_day", "total", "x", "y", "z"])
        self.assertEqual(
            dbt_adapter._dataflow_group_by_grain(m, {c.name for c in m.columns}),
            [],
        )

    def test_the_rescue_sits_below_every_test_backed_rule(self) -> None:
        """Ladder-order pin: the rung must never override an explicit test."""
        import inspect

        # Search the rules tuple itself: the docstring above it names the same
        # rules in prose, so a plain search over the whole function finds the
        # summary rather than the ladder.
        src = inspect.getsource(dbt_adapter.mart_key_columns)
        src = src[src.index("rules: tuple["):]
        rules_order = [
            "dbt_utils.unique_combination_of_columns test",
            "unique test",
            "model GROUP BY grain",
            "not_null test",
            "inner GROUP BY grain (evidence, not proof)",
            "<entity>_id naming convention",
        ]
        positions = [src.index(r) for r in rules_order]
        self.assertEqual(positions, sorted(positions))

if __name__ == "__main__":  # pragma: no cover
    unittest.main()

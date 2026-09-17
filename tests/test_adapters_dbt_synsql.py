"""Tests for adapters/dbt.py and adapters/synsql.py.

Small inline fixtures for both real formats:
  * a dbt manifest.json with sources, models, unique/not_null/relationships
    tests, one multi-source mart component, and trivial-cut cases;
  * a SynSQL tables.json entry (column-index FK pairs, DDLs) plus data.json
    records carrying answer material that must never leak into the TaskIR.

SynSQL carries a THIRD concern beyond the two above: it was the only ingest
path that reached `Engine.register` without a contamination pre-check.
`SynSQLContaminationGateTest` pins that gate shut from both directions — the
adapter helper (which programmatic callers must go through) and the CLI
command (whose refusal semantics must match the other pools) — including the
armed-but-empty index, which stays a FAILURE rather than a clean pass.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import re
import unittest
from pathlib import Path

from elt_taskgen import cli
from elt_taskgen.adapters import dbt as dbt_adapter
from elt_taskgen.generation import mart_plan
from elt_taskgen.adapters import synsql as synsql_adapter
from elt_taskgen.verification import contamination
from elt_taskgen.models import (
    Backend,
    ColumnType,
    MartOpKind,
    Origin,
    PopulationName,
    TaskIR,
)

# ---------------------------------------------------------------------------
# dbt fixture
# ---------------------------------------------------------------------------

def _dbt_col(name: str, data_type: str | None = None, description: str = "") -> dict:
    return {"name": name, "data_type": data_type, "description": description}


def _dbt_manifest() -> dict:
    """One shop component (2 sources -> staging -> mart) + 1 orphan source."""
    return {
        "metadata": {"project_name": "shop_pkg"},
        "sources": {
            "source.shop_pkg.shop.customers": {
                "name": "customers",
                "package_name": "shop_pkg",
                "description": "Raw customers.",
                "columns": {
                    "customer_id": _dbt_col("customer_id", "integer", "Customer id."),
                    "customer_name": _dbt_col("customer_name", "varchar(64)"),
                },
            },
            "source.shop_pkg.shop.orders": {
                "name": "orders",
                "package_name": "shop_pkg",
                "columns": {
                    "order_id": _dbt_col("order_id", "bigint"),
                    "customer_id": _dbt_col("customer_id", "integer"),
                    "amount": _dbt_col("amount", "numeric(10,2)"),
                },
            },
            "source.shop_pkg.shop.unused_lookup": {
                "name": "unused_lookup",
                "package_name": "shop_pkg",
                "columns": {"code": _dbt_col("code", "text")},
            },
        },
        "nodes": {
            "model.shop_pkg.stg_orders": {
                "resource_type": "model",
                "name": "stg_orders",
                "package_name": "shop_pkg",
                "depends_on": {"nodes": ["source.shop_pkg.shop.orders"]},
                "columns": {
                    "order_id": _dbt_col("order_id", "bigint"),
                    "customer_id": _dbt_col("customer_id", "integer"),
                    "amount": _dbt_col("amount", "numeric"),
                },
            },
            "model.shop_pkg.customer_orders": {
                "resource_type": "model",
                "name": "customer_orders",
                "package_name": "shop_pkg",
                "description": "One row per customer with order totals.",
                "depends_on": {
                    "nodes": [
                        "source.shop_pkg.shop.customers",
                        "model.shop_pkg.stg_orders",
                    ]
                },
                "columns": {
                    "customer_id": _dbt_col("customer_id", "integer", "Customer key."),
                    "order_count": _dbt_col("order_count", "integer", "Orders per customer."),
                    "total_amount": _dbt_col("total_amount", "numeric", "Total spend."),
                },
            },
            "test.shop_pkg.unique_customers_customer_id": {
                "resource_type": "test",
                "test_metadata": {"name": "unique", "kwargs": {"column_name": "customer_id"}},
                "attached_node": "source.shop_pkg.shop.customers",
                "depends_on": {"nodes": ["source.shop_pkg.shop.customers"]},
            },
            "test.shop_pkg.not_null_customers_customer_id": {
                "resource_type": "test",
                "test_metadata": {"name": "not_null", "kwargs": {"column_name": "customer_id"}},
                "attached_node": "source.shop_pkg.shop.customers",
                "depends_on": {"nodes": ["source.shop_pkg.shop.customers"]},
            },
            "test.shop_pkg.not_null_orders_customer_id": {
                "resource_type": "test",
                "test_metadata": {"name": "not_null", "kwargs": {"column_name": "customer_id"}},
                "attached_node": "source.shop_pkg.shop.orders",
                "depends_on": {"nodes": ["source.shop_pkg.shop.orders"]},
            },
            "test.shop_pkg.relationships_orders_customer_id": {
                "resource_type": "test",
                "test_metadata": {
                    "name": "relationships",
                    "kwargs": {
                        "column_name": "customer_id",
                        "to": "source('shop', 'customers')",
                        "field": "customer_id",
                    },
                },
                "attached_node": "source.shop_pkg.shop.orders",
                "depends_on": {
                    "nodes": [
                        "source.shop_pkg.shop.customers",
                        "source.shop_pkg.shop.orders",
                    ]
                },
            },
            "test.shop_pkg.unique_customer_orders_customer_id": {
                "resource_type": "test",
                "test_metadata": {"name": "unique", "kwargs": {"column_name": "customer_id"}},
                "attached_node": "model.shop_pkg.customer_orders",
                "depends_on": {"nodes": ["model.shop_pkg.customer_orders"]},
            },
        },
    }


def _write_json(tmpdir: str, name: str, payload: object) -> Path:
    path = Path(tmpdir) / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class DbtAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manifest_path = _write_json(self.tmp.name, "manifest.json", _dbt_manifest())

    def test_load_manifest_parses_sources_models_tests(self) -> None:
        spec = dbt_adapter.load_manifest(self.manifest_path)
        self.assertEqual(spec.package_name, "shop_pkg")
        self.assertEqual(len(spec.sources), 3)
        self.assertEqual({m.name for m in spec.models}, {"stg_orders", "customer_orders"})
        kinds = sorted(t.test_name for t in spec.tests)
        self.assertEqual(kinds, ["not_null", "not_null", "relationships", "unique", "unique"])
        rel = next(t for t in spec.tests if t.test_name == "relationships")
        self.assertEqual(rel.attached_to, "source.shop_pkg.shop.orders")
        self.assertEqual(rel.to, "source.shop_pkg.shop.customers")
        self.assertEqual(rel.to_field, "customer_id")

    def test_extract_tasks_builds_complete_task(self) -> None:
        spec = dbt_adapter.load_manifest(self.manifest_path)
        tasks = dbt_adapter.extract_tasks(spec)
        self.assertEqual(len(tasks), 1)  # orphan unused_lookup skipped
        task = tasks[0]
        self.assertIsInstance(task, TaskIR)
        self.assertEqual(task.origin, Origin.DBT)
        self.assertEqual(task.family_id, "dbt__shop_pkg")
        # full source closure of the mart: both source tables, not stg model
        self.assertEqual({t.name for t in task.tables}, {"customers", "orders"})
        # tests became schema facts
        customers = task.table("customers")
        self.assertEqual(customers.primary_key, ("customer_id",))
        self.assertFalse(customers.column("customer_id").nullable)
        self.assertTrue(customers.column("customer_name").nullable)
        self.assertEqual(task.table("orders").column("amount").type, ColumnType.DECIMAL)
        # relationship test became a Relationship (required: child not_null)
        self.assertEqual(len(task.relationships), 1)
        rel = task.relationships[0]
        self.assertEqual((rel.child_table, rel.parent_table), ("orders", "customers"))
        self.assertTrue(rel.required)
        # terminal model became the mart, with a plan covering the closure
        self.assertEqual([m.name for m in task.marts], ["customer_orders"])
        mart = task.marts[0]
        self.assertEqual(mart.key_columns, ("customer_id",))
        source_ops = [op for op in mart.plan.ops if op.kind == MartOpKind.SOURCE]
        self.assertEqual(sorted(t for op in source_ops for t in op.tables),
                         ["customers", "orders"])
        self.assertTrue(any(op.kind == MartOpKind.JOIN for op in mart.plan.ops))
        # every table has exactly one backend assignment
        self.assertEqual({b.table for b in task.backends}, {"customers", "orders"})
        for b in task.backends:
            self.assertIsInstance(b.backend, Backend)

    def test_extract_tasks_is_deterministic(self) -> None:
        spec = dbt_adapter.load_manifest(self.manifest_path)
        t1 = dbt_adapter.extract_tasks(spec)[0]
        t2 = dbt_adapter.extract_tasks(dbt_adapter.load_manifest(self.manifest_path))[0]
        self.assertEqual(t1.content_hash(), t2.content_hash())
        self.assertEqual(t1.task_id, t2.task_id)

    def test_trivial_single_source_cut_rejected(self) -> None:
        manifest = {
            "metadata": {"project_name": "tiny_pkg"},
            "sources": {
                "source.tiny_pkg.raw.events": {
                    "name": "events",
                    "package_name": "tiny_pkg",
                    "columns": {"event_id": _dbt_col("event_id", "integer")},
                },
            },
            "nodes": {
                "model.tiny_pkg.events_copy": {
                    "resource_type": "model",
                    "name": "events_copy",
                    "package_name": "tiny_pkg",
                    "depends_on": {"nodes": ["source.tiny_pkg.raw.events"]},
                    "columns": {"event_id": _dbt_col("event_id", "integer")},
                },
            },
        }
        path = _write_json(self.tmp.name, "tiny_manifest.json", manifest)
        spec = dbt_adapter.load_manifest(path)
        with self.assertRaises(ValueError):
            dbt_adapter.extract_tasks(spec)

    def test_source_without_columns_rejected(self) -> None:
        manifest = _dbt_manifest()
        manifest["sources"]["source.shop_pkg.shop.orders"]["columns"] = {}
        path = _write_json(self.tmp.name, "nocol_manifest.json", manifest)
        spec = dbt_adapter.load_manifest(path)
        with self.assertRaises(ValueError):
            dbt_adapter.extract_tasks(spec)


# Mart-key tests exclude measured Fivetran load metadata and dbt snapshot columns.
_REAL_METADATA_COLUMNS = (
    "_fivetran_synced",
    "_fivetran_deleted",
    "_fivetran_active",
    "_fivetran_id",
    "_fivetran_user_id",
    "_fivetran_synced_date",
    "_fivetran_start",
    "_fivetran_end",
    "fivetran_id",
    "fivetran_synced",
    "transaction_line_fivetran_synced_date",
    "dbt_run_date",
    "_file",
    "_line",
    "_modified",
)


class DbtMartKeyTest(unittest.TestCase):
    """`MartSpec.key_columns` is the mart's GRAIN — it must be evidence.

    It is exported as the private `answer_key/sort_key.json`, shown to the
    solver and the independent implementer as the mart's grain, and — the part
    that bites — `verification/gates.py` uses it to PARTITION mart columns into
    identifying and informative ones. The pre-existing adapter fell back to
    `col_names[0]`, which measured out as `_fivetran_synced` (a Fivetran load
    timestamp) on 12 of 84 real terminal marts and an arbitrary column on 69.
    These tests pin the replacement ladder and, above all, its two invariants.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _mart_of(self, manifest: dict, name: str = "manifest.json"):
        path = _write_json(self.tmp.name, name, manifest)
        spec = dbt_adapter.load_manifest(path)
        task = dbt_adapter.extract_tasks(spec)[0]
        return task.marts[0]

    def _ladder_key(
        self,
        manifest: dict,
        model: str = "model.shop_pkg.customer_orders",
        name: str = "manifest.json",
    ) -> tuple[tuple[str, ...], str]:
        """The KEY LADDER's own answer — `mart_key_columns(spec, model)`.

        WHY THESE TESTS READ THE LADDER AND NOT THE EMITTED `MartSpec`. The two
        used to be the same tuple and are no longer, in both directions:

          * a ROLLUP's declared grain is now its WHOLE GROUP BY (the evidence
            key plus every attribute the model projects alongside it), because
            nothing in a dbt manifest proves those attributes are functionally
            dependent on the evidence key — measured false in 25 of 48 vendored
            marts, which is exactly how a mart came to declare "one row per
            ad_group_id, date_day" over gold that had several;
          * a PROJECTION whose key is a foreign key is DROPPED rather than
            emitted, so there is no MartSpec to read at all.

        Neither of those is a change to the ladder, which is what this class
        pins. `RollupGrainIsTheGroupByTest` and `ProjectionGrainMustBeMintable`
        pin the two declaration rules separately, so no rule is asserted twice
        and none is asserted nowhere.
        """
        path = _write_json(self.tmp.name, name, manifest)
        spec = dbt_adapter.load_manifest(path)
        return dbt_adapter.mart_key_columns(spec, spec.node_by_id()[model])

    @staticmethod
    def _with_mart(columns: dict, *, tests: dict | None = None,
                   raw_code: str = "", description: str = "") -> dict:
        """Base fixture with the mart's columns / tests / SQL replaced.

        The mart's non-aggregate columns are also added to the `orders` SOURCE
        so the mart GROUNDS (adapters/dbt.py::ground_mart): a declared column
        with no lineage back to a source column cannot be produced by any
        reference solution, so the adapter drops such a mart. These fixtures
        exercise the KEY LADDER, so they must be groundable for the ladder's
        answer to be observable on the emitted MartSpec.
        """
        manifest = _dbt_manifest()
        mart = manifest["nodes"]["model.shop_pkg.customer_orders"]
        mart["columns"] = {c: _dbt_col(c, t) for c, t in columns.items()}
        orders = manifest["sources"]["source.shop_pkg.shop.orders"]["columns"]
        for c, t in columns.items():
            orders.setdefault(c, _dbt_col(c, t))
        mart["raw_code"] = raw_code
        if description:
            mart["description"] = description
        del manifest["nodes"]["test.shop_pkg.unique_customer_orders_customer_id"]
        for tid, node in (tests or {}).items():
            manifest["nodes"][tid] = node
        return manifest

    @staticmethod
    def _test_node(name: str, kwargs: dict) -> dict:
        return {
            "resource_type": "test",
            "test_metadata": {"name": name, "kwargs": kwargs},
            "attached_node": "model.shop_pkg.customer_orders",
            "depends_on": {"nodes": ["model.shop_pkg.customer_orders"]},
        }

    # -- the exclusion list -------------------------------------------------

    def test_every_real_fivetran_metadata_column_is_excluded(self) -> None:
        for name in _REAL_METADATA_COLUMNS:
            with self.subTest(column=name):
                self.assertTrue(dbt_adapter.is_load_metadata_column(name))
        # Business columns — including `source_relation`, which IS part of the
        # grain in Fivetran's union models — are NOT metadata.
        for name in ("customer_id", "source_relation", "date_day", "amount"):
            with self.subTest(column=name):
                self.assertFalse(dbt_adapter.is_load_metadata_column(name))

    def test_load_metadata_never_becomes_a_key(self) -> None:
        """The exact defect: `_fivetran_synced` sorts first, so it used to win."""
        manifest = self._with_mart(
            {
                "_fivetran_synced": "timestamp",
                "customer_id": "integer",
                "order_count": "integer",
            }
        )
        key, _evidence = self._ladder_key(manifest)
        self.assertEqual(key, ("customer_id",))
        for column in key:
            self.assertFalse(dbt_adapter.is_load_metadata_column(column))

    def test_a_unique_test_on_a_metadata_column_is_not_evidence(self) -> None:
        manifest = self._with_mart(
            {
                "_fivetran_id": "text",
                "customer_id": "integer",
                "order_count": "integer",
            },
            tests={
                "test.shop_pkg.unique_fivetran_id": self._test_node(
                    "unique", {"column_name": "_fivetran_id"}
                )
            },
        )
        key, _evidence = self._ladder_key(manifest)
        self.assertNotIn("_fivetran_id", key)
        self.assertEqual(key, ("customer_id",))

    # -- the ladder ---------------------------------------------------------

    def test_composite_unique_combination_of_columns_is_honored(self) -> None:
        """dbt_utils' COMPOSITE key test — the canonical multi-column key."""
        manifest = self._with_mart(
            {
                "ad_id": "integer",
                "date_day": "date",
                "clicks": "integer",
                "customer_id": "integer",
            },
            tests={
                "test.shop_pkg.combo": self._test_node(
                    "unique_combination_of_columns",
                    {"combination_of_columns": ["ad_id", "date_day"]},
                ),
                "test.shop_pkg.not_null_clicks": self._test_node(
                    "not_null", {"column_name": "clicks"}
                ),
            },
        )
        key, evidence = self._ladder_key(manifest)
        self.assertEqual(key, ("ad_id", "date_day"))
        self.assertIn("unique_combination_of_columns", evidence)

    def test_unique_test_beats_the_lower_rules(self) -> None:
        manifest = self._with_mart(
            {"customer_id": "integer", "order_count": "integer"},
            tests={
                "test.shop_pkg.unique_customer_id": self._test_node(
                    "unique", {"column_name": "customer_id"}
                ),
                "test.shop_pkg.not_null_order_count": self._test_node(
                    "not_null", {"column_name": "order_count"}
                ),
            },
        )
        key, evidence = self._ladder_key(manifest)
        self.assertEqual(key, ("customer_id",))
        self.assertIn("unique test", evidence)

    def test_group_by_grain_is_recovered_from_the_model_sql(self) -> None:
        """An aggregate mart's GROUP BY IS its grain — one row per tuple."""
        manifest = self._with_mart(
            {
                "customer_id": "integer",
                "order_month": "date",
                "total_amount": "numeric",
            },
            raw_code=(
                "with base as (select * from {{ ref('stg_orders') }})\n"
                "select customer_id, order_month, sum(amount) as total_amount\n"
                "from base group by 1, 2"
            ),
        )
        key, evidence = self._ladder_key(manifest)
        self.assertEqual(key, ("customer_id", "order_month"))
        self.assertIn("GROUP BY", evidence)

    def test_group_by_with_jinja_control_flow_falls_through(self) -> None:
        """Unrecoverable SQL yields NO key claim; the next rule decides."""
        manifest = self._with_mart(
            {"customer_id": "integer", "order_count": "integer"},
            raw_code=(
                "select customer_id{% for c in cols %}, {{ c }}{% endfor %}\n"
                "from base group by 1, 2, 3"
            ),
        )
        key, evidence = self._ladder_key(manifest)
        self.assertEqual(key, ("customer_id",))
        self.assertNotIn("GROUP BY", evidence)

    def test_not_null_set_is_the_declared_grain(self) -> None:
        """Fivetran declares not_null on exactly the grain columns of a report
        mart (measured: 51 of 62 shipped marts key off this rule)."""
        manifest = self._with_mart(
            {
                "ad_id": "integer",
                "date_day": "date",
                "clicks": "integer",
                "customer_id": "integer",
            },
            tests={
                "test.shop_pkg.nn_ad": self._test_node(
                    "not_null", {"column_name": "ad_id"}
                ),
                "test.shop_pkg.nn_day": self._test_node(
                    "not_null", {"column_name": "date_day"}
                ),
            },
        )
        key, evidence = self._ladder_key(manifest)
        self.assertEqual(key, ("ad_id", "date_day"))
        self.assertIn("not_null test", evidence)

    def test_entity_id_naming_convention(self) -> None:
        manifest = self._with_mart(
            {"customer_order_id": "integer", "amount": "numeric"}
        )
        mart = self._mart_of(manifest)
        self.assertEqual(mart.key_columns, ("customer_order_id",))
        self.assertIn("naming convention", mart.plan.notes)

    def test_ambiguous_entity_id_does_not_guess(self) -> None:
        """Two columns match the entity stem by suffix: no arbitrary pick."""
        manifest = self._with_mart(
            {
                "amazon_customer_order_id": "text",
                "seller_customer_order_id": "text",
                "amount": "numeric",
            }
        )
        path = _write_json(self.tmp.name, "ambiguous.json", manifest)
        spec = dbt_adapter.load_manifest(path)
        with self.assertRaises(dbt_adapter.KeylessMartError):
            dbt_adapter.extract_tasks(spec)

    def test_upstream_declared_key_carried_into_the_mart(self) -> None:
        """`customer_id` is declared unique+not_null on the `customers` SOURCE
        and survives into the mart under the same name."""
        manifest = self._with_mart(
            {"customer_id": "integer", "spend_bucket": "text"}
        )
        # Neutralize the naming rule: the mart is not named after `customer`.
        manifest["nodes"]["model.shop_pkg.spend_profile"] = manifest["nodes"].pop(
            "model.shop_pkg.customer_orders"
        )
        manifest["nodes"]["model.shop_pkg.spend_profile"]["name"] = "spend_profile"
        key, evidence = self._ladder_key(
            manifest, model="model.shop_pkg.spend_profile", name="upstream.json"
        )
        self.assertEqual(key, ("customer_id",))
        self.assertIn("upstream declared key", evidence)

    # -- the two invariants -------------------------------------------------

    def test_a_key_can_never_cover_every_column(self) -> None:
        """The `keys_only` degenerate probe (gates.degenerate-zero,
        attacks.KEYS_ONLY) becomes the IDENTITY query when the key covers every
        column, and gates.info-content fails a mart with no non-key column. So
        an all-columns key is not a key — this is exactly why "fall back to ALL
        columns" is wrong here, even though the comparator orders by every
        column (upstream_eval.sort_rows appends the rest itself)."""
        manifest = self._with_mart(
            {"customer_id": "integer", "order_month": "date"},
            tests={
                "test.shop_pkg.nn_a": self._test_node(
                    "not_null", {"column_name": "customer_id"}
                ),
                "test.shop_pkg.nn_b": self._test_node(
                    "not_null", {"column_name": "order_month"}
                ),
            },
        )
        key, _evidence = self._ladder_key(manifest)
        column_names = {"customer_id", "order_month"}
        self.assertTrue(set(key) < column_names)
        self.assertEqual(key, ("customer_id",))  # entity-id rule

    def test_keyless_mart_fails_closed_and_names_the_reason(self) -> None:
        """No evidence => the CUT is skipped, never keyed arbitrarily."""
        manifest = self._with_mart(
            {
                "_fivetran_synced": "timestamp",
                "amount": "numeric",
                "note": "text",
            }
        )
        path = _write_json(self.tmp.name, "keyless.json", manifest)
        spec = dbt_adapter.load_manifest(path)

        with self.assertRaises(dbt_adapter.KeylessMartError) as ctx:
            dbt_adapter.extract_tasks(spec)
        message = str(ctx.exception)
        self.assertIn("customer_orders", message)
        self.assertIn("no defensible key", message)
        self.assertIn("_fivetran_synced", message)

        result = dbt_adapter.extract_candidates(spec)
        self.assertEqual(result.tasks, ())
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("no defensible key", result.skipped[0].reason)

    def test_no_arbitrary_first_column_fallback_survives(self) -> None:
        """The old behaviour, stated as a negative: a mart with no key evidence
        must NOT silently key on its first column."""
        manifest = self._with_mart(
            {"amount": "numeric", "note": "text", "quantity": "integer"}
        )
        path = _write_json(self.tmp.name, "nofallback.json", manifest)
        spec = dbt_adapter.load_manifest(path)
        self.assertEqual(dbt_adapter.extract_candidates(spec).tasks, ())

    def test_key_derivation_is_deterministic_and_in_column_order(self) -> None:
        manifest = self._with_mart(
            {
                "zeta_amount": "numeric",
                "ad_id": "integer",
                "date_day": "date",
            },
            tests={
                "test.shop_pkg.combo": self._test_node(
                    "unique_combination_of_columns",
                    {"combination_of_columns": ["date_day", "ad_id"]},
                )
            },
        )
        path = _write_json(self.tmp.name, "order.json", manifest)
        first = dbt_adapter.extract_tasks(dbt_adapter.load_manifest(path))[0]
        second = dbt_adapter.extract_tasks(dbt_adapter.load_manifest(path))[0]
        # Model column order (name-sorted), not the order the test declared.
        key, _evidence = self._ladder_key(manifest, name="order.json")
        self.assertEqual(key, ("ad_id", "date_day"))
        # The EMITTED grain appends the attribute the GROUP BY carries — in
        # that same deterministic order, evidence key first (see `_ladder_key`).
        self.assertEqual(
            first.marts[0].key_columns, ("ad_id", "date_day", "zeta_amount")
        )
        self.assertEqual(first.content_hash(), second.content_hash())


# ---------------------------------------------------------------------------
# A keyless MART is dropped; the CUT survives
# ---------------------------------------------------------------------------

def _multi_mart_manifest() -> dict:
    """One component, two marts: `customer_orders` is keyed, `shipment_log`
    is not (its only non-metadata columns are attributes).

    `customer_orders` carries `compiled_code` so that it recovers a real
    MEASURE. It did not before, and that made it a mart whose grain is its
    whole GROUP BY with a `COUNT(*)` that is 1 on every row — a SELECT DISTINCT
    measuring nothing, which `_degenerate_rollup_problem` now drops at ingest
    (gates.info-content refused it at stage 9 anyway). This fixture is about
    dropping a KEYLESS mart, so its surviving mart has to be one that survives
    for its own reasons; `shipments.weight` exists to give the second mart of
    the disconnect variant a measure of its own.

    `shipments` is read ONLY by the keyless mart, so dropping that mart
    strands it — the case that proves pruning, not just dropping.
    """
    manifest = {
        "metadata": {"project_name": "multi_pkg"},
        "sources": {
            "source.multi_pkg.shop.customers": {
                "name": "customers",
                "package_name": "multi_pkg",
                "columns": {
                    "customer_id": _dbt_col("customer_id", "integer"),
                    "customer_name": _dbt_col("customer_name", "text"),
                },
            },
            "source.multi_pkg.shop.orders": {
                "name": "orders",
                "package_name": "multi_pkg",
                "columns": {
                    "order_id": _dbt_col("order_id", "bigint"),
                    "customer_id": _dbt_col("customer_id", "integer"),
                    "amount": _dbt_col("amount", "numeric"),
                },
            },
            "source.multi_pkg.shop.shipments": {
                "name": "shipments",
                "package_name": "multi_pkg",
                "columns": {
                    "shipment_id": _dbt_col("shipment_id", "bigint"),
                    "order_id": _dbt_col("order_id", "bigint"),
                    "carrier": _dbt_col("carrier", "text"),
                    "weight": _dbt_col("weight", "numeric"),
                },
            },
        },
        "nodes": {
            "model.multi_pkg.stg_orders": {
                "resource_type": "model",
                "name": "stg_orders",
                "package_name": "multi_pkg",
                "depends_on": {"nodes": ["source.multi_pkg.shop.orders"]},
                "columns": {"order_id": _dbt_col("order_id", "bigint")},
            },
            "model.multi_pkg.customer_orders": {
                "resource_type": "model",
                "name": "customer_orders",
                "package_name": "multi_pkg",
                "depends_on": {
                    "nodes": [
                        "source.multi_pkg.shop.customers",
                        "model.multi_pkg.stg_orders",
                    ]
                },
                "compiled_code": (
                    "select customer_id, sum(amount) as order_count "
                    "from orders group by 1"
                ),
                "columns": {
                    "customer_id": _dbt_col("customer_id", "integer"),
                    "order_count": _dbt_col("order_count", "integer"),
                },
            },
            "model.multi_pkg.shipment_log": {
                "resource_type": "model",
                "name": "shipment_log",
                "package_name": "multi_pkg",
                "depends_on": {
                    "nodes": [
                        "model.multi_pkg.stg_orders",
                        "source.multi_pkg.shop.shipments",
                    ]
                },
                "columns": {
                    "_fivetran_synced": _dbt_col("_fivetran_synced", "timestamp"),
                    "carrier": _dbt_col("carrier", "text"),
                    "note": _dbt_col("note", "text"),
                },
            },
            "test.multi_pkg.unique_customer_orders": {
                "resource_type": "test",
                "test_metadata": {
                    "name": "unique", "kwargs": {"column_name": "customer_id"}
                },
                "attached_node": "model.multi_pkg.customer_orders",
                "depends_on": {"nodes": ["model.multi_pkg.customer_orders"]},
            },
        },
    }
    return manifest


class KeylessMartDropTest(unittest.TestCase):
    """A keyless MART is dropped; its CUT survives on the evidence-backed rest.

    Keylessness is a property of one mart (`mart_key_columns` reads that
    mart's tests, its SQL, its name), but it used to be enforced on the whole
    connected component — so one mart with no key discarded every sibling mart
    in the cut, including properly keyed ones. Measured over the 16 vendored
    Fivetran packages that cost `dbt_amazon_selling_partner` entirely: 2 of its
    3 marts are keyless (that connector ships no dbt tests at all) and took
    `amazon_selling_partner__order_items` — keyed on `order_item_id` — with
    them.

    A drop is only allowed when what remains is still ONE COHERENT COMPLETE
    DATA PROJECT, which is what this class pins:
      * at least one mart survives (an all-keyless cut is still refused),
      * survivors keep their FULL closure and stay connected,
      * sources only the dropped mart read are PRUNED, not shipped unread,
      * every drop is REPORTED as a SkippedCut, so `--strict` still hard-fails.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _extract(self, manifest: dict, name: str = "multi.json"):
        path = _write_json(self.tmp.name, name, manifest)
        return dbt_adapter.extract_candidates(dbt_adapter.load_manifest(path))

    def test_keyless_mart_is_dropped_and_the_cut_survives(self) -> None:
        result = self._extract(_multi_mart_manifest())
        self.assertEqual(len(result.tasks), 1)
        task = result.tasks[0]
        self.assertEqual([m.name for m in task.marts], ["customer_orders"])
        self.assertEqual(task.marts[0].key_columns, ("customer_id",))

        drops = [s for s in result.skipped if s.scope == dbt_adapter.MART_SCOPE]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0].kind, dbt_adapter.KEYLESS_MART_SKIP)
        self.assertEqual(drops[0].members, ("model.multi_pkg.shipment_log",))
        self.assertEqual(drops[0].task_id, task.task_id)
        self.assertIn("no defensible key", drops[0].reason)
        # the reason still names the load-metadata column that is not a key
        self.assertIn("_fivetran_synced", drops[0].reason)

    def test_dropping_a_mart_prunes_the_sources_it_stranded(self) -> None:
        """A project shipping tables nothing reads is a different, easier task
        — so `shipments` leaves with the mart that was its only reader, and
        leaves REPORTED."""
        result = self._extract(_multi_mart_manifest())
        task = result.tasks[0]
        self.assertEqual({t.name for t in task.tables}, {"customers", "orders"})

        pruned = [s for s in result.skipped if s.scope == dbt_adapter.SOURCE_SCOPE]
        self.assertEqual(len(pruned), 1)
        self.assertEqual(pruned[0].kind, dbt_adapter.STRANDED_SOURCE_SKIP)
        self.assertEqual(pruned[0].members, ("source.multi_pkg.shop.shipments",))
        self.assertEqual(pruned[0].task_id, task.task_id)
        self.assertIn("shipments", pruned[0].reason)

        # and the survivor still reads every table the cut ships (full closure)
        read = {
            t
            for mart in task.marts
            for op in mart.plan.ops
            if op.kind == MartOpKind.SOURCE
            for t in op.tables
        }
        self.assertEqual(read, {t.name for t in task.tables})

    def test_strict_entry_point_still_hard_fails_on_a_per_mart_drop(self) -> None:
        """--strict means NOTHING was dropped, at any granularity."""
        path = _write_json(self.tmp.name, "strict.json", _multi_mart_manifest())
        spec = dbt_adapter.load_manifest(path)
        with self.assertRaises(dbt_adapter.KeylessMartError) as ctx:
            dbt_adapter.extract_tasks(spec)
        self.assertIn("shipment_log", str(ctx.exception))

    def test_a_cut_whose_every_mart_is_keyless_is_still_refused(self) -> None:
        manifest = _multi_mart_manifest()
        del manifest["nodes"]["test.multi_pkg.unique_customer_orders"]
        manifest["nodes"]["model.multi_pkg.customer_orders"]["columns"] = {
            "amount": _dbt_col("amount", "numeric"),
            "note": _dbt_col("note", "text"),
        }
        result = self._extract(manifest, name="allkeyless.json")
        self.assertEqual(result.tasks, ())
        self.assertEqual(len(result.skipped), 1)
        skip = result.skipped[0]
        self.assertEqual(skip.scope, dbt_adapter.CUT_SCOPE)
        self.assertEqual(skip.kind, dbt_adapter.KEYLESS_MART_SKIP)
        # the whole component is reported, with the first mart's own reason
        self.assertIn("model.multi_pkg.shipment_log", skip.members)
        self.assertIn("customer_orders", skip.reason)
        self.assertIn("no defensible key", skip.reason)

    def test_a_drop_that_splits_the_cut_in_two_refuses_the_cut(self) -> None:
        """The dropped mart was the only link between two survivors: the
        remainder is two unrelated projects stapled together, not one data
        project, so the cut is refused rather than silently redefined."""
        manifest = _multi_mart_manifest()
        # `bridge` is the only path between the customers side and the
        # shipments side; it is keyless, so dropping it disconnects the cut.
        manifest["nodes"]["model.multi_pkg.shipment_log"]["name"] = "bridge"
        manifest["nodes"]["model.multi_pkg.bridge"] = manifest["nodes"].pop(
            "model.multi_pkg.shipment_log"
        )
        manifest["nodes"]["model.multi_pkg.bridge"]["depends_on"]["nodes"] = [
            "source.multi_pkg.shop.customers",
            "source.multi_pkg.shop.shipments",
        ]
        # a SECOND keyed mart on the far side, sharing nothing with the first
        manifest["sources"]["source.multi_pkg.shop.carriers"] = {
            "name": "carriers",
            "package_name": "multi_pkg",
            "columns": {"carrier_id": _dbt_col("carrier_id", "integer")},
        }
        manifest["nodes"]["model.multi_pkg.shipment_report"] = {
            "resource_type": "model",
            "name": "shipment_report",
            "package_name": "multi_pkg",
            "depends_on": {
                "nodes": [
                    "source.multi_pkg.shop.shipments",
                    "source.multi_pkg.shop.carriers",
                ]
            },
            # A mart of its own, with its own recovered measure — otherwise it
            # is dropped as degenerate and there is no second survivor for the
            # drop to DISCONNECT from.
            "compiled_code": (
                "select shipment_id, sum(weight) as total_weight "
                "from shipments group by 1"
            ),
            "columns": {
                "shipment_id": _dbt_col("shipment_id", "bigint"),
                "total_weight": _dbt_col("total_weight", "numeric"),
            },
        }
        result = self._extract(manifest, name="disconnect.json")
        self.assertEqual(result.tasks, ())
        self.assertEqual(len(result.skipped), 1)
        skip = result.skipped[0]
        self.assertEqual(skip.scope, dbt_adapter.CUT_SCOPE)
        self.assertEqual(skip.kind, dbt_adapter.KEYLESS_MART_SKIP)
        self.assertIn("disconnects", skip.reason)

    def test_a_manifest_defect_still_kills_the_whole_cut(self) -> None:
        """Keylessness is normal in a real package; a mart with NO COLUMNS
        means the manifest is not the shape this adapter believes — that stays
        cut-level, and is reported as an unusable cut, not a keyless mart."""
        manifest = _multi_mart_manifest()
        manifest["nodes"]["model.multi_pkg.customer_orders"]["columns"] = {}
        result = self._extract(manifest, name="nocols.json")
        self.assertEqual(result.tasks, ())
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0].scope, dbt_adapter.CUT_SCOPE)
        self.assertEqual(result.skipped[0].kind, dbt_adapter.UNUSABLE_CUT_SKIP)
        self.assertIn("declares no columns", result.skipped[0].reason)

    def test_a_cut_with_no_drops_keeps_its_historical_task_id(self) -> None:
        """The id hashes the SURVIVING membership; with nothing dropped that is
        the whole component, so no existing task moves. (Verified over all 16
        vendored packages: 13 of 13 pre-existing task ids and content hashes
        are byte-identical after this change.)"""
        path = _write_json(self.tmp.name, "base.json", _dbt_manifest())
        task = dbt_adapter.extract_tasks(dbt_adapter.load_manifest(path))[0]
        self.assertEqual(task.task_id, "dbt__shop_pkg__customer_orders_3229f36d")

    def test_a_surviving_mart_always_keeps_a_column_outside_its_key(self) -> None:
        """gates.info-content needs a non-key column and the `keys_only`
        degenerate probe becomes the IDENTITY query without one. The ladder
        enforces this rule-by-rule; this pins the standing guard that a
        fabricated all-columns key DROPS the mart instead of shipping it."""
        result = self._extract(_multi_mart_manifest(), name="invariant.json")
        for mart in result.tasks[0].marts:
            with self.subTest(mart=mart.name):
                self.assertTrue(set(mart.key_columns) < {c.name for c in mart.columns})

        path = _write_json(self.tmp.name, "guard.json", _multi_mart_manifest())
        spec = dbt_adapter.load_manifest(path)
        model = next(m for m in spec.models if m.name == "customer_orders")
        with self.assertRaises(dbt_adapter.KeylessMartError):
            grounding = dbt_adapter.ground_mart(
                model,
                tuple(c.name for c in model.columns),
                ["customers", "orders"],
                {n.name: {c.name for c in n.columns} for n in spec.sources},
                dbt_adapter.staging_alias_map(spec),
            )
            dbt_adapter._model_to_mart(
                spec,
                model,
                ["customers", "orders"],
                (),
                grounding,
                "fabricated",
                {n.name: {c.name for c in n.columns} for n in spec.sources},
                {},
            )

    def test_partially_grounded_grain_drops_the_mart(self) -> None:
        """I6-A: a grain the base can only PARTIALLY reproduce is refused
        whole, not shipped narrower. Measured: reddit `url_report`'s evidence
        key (ad_id, base_url, date_day) shipped as (ad_id, date_day,
        account_id) — a byte-identical clone of `ad_report`."""
        manifest = _multi_mart_manifest()
        model = manifest["nodes"]["model.multi_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "base_url": _dbt_col("base_url", "text"),
            "amount": _dbt_col("amount", "numeric"),
        }
        # `base_url` is computed from a column no source declares: no lineage.
        model["compiled_code"] = (
            "select customer_id, split_part(click_url, '?', 1) as base_url, "
            "sum(amount) as amount from orders group by 1, 2"
        )
        del manifest["nodes"]["test.multi_pkg.unique_customer_orders"]
        for col in ("customer_id", "base_url"):
            manifest["nodes"][f"test.multi_pkg.not_null_customer_orders_{col}"] = {
                "resource_type": "test",
                "test_metadata": {"name": "not_null", "kwargs": {"column_name": col}},
                "attached_node": "model.multi_pkg.customer_orders",
                "depends_on": {"nodes": ["model.multi_pkg.customer_orders"]},
            }
        # A sibling that survives on its own, so the drop is per-mart.
        manifest["nodes"]["model.multi_pkg.shipment_log"]["compiled_code"] = (
            "select order_id, sum(weight) as total_weight from shipments group by 1"
        )
        manifest["nodes"]["model.multi_pkg.shipment_log"]["columns"] = {
            "order_id": _dbt_col("order_id", "bigint"),
            "total_weight": _dbt_col("total_weight", "numeric"),
        }
        manifest["nodes"]["test.multi_pkg.not_null_shipment_log_order_id"] = {
            "resource_type": "test",
            "test_metadata": {"name": "not_null", "kwargs": {"column_name": "order_id"}},
            "attached_node": "model.multi_pkg.shipment_log",
            "depends_on": {"nodes": ["model.multi_pkg.shipment_log"]},
        }
        result = self._extract(manifest, name="partial.json")
        self.assertEqual(len(result.tasks), 1, [s.reason for s in result.skipped])
        self.assertEqual([m.name for m in result.tasks[0].marts], ["shipment_log"])
        drops = [s for s in result.skipped if s.scope == dbt_adapter.MART_SCOPE]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0].kind, dbt_adapter.KEYLESS_MART_SKIP)
        self.assertEqual(drops[0].members, ("model.multi_pkg.customer_orders",))
        self.assertIn("only partially grounds", drops[0].reason)
        self.assertIn("base_url", drops[0].reason)

    def test_same_named_sources_in_two_schemas_are_a_typed_skip(self) -> None:
        """I4(e): two sources sharing a NAME cannot both be TaskIR tables; the
        component is refused (typed, cut-scoped), never merged by name."""
        manifest = _multi_mart_manifest()
        manifest["sources"]["source.multi_pkg.archive.orders"] = {
            "name": "orders",
            "package_name": "multi_pkg",
            "columns": {"order_id": _dbt_col("order_id", "bigint")},
        }
        manifest["nodes"]["model.multi_pkg.stg_orders"]["depends_on"]["nodes"].append(
            "source.multi_pkg.archive.orders"
        )
        result = self._extract(manifest, name="samename.json")
        self.assertEqual(result.tasks, ())
        self.assertEqual(len(result.skipped), 1)
        skip = result.skipped[0]
        self.assertEqual(skip.scope, dbt_adapter.CUT_SCOPE)
        self.assertEqual(skip.kind, dbt_adapter.UNUSABLE_CUT_SKIP)
        self.assertIn("same-named sources", skip.reason)
        self.assertIn("source.multi_pkg.archive.orders", skip.reason)
        self.assertIn("source.multi_pkg.shop.orders", skip.reason)


# ---------------------------------------------------------------------------
# A mart may only DECLARE a grain the factory can make TRUE
# ---------------------------------------------------------------------------

class GrainHonestyTest(unittest.TestCase):
    """The adapter may not declare a grain and hope — the pool's whole blocker.

    Three vendored records were refused for one defect wearing three faces, and
    all three were an ASSERTION MADE IN THE ADAPTER THAT NOTHING TESTED until a
    whole council run later:

      * `dbt__reddit_ads__account_report` — `GoldGrainError` at reference:
        `reddit_ads__ad_group_report`, key `{ad_group_id: None, date_day: None}`
        63 times on `resampled`. The grain rested on NULLABLE source columns.
      * `dbt5 / fivetran_platform` — `GoldGrainError` at reference:
        `fivetran_platform__audit_user_activity`, key `{user_id: ''}` 58 times
        on `primary`. Same NULL, flattened to `''` by a CSV backend.
      * `dbt__snapchat_ads__ad_report` — dual-build agreement 0.0 on all five
        populations, because the mart declared a currency measure with no unit.

    This class pins the four rules that make those unconstructible. Each one is
    checkable at INGEST, for $0, before a single row is generated — which is
    the point: `reference/gold.py::GoldGrainError` catches the same thing, but
    only after generation and a reference run.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _task(self, manifest: dict, name: str):
        # `extract_candidates`, not `extract_tasks`: the multi-mart fixture
        # legitimately DROPS its keyless mart, and the strict entry point
        # hard-fails on any drop (that is what --strict means).
        path = _write_json(self.tmp.name, name, manifest)
        result = dbt_adapter.extract_candidates(dbt_adapter.load_manifest(path))
        return result.tasks[0]

    # -- rule 1: a rollup's declared grain IS its GROUP BY -------------------

    def test_a_rollup_declares_its_whole_group_by_as_the_grain(self) -> None:
        """The evidence key alone is NOT the grain when the model projects
        attributes alongside it: nothing in a dbt manifest proves an attribute
        is functionally dependent on the key, and measured over the vendored
        pool it is false in 25 of 48 marts. Declaring the narrow key is what
        put duplicate keys in gold; declaring the GROUP BY is what the plan
        mechanically produces."""
        manifest = _multi_mart_manifest()
        model = manifest["nodes"]["model.multi_pkg.customer_orders"]
        model["columns"]["order_month"] = _dbt_col("order_month", "date")
        model["compiled_code"] = (
            "select customer_id, order_month, sum(amount) as order_count "
            "from orders group by 1, 2"
        )
        manifest["sources"]["source.multi_pkg.shop.orders"]["columns"][
            "order_month"
        ] = _dbt_col("order_month", "date")
        task = self._task(manifest, "rollup.json")
        mart = next(m for m in task.marts if m.name == "customer_orders")
        self.assertEqual(set(mart.key_columns), {"customer_id", "order_month"})
        self.assertIn("One row per", mart.grain)
        # …and the grain SENTENCE names exactly those columns, so the prose and
        # the tuple cannot drift (they did: "per ad group, per day" over a
        # three-column key).
        for column in mart.key_columns:
            self.assertIn(column, mart.grain)

    # -- rule 2: a projection's key must be MINTABLE -------------------------

    def test_a_projection_keyed_on_a_foreign_key_is_dropped(self) -> None:
        """`_source_to_table` can mint an identity column uniquely, but a
        foreign key is drawn from the parent's pool WITH REPETITION — that is
        what a foreign key IS — so no minting makes "one row per FK" true of a
        one-row-per-source-row mart. The mart is dropped, per-mart, with the
        reason naming the column and the relationship."""
        manifest = _dbt_manifest()
        # `customer_orders` projects orders (one row per order) and the base
        # fixture declares orders.customer_id -> customers.customer_id.
        manifest["nodes"]["model.shop_pkg.customer_orders"]["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "amount": _dbt_col("amount", "numeric"),
        }
        path = _write_json(self.tmp.name, "fk.json", manifest)
        result = dbt_adapter.extract_candidates(dbt_adapter.load_manifest(path))
        self.assertEqual(result.tasks, ())
        reason = result.skipped[0].reason
        self.assertIn("CHILD side", reason)
        self.assertIn("customer_id", reason)
        self.assertIn("cannot be minted unique", reason)

    # -- rule 3: a grain column is NEVER nullable ---------------------------

    def test_every_grain_column_is_declared_not_null_on_its_source(self) -> None:
        """The two `GoldGrainError` records, at the place they become
        impossible. A grain column that is NULL is not an identity, and a CSV
        backend turns the NULL into `''` — which is how `{user_id: ''}` came to
        appear 58 times. `_source_to_table` declares every column any surviving
        mart's grain grounds to NOT NULL, and `generation/source_data.py` only
        nulls `col.nullable` columns."""
        task = self._task(_multi_mart_manifest(), "notnull.json")
        by_name = {t.name: t for t in task.tables}
        for mart in task.marts:
            bound = dbt_adapter._grain_bindings(mart, set(by_name))
            for column in mart.key_columns:
                with self.subTest(mart=mart.name, column=column):
                    table, source = bound[column]
                    self.assertFalse(
                        by_name[table].column(source).nullable,
                        f"{table}.{source} backs a declared grain and is nullable",
                    )

    # -- rule 4: the standing guard -----------------------------------------

    def test_verify_grain_refuses_a_declaration_the_plan_does_not_produce(
        self,
    ) -> None:
        """`verify_grain` re-derives the grain from the EMITTED PLAN, not from
        the builder's local variables, so a builder that drifts from its own
        declaration is caught rather than agreed with. Simulated here by
        widening the plan's GROUP BY under a key the MartSpec still declares —
        the exact shape of the 25-of-48 defect."""
        task = self._task(_multi_mart_manifest(), "guard.json")
        mart = task.marts[0]
        drifted = mart.model_copy(
            update={
                "plan": mart.plan.model_copy(
                    update={
                        "ops": tuple(
                            op.model_copy(
                                update={
                                    "details": {
                                        **op.details,
                                        "group_by": '"customer_id", "order_count"',
                                    }
                                }
                            )
                            if op.details.get("group_by")
                            else op
                            for op in mart.plan.ops
                        )
                    }
                )
            }
        )
        with self.assertRaises(dbt_adapter.DeclaredGrainViolation) as ctx:
            dbt_adapter.verify_grain(task.model_copy(update={"marts": (drifted,)}))
        self.assertIn("groups by", str(ctx.exception))

    def test_verify_grain_passes_every_task_the_adapter_emits(self) -> None:
        """The guard is not vacuous: it runs on the built cut and it is quiet."""
        for manifest, name in (
            (_dbt_manifest(), "vg_base.json"),
            (_multi_mart_manifest(), "vg_multi.json"),
        ):
            with self.subTest(manifest=name):
                dbt_adapter.verify_grain(self._task(manifest, name))


class SourceUnitsCarriedTest(unittest.TestCase):
    """A measure whose SOURCE states a unit must state it too — see
    `adapters/dbt._carry_units`.

    `dbt__snapchat_ads__ad_report` was refused with dual-build agreement 0.0 on
    all five populations: the witness divided currency by 1e6 (matching the
    real Fivetran staging model, `(spend / 1000000.0) as spend`) while gold
    summed the raw column. Nothing was wrong with the data — the bundle
    published `spend` with UNDECLARED UNITS while publishing the source column
    as "in microdollars", so two competent implementations legitimately
    disagreed by exactly 1e6 and the gate correctly refused.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _mart(self, source_description: str, mart_description: str = "Spend."):
        manifest = _multi_mart_manifest()
        manifest["sources"]["source.multi_pkg.shop.orders"]["columns"]["amount"] = (
            _dbt_col("amount", "numeric", source_description)
        )
        model = manifest["nodes"]["model.multi_pkg.customer_orders"]
        model["columns"]["order_count"] = _dbt_col(
            "order_count", "numeric", mart_description
        )
        path = _write_json(self.tmp.name, "units.json", manifest)
        task = dbt_adapter.extract_candidates(
            dbt_adapter.load_manifest(path)
        ).tasks[0]
        mart = next(m for m in task.marts if m.name == "customer_orders")
        return next(c for c in mart.columns if c.name == "order_count")

    def test_a_stated_source_unit_is_carried_into_the_mart_column(self) -> None:
        column = self._mart("The amount of spend in microdollars.")
        self.assertIn("Units: microdollars", column.description)
        self.assertIn("orders.amount", column.description)
        self.assertIn("no unit conversion", column.description)

    def test_a_source_that_states_no_unit_adds_nothing(self) -> None:
        """Silence is not evidence. Where NEITHER side states a unit the
        dual-build gate IS the detector and must keep refusing — this adapter
        does not invent a unit to quiet it."""
        column = self._mart("The amount of the order.")
        self.assertTrue(column.description.startswith("Spend."))
        self.assertIn("Per-input source lineage", column.description)
        self.assertIn("`orders.amount`", column.description)

    def test_a_mart_that_already_states_the_unit_is_left_alone(self) -> None:
        column = self._mart(
            "The amount of spend in microdollars.",
            mart_description="Spend, in microdollars.",
        )
        self.assertTrue(column.description.startswith("Spend, in microdollars."))
        self.assertIn("Per-input source lineage", column.description)
        self.assertIn("`orders.amount`", column.description)

    def test_a_false_conversion_claim_is_contradicted_in_place(self) -> None:
        """Fivetran documents `reddit_ads__ad_group_report.spend` as "Spend
        converted out of microcurrency (so Spend/1,000,000)" while no compiled
        model in that package divides by 1,000,000. Republishing that sentence
        unchanged publishes a FALSEHOOD ABOUT GOLD — it tells the independent
        implementer to divide a number gold never divided."""
        column = self._mart(
            "The amount (in microcurrency) spent for this report period.",
            mart_description="Spend converted out of microcurrency (so Spend/1,000,000)",
        )
        self.assertIn("applies NO such conversion", column.description)
        self.assertIn("micro-currency", column.description)

    def test_the_unit_vocabulary_is_closed_and_does_not_fire_on_prose(self) -> None:
        """An open-ended "does this mention a unit" heuristic would fire on
        `pricing_unit`, `item_height_unit` and "measured in the currency used
        in the account" — none of which state the unit of a number this factory
        sums. Measured across the source descriptions of the 15 buildable
        vendored packages, the closed vocabulary matches 18 columns and those
        three phrasings are not among them."""
        for prose in (
            "Measurement unit of the dimension value.",
            "The pricing unit AWS used to calculate your usage cost.",
            "Spend is measured in the currency used in the account.",
            "The duration of the task in seconds.",
        ):
            with self.subTest(prose=prose):
                self.assertIsNone(dbt_adapter._stated_unit(prose))
        for prose, unit in (
            ("The amount of spend in microdollars.", "microdollars"),
            ("The amount (in microcurrency) spent.", "micro-currency"),
            ("Daily Spend Cap (micro-currency)", "micro-currency"),
            ("The duration of the video in milliseconds.", "milliseconds"),
            # The real Fivetran twitter_ads wording, which matched nothing
            # until batch10 2026-09-11.
            (
                "The spend for the line item + keyword on that day, in micros "
                "and in whichever currency was selected during account creation.",
                "micros",
            ),
            ("The spend (in micros) for the account on that day.", "micros"),
        ):
            with self.subTest(prose=prose):
                self.assertEqual(dbt_adapter._stated_unit(prose), unit)

    def test_a_measure_that_converts_states_the_conversion(self) -> None:
        """batch10 2026-09-11, dbt__twitter_ads: `_carry_units` stated the
        unit when gold applied NO conversion and contradicted a description
        that claimed one, but returned the description untouched when gold DID
        convert. So `spend (float)` shipped as "The spend for the account on
        that day" with a lineage line naming `billed_charge_local_micro`,
        beside `spend_micro (bigint)` built from the same source column, and
        no rule anywhere stated the division by a million. The ambiguity
        critic filed it as a graded fork worth 1e6 and the task blocked."""
        source = {"promoted_tweet_report": {
            "billed_charge_local_micro": "The spend in micro-dollars."}}
        origins = (("promoted_tweet_report", "billed_charge_local_micro"),)

        converted = dbt_adapter._carry_units(
            "The spend for the account on that day.", origins, source,
            converts="SUM(ROUND(billed_charge_local_micro / 1000000.0, 2))",
        )
        self.assertIn("divided by 1,000,000", converted)
        self.assertIn("rounded to 2 decimal places", converted)
        # Round-before-sum and round-after-sum disagree on real data, so which
        # one gold does is part of the contract.
        self.assertIn("those values are then added together", converted)

        # The sibling column divides by nothing, so no division is claimed of
        # it. It already states its own unit, so nothing is added either.
        stated = dbt_adapter._carry_units(
            "The spend (in micros) for the account on that day.", origins,
            source, converts="SUM(billed_charge_local_micro)",
        )
        self.assertEqual(
            stated, "The spend (in micros) for the account on that day.")

        # A silent sibling still gets the carried-unit sentence: branching on
        # whether an expression was PASSED rather than on whether it converts
        # silenced this one.
        silent = dbt_adapter._carry_units(
            "The spend for the account on that day.", origins, source,
            converts="SUM(billed_charge_local_micro)",
        )
        self.assertIn("carried unchanged from", silent)
        self.assertNotIn("divided by", silent)

    def test_the_conversion_sentence_is_silent_on_a_shape_it_cannot_read(self):
        """Only ONE division by ONE numeric literal, with at most one ROUND,
        is described. A ratio of two aggregates or a division by a column is
        described by silence rather than by a half-true claim."""
        for expression in (
            "SUM(billed_charge_local_micro)",
            "SUM(a / b)",
            "SUM(a / 1000000.0) / COUNT(b)",
        ):
            with self.subTest(expression=expression):
                self.assertEqual(
                    dbt_adapter._conversion_sentence(expression, "microdollars", "t.c"),
                    "",
                )

    def test_a_recovered_measure_states_how_it_combines_its_rows(self):
        """batch10 2026-09-11, dbt__twitter_ads: six measures said only
        "`X` is the value of source column `T.X`" and the ambiguity critic
        read that as taken per row, against the aggregate rule's "for that
        row's matching rows". The outer aggregate of the emitted expression
        says which it is; a non-aggregate expression adds nothing."""
        self.assertEqual(
            dbt_adapter._aggregation_clause("SUM(COALESCE(CAST(c AS BIGINT), 0))"),
            "added up over the source rows that share the mart row's grain",
        )
        self.assertIn("largest value", dbt_adapter._aggregation_clause("MAX(updated_at)"))
        self.assertIn("counted", dbt_adapter._aggregation_clause("COUNT(DISTINCT id)"))
        self.assertEqual(dbt_adapter._aggregation_clause("clicks + impressions"), "")
        self.assertEqual(dbt_adapter._aggregation_clause("SUM(a) / NULLIF(COUNT(b), 0)"), "")
        described = dbt_adapter._carry_measure_lineage(
            "The clicks for the account on that day.",
            (("promoted_tweet_report", "clicks", "clicks"),),
            "SUM(clicks)",
        )
        self.assertTrue(described.endswith(
            "`clicks` is the value of source column `promoted_tweet_report.clicks`, "
            "added up over the source rows that share the mart row's grain."))
        # A converted measure's sentence already says how its values combine
        # (per value, then added); a second "added up" beside it read as
        # "sum the raw micros, then divide" — a different number.
        converted = dbt_adapter._carry_measure_lineage(
            "The spend. Units: converted out of micros — each source value is "
            "divided by 1,000,000 and rounded to 2 decimal places, and those "
            "values are then added together.",
            (("promoted_tweet_report", "billed_charge_local_micro", "billed_charge_local_micro"),),
            "SUM(ROUND(billed_charge_local_micro / 1000000.0, 2))",
        )
        self.assertNotIn("added up over", converted)
        self.assertTrue(converted.endswith("`promoted_tweet_report.billed_charge_local_micro`."))

    def test_the_conversion_sentence_says_which_side_of_the_aggregate(self):
        """Dividing each source value and dividing the added-up total are
        different marts; so are rounding before and after the addition."""
        each = dbt_adapter._conversion_sentence(
            "SUM(x / 1000000.0)", "microdollars", "t.c")
        self.assertIn("each source value is divided by 1,000,000", each)
        total = dbt_adapter._conversion_sentence(
            "ROUND(SUM(x) / 1000000.0, 2)", "microdollars", "t.c")
        self.assertIn("the added-up total is divided by 1,000,000", total)
        split = dbt_adapter._conversion_sentence(
            "ROUND(SUM(x / 1000000.0), 2)", "microdollars", "t.c")
        self.assertIn("each source value is divided by 1,000,000", split)
        self.assertIn("the added-up total is rounded to 2 decimal places", split)


# ---------------------------------------------------------------------------
# SynSQL fixture (real format: index-pair FKs, DDL list, global column arrays)
# ---------------------------------------------------------------------------

_SYNSQL_QUESTION = "How many completed orders does each customer have in total?"
_SYNSQL_SQL = (
    "SELECT c.customer_id, COUNT(DISTINCT o.order_id) AS n FROM customers c "
    "LEFT JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id"
)
_SYNSQL_COT = "First join the customers table to orders, then count distinct order ids."
_SYNSQL_EK = "A completed order is an order whose status equals completed exactly."


def _synsql_tables_entry() -> dict:
    # global column order: 0:* 1:customers.customer_id 2:customers.customer_name
    # 3:orders.order_id 4:orders.customer_id 5:orders.status 6:orders.amount
    return {
        "db_id": "retail_orders",
        "ddls": [
            (
                "CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, "
                "customer_name TEXT NOT NULL)"
            ),
            (
                "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, "
                "customer_id INTEGER, "
                "status TEXT NOT NULL CHECK(status IN ('completed','cancelled')), "
                "amount REAL)"
            ),
        ],
        "table_names": ["customers", "orders"],
        "table_names_original": ["customers", "orders"],
        "column_names": [
            [-1, "*"],
            [0, "customer id"],
            [0, "customer name"],
            [1, "order id"],
            [1, "customer id"],
            [1, "order status"],
            [1, "order amount"],
        ],
        "column_names_original": [
            [-1, "*"],
            [0, "customer_id"],
            [0, "customer_name"],
            [1, "order_id"],
            [1, "customer_id"],
            [1, "status"],
            [1, "amount"],
        ],
        "column_types": ["text", "number", "text", "number", "number", "text", "number"],
        "foreign_keys": [[4, 1]],
        "primary_keys": [1, 3],
    }


def _synsql_records() -> list[dict]:
    return [
        {
            "db_id": "retail_orders",
            "sql_complexity": "moderate",
            "question_style": "colloquial",
            "question": _SYNSQL_QUESTION,
            "external_knowledge": _SYNSQL_EK,
            "cot": _SYNSQL_COT,
            "sql": _SYNSQL_SQL,
        },
        {
            "db_id": "other_db",
            "sql_complexity": "simple",
            "question_style": "formal",
            "question": "List all codes.",
            "external_knowledge": "",
            "cot": "Trivial scan.",
            "sql": "SELECT code FROM lookup",
        },
    ]


class SynSQLAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tables_path = _write_json(
            self.tmp.name, "tables.json", [_synsql_tables_entry()]
        )
        self.data_path = _write_json(self.tmp.name, "data.json", _synsql_records())

    # -- record ingestion + quarantine ------------------------------------

    def test_iter_records_streams_json_array(self) -> None:
        records = list(synsql_adapter.iter_records(self.data_path))
        self.assertEqual([r.db_id for r in records], ["retail_orders", "other_db"])
        self.assertEqual(records[0].sql_complexity, "moderate")
        self.assertEqual(records[0].question_style, "colloquial")

    def test_iter_records_handles_jsonl(self) -> None:
        path = Path(self.tmp.name) / "data.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for rec in _synsql_records():
                fh.write(json.dumps(rec) + "\n")
        records = list(synsql_adapter.iter_records(path))
        self.assertEqual(len(records), 2)

    def test_answer_fields_never_in_model_dump(self) -> None:
        rec = next(synsql_adapter.iter_records(self.data_path))
        dump = rec.model_dump()
        dump_json = rec.model_dump_json()
        for field in synsql_adapter.QUARANTINED_FIELDS:
            self.assertNotIn(field, dump)
            self.assertNotIn(field, json.loads(dump_json))
        for secret in (_SYNSQL_QUESTION, _SYNSQL_SQL, _SYNSQL_COT, _SYNSQL_EK):
            self.assertNotIn(secret, dump_json)
        # quarantined access is explicit and complete
        q = rec.quarantined_texts()
        self.assertEqual(q["question"], _SYNSQL_QUESTION)
        self.assertEqual(q["sql"], _SYNSQL_SQL)

    # -- schema conversion --------------------------------------------------

    def test_schema_to_tables(self) -> None:
        tables, rels = synsql_adapter.schema_to_tables(_synsql_tables_entry())
        self.assertEqual([t.name for t in tables], ["customers", "orders"])
        customers, orders = tables
        self.assertEqual(customers.primary_key, ("customer_id",))
        self.assertEqual(orders.primary_key, ("order_id",))
        # '*' pseudo-column skipped
        self.assertEqual([c.name for c in customers.columns],
                         ["customer_id", "customer_name"])
        # DDL hints via sqlglot: NOT NULL and CHECK ... IN enum
        self.assertFalse(customers.column("customer_name").nullable)
        self.assertEqual(orders.column("status").enum_values,
                         ("completed", "cancelled"))
        self.assertFalse(orders.column("status").nullable)
        self.assertTrue(orders.column("customer_id").nullable)
        # FK index pair [[4, 1]] -> Relationship; child col nullable => optional
        self.assertEqual(len(rels), 1)
        rel = rels[0]
        self.assertEqual(
            (rel.child_table, rel.child_columns, rel.parent_table, rel.parent_columns),
            ("orders", ("customer_id",), "customers", ("customer_id",)),
        )
        self.assertFalse(rel.required)

    def test_schema_to_tables_rejects_mismatched_fk_types(self) -> None:
        entry = _synsql_tables_entry()
        # orders.customer_id is global column 4; its parent customers.customer_id
        # is column 1. A cross-type relationship has destination-dependent cast
        # semantics and cannot be a portable task contract.
        entry["column_types"][4] = "text"
        with self.assertRaisesRegex(
            ValueError,
            r"orders\.customer_id \(text\) -> customers\.customer_id "
            r"\(decimal\).*incompatible logical types",
        ):
            synsql_adapter.schema_to_tables(entry)

    def test_to_task_ir_builds_mart_from_schema_structure(self) -> None:
        task = synsql_adapter.to_task_ir("retail_orders", self.tables_path)
        self.assertEqual(task.origin, Origin.SYNSQL)
        self.assertEqual(task.family_id, "synsql__retail_orders")
        self.assertTrue(task.cluster_id.startswith("synsql__cluster_"))
        self.assertEqual(task.title, "Retail Orders")
        self.assertEqual({t.name for t in task.tables}, {"customers", "orders"})
        self.assertEqual({b.table for b in task.backends}, {"customers", "orders"})
        # ROUND 6: the adapter selects plan-library SHAPES from the FK graph
        # instead of hand-building one two-measure star, so what is asserted
        # here is the CONTRACT (a joined, grouped plan clearing the anchor's
        # measured column minima) rather than one shape's column names.
        self.assertGreaterEqual(len(task.marts), 1)
        mart = task.marts[0]
        kinds = [op.kind for op in mart.plan.ops]
        self.assertIn(MartOpKind.JOIN, kinds)
        self.assertTrue(
            {
                MartOpKind.AGGREGATE,
                MartOpKind.FILTERED_AGGREGATE,
                MartOpKind.DISTINCT,
            }
            & set(kinds),
            f"no group-by op in {kinds}",
        )
        self.assertIn(MartOpKind.DERIVE, kinds)  # COALESCE-to-0 op
        self.assertTrue(set(mart.key_columns) <= {c.name for c in mart.columns})
        # The anchor's MEASURED per-task minima: 6 target columns, 4 computed.
        target = sum(len(m.columns) for m in task.marts)
        computed = sum(1 for m in task.marts for c in m.columns if c.computed)
        self.assertGreaterEqual(target, 6, [c.name for c in mart.columns])
        self.assertGreaterEqual(computed, 4)
        # deterministic
        again = synsql_adapter.to_task_ir("retail_orders", self.tables_path)
        self.assertEqual(task.content_hash(), again.content_hash())

    def test_to_task_ir_rejects_trivial_schema(self) -> None:
        entry = _synsql_tables_entry()
        entry["db_id"] = "no_fk_db"
        entry["foreign_keys"] = []
        path = _write_json(self.tmp.name, "tables_nofk.json", [entry])
        with self.assertRaises(ValueError):
            synsql_adapter.to_task_ir("no_fk_db", path)
        with self.assertRaises(ValueError):
            synsql_adapter.to_task_ir("missing_db", path)

    # -- THE leak firewall --------------------------------------------------

    def test_emitted_task_contains_no_answer_material(self) -> None:
        """Grep every emitted string field against question/sql/cot/ek."""
        task = synsql_adapter.to_task_ir("retail_orders", self.tables_path)
        records = list(synsql_adapter.iter_records(self.data_path))
        # helper passes on a clean task
        synsql_adapter.assert_no_answer_leak(task, records)
        # and belt-and-braces: raw substring scan of the full dump
        full_dump = json.dumps(task.model_dump(mode="json"))
        for secret in (_SYNSQL_QUESTION, _SYNSQL_SQL, _SYNSQL_COT, _SYNSQL_EK):
            self.assertNotIn(secret, full_dump)

    def test_leak_guard_catches_planted_leak(self) -> None:
        task = synsql_adapter.to_task_ir("retail_orders", self.tables_path)
        records = list(synsql_adapter.iter_records(self.data_path))
        for planted in (_SYNSQL_QUESTION, _SYNSQL_SQL, _SYNSQL_COT):
            leaked = task.model_copy(
                update={"solver_prompt": f"Hint from upstream: {planted}"}
            )
            with self.assertRaises(synsql_adapter.AnswerLeakError):
                synsql_adapter.assert_no_answer_leak(leaked, records)

    def test_leak_guard_allows_schema_side_tokens(self) -> None:
        """Table/column names appear in answer SQL too; they are NOT leaks."""
        task = synsql_adapter.to_task_ir("retail_orders", self.tables_path)
        records = list(synsql_adapter.iter_records(self.data_path))
        ok = task.model_copy(
            update={"solver_prompt": "Build a mart joining customers and orders "
                                     "on customer_id."}
        )
        synsql_adapter.assert_no_answer_leak(ok, records)

    def test_leak_guard_allows_long_identifier_line_from_answer_sql(self) -> None:
        """A pretty-printed SQL identifier is public schema, not an answer.

        This is the shape found in the current SynSQL release: a long table
        name occupies its own SQL line, so the old length-only heuristic
        mistook that one line for leaked answer material.
        """
        entry = _synsql_tables_entry()
        entry["db_id"] = "fisheries_data_and_management"
        entry["table_names"][1] = "fishing_activities"
        entry["table_names_original"][1] = "fishing_activities"
        entry["ddls"][1] = entry["ddls"][1].replace(
            "CREATE TABLE orders", "CREATE TABLE fishing_activities"
        )
        tables_path = _write_json(self.tmp.name, "long_table.json", [entry])
        task, trusted_atoms = synsql_adapter.to_task_ir_with_schema_atoms(
            entry["db_id"], tables_path
        )

        # The clean schema legitimately publishes the table name, while the
        # complete answer SQL is absent. One complete SQL identifier token may
        # use any of SQLite's identifier wrappers; none permits a larger SQL
        # expression or clause.
        for token in (
            "fishing_activities",
            '"fishing_activities"',
            "`fishing_activities`",
            "[fishing_activities]",
        ):
            with self.subTest(token=token):
                record = synsql_adapter.SynSQLRecord.from_raw(
                    {
                        "db_id": entry["db_id"],
                        "sql": f"SELECT confidential_answer\nFROM\n{token}",
                    }
                )
                mentions_public_atom = task.model_copy(
                    update={"solver_prompt": f"Read source {token}."}
                )
                synsql_adapter.assert_no_answer_leak(
                    mentions_public_atom,
                    [record],
                    trusted_schema_atoms=trusted_atoms,
                )

        bare_record = synsql_adapter.SynSQLRecord.from_raw(
            {
                "db_id": entry["db_id"],
                "sql": "SELECT confidential_answer\nFROM\nfishing_activities",
            }
        )
        # Programmatic callers that omit trusted raw-schema context receive no
        # exemptions; the TaskIR is never allowed to mint its own allowlist.
        with self.assertRaises(synsql_adapter.AnswerLeakError):
            synsql_adapter.assert_no_answer_leak(task, [bare_record])

        # Exempting the exact identifier must not exempt the SQL that contains
        # it. Copying the complete answer remains a hard failure.
        leaked = task.model_copy(
            update={"solver_prompt": bare_record.quarantined_texts()["sql"]}
        )
        with self.assertRaises(synsql_adapter.AnswerLeakError):
            synsql_adapter.assert_no_answer_leak(
                leaked,
                [bare_record],
                trusted_schema_atoms=trusted_atoms,
            )

    def test_leak_guard_allows_exact_declared_domain_value_only(self) -> None:
        """A DDL-declared enum atom is public; an undeclared literal is not."""
        entry = _synsql_tables_entry()
        entry["ddls"][1] = entry["ddls"][1].replace(
            "'cancelled'", "'awaiting_manual_review'"
        )
        tables_path = _write_json(self.tmp.name, "long_enum.json", [entry])
        task, trusted_atoms = synsql_adapter.to_task_ir_with_schema_atoms(
            "retail_orders", tables_path
        )

        for token in ("awaiting_manual_review", "'awaiting_manual_review'"):
            with self.subTest(token=token):
                declared = synsql_adapter.SynSQLRecord.from_raw(
                    {
                        "db_id": "retail_orders",
                        "external_knowledge": token,
                    }
                )
                synsql_adapter.assert_no_answer_leak(
                    task,
                    [declared],
                    trusted_schema_atoms=trusted_atoms,
                )

        # The wrapper exemption is a one-token grammar, not quote stripping
        # over arbitrary answer SQL.
        quoted_clause = synsql_adapter.SynSQLRecord.from_raw(
            {
                "db_id": "retail_orders",
                "sql": "SELECT 'awaiting_manual_review'",
            }
        )
        clause_leak = task.model_copy(
            update={"solver_prompt": "SELECT 'awaiting_manual_review'"}
        )
        with self.assertRaises(synsql_adapter.AnswerLeakError):
            synsql_adapter.assert_no_answer_leak(
                clause_leak,
                [quoted_clause],
                trusted_schema_atoms=trusted_atoms,
            )

        answer_only = synsql_adapter.SynSQLRecord.from_raw(
            {
                "db_id": "retail_orders",
                "sql": "SELECT\nanswer_only_literal_value\nFROM\norders",
            }
        )
        leaked = task.model_copy(
            update={"solver_prompt": "Hidden literal: answer_only_literal_value"}
        )
        with self.assertRaises(synsql_adapter.AnswerLeakError):
            synsql_adapter.assert_no_answer_leak(
                leaked,
                [answer_only],
                trusted_schema_atoms=trusted_atoms,
            )

    def test_leak_guard_does_not_let_task_schema_self_authenticate(self) -> None:
        """The inspected TaskIR cannot add answer text to its own allowlist."""
        task, trusted_atoms = synsql_adapter.to_task_ir_with_schema_atoms(
            "retail_orders", self.tables_path
        )
        secret = "the confidential result is forty two"
        record = synsql_adapter.SynSQLRecord.from_raw(
            {"db_id": "retail_orders", "external_knowledge": secret}
        )
        table = task.tables[0]
        poisoned_column = table.columns[0].model_copy(
            update={"enum_values": (secret,)}
        )
        poisoned_table = table.model_copy(
            update={"columns": (poisoned_column,) + table.columns[1:]}
        )
        poisoned_task = task.model_copy(
            update={
                "tables": (poisoned_table,) + task.tables[1:],
                "solver_prompt": secret,
            }
        )

        with self.assertRaises(synsql_adapter.AnswerLeakError):
            synsql_adapter.assert_no_answer_leak(
                poisoned_task,
                [record],
                trusted_schema_atoms=trusted_atoms,
            )


# ---------------------------------------------------------------------------
# The contamination pre-check (the gap Finding 2 closed)
# ---------------------------------------------------------------------------

class SynSQLContaminationGateTest(unittest.TestCase):
    """SynSQL must not be the one pool that registers unchecked.

    Two properties are pinned here, because both were absent before: the
    ADAPTER gate raises (so a programmatic `to_task_ir` -> `Engine.register`
    caller cannot bypass it), and the CLI refuses with a non-zero exit and
    registers NOTHING. Armed-but-empty stays a failure, not a clean pass.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "ws"
        self.index_dir = self.workspace / "state" / "contamination"
        self.tables_path = _write_json(
            self.tmp.name, "tables.json", [_synsql_tables_entry()]
        )

    # -- helpers -----------------------------------------------------------

    def _task(self, db_id: str = "retail_orders"):
        return synsql_adapter.to_task_ir(db_id, self.tables_path)

    def _arm(self, fingerprints) -> None:
        contamination.ContaminationIndex(self.index_dir).add_benchmark(
            "eltbench", sorted(fingerprints)
        )

    def _run_cli(self, *extra: str) -> tuple[int, str]:
        args = cli.build_parser().parse_args(
            [
                "ingest-synsql",
                "--workspace", str(self.workspace),
                "--tables", str(self.tables_path),
                *extra,
            ]
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.cmd_ingest_synsql(args)
        return code, buf.getvalue()

    def _registered(self) -> list[str]:
        tasks_dir = self.workspace / "tasks"
        if not tasks_dir.is_dir():
            return []
        return sorted(p.name for p in tasks_dir.iterdir() if p.is_dir())

    # -- the adapter gate ---------------------------------------------------

    def test_unarmed_index_is_a_fatal_refusal_not_a_clean_pass(self) -> None:
        with self.assertRaises(synsql_adapter.SynSQLContaminationError) as ctx:
            synsql_adapter.assert_uncontaminated(self._task(), self.index_dir)
        self.assertTrue(any(c.kind == "index" for c in ctx.exception.collisions))

    def test_family_collision_is_fatal(self) -> None:
        self._arm({"family:retail_orders"})
        with self.assertRaises(synsql_adapter.SynSQLContaminationError) as ctx:
            synsql_adapter.assert_uncontaminated(self._task(), self.index_dir)
        kinds = {c.kind for c in ctx.exception.collisions if c.fatal}
        self.assertEqual(kinds, {"family"})

    def test_whole_schema_collision_is_fatal_without_a_name_collision(self) -> None:
        """The SchemaPile-style case: family name clean, schema identical."""
        task = self._task()
        schema_fp = next(
            fp for fp in contamination.schema_fingerprints(task.tables)
            if fp.startswith("schema:")
        )
        self._arm({schema_fp})
        with self.assertRaises(synsql_adapter.SynSQLContaminationError) as ctx:
            synsql_adapter.assert_uncontaminated(task, self.index_dir)
        fatal = [c for c in ctx.exception.collisions if c.fatal]
        self.assertEqual([c.kind for c in fatal], ["schema"])
        self.assertIn(schema_fp, fatal[0].detail)

    def test_per_table_collision_is_borderline_and_returned_not_raised(self) -> None:
        task = self._task()
        per_table = sorted(
            fp for fp in contamination.schema_fingerprints(task.tables)
            if fp.startswith("schema-table:")
        )
        self._arm(set(per_table))
        borderline = synsql_adapter.assert_uncontaminated(task, self.index_dir)
        self.assertEqual(len(borderline), len(per_table))
        self.assertTrue(all(not c.fatal for c in borderline))

    def test_clean_candidate_passes_against_an_armed_index(self) -> None:
        self._arm({"family:some_other_benchmark_family"})
        self.assertEqual(
            synsql_adapter.assert_uncontaminated(self._task(), self.index_dir), []
        )

    # -- the CLI convention (same as cmd_ingest_wikidbs) --------------------

    def test_cli_refuses_a_colliding_candidate_and_registers_nothing(self) -> None:
        self._arm({"family:retail_orders"})
        code, out = self._run_cli("--db-id", "retail_orders")
        self.assertEqual(code, 2)
        self.assertIn("contamination FATAL [family/eltbench]", out)
        self.assertIn("was NOT registered", out)
        self.assertEqual(self._registered(), [])

    def test_cli_seeds_the_embedded_deny_lists_when_the_index_is_unarmed(self) -> None:
        """An unarmed workspace is armed deliberately, then still enforced: a
        db_id equal to an ELT-Bench family is refused on the seeded list alone."""
        entry = _synsql_tables_entry()
        entry["db_id"] = "asana"  # a real ELT-Bench family
        path = _write_json(self.tmp.name, "tables_asana.json", [entry])
        args = cli.build_parser().parse_args(
            [
                "ingest-synsql",
                "--workspace", str(self.workspace),
                "--tables", str(path),
                "--db-id", "asana",
            ]
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.cmd_ingest_synsql(args)
        out = buf.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("seeded with the embedded benchmark deny lists", out)
        self.assertIn("family:asana", out)
        self.assertEqual(self._registered(), [])

    def test_cli_registers_a_clean_candidate(self) -> None:
        self._arm({"family:some_other_benchmark_family"})
        code, out = self._run_cli("--db-id", "retail_orders")
        self.assertEqual(code, 0)
        self.assertIn("registered synsql__retail_orders__", out)
        # The task id ends in the LIBRARY SHAPE the schema funded, so it is not
        # pinned to one spelling here: what this gate test asserts is that a
        # clean candidate registers under the retail_orders family, not which
        # of the four shapes its FK graph happened to support.
        registered = self._registered()
        self.assertEqual(len(registered), 1)
        self.assertTrue(
            registered[0].startswith("synsql__retail_orders__"), registered
        )

    def test_cli_passes_raw_schema_context_to_answer_leak_guard(self) -> None:
        """The data scan gets atoms from the parse, not from its TaskIR."""
        entry = _synsql_tables_entry()
        entry["db_id"] = "fisheries_data_and_management"
        entry["table_names"][1] = "fishing_activities"
        entry["table_names_original"][1] = "fishing_activities"
        entry["ddls"][1] = entry["ddls"][1].replace(
            "CREATE TABLE orders", "CREATE TABLE fishing_activities"
        )
        self.tables_path = _write_json(
            self.tmp.name, "long_cli_tables.json", [entry]
        )
        data_path = _write_json(
            self.tmp.name,
            "long_cli_data.json",
            [
                {
                    "db_id": entry["db_id"],
                    "sql": "SELECT confidential_answer\nFROM\nfishing_activities",
                }
            ],
        )
        self._arm({"family:some_other_benchmark_family"})

        code, out = self._run_cli(
            "--db-id", entry["db_id"], "--data", str(data_path)
        )

        self.assertEqual(code, 0)
        self.assertIn("leak guard: checked against 1 SynSQL record(s), clean", out)
        self.assertEqual(len(self._registered()), 1)



class HopPassthroughStatesItsOrigin(unittest.TestCase):
    """A joined passthrough must SAY which record it is taken from.

    The adapter resolves (table, column) for every joined passthrough and used
    to drop it, so several marts of one package shipped the same column name
    with byte-identical prose while the reference read each from a DIFFERENT
    table. The author cannot recover the binding — the plan summary withholds
    the SELECT list on purpose — so it guesses, and a wrong guess is only
    caught at dual-build agreement one council run later.

    PROPERTY-BASED ON PURPOSE: the hop columns are read out of whatever the
    plan actually declares, never from a fixed list, so a different package or
    a re-recovered plan stays covered.
    """

    #: `<table>."<source column>" AS "<mart column>"` inside a join op's SELECT.
    _CARRIED = re.compile(r'(\w+)\."([^"]+)"\s+AS\s+"([^"]+)"')

    def _hop_sources(self, mart) -> dict[str, set[str]]:
        """mart column -> source tables a JOIN op carries it from."""
        carried: dict[str, set[str]] = {}
        for op in mart.plan.ops:
            if op.kind is not MartOpKind.JOIN:
                continue
            select = (op.details or {}).get("select", "")
            for table, _src, dst in self._CARRIED.findall(select):
                carried.setdefault(dst, set()).add(table)
        return carried

    def _built_packages(self):
        root = Path("runs/dbt_elt/dbt_builds")
        for manifest in sorted(root.glob("*/package/*/target/manifest.json")):
            yield manifest.parts[-4], manifest

    def test_every_hop_passthrough_names_its_source_table(self) -> None:
        checked = 0
        for pkg, manifest in self._built_packages():
            result = dbt_adapter.extract_candidates(
                dbt_adapter.load_manifest(manifest)
            )
            for task in result.tasks:
                sources = {t.name for t in task.tables}
                for mart in task.marts:
                    hops = self._hop_sources(mart)
                    for column in mart.columns:
                        tables = hops.get(column.name, set()) & sources
                        if not tables:
                            continue
                        checked += 1
                        named = {
                            t for t in sources
                            if f"`{t}`" in (column.description or "")
                        }
                        self.assertTrue(
                            named & tables,
                            f"{pkg}:{mart.name}.{column.name} is carried from "
                            f"{sorted(tables)} but its description names no "
                            f"source table: {column.description!r}",
                        )
        if checked == 0:
            self.skipTest("no built dbt package with hop passthroughs on disk")

    def test_lineage_is_idempotent(self) -> None:
        once = dbt_adapter._carry_lineage("Some column.", "orders", "order_id")
        twice = dbt_adapter._carry_lineage(once, "orders", "order_id")
        self.assertEqual(once, twice)
        self.assertIn("`orders`", once)

    def test_a_description_that_already_names_the_table_is_untouched(self) -> None:
        text = "The status of the matching `orders` record."
        self.assertEqual(
            dbt_adapter._carry_lineage(text, "orders", "status"), text
        )


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Column lineage: the dbt mart contract, traced back to source columns
# ---------------------------------------------------------------------------

class DbtLineageRecoveryTest(unittest.TestCase):
    """A dbt mart's columns are almost never source column names.

    `stg_servicenow__incident` renames `sys_id` to `incident_id`; the mart
    declares the RENAMED name. Without the rename map, grounding a mart in its
    sources finds nothing — not even its key — so `ground_mart` would drop
    every mart of the package and the pool would produce no task at all
    (measured on `dbt_servicenow`: 26 of 57 columns of
    `servicenow__incident_enhanced` ground by name, and NOT `incident_id`).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _spec(self, manifest: dict, name: str = "lineage.json"):
        return dbt_adapter.load_manifest(_write_json(self.tmp.name, name, manifest))

    def test_staging_renames_are_recovered_from_single_source_models(self) -> None:
        manifest = _dbt_manifest()
        manifest["nodes"]["model.shop_pkg.stg_orders"]["raw_code"] = (
            "select\n"
            "    cast(order_id as {{ dbt.type_string() }}) as sales_order_id,\n"
            "    amount as order_amount\n"
            "from base"
        )
        aliases = dbt_adapter.staging_alias_map(self._spec(manifest))
        self.assertEqual(aliases["sales_order_id"], {"orders": "order_id"})
        self.assertEqual(aliases["order_amount"], {"orders": "amount"})

    def test_a_multi_source_model_contributes_no_lineage(self) -> None:
        """An alias in a model reading two tables cannot be attributed to one
        of them, so it is not recovered — the map never guesses."""
        manifest = _dbt_manifest()
        manifest["nodes"]["model.shop_pkg.customer_orders"]["raw_code"] = (
            "select customer_name as who from base"
        )
        self.assertNotIn("who", dbt_adapter.staging_alias_map(self._spec(manifest)))

    def test_measures_are_recovered_from_the_marts_own_sql(self) -> None:
        """The measures are the only genuinely transformational part of a dbt
        mart, and the model states them; recovering them is what keeps the
        emitted mart faithful instead of replacing every measure with COUNT."""
        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["raw_code"] = (
            "select customer_id, count(*) as order_count, "
            "sum(amount) as total_amount from base group by 1"
        )
        spec = self._spec(manifest)
        node = next(m for m in spec.models if m.name == "customer_orders")
        measures = {m.column: m for m in dbt_adapter.recover_measures(node)}
        self.assertEqual(set(measures), {"order_count", "total_amount"})
        self.assertEqual(measures["total_amount"].refs, ("amount",))
        self.assertIn("SUM", measures["total_amount"].expr.upper())

    def test_jinja_control_flow_yields_no_measures(self) -> None:
        manifest = _dbt_manifest()
        manifest["nodes"]["model.shop_pkg.customer_orders"]["raw_code"] = (
            "select customer_id{% for c in cols %}, sum({{ c }}) as x{% endfor %} "
            "from base group by 1"
        )
        spec = self._spec(manifest)
        node = next(m for m in spec.models if m.name == "customer_orders")
        self.assertEqual(dbt_adapter.recover_measures(node), ())

    def test_a_recovered_measure_becomes_a_mart_column(self) -> None:
        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["raw_code"] = (
            "select customer_id, sum(amount) as total_amount from base group by 1"
        )
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "total_amount": _dbt_col("total_amount", "numeric"),
        }
        task = dbt_adapter.extract_tasks(self._spec(manifest, "measure.json"))[0]
        mart = task.marts[0]
        self.assertEqual(
            [c.name for c in mart.columns], ["customer_id", "total_amount"]
        )
        # `amount` lives in `orders`, so the mart grounds there, not in
        # `customers` — the base table is chosen by what it can PRODUCE.
        self.assertIn("orders", mart.plan.notes)

    def test_recovered_measure_alias_publishes_its_source_column(self) -> None:
        """A solver must be able to resolve a staging-only measure name from
        the published source schemas, without access to the recovered SQL."""
        manifest = _dbt_manifest()
        orders = manifest["sources"]["source.shop_pkg.shop.orders"]["columns"]
        orders["billed_charge_local_micro"] = _dbt_col(
            "billed_charge_local_micro", "bigint", "Charge in microdollars."
        )
        staging = manifest["nodes"]["model.shop_pkg.stg_orders"]
        staging["columns"]["spend_micro"] = _dbt_col("spend_micro", "bigint")
        staging["compiled_code"] = (
            "select order_id, customer_id, amount, "
            "billed_charge_local_micro as spend_micro from orders"
        )
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "spend_micro": _dbt_col("spend_micro", "bigint", "Spend in micros."),
        }
        model["compiled_code"] = (
            "select customer_id, sum(spend_micro) as spend_micro "
            "from stg_orders group by 1"
        )

        task = dbt_adapter.extract_tasks(self._spec(manifest, "measure_alias.json"))[0]
        mart = task.marts[0]
        spend = next(column for column in mart.columns if column.name == "spend_micro")
        self.assertIn(
            "`spend_micro` is the value of source column "
            "`orders.billed_charge_local_micro`",
            spend.description,
        )

    def test_an_ungroundable_mart_is_dropped_not_keyed_arbitrarily(self) -> None:
        manifest = _dbt_manifest()
        manifest["nodes"]["model.shop_pkg.customer_orders"]["columns"] = {
            # A defensible key (the <entity>_id naming convention) that is a
            # surrogate no source column produces: the KEY ladder succeeds and
            # GROUNDING is what refuses the mart.
            "customer_order_id": _dbt_col("customer_order_id", "integer"),
            "computed_metric": _dbt_col("computed_metric", "numeric"),
        }
        # Deliberately the SAME drop path as a keyless mart (UngroundedMartError
        # subclasses KeylessMartError): in both cases the mart has no grain this
        # factory can GRADE, which is one admission decision, not two.
        with self.assertRaises(dbt_adapter.KeylessMartError) as ctx:
            dbt_adapter.extract_tasks(self._spec(manifest, "ungrounded.json"))
        self.assertIn("cannot be executed from the sources", str(ctx.exception))

    def test_grounding_failure_is_reported_as_a_typed_skip(self) -> None:
        manifest = _dbt_manifest()
        manifest["nodes"]["model.shop_pkg.customer_orders"]["columns"] = {
            "customer_order_id": _dbt_col("customer_order_id", "integer"),
            "computed_metric": _dbt_col("computed_metric", "numeric"),
        }
        result = dbt_adapter.extract_candidates(self._spec(manifest, "skip.json"))
        self.assertEqual(result.tasks, ())
        self.assertTrue(
            any("recovered staging rename" in s.reason for s in result.skipped),
            [s.reason for s in result.skipped],
        )

    def test_every_emitted_mart_plan_compiles(self) -> None:
        """The A2 blocker, at the pool's own entry point."""
        from elt_taskgen.reference.solution import compile_plan_sql

        task = dbt_adapter.extract_tasks(self._spec(_dbt_manifest(), "compiles.json"))[0]
        for mart in task.marts:
            with self.subTest(mart=mart.name):
                self.assertIn("SELECT", compile_plan_sql(task, mart))

    def test_every_emitted_task_is_generateable(self) -> None:
        """The A1 blocker, at the pool's own entry point: the exact check
        `cli.run_generate` makes before it materializes anything."""
        from elt_taskgen.generation import populations as populations_mod

        task = dbt_adapter.extract_tasks(self._spec(_dbt_manifest(), "gen.json"))[0]
        self.assertEqual(
            {p.name for p in task.populations}, set(PopulationName)
        )
        self.assertEqual(
            populations_mod.validate_population_coverage(task), []
        )
        self.assertIsNotNone(task.reference)


class DbtCompiledRecoveryTest(unittest.TestCase):
    """Recovery reads COMPILED dbt SQL, and reads all of it.

    The pool's whole reason to exist is that its marts are real, audited
    transformation logic — and the adapter could not see it. `dbt parse` leaves
    `compiled_code` null, a Fivetran model's `raw_code` is Jinja, and the old
    reader took only `<aggregate> AS <alias>`: measured, 0 aggregate
    expressions were recovered from 67 of 67 terminal marts while 53 of them
    textually contain `sum(`/`count(`/`min(`/`max(`/`avg(`, so 109 of 413
    shipped columns carried prose promising computation over a pure projection.
    These tests pin the four mechanisms that closed that gap: compiled-SQL
    preference, full-projection classification, expression grounding, and the
    type/qualifier certification that keeps a recovered expression EXECUTABLE.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _spec(self, manifest: dict, name: str = "compiled.json"):
        return dbt_adapter.load_manifest(_write_json(self.tmp.name, name, manifest))

    def _mart_model(self, manifest: dict, name: str = "compiled.json"):
        spec = self._spec(manifest, name)
        return spec, next(m for m in spec.models if m.name == "customer_orders")

    # -- compiled SQL is what recovery reads --------------------------------

    def test_compiled_code_is_read_and_raw_jinja_is_not(self) -> None:
        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["raw_code"] = (
            "select customer_id, count(*) as order_count, "
            "sum({{ var('amount_col') }}) as total_amount "
            "from {{ ref('stg_orders') }} group by 1"
        )
        spec, node = self._mart_model(manifest, "jinja_only.json")
        # Parse-only manifest: the Jinja guard refuses the text outright.
        self.assertEqual(dbt_adapter.recover_projections(node), ())
        model["compiled_code"] = (
            "select customer_id, count(*) as order_count, "
            'sum(amount) as total_amount from "db"."sch"."stg_orders" group by 1'
        )
        _, node = self._mart_model(manifest, "compiled_ok.json")
        self.assertEqual(node.sql_text, model["compiled_code"])
        recovered = {p.column: p for p in dbt_adapter.recover_projections(node)}
        self.assertEqual(set(recovered), {"customer_id", "order_count", "total_amount"})
        self.assertEqual(recovered["total_amount"].refs, ("amount",))

    # -- what each column IS -------------------------------------------------

    def test_projection_kinds_are_read_off_the_ast(self) -> None:
        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "order_count": _dbt_col("order_count", "integer"),
            "paid_count": _dbt_col("paid_count", "integer"),
            "distinct_orders": _dbt_col("distinct_orders", "integer"),
            "band": _dbt_col("band", "text"),
            "paid_share": _dbt_col("paid_share", "numeric"),
        }
        model["compiled_code"] = (
            "select customer_id,"
            " count(*) as order_count,"
            " count(case when amount > 0 then order_id end) as paid_count,"
            " count(distinct order_id) as distinct_orders,"
            " case when amount > 10 then 'big' else 'small' end as band,"
            " cast(amount as double) / nullif(amount, 0) as paid_share"
            " from base group by 1"
        )
        _, node = self._mart_model(manifest, "kinds.json")
        kinds = {p.column: p.kind for p in dbt_adapter.recover_projections(node)}
        K = dbt_adapter.ProjectionKind
        self.assertEqual(kinds["customer_id"], K.PASSTHROUGH)
        self.assertEqual(kinds["order_count"], K.AGGREGATE)
        self.assertEqual(kinds["paid_count"], K.FILTERED_AGGREGATE)
        self.assertEqual(kinds["distinct_orders"], K.DISTINCT_AGGREGATE)
        self.assertEqual(kinds["band"], K.CONDITIONAL)
        self.assertEqual(kinds["paid_share"], K.RATIO)
        computed = {p.column for p in dbt_adapter.recover_projections(node) if p.computed}
        self.assertNotIn("customer_id", computed)
        self.assertEqual(len(computed), 5)

    def test_a_filtered_aggregate_ships_as_a_filtered_aggregate_op(self) -> None:
        """The op KIND is what `attack_surface` routes on, so a CASE inside an
        aggregate must not ship as a plain `aggregate` op — and the predicate
        must be stated, because an unstated predicate is an unfair key."""
        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "paid_count": _dbt_col("paid_count", "integer"),
        }
        model["compiled_code"] = (
            "select customer_id,"
            " count(case when amount > 0 then order_id end) as paid_count"
            " from base group by 1"
        )
        task = dbt_adapter.extract_tasks(self._spec(manifest, "filtered.json"))[0]
        mart = task.marts[0]
        agg = [op for op in mart.plan.ops if op.kind is MartOpKind.FILTERED_AGGREGATE]
        self.assertEqual(len(agg), 1)
        self.assertIn("amount", agg[0].predicate)
        paid = next(c for c in mart.columns if c.name == "paid_count")
        self.assertTrue(paid.computed)

    # -- multi-column lineage -----------------------------------------------

    def test_a_multi_column_expression_grounds_when_every_ref_grounds(self) -> None:
        """The lineage the `<col> as <alias>` reader could not express."""
        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "net_amount": _dbt_col("net_amount", "numeric"),
        }
        model["compiled_code"] = (
            "select customer_id, sum(amount - order_id) as net_amount"
            " from base group by 1"
        )
        task = dbt_adapter.extract_tasks(self._spec(manifest, "multicol.json"))[0]
        mart = task.marts[0]
        self.assertIn("net_amount", {c.name for c in mart.columns})

    def test_a_staging_definition_is_inlined_into_the_measure(self) -> None:
        """`is_paid` exists only in the staging model; the mart's measure reads
        it, and without inlining the measure grounds nowhere and is dropped."""
        manifest = _dbt_manifest()
        manifest["nodes"]["model.shop_pkg.stg_orders"]["compiled_code"] = (
            "select order_id, customer_id, amount,"
            " case when amount > 0 then true else false end as is_paid"
            " from base"
        )
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "paid_count": _dbt_col("paid_count", "integer"),
        }
        model["compiled_code"] = (
            "select customer_id, count(case when is_paid then order_id end)"
            " as paid_count from base group by 1"
        )
        spec = self._spec(manifest, "inline.json")
        self.assertIn("is_paid", dbt_adapter.staging_expression_map(spec))
        mart = dbt_adapter.extract_tasks(spec)[0].marts[0]
        self.assertIn("paid_count", {c.name for c in mart.columns})
        agg = next(op for op in mart.plan.ops if op.kind is MartOpKind.FILTERED_AGGREGATE)
        # The staging definition, not the staging COLUMN NAME, is what ships.
        self.assertNotIn("is_paid", " ".join(agg.details.values()))
        self.assertIn("amount", " ".join(agg.details.values()))

    def test_staging_day_and_same_named_null_defaults_survive_recovery(self) -> None:
        """Regression for the Twitter daily reports.

        ``date_day`` exists only as a staging expression, while the two metric
        aliases keep their raw names after supplying zero defaults.  Recovery
        must preserve both forms of staging work: the day remains in the mart
        grain and a missing metric contributes zero before row-level addition.
        """
        manifest = _dbt_manifest()
        raw = manifest["sources"]["source.shop_pkg.shop.orders"]["columns"]
        raw.update(
            {
                "event_date": _dbt_col("event_date", "timestamp"),
                "first_metric": _dbt_col("first_metric", "integer"),
                "second_metric": _dbt_col("second_metric", "integer"),
            }
        )
        staging = manifest["nodes"]["model.shop_pkg.stg_orders"]
        staging["columns"].update(
            {
                "date_day": _dbt_col("date_day", "date"),
                "first_metric": _dbt_col("first_metric", "integer"),
                "second_metric": _dbt_col("second_metric", "integer"),
            }
        )
        staging["compiled_code"] = (
            "select order_id, customer_id, amount, "
            "date_trunc('day', event_date) as date_day, "
            "coalesce(first_metric, 0) as first_metric, "
            "coalesce(second_metric, 0) as second_metric from raw_orders"
        )
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            # Fivetran manifests commonly leave a derived output untyped; the
            # recovered scalar expression must supply the public result type.
            "date_day": _dbt_col("date_day", None, "Performance day."),
            "total_metric": _dbt_col(
                "total_metric", "integer", "Combined metric total."
            ),
        }
        model["compiled_code"] = (
            "with final as (select customer_id, date_day, "
            "sum(first_metric + second_metric) as total_metric "
            "from stg_orders group by 1, 2) select * from final"
        )

        task = dbt_adapter.extract_tasks(self._spec(manifest, "daily.json"))[0]
        mart = task.marts[0]
        self.assertIn("date_day", mart.key_columns)
        day = next(column for column in mart.columns if column.name == "date_day")
        self.assertEqual(day.kind.value, "derived")
        self.assertEqual(day.type, ColumnType.TIMESTAMP)
        self.assertIn("calendar day at 00:00:00", day.description)
        self.assertFalse(task.table("orders").column("event_date").nullable)

        aggregate = next(
            op
            for op in mart.plan.ops
            if op.kind in {MartOpKind.AGGREGATE, MartOpKind.FILTERED_AGGREGATE}
        )
        total_expression = next(
            value
            for key, value in aggregate.details.items()
            if key.startswith("m_") and "first_metric" in value
        )
        self.assertIn("COALESCE(first_metric, 0)", total_expression)
        self.assertIn("COALESCE(second_metric, 0)", total_expression)
        total = next(column for column in mart.columns if column.name == "total_metric")
        self.assertIn("missing first_metric, second_metric values contribute 0", total.description)
        self.assertIn(
            "exact per-input component list is complete and authoritative",
            total.description,
        )
        self.assertIn(
            "even if it is not emitted as a separate mart output",
            total.description,
        )
        self.assertNotIn("first_metric", {column.name for column in mart.columns})
        self.assertNotIn("second_metric", {column.name for column in mart.columns})
        self.assertFalse(
            any(
                "ALL-NULL AGGREGATE WITNESS" in line
                for line in task.population(PopulationName.COUNTERFACTUAL).conditions
            ),
            "row-level zero fallbacks must not advertise a NULL result",
        )

    def test_nullable_undefaulted_sum_gets_executable_all_null_witness(self) -> None:
        """A recovered NULL-preserving aggregate must be exercised by data.

        Prose saying ``SUM(amount)`` returns NULL for an all-missing group is
        not enough: the counterfactual must make the plausible
        ``SUM(COALESCE(amount, 0))`` implementation observably wrong.
        """
        import duckdb
        import sqlglot
        from sqlglot import exp

        from elt_taskgen.generation import populations as populations_mod
        from elt_taskgen.reference import solution as reference

        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "total_amount": _dbt_col("total_amount", "numeric"),
        }
        model["compiled_code"] = (
            "select customer_id, sum(amount) as total_amount "
            "from stg_orders group by 1"
        )

        task = dbt_adapter.extract_tasks(
            self._spec(manifest, "all_null_sum.json")
        )[0]
        counterfactual = task.population(PopulationName.COUNTERFACTUAL)
        witness_rows = [
            row
            for row in counterfactual.literal_rows["orders"]
            if row["amount"] is None
        ]
        self.assertEqual(len(witness_rows), 1, counterfactual.literal_rows)
        target_customer = witness_rows[0]["customer_id"]
        self.assertEqual(
            sum(
                row["customer_id"] == target_customer
                for row in counterfactual.literal_rows["orders"]
            ),
            1,
        )
        self.assertIn(
            target_customer,
            {
                row["customer_id"]
                for row in counterfactual.literal_rows["customers"]
            },
            "isolating the group must preserve the required relationship",
        )
        self.assertEqual(populations_mod.validate_population_coverage(task), [])
        condition = next(
            line
            for line in counterfactual.conditions
            if "ALL-NULL AGGREGATE WITNESS" in line
        )
        self.assertIn("orders", condition)
        self.assertIn("amount", condition)
        self.assertIn("total_amount", condition)
        self.assertIn("result of each is NULL, not 0", condition)

        mart = task.marts[0]
        gold_sql = reference.compile_plan_sql(task, mart)
        mutant_ast = sqlglot.parse_one(gold_sql, read="duckdb")
        amount_sum = next(
            aggregate
            for aggregate in mutant_ast.find_all(exp.Sum)
            if any(
                column.name == "amount"
                for column in aggregate.find_all(exp.Column)
            )
        )
        amount_sum.set(
            "this",
            exp.Coalesce(
                this=amount_sum.this.copy(),
                expressions=[exp.Literal.number(0)],
            ),
        )
        mutant_sql = mutant_ast.sql(dialect="duckdb")

        connection = duckdb.connect(":memory:")
        try:
            for table in task.tables:
                reference.create_table(connection, table)
                rows = counterfactual.literal_rows[table.name]
                if rows:
                    reference._insert_rows(connection, table, rows)

            def result(sql: str) -> dict[object, object]:
                cursor = connection.execute(sql)
                columns = [description[0] for description in cursor.description]
                return {
                    row[columns.index("customer_id")]: row[
                        columns.index("total_amount")
                    ]
                    for row in cursor.fetchall()
                }

            gold = result(gold_sql)
            mutant = result(mutant_sql)
        finally:
            connection.close()

        self.assertIsNone(gold[target_customer])
        self.assertEqual(mutant[target_customer], 0)
        self.assertNotEqual(gold, mutant)

    def test_per_row_rounding_gets_an_executable_two_row_witness(self) -> None:
        """Two 6000-micro rows make round-before-sum observably required."""
        import duckdb
        import sqlglot
        from sqlglot import exp

        from elt_taskgen.generation import populations as populations_mod
        from elt_taskgen.reference import solution as reference

        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "rounded_amount": _dbt_col("rounded_amount", "numeric"),
        }
        model["compiled_code"] = (
            "select customer_id, "
            "sum(round(amount / 1000000.0, 2)) as rounded_amount "
            "from stg_orders group by 1"
        )

        task = dbt_adapter.extract_tasks(
            self._spec(manifest, "round_before_sum.json")
        )[0]
        counterfactual = task.population(PopulationName.COUNTERFACTUAL)
        witness_rows = [
            row
            for row in counterfactual.literal_rows["orders"]
            if row["amount"] == 6000.0
        ]
        self.assertEqual(len(witness_rows), 2, counterfactual.literal_rows)
        target_customer = witness_rows[0]["customer_id"]
        self.assertEqual(
            {row["customer_id"] for row in witness_rows}, {target_customer}
        )
        self.assertEqual(
            sum(
                row["customer_id"] == target_customer
                for row in counterfactual.literal_rows["orders"]
            ),
            2,
            "the planted pair must be the whole output group",
        )
        self.assertIn(
            target_customer,
            {
                row["customer_id"]
                for row in counterfactual.literal_rows["customers"]
            },
            "the two-row group must retain its required foreign key",
        )
        condition = next(
            line
            for line in counterfactual.conditions
            if "ROUND-BEFORE-SUM WITNESS" in line
        )
        self.assertIn("exactly two real orders rows", condition)
        self.assertIn("amount is 6000.0 on each row", condition)
        self.assertIn("therefore yields 0.02", condition)
        self.assertIn("yields 0.01", condition)
        self.assertEqual(populations_mod.validate_population_coverage(task), [])

        mart = task.marts[0]
        gold_sql = reference.compile_plan_sql(task, mart)
        mutant_ast = sqlglot.parse_one(gold_sql, read="duckdb")
        aggregate = next(
            node
            for node in mutant_ast.find_all(exp.Sum)
            if isinstance(node.this, exp.Round)
            and isinstance(node.this.this, exp.Div)
        )
        rounded = aggregate.this
        divided = rounded.this
        aggregate.replace(
            exp.Round(
                this=exp.Div(
                    this=exp.Sum(this=divided.this.copy()),
                    expression=divided.expression.copy(),
                ),
                decimals=rounded.args["decimals"].copy(),
            )
        )
        mutant_sql = mutant_ast.sql(dialect="duckdb")

        connection = duckdb.connect(":memory:")
        try:
            for table in task.tables:
                reference.create_table(connection, table)
                reference._insert_rows(
                    connection, table, counterfactual.literal_rows[table.name]
                )

            def result(sql: str) -> dict[object, object]:
                cursor = connection.execute(sql)
                columns = [description[0] for description in cursor.description]
                return {
                    row[columns.index("customer_id")]: row[
                        columns.index("rounded_amount")
                    ]
                    for row in cursor.fetchall()
                }

            gold = result(gold_sql)
            mutant = result(mutant_sql)
        finally:
            connection.close()

        self.assertAlmostEqual(gold[target_customer], 0.02)
        self.assertAlmostEqual(mutant[target_customer], 0.01)
        self.assertNotEqual(gold, mutant)

    def test_rounding_witness_declines_a_primary_key_constrained_source(self) -> None:
        """The pair is not advertised when two identical rows are illegal."""
        manifest = _dbt_manifest()
        manifest["nodes"]["test.shop_pkg.unique_orders_order_id"] = {
            "resource_type": "test",
            "test_metadata": {
                "name": "unique",
                "kwargs": {"column_name": "order_id"},
            },
            "attached_node": "source.shop_pkg.shop.orders",
            "depends_on": {"nodes": ["source.shop_pkg.shop.orders"]},
        }
        manifest["nodes"]["test.shop_pkg.not_null_orders_order_id"] = {
            "resource_type": "test",
            "test_metadata": {
                "name": "not_null",
                "kwargs": {"column_name": "order_id"},
            },
            "attached_node": "source.shop_pkg.shop.orders",
            "depends_on": {"nodes": ["source.shop_pkg.shop.orders"]},
        }
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "rounded_amount": _dbt_col("rounded_amount", "numeric"),
        }
        model["compiled_code"] = (
            "select customer_id, "
            "sum(round(amount / 1000000.0, 2)) as rounded_amount "
            "from stg_orders group by 1"
        )

        task = dbt_adapter.extract_tasks(
            self._spec(manifest, "round_before_sum_keyed.json")
        )[0]
        self.assertEqual(task.table("orders").primary_key, ("order_id",))
        self.assertFalse(
            any(
                "ROUND-BEFORE-SUM WITNESS" in line
                for line in task.population(
                    PopulationName.COUNTERFACTUAL
                ).conditions
            )
        )

    def test_rounding_signature_is_closed_to_the_exact_supported_ast(self) -> None:
        self.assertIsNotNone(
            dbt_adapter._round_before_sum_signature(
                "SUM(ROUND(amount / 1000000.0, 2))"
            )
        )
        for expression in (
            "SUM(ROUND(amount, 2))",
            "SUM(ROUND((amount + fee) / 1000000.0, 2))",
            "SUM(ROUND(amount / divisor, 2))",
            "ROUND(SUM(amount) / 1000000.0, 2)",
            "SUM(ROUND(amount / -1000000.0, 2))",
            "SUM(ROUND(amount / 1000000.0, 9))",
        ):
            with self.subTest(expression=expression):
                self.assertIsNone(
                    dbt_adapter._round_before_sum_signature(expression)
                )

    def test_post_aggregate_expression_is_not_promoted_into_the_grain(self) -> None:
        """Regression for Twitter's two-stage account-report rollup.

        ``post_total`` is first defined row-wise, then summed in two later CTEs.
        Recovery deliberately resolves it to the original arithmetic expression.
        The compact plan cannot replay an expression *after* aggregation, so it
        must report and omit that output instead of widening GROUP BY with the
        two component metrics.  Independently recoverable aggregates survive.
        """
        manifest = _dbt_manifest()
        raw = manifest["sources"]["source.shop_pkg.shop.orders"]["columns"]
        raw.update(
            {
                "first_metric": _dbt_col("first_metric", "integer"),
                "second_metric": _dbt_col("second_metric", "integer"),
            }
        )
        staging = manifest["nodes"]["model.shop_pkg.stg_orders"]
        staging["columns"].update(
            {
                "first_metric": _dbt_col("first_metric", "integer"),
                "second_metric": _dbt_col("second_metric", "integer"),
            }
        )
        staging["compiled_code"] = (
            "select order_id, customer_id, amount, first_metric, second_metric "
            "from orders"
        )
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "customer_name": _dbt_col("customer_name", "text"),
            "total_amount": _dbt_col("total_amount", "integer"),
            "post_total": _dbt_col("post_total", "integer"),
        }
        model["compiled_code"] = (
            "with customers as (select * from customers), "
            "promoted as (select *, first_metric + second_metric as post_total "
            "from stg_orders), "
            "rollup as (select customer_id, sum(amount) as total_amount, "
            "sum(post_total) as post_total from promoted group by 1), "
            "final as (select report.customer_id, customers.customer_name, "
            "sum(report.total_amount) as total_amount, "
            "sum(report.post_total) as post_total from rollup as report "
            "left join customers on report.customer_id = customers.customer_id "
            "group by 1, 2) select * from final"
        )

        task = dbt_adapter.extract_tasks(self._spec(manifest, "post_aggregate.json"))[0]
        mart = task.marts[0]
        self.assertIn("total_amount", {column.name for column in mart.columns})
        self.assertNotIn("post_total", {column.name for column in mart.columns})
        self.assertNotIn("first_metric", mart.key_columns)
        self.assertNotIn("second_metric", mart.key_columns)
        self.assertIn("post-aggregation expression", mart.plan.notes)

    def test_sum_of_fallback_components_states_the_all_missing_behavior(self) -> None:
        description = dbt_adapter._describe_component_nulls(
            "Install count.",
            "SUM(COALESCE(tap_new_downloads, new_downloads) + "
            "COALESCE(tap_redownloads, redownloads))",
        )
        self.assertIn("If any added component still has no value", description)
        self.assertIn("does not contribute", description)

    def test_recovered_components_replace_a_stale_vendor_default(self) -> None:
        description = dbt_adapter._describe_component_nulls(
            "Sale total (default = purchases + custom).",
            "SUM(COALESCE(purchases, 0) + COALESCE(custom, 0) + "
            "COALESCE(sign_ups, 0))",
        )
        self.assertNotIn("default = purchases + custom", description)
        self.assertIn(
            "uses exactly purchases + custom + sign_ups", description
        )
        self.assertNotIn(
            "uses exactly sign_ups + purchases + custom", description
        )
        self.assertIn("purchases", description)
        self.assertIn("custom", description)
        self.assertIn("sign_ups", description)
        self.assertIn(
            "exact per-input component list is complete and authoritative",
            description,
        )
        self.assertIn(
            "every named component remains an input even if it is not emitted",
            description,
        )

    def test_md5_surrogate_key_contract_preserves_every_graded_byte_rule(self) -> None:
        expression = (
            "MD5(CAST(COALESCE(CAST(account_id AS TEXT), "
            "'_dbt_utils_surrogate_key_null_') || '-' || "
            "COALESCE(CAST(line_item_id AS TEXT), "
            "'_dbt_utils_surrogate_key_null_') || '-' || "
            "COALESCE(CAST(segment AS TEXT), "
            "'_dbt_utils_surrogate_key_null_') || '-' || "
            "COALESCE(CAST(placement AS TEXT), "
            "'_dbt_utils_surrogate_key_null_') AS TEXT))"
        )
        description = dbt_adapter._md5_surrogate_key_contract(
            expression, "keyword_id"
        )
        self.assertIsNotNone(description)
        assert description is not None
        self.assertIn(
            "account_id, line_item_id, segment, placement in that order",
            description,
        )
        self.assertIn("'_dbt_utils_surrogate_key_null_'", description)
        self.assertIn("place '-' between adjacent values", description)
        self.assertIn("lowercase 32-character hexadecimal MD5 digest", description)

    def test_versioned_source_with_a_generated_key_states_the_task_domain(self) -> None:
        node = dbt_adapter.DbtNode(
            unique_id="source.pkg.raw.account_history",
            name="account_history",
            resource_type="source",
            description=(
                "Each record represents a version of an account; versions are "
                "differentiated by updated_at."
            ),
            columns=(
                dbt_adapter.DbtColumn(name="id", data_type="text"),
                dbt_adapter.DbtColumn(name="updated_at", data_type="timestamp"),
            ),
        )
        table = dbt_adapter._source_to_table(
            dbt_adapter.CandidateSpec(package_name="pkg", sources=(node,)),
            node,
            grounded_keys=("id",),
        )
        self.assertEqual(table.primary_key, ("id",))
        self.assertIn("at most one row per id is present", table.description)
        self.assertIn("no history-version selection", table.description)
        self.assertIn("version deduplication is performed", table.description)

    def test_versioned_source_without_a_key_states_rows_are_used_verbatim(self) -> None:
        node = dbt_adapter.DbtNode(
            unique_id="source.pkg.raw.campaign_history",
            name="campaign_history",
            resource_type="source",
            description="Each record represents a version of a campaign.",
            columns=(dbt_adapter.DbtColumn(name="id", data_type="text"),),
        )
        table = dbt_adapter._source_to_table(
            dbt_adapter.CandidateSpec(package_name="pkg", sources=(node,)),
            node,
        )
        self.assertEqual(table.primary_key, ())
        self.assertIn("Rows are used exactly as supplied", table.description)
        self.assertIn("no history-version selection", table.description)

    # -- certification: a recovered expression must be EXECUTABLE ------------

    def test_table_qualifiers_are_stripped_and_ambiguity_is_refused(self) -> None:
        self.assertEqual(
            dbt_adapter._unqualify("SUM(report.clicks)"), "SUM(clicks)"
        )
        self.assertIsNone(
            dbt_adapter._unqualify("SUM(a.clicks) + SUM(b.clicks)")
        )

    def test_the_type_a_recovered_expression_demands_is_inferred(self) -> None:
        """Every one of the 857 source columns in the producing packages
        declares `data_type: null`, so recovery has to read the type off the
        package's own SQL or the measure cannot execute at all."""
        want = dbt_adapter._required_types(
            "SUM(COALESCE(clicks, click_through_conversions))"
        )
        self.assertEqual(want["clicks"], ColumnType.BIGINT)
        # The COALESCE sibling is typed too — mixing BIGINT and VARCHAR in one
        # COALESCE is a DuckDB binder error, and it failed a real reference run.
        self.assertEqual(want["click_through_conversions"], ColumnType.BIGINT)
        self.assertEqual(
            dbt_adapter._required_types("CASE WHEN is_active THEN id END")["is_active"],
            ColumnType.BOOLEAN,
        )
        casted = dbt_adapter._required_types(
            "SUM(COALESCE(CAST(conversion_metric AS BIGINT), 0))"
        )
        self.assertEqual(casted["conversion_metric"], ColumnType.BIGINT)
        truncated = dbt_adapter._required_types("DATE_TRUNC('DAY', event_at)")
        self.assertEqual(truncated["event_at"], ColumnType.TIMESTAMP)

    def test_date_trunc_of_a_date_has_duckdb_timestamp_result_type(self) -> None:
        self.assertEqual(
            dbt_adapter._derived_result_type(
                "DATE_TRUNC('DAY', event_date)",
                {"event_date": ColumnType.DATE},
            ),
            ColumnType.TIMESTAMP,
        )

    def test_an_inferred_type_reaches_the_source_table(self) -> None:
        manifest = _dbt_manifest()
        manifest["sources"]["source.shop_pkg.shop.orders"]["columns"]["is_paid"] = (
            _dbt_col("is_paid", None)
        )
        manifest["nodes"]["model.shop_pkg.stg_orders"]["columns"]["is_paid"] = (
            _dbt_col("is_paid", None)
        )
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "paid_count": _dbt_col("paid_count", "integer"),
        }
        model["compiled_code"] = (
            "select customer_id, count(case when is_paid then order_id end)"
            " as paid_count from base group by 1"
        )
        task = dbt_adapter.extract_tasks(self._spec(manifest, "typed.json"))[0]
        orders = next(t for t in task.tables if t.name == "orders")
        is_paid = next(c for c in orders.columns if c.name == "is_paid")
        self.assertEqual(is_paid.type, ColumnType.BOOLEAN)
        # A DECLARED type always wins: inference is a fallback, not a correction.
        amount = next(c for c in orders.columns if c.name == "amount")
        self.assertEqual(amount.type, ColumnType.DECIMAL)

    # -- fail closed, and say what was lost ----------------------------------

    def test_every_dropped_column_is_named_with_its_reason(self) -> None:
        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "total_amount": _dbt_col("total_amount", "numeric"),
            "mystery_metric": _dbt_col("mystery_metric", "numeric"),
        }
        model["compiled_code"] = (
            "select customer_id, sum(amount) as total_amount,"
            " sum(nothing_grounds_here) as mystery_metric from base group by 1"
        )
        task = dbt_adapter.extract_tasks(self._spec(manifest, "dropped.json"))[0]
        mart = task.marts[0]
        self.assertNotIn("mystery_metric", {c.name for c in mart.columns})
        notes = mart.plan.notes
        self.assertIn("3 declared", notes)
        self.assertIn("mystery_metric", notes)
        self.assertIn("nothing_grounds_here", notes)

    def test_an_ungroundable_mart_is_still_dropped_whole(self) -> None:
        """Loosening lineage must not loosen the refusal."""
        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_order_id": _dbt_col("customer_order_id", "integer"),
            "computed_metric": _dbt_col("computed_metric", "numeric"),
        }
        model["compiled_code"] = "select 1 as customer_order_id, 2 as computed_metric"
        with self.assertRaises(dbt_adapter.KeylessMartError):
            dbt_adapter.extract_tasks(self._spec(manifest, "ungrounded.json"))

    def test_every_shipped_column_declares_its_kind(self) -> None:
        """`MartColumn.kind` is the instrument the anchor's headline statistic
        is measured with; a column with no declared kind is unmeasurable."""
        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["compiled_code"] = (
            "select customer_id, count(*) as order_count,"
            " sum(amount) as total_amount from base group by 1"
        )
        task = dbt_adapter.extract_tasks(self._spec(manifest, "kinds2.json"))[0]
        for mart in task.marts:
            with self.subTest(mart=mart.name):
                self.assertEqual(mart_plan.unclassified_columns(mart), [])
                self.assertEqual(mart_plan.column_kind_problems(mart), [])


class DbtLineageAwareGroundingTest(unittest.TestCase):
    """C1: a recovered ref binds ONLY within the source lineage the model's
    own SQL gives it — never to a same-named column of another table.

    Measured on the released reddit_ads task: every mart shipped `clicks` as
    `SUM(clicks)` over the CONVERSIONS table (Fivetran's deprecated
    click-through-conversions column of that name) while the vendor computes
    `sum(report.clicks)` over the REPORT table; gold had `clicks == conversions`
    in 54 of 63 rows. `recover_projections` now carries per-ref lineage and
    `ground_mart` refuses to bind a ref outside it.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _spec(self, manifest: dict, name: str):
        return dbt_adapter.load_manifest(_write_json(self.tmp.name, name, manifest))

    @staticmethod
    def _manifest(mart_sql: str, mart_columns: dict) -> dict:
        """Two sources BOTH declaring `clicks`, each behind a staging model,
        one mart reading both — the reddit_ads name collision in miniature."""
        return {
            "metadata": {"project_name": "ads_pkg"},
            "sources": {
                "source.ads_pkg.ads.report": {
                    "name": "report",
                    "package_name": "ads_pkg",
                    "columns": {
                        "account_id": _dbt_col("account_id"),
                        "date": _dbt_col("date"),
                        "clicks": _dbt_col("clicks"),
                        "spend": _dbt_col("spend"),
                    },
                },
                "source.ads_pkg.ads.conv": {
                    "name": "conv",
                    "package_name": "ads_pkg",
                    "columns": {
                        "account_id": _dbt_col("account_id"),
                        "date": _dbt_col("date"),
                        "clicks": _dbt_col("clicks", None, "Click-through conversions."),
                        "value": _dbt_col("value"),
                    },
                },
            },
            "nodes": {
                "model.ads_pkg.stg_report": {
                    "resource_type": "model",
                    "name": "stg_report",
                    "package_name": "ads_pkg",
                    "depends_on": {"nodes": ["source.ads_pkg.ads.report"]},
                    "compiled_code": (
                        'select account_id, date as date_day, clicks, spend '
                        'from "db"."s"."report"'
                    ),
                    "columns": {},
                },
                "model.ads_pkg.stg_conv": {
                    "resource_type": "model",
                    "name": "stg_conv",
                    "package_name": "ads_pkg",
                    "depends_on": {"nodes": ["source.ads_pkg.ads.conv"]},
                    "compiled_code": (
                        'select account_id, date as date_day, clicks, value '
                        'from "db"."s"."conv"'
                    ),
                    "columns": {},
                },
                "model.ads_pkg.ads__account_report": {
                    "resource_type": "model",
                    "name": "ads__account_report",
                    "package_name": "ads_pkg",
                    "depends_on": {
                        "nodes": ["model.ads_pkg.stg_report", "model.ads_pkg.stg_conv"]
                    },
                    "compiled_code": mart_sql,
                    "columns": mart_columns,
                },
                "test.ads_pkg.not_null_account_id": {
                    "resource_type": "test",
                    "test_metadata": {"name": "not_null", "kwargs": {"column_name": "account_id"}},
                    "attached_node": "model.ads_pkg.ads__account_report",
                    "depends_on": {"nodes": ["model.ads_pkg.ads__account_report"]},
                },
                "test.ads_pkg.not_null_date_day": {
                    "resource_type": "test",
                    "test_metadata": {"name": "not_null", "kwargs": {"column_name": "date_day"}},
                    "attached_node": "model.ads_pkg.ads__account_report",
                    "depends_on": {"nodes": ["model.ads_pkg.ads__account_report"]},
                },
            },
        }

    _MART_SQL = (
        'with report as (select * from "db"."s"."stg_report"), '
        'conv as (select * from "db"."s"."stg_conv") '
        "select conv.account_id, conv.date_day, sum(conv.value) as value, "
        "sum(report.clicks) as clicks "
        "from conv left join report on conv.account_id = report.account_id "
        "and conv.date_day = report.date_day group by 1, 2"
    )
    _MART_COLUMNS = {
        "account_id": _dbt_col("account_id"),
        "date_day": _dbt_col("date_day"),
        "value": _dbt_col("value"),
        "clicks": _dbt_col("clicks"),
    }

    def test_a_qualified_ref_never_binds_to_a_same_named_column_of_another_table(self) -> None:
        spec = self._spec(self._manifest(self._MART_SQL, self._MART_COLUMNS), "c1a.json")
        result = dbt_adapter.extract_candidates(spec)
        self.assertEqual(len(result.tasks), 1, [s.reason for s in result.skipped])
        mart = result.tasks[0].marts[0]
        self.assertIn("conv", mart.plan.notes.split("GROUNDING")[0])  # base is conv
        names = {c.name for c in mart.columns}
        self.assertIn("value", names)
        self.assertNotIn("clicks", names)
        self.assertIn("clicks:", mart.plan.notes)
        self.assertIn("report", mart.plan.notes.split("clicks:")[1][:200])

    def test_an_unqualified_ref_takes_the_lineage_of_its_select_scope(self) -> None:
        """The campaign_country pattern: `sum(clicks)` in a CTE whose only
        FROM is the report table has report lineage, and the base (conv, by
        hit count) also declares `clicks`."""
        sql = (
            'with report as (select * from "db"."s"."stg_report"), '
            "rollup as (select account_id, date_day, sum(clicks) as clicks "
            "from report group by 1, 2), "
            'conv as (select * from "db"."s"."stg_conv") '
            "select conv.account_id, conv.date_day, sum(conv.value) as value, "
            "max(conv.value) as top_value, max(rollup.clicks) as clicks "
            "from conv left join rollup on conv.account_id = rollup.account_id "
            "and conv.date_day = rollup.date_day group by 1, 2"
        )
        columns = {**self._MART_COLUMNS, "top_value": _dbt_col("top_value")}
        spec = self._spec(self._manifest(sql, columns), "c1b.json")
        result = dbt_adapter.extract_candidates(spec)
        self.assertEqual(len(result.tasks), 1, [s.reason for s in result.skipped])
        mart = result.tasks[0].marts[0]
        self.assertNotIn("clicks", {c.name for c in mart.columns})
        self.assertIn("value", {c.name for c in mart.columns})

    def test_lineage_resolution_follows_ctes_to_source_tables(self) -> None:
        spec = self._spec(self._manifest(self._MART_SQL, self._MART_COLUMNS), "c1c.json")
        model = next(m for m in spec.models if m.name == "ads__account_report")
        resolve = dbt_adapter._source_resolver(spec, model)
        self.assertEqual(resolve("report"), frozenset({"report"}))     # CTE -> stg -> source
        self.assertEqual(resolve("conv"), frozenset({"conv"}))
        self.assertEqual(resolve("stg_conv"), frozenset({"conv"}))     # a model name
        self.assertEqual(resolve("conv"), resolve("conv"))            # memoized, stable
        self.assertEqual(resolve("nowhere"), frozenset({"?nowhere"}))  # unknown, marked
        projections = {
            p.column: p for p in dbt_adapter.recover_projections(model, resolve)
        }
        self.assertEqual(projections["clicks"].lineage, (("clicks", ("report",)),))
        self.assertEqual(projections["value"].lineage, (("value", ("conv",)),))

    def test_type_requirements_land_on_the_lineage_table(self) -> None:
        """`sum(conv.value)` puts BIGINT on conv.value; the report table's
        same-named `clicks` (dropped) receives no misplaced requirement, and
        conv.clicks — never summed by this mart — stays untyped (TEXT)."""
        spec = self._spec(self._manifest(self._MART_SQL, self._MART_COLUMNS), "c1d.json")
        task = dbt_adapter.extract_candidates(spec).tasks[0]
        by_name = {t.name: t for t in task.tables}
        self.assertEqual(by_name["conv"].column("value").type, ColumnType.BIGINT)
        self.assertEqual(by_name["conv"].column("clicks").type, ColumnType.TEXT)
        self.assertEqual(by_name["report"].column("clicks").type, ColumnType.TEXT)

    def test_measure_binding_in_model_to_mart_honours_lineage(self) -> None:
        """With the report table reachable as a lookup hop (a declared
        relationship makes conv the child), a measure `sum(conv.clicks)` binds
        to conv, not the hop, even though both declare `clicks`; and the hop's
        own additive `sum(report.spend)` is refused as a fan-out."""
        sql = (
            'with report as (select * from "db"."s"."stg_report"), '
            'conv as (select * from "db"."s"."stg_conv") '
            "select conv.account_id, conv.date_day, sum(conv.value) as value, "
            "sum(conv.clicks) as clicks, sum(report.spend) as spend, "
            "max(report.spend) as top_spend "
            "from conv left join report on conv.account_id = report.account_id "
            "and conv.date_day = report.date_day group by 1, 2"
        )
        columns = {
            **self._MART_COLUMNS,
            "spend": _dbt_col("spend"),
            "top_spend": _dbt_col("top_spend"),
        }
        manifest = self._manifest(sql, columns)
        # A declared relationships test: conv (child) -> report (parent) on
        # (account_id, date) — the report is a lookup hop of the conv base.
        manifest["nodes"]["test.ads_pkg.rel_conv_report"] = {
            "resource_type": "test",
            "test_metadata": {
                "name": "relationships",
                "kwargs": {
                    "column_name": "date",
                    "to": "source('ads', 'report')",
                    "field": "date",
                },
            },
            "attached_node": "source.ads_pkg.ads.conv",
            "depends_on": {"nodes": ["source.ads_pkg.ads.report", "source.ads_pkg.ads.conv"]},
        }
        spec = self._spec(manifest, "c1e.json")
        result = dbt_adapter.extract_candidates(spec)
        self.assertEqual(len(result.tasks), 1, [s.reason for s in result.skipped])
        task = result.tasks[0]
        mart = task.marts[0]
        names = {c.name for c in mart.columns}
        self.assertIn("clicks", names)
        self.assertIn("top_spend", names)     # MAX over the hop: a row-level fact
        self.assertNotIn("spend", names)      # SUM over the hop: fans out
        self.assertIn("fan-out", mart.plan.notes)
        derive = next(op for op in mart.plan.ops if op.details.get("name") == "mart_base")
        self.assertIn('conv."clicks" AS "clicks"', derive.details["select"])
        join = next(op for op in mart.plan.ops if op.kind is MartOpKind.JOIN)
        self.assertEqual(join.tables[-1], "report")
        # The type demanded by the kept measure lands on conv, not report.
        by_name = {t.name: t for t in task.tables}
        self.assertEqual(by_name["conv"].column("clicks").type, ColumnType.BIGINT)
        self.assertEqual(by_name["report"].column("clicks").type, ColumnType.TEXT)
        # The lookup parent is minted unique on the join key (`date`, not the
        # FK-free requirement here: report has no foreign keys at all).
        self.assertEqual(by_name["report"].primary_key, ("date",))

    def test_unknown_lineage_keeps_the_bare_name_binding(self) -> None:
        """No resolver (or a `?` relation) means no evidence: today's
        behaviour, bind by bare name — never a silent drop."""
        spec = self._spec(self._manifest(self._MART_SQL, self._MART_COLUMNS), "c1f.json")
        model = next(m for m in spec.models if m.name == "ads__account_report")
        projections = {p.column: p for p in dbt_adapter.recover_projections(model)}
        self.assertEqual(projections["clicks"].lineage, ())
        self.assertEqual(
            dbt_adapter._lineage_tables("clicks", ("conv", "report"), ()),
            ("conv", "report"),
        )
        self.assertEqual(
            dbt_adapter._lineage_tables(
                "clicks", ("conv", "report"), (("clicks", ("?x",)),)
            ),
            ("conv", "report"),
        )
        self.assertEqual(
            dbt_adapter._lineage_tables(
                "clicks", ("conv", "report"), (("clicks", ("report",)),)
            ),
            ("report",),
        )
        self.assertEqual(
            dbt_adapter._lineage_tables("clicks", ("conv",), (("clicks", ("report",)),)),
            (),
        )


class DbtStagingTypeEvidenceTest(unittest.TestCase):
    """I4(a)/G1(1): the type a package's OWN STAGING declares reaches the
    source table (and the mart column), by evidence strength.

    Measured on the released reddit_ads task: 77 of 141 staging-typed columns
    shipped TEXT — public docs promised `YYYY-MM-DD` dates over `date_217317`
    tokens. Precedence: declared data_type > staging non-TEXT cast (or the
    `get_<table>_columns` macro) > recovered-use requirement > staging TEXT >
    TEXT.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _spec(self, manifest: dict, name: str):
        return dbt_adapter.load_manifest(_write_json(self.tmp.name, name, manifest))

    @staticmethod
    def _typed_manifest() -> dict:
        """The base shop fixture with UNDECLARED order types and a Fivetran-
        style staging model casting them."""
        manifest = _dbt_manifest()
        orders = manifest["sources"]["source.shop_pkg.shop.orders"]["columns"]
        orders["order_id"] = _dbt_col("order_id", "bigint")     # declared
        orders["customer_id"] = _dbt_col("customer_id", "integer")
        orders["amount"] = _dbt_col("amount", None)              # undeclared
        orders["order_date"] = _dbt_col("order_date", None)
        orders["_fivetran_synced"] = _dbt_col("_fivetran_synced", None)
        orders["is_gift"] = _dbt_col("is_gift", None)
        orders["note"] = _dbt_col("note", None)
        manifest["nodes"]["model.shop_pkg.stg_orders"]["compiled_code"] = (
            'with base as (select * from "db"."s"."orders"), fields as (select '
            "cast(null as bigint) as order_id, cast(null as integer) as customer_id, "
            "cast(null as float) as amount, cast(null as date) as order_date, "
            "cast(null as timestamp) as _fivetran_synced, "
            "cast(null as boolean) as is_gift, cast(null as text) as note "
            "from base) select order_id, customer_id, amount, order_date as order_day, "
            "_fivetran_synced, is_gift, note from fields"
        )
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "order_day": _dbt_col("order_day"),
            "order_count": _dbt_col("order_count"),
            "total_amount": _dbt_col("total_amount"),
            "avg_amount": _dbt_col("avg_amount"),
            "gift_orders": _dbt_col("gift_orders"),
            "declared_total": _dbt_col("declared_total", "numeric"),
        }
        model["compiled_code"] = (
            "select customer_id, order_day, count(order_id) as order_count, "
            "sum(amount) as total_amount, avg(amount) as avg_amount, "
            "sum(case when is_gift then 1 else 0 end) as gift_orders, "
            "sum(amount) as declared_total "
            'from "db"."s"."stg_orders" group by 1, 2'
        )
        # `customer_orders` now groups by two columns; the unique test on
        # customer_id no longer describes it, so declare the grain by not_null.
        del manifest["nodes"]["test.shop_pkg.unique_customer_orders_customer_id"]
        for col in ("customer_id", "order_day"):
            manifest["nodes"][f"test.shop_pkg.not_null_customer_orders_{col}"] = {
                "resource_type": "test",
                "test_metadata": {"name": "not_null", "kwargs": {"column_name": col}},
                "attached_node": "model.shop_pkg.customer_orders",
                "depends_on": {"nodes": ["model.shop_pkg.customer_orders"]},
            }
        return manifest

    def test_staging_type_map_reads_compiled_casts(self) -> None:
        spec = self._spec(self._typed_manifest(), "stm.json")
        types = dbt_adapter.staging_type_map(spec)["orders"]
        self.assertEqual(types["order_date"], ColumnType.DATE)
        self.assertEqual(types["_fivetran_synced"], ColumnType.TIMESTAMP)
        self.assertEqual(types["is_gift"], ColumnType.BOOLEAN)
        self.assertEqual(types["amount"], ColumnType.FLOAT)
        self.assertEqual(types["customer_id"], ColumnType.INTEGER)
        self.assertEqual(types["note"], ColumnType.TEXT)
        # `order_day` is an alias, not a declared source column: not recorded.
        self.assertNotIn("order_day", types)

    def test_a_staging_cast_null_type_reaches_the_source_table(self) -> None:
        task = dbt_adapter.extract_tasks(self._spec(self._typed_manifest(), "cast.json"))[0]
        orders = next(t for t in task.tables if t.name == "orders")
        self.assertEqual(orders.column("order_date").type, ColumnType.DATE)
        self.assertEqual(orders.column("_fivetran_synced").type, ColumnType.TIMESTAMP)
        self.assertEqual(orders.column("is_gift").type, ColumnType.BOOLEAN)
        self.assertEqual(orders.column("note").type, ColumnType.TEXT)

    def test_declared_data_type_beats_staging_cast(self) -> None:
        manifest = self._typed_manifest()
        manifest["sources"]["source.shop_pkg.shop.orders"]["columns"]["order_date"] = (
            _dbt_col("order_date", "timestamp")
        )
        task = dbt_adapter.extract_tasks(self._spec(manifest, "declared.json"))[0]
        orders = next(t for t in task.tables if t.name == "orders")
        self.assertEqual(orders.column("order_date").type, ColumnType.TIMESTAMP)

    def test_staging_float_keeps_a_sum_measure(self) -> None:
        """`sum(amount)` demands BIGINT; the staging FLOAT satisfies it
        (`_type_satisfies`), so the measure is kept, the source ships FLOAT and
        the mart column follows the aggregated value's type."""
        task = dbt_adapter.extract_tasks(self._spec(self._typed_manifest(), "float.json"))[0]
        orders = next(t for t in task.tables if t.name == "orders")
        self.assertEqual(orders.column("amount").type, ColumnType.FLOAT)
        mart = task.marts[0]
        by_name = {c.name: c for c in mart.columns}
        self.assertIn("total_amount", by_name)
        self.assertEqual(by_name["total_amount"].type, ColumnType.FLOAT)
        self.assertTrue(dbt_adapter._type_satisfies(ColumnType.FLOAT, ColumnType.BIGINT))
        self.assertTrue(dbt_adapter._type_satisfies(ColumnType.DECIMAL, ColumnType.FLOAT))
        self.assertTrue(dbt_adapter._type_satisfies(ColumnType.DATE, ColumnType.TIMESTAMP))
        self.assertFalse(dbt_adapter._type_satisfies(ColumnType.TEXT, ColumnType.BIGINT))
        self.assertFalse(dbt_adapter._type_satisfies(ColumnType.BOOLEAN, ColumnType.BIGINT))
        self.assertFalse(dbt_adapter._type_satisfies(ColumnType.TIMESTAMP, ColumnType.BOOLEAN))

    def test_mart_column_type_follows_its_source_when_undeclared(self) -> None:
        task = dbt_adapter.extract_tasks(self._spec(self._typed_manifest(), "martty.json"))[0]
        by_name = {c.name: c for c in task.marts[0].columns}
        self.assertEqual(by_name["order_day"].type, ColumnType.DATE)       # passthrough grain
        self.assertEqual(by_name["order_count"].type, ColumnType.BIGINT)   # COUNT
        self.assertEqual(by_name["avg_amount"].type, ColumnType.FLOAT)     # AVG
        self.assertEqual(by_name["gift_orders"].type, ColumnType.BIGINT)   # SUM of literals
        self.assertEqual(by_name["declared_total"].type, ColumnType.DECIMAL)  # declared wins
        self.assertEqual(by_name["customer_id"].type, ColumnType.INTEGER)  # declared

    def test_a_staging_type_a_measure_cannot_run_over_drops_the_measure(self) -> None:
        """Fail closed, never retype: `sum(_fivetran_synced)` over a staging
        TIMESTAMP is dropped with the adopted type named."""
        manifest = self._typed_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"]["synced_total"] = _dbt_col("synced_total")
        model["compiled_code"] = model["compiled_code"].replace(
            "sum(amount) as declared_total",
            "sum(amount) as declared_total, sum(_fivetran_synced) as synced_total",
        )
        task = dbt_adapter.extract_tasks(self._spec(manifest, "badtype.json"))[0]
        mart = task.marts[0]
        self.assertNotIn("synced_total", {c.name for c in mart.columns})
        self.assertIn("synced_total: recovered aggregate needs _fivetran_synced", mart.plan.notes)
        self.assertIn("timestamp", mart.plan.notes)

    def test_contradictory_staging_casts_record_nothing(self) -> None:
        manifest = self._typed_manifest()
        manifest["nodes"]["model.shop_pkg.stg_orders_b"] = {
            "resource_type": "model",
            "name": "stg_orders_b",
            "package_name": "shop_pkg",
            "depends_on": {"nodes": ["source.shop_pkg.shop.orders"]},
            "compiled_code": 'select cast(order_date as timestamp) as order_date from "db"."s"."orders"',
            "columns": {},
        }
        types = dbt_adapter.staging_type_map(self._spec(manifest, "conflict.json"))["orders"]
        self.assertNotIn("order_date", types)   # DATE vs TIMESTAMP: no evidence
        self.assertEqual(types["is_gift"], ColumnType.BOOLEAN)  # the rest unaffected

    def test_staging_macro_datatypes_reach_the_source_table(self) -> None:
        """G1(1) fallback: the `get_<table>_columns` macro types a column the
        compiled casts do not; a declared data_type still wins; a recovered
        SUM over a macro-TEXT column still adopts BIGINT."""
        manifest = _dbt_manifest()
        orders = manifest["sources"]["source.shop_pkg.shop.orders"]["columns"]
        orders["amount"] = _dbt_col("amount", None)
        orders["order_date"] = _dbt_col("order_date", None)
        orders["synced_at"] = _dbt_col("synced_at", None)
        orders["qty"] = _dbt_col("qty", None)
        orders["label"] = _dbt_col("label", None)
        manifest["macros"] = {
            "macro.shop_pkg.get_orders_columns": {
                "name": "get_orders_columns",
                "package_name": "shop_pkg",
                "macro_sql": (
                    "{% macro get_orders_columns() %}{% set columns = [\n"
                    '    {"name": "order_date", "datatype": "date"},\n'
                    '    {"name": "synced_at", "datatype": dbt.type_timestamp()},\n'
                    '    {"name": "qty", "datatype": dbt.type_int()},\n'
                    '    {"name": "amount", "datatype": dbt.type_string()},\n'
                    '    {"name": "label", "datatype": dbt.type_string()},\n'
                    '    {"name": "order_id", "datatype": dbt.type_string()},\n'
                    '    {"name": "not_a_column", "datatype": "date"}\n'
                    "] %}{{ return(columns) }}{% endmacro %}"
                ),
            },
            "macro.dbt.get_merge_update_columns": {"macro_sql": "irrelevant"},
        }
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["columns"] = {
            "customer_id": _dbt_col("customer_id", "integer"),
            "total_amount": _dbt_col("total_amount"),
        }
        model["compiled_code"] = (
            "select customer_id, sum(amount) as total_amount from base group by 1"
        )
        spec = self._spec(manifest, "macro.json")
        self.assertEqual(
            spec.macro_column_types["orders"]["synced_at"], "timestamp"
        )
        task = dbt_adapter.extract_tasks(spec)[0]
        orders_t = next(t for t in task.tables if t.name == "orders")
        self.assertEqual(orders_t.column("order_date").type, ColumnType.DATE)
        self.assertEqual(orders_t.column("synced_at").type, ColumnType.TIMESTAMP)
        self.assertEqual(orders_t.column("qty").type, ColumnType.INTEGER)
        self.assertEqual(orders_t.column("label").type, ColumnType.TEXT)
        self.assertEqual(orders_t.column("order_id").type, ColumnType.BIGINT)  # declared wins
        self.assertEqual(orders_t.column("amount").type, ColumnType.BIGINT)    # SUM beats macro TEXT
        self.assertNotIn("not_a_column", {c.name for c in orders_t.columns})


class DbtOutputSelectRecoveryTest(unittest.TestCase):
    """I4(c): the OUTPUT select's definition of a column wins over an earlier
    CTE alias, and a mart-internal non-aggregate alias is inlined."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _model(self, sql: str, columns: tuple[str, ...]):
        manifest = _dbt_manifest()
        model = manifest["nodes"]["model.shop_pkg.customer_orders"]
        model["compiled_code"] = sql
        model["columns"] = {c: _dbt_col(c) for c in columns}
        spec = dbt_adapter.load_manifest(_write_json(self.tmp.name, "out.json", manifest))
        return spec, next(m for m in spec.models if m.name == "customer_orders")

    def test_the_output_select_definition_beats_an_earlier_cte_alias(self) -> None:
        _, node = self._model(
            "with h as (select conversion_purchases as total_conversions, ad_id "
            'from "db"."s"."src"), '
            "final as (select ad_id, sum(h.total_conversions) as total_conversions "
            "from h group by 1) select * from final",
            ("ad_id", "total_conversions"),
        )
        recovered = {p.column: p for p in dbt_adapter.recover_projections(node)}
        total = recovered["total_conversions"]
        self.assertEqual(total.kind, dbt_adapter.ProjectionKind.AGGREGATE)
        self.assertEqual(total.expr, "SUM(conversion_purchases)")
        self.assertEqual(total.refs, ("conversion_purchases",))

    def test_an_aggregate_of_an_aggregate_alias_is_recovered_where_stated(self) -> None:
        """Fivetran's rollup CTE re-summed in `joined`: `sum(lead_conversions)`
        is not taken from the outer select; the CTE's own aggregate is."""
        _, node = self._model(
            'with conv as (select * from "db"."s"."stg_conv"), '
            "rollup as (select account_id, sum(case when event_name = 'lead' "
            "then conversions else 0 end) as lead_conversions from conv group by 1), "
            'report as (select * from "db"."s"."stg_report"), '
            "joined as (select report.account_id, sum(report.clicks) as clicks, "
            "sum(lead_conversions) as lead_conversions from report left join rollup "
            "on report.account_id = rollup.account_id group by 1) "
            "select * from joined",
            ("account_id", "clicks", "lead_conversions"),
        )
        recovered = {p.column: p for p in dbt_adapter.recover_projections(node)}
        self.assertEqual(
            recovered["lead_conversions"].expr,
            "SUM(CASE WHEN event_name = 'lead' THEN conversions ELSE 0 END)",
        )
        self.assertEqual(recovered["clicks"].expr, "SUM(report.clicks)")

    def test_inlining_follows_a_rename_chain_and_keeps_lineage(self) -> None:
        spec, node = self._model(
            'with a as (select amount as amt, customer_id from "db"."s"."stg_orders"), '
            "b as (select amt as amt2, customer_id from a), "
            "final as (select customer_id, sum(b.amt2) as total from b group by 1) "
            "select * from final",
            ("customer_id", "total"),
        )
        resolve = dbt_adapter._source_resolver(spec, node)
        recovered = {p.column: p for p in dbt_adapter.recover_projections(node, resolve)}
        self.assertEqual(recovered["total"].expr, "SUM(amount)")
        self.assertEqual(recovered["total"].lineage, (("amount", ("orders",)),))


class ConversionSentenceMissingInput(unittest.TestCase):
    """batch10 run K (2026-09-11), twitter_ads: "each source value is divided
    ... and those values are then added together" was read as a row-level sum
    in which a missing value contributes 0, against the aggregate rule that
    preserves an all-missing total as empty. The per-value form now says
    what a missing source value does; the total-then-divide form (no per-row
    step) is unchanged."""

    def test_the_per_value_form_says_a_missing_value_adds_nothing(self) -> None:
        per_value = dbt_adapter._conversion_sentence(
            "SUM(ROUND(billed_charge_local_micro / 1000000.0, 2))",
            "micros", "promoted_tweet_report.billed_charge_local_micro",
        )
        self.assertEqual(
            per_value,
            " Units: converted out of the micros of "
            "promoted_tweet_report.billed_charge_local_micro — each source value is "
            "divided by 1,000,000 and rounded to 2 decimal places, and those values "
            "are then added together, a source row with no value adding nothing to "
            "the total, and a mart row whose source rows all lack a value reports "
            "an empty total, not 0.",
        )
        # A 0-substituted argument is never empty: no such clause.
        defaulted = dbt_adapter._conversion_sentence(
            "SUM(ROUND(COALESCE(billed_charge_local_micro, 0) / 1000000.0, 2))",
            "micros", "promoted_tweet_report.billed_charge_local_micro",
        )
        self.assertNotIn("empty total", defaulted)
        total_first = dbt_adapter._conversion_sentence(
            "ROUND(SUM(billed_charge_local_micro) / 1000000.0, 2)", "micros", "t.c",
        )
        self.assertNotIn("adding nothing", total_first)
        self.assertIn("the added-up total is divided by 1,000,000", total_first)

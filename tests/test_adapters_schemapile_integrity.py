"""Integrity tests for adapters/schemapile.py: the armed key_parents gate
and the ported dedupe prose.

WHY THIS EXISTS. The audit (docs/plans/source_completeness.md §3.1) confirmed
3 of 8 ingested schemapile tasks shipped gold contradicting their own declared
grain because (1) `_build_marts` called `chain_candidates` with
`key_parents=None` — the grain gate in adapters/evidence.py DISARMED — and
(2) `_build_legacy_star` passed `dedupe=` to `build_star` while keeping
raw-count prose. These tests pin the fix:

  * key_parents is MEASURED from the record's own declared primary keys and
    fed to every place the adapter picks a join surface, so a non-PK parent
    column can never again become a grain anchor;
  * a record whose EVERY candidate grain rests on a non-PK parent column is
    REFUSED at ingest (fail closed), not admitted with wrong-gold potential;
  * every dedupe-bearing legacy mart says the deduplicated basis in the
    column prose the solver actually receives (export ships descriptions,
    not plan ops), exactly as adapters/wikidbs.py already does.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from elt_taskgen.adapters import schemapile as sp  # noqa: E402
from elt_taskgen.models import MartOpKind  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures: the real record shape, in miniature (same helpers as the main
# schemapile test module, duplicated on purpose — this module must not import
# another test module to stay runnable in isolation).
# ---------------------------------------------------------------------------


def _col(type_: str, *, nullable=None, primary=False, checks=None, comment=None) -> dict:
    return {
        "TYPE": type_,
        "NULLABLE": nullable,
        "UNIQUE": None,
        "DEFAULT": None,
        "CHECKS": list(checks or []),
        "IS_PRIMARY": primary,
        "IS_INDEX": False,
        "COMMENT": comment,
    }


def _table(columns: dict, *, pks=(), fks=()) -> dict:
    return {
        "COLUMNS": columns,
        "PRIMARY_KEYS": list(pks),
        "FOREIGN_KEYS": list(fks),
        "CHECKS": [],
        "INDEXES": [],
        "COMMENT": None,
    }


def _fk(columns, table, referred) -> dict:
    return {
        "COLUMNS": list(columns),
        "FOREIGN_TABLE": table,
        "REFERRED_COLUMNS": list(referred),
        "ON_DELETE": None,
        "ON_UPDATE": None,
    }


def _tables_and_rels(raw: dict, key: str = "fixture.sql"):
    return sp.record_to_tables({"TABLES": raw}, key=key)


def mixed_key_tables() -> dict:
    """One KEYED chain (customers <- orders) and one UNKEYED link.

    `groups` declares group_id as its PK but the orders FK targets
    group_code — a legal SchemaPile declaration whose parent column is NOT a
    key, i.e. the exact shape that shipped inflated gold on 204135/388796/
    042316. The armed gate must keep `groups` out of every grain anchor while
    the keyed chain still funds marts.
    """
    return {
        "customers": _table(
            {
                "customer_id": _col("Int", nullable=False, primary=True),
                "customer_name": _col("Varchar", nullable=False),
                "region": _col("Varchar"),
            },
            pks=["customer_id"],
        ),
        "groups": _table(
            {
                "group_id": _col("Int", nullable=False, primary=True),
                "group_code": _col("Int"),
                "group_name": _col("Varchar"),
            },
            pks=["group_id"],
        ),
        "orders": _table(
            {
                "order_id": _col("BigInt", nullable=False, primary=True),
                "customer_id": _col("Int", nullable=False),
                "group_code": _col("Int"),
                "amount": _col("Decimal(10,2)"),
                "status": _col("Varchar", checks=["status IN ('open', 'closed')"]),
                "note": _col("Varchar"),
            },
            pks=["order_id"],
            fks=[
                _fk(["customer_id"], "customers", ["customer_id"]),
                _fk(["group_code"], "groups", ["group_code"]),  # NOT a key
            ],
        ),
        "order_items": _table(
            {
                "item_id": _col("Int", nullable=False, primary=True),
                "order_id": _col("BigInt", nullable=False),
                "label": _col("Varchar"),
                "quantity": _col("Int"),
            },
            pks=["item_id"],
            fks=[_fk(["order_id"], "orders", ["order_id"])],
        ),
    }


def unkeyed_only_tables() -> dict:
    """EVERY declared link rests on a non-PK parent column: must be refused."""
    return {
        "groups": _table(
            {
                "group_id": _col("Int", nullable=False, primary=True),
                "group_code": _col("Int"),
                "group_name": _col("Varchar"),
            },
            pks=["group_id"],
        ),
        "events": _table(
            {
                "event_id": _col("Int", nullable=False, primary=True),
                "group_code": _col("Int"),
                "note": _col("Varchar"),
            },
            pks=["event_id"],
            fks=[_fk(["group_code"], "groups", ["group_code"])],
        ),
    }


def pkless_fact_tables() -> dict:
    """Keyed parent, fact with NO declared PK: the dedupe-bearing legacy star."""
    return {
        "customers": _table(
            {
                "customer_id": _col("Int", nullable=False, primary=True),
                "customer_name": _col("Varchar", nullable=False),
            },
            pks=["customer_id"],
        ),
        "orders": _table(
            {
                "customer_id": _col("Int", nullable=False),
                "amount": _col("Decimal(10,2)"),
                "note": _col("Varchar"),
            },
            fks=[_fk(["customer_id"], "customers", ["customer_id"])],
        ),
    }


def incompatible_fk_tables() -> dict:
    """The production failure in miniature: a TEXT season targets an INT id."""
    return {
        "time_periods": _table(
            {
                "t_periods": _col("Int", nullable=False, primary=True),
                "label": _col("Varchar"),
            },
            pks=["t_periods"],
        ),
        "output_vflow_in": _table(
            {
                "row_id": _col("BigInt", nullable=False, primary=True),
                "t_season": _col("Varchar", nullable=False),
                "amount": _col("Decimal(10,2)"),
            },
            pks=["row_id"],
            fks=[_fk(["t_season"], "time_periods", ["t_periods"])],
        ),
    }


# ---------------------------------------------------------------------------
# key_parents: measured, armed, and applied to every join surface
# ---------------------------------------------------------------------------


class MeasuredKeyParentsTest(unittest.TestCase):
    def test_measured_from_declared_single_column_pks_only(self):
        tables, _rels = _tables_and_rels(mixed_key_tables())
        measured = sp._measured_key_parents(tables)
        self.assertIn(("customers", "customer_id"), measured)
        self.assertIn(("groups", "group_id"), measured)
        # The FK's actual target column is NOT a key and must not be one here.
        self.assertNotIn(("groups", "group_code"), measured)

    def test_no_pk_parent_is_never_a_grain_anchor(self):
        tables, rels = _tables_and_rels(mixed_key_tables())
        marts, shapes = sp._build_marts(tables, rels, label="fixture")
        self.assertTrue(marts, "keyed chain must still fund marts")
        measured = sp._measured_key_parents(tables)
        for shape in shapes:
            for key in shape.parent_keys:
                self.assertIn(
                    (shape.parent, key),
                    measured,
                    f"mart {shape.mart} grains on ({shape.parent}, {key}), "
                    "which the record does not declare as a PK",
                )
            self.assertNotEqual(shape.parent, "groups")

    def test_unkeyed_only_record_is_refused_not_admitted(self):
        tables, rels = _tables_and_rels(unkeyed_only_tables())
        with self.assertRaises(sp.RelationalFilterError):
            sp._build_marts(tables, rels, label="fixture")

    def test_pick_join_surface_skips_unkeyed_second_link(self):
        tables, rels = _tables_and_rels(mixed_key_tables())
        primary, second = sp._pick_join_surface(
            tables, rels, key_parents=sp._measured_key_parents(tables)
        )
        # With the unkeyed groups link filtered OUT, orders no longer has
        # out-degree 2, so the busiest KEYED fact is order_items and the
        # grouping link is its declared-PK parent, orders(order_id).
        self.assertEqual(
            (primary.parent_table, tuple(primary.parent_columns)),
            ("orders", ("order_id",)),
        )
        measured = sp._measured_key_parents(tables)
        self.assertIn((primary.parent_table, primary.parent_columns[0]), measured)
        # No second keyed parent of order_items exists; the unkeyed groups
        # link must be declined as a dimension hop too, not fanned out.
        self.assertIsNone(second)


class ForeignKeyTypeCompatibilityTest(unittest.TestCase):
    def test_cross_family_foreign_key_is_removed_before_task_ir(self):
        tables, relationships = _tables_and_rels(incompatible_fk_tables())
        self.assertEqual({t.name for t in tables}, {"time_periods", "output_vflow_in"})
        self.assertEqual(relationships, ())

    def test_numeric_width_difference_remains_compatible(self):
        raw = incompatible_fk_tables()
        raw["output_vflow_in"]["COLUMNS"]["t_season"] = _col(
            "BigInt", nullable=False
        )
        _tables, relationships = _tables_and_rels(raw)
        self.assertEqual(len(relationships), 1)
        self.assertTrue(relationships[0].required)


# ---------------------------------------------------------------------------
# Dedupe prose: the DISTINCT basis must live in the column descriptions
# ---------------------------------------------------------------------------


class DedupeProseTest(unittest.TestCase):
    def _legacy(self, raw: dict):
        tables, rels = _tables_and_rels(raw)
        return sp._build_legacy_star(
            tables, rels, label="fixture", key_parents=sp._measured_key_parents(tables)
        )

    def test_pkless_fact_star_says_the_deduplicated_basis(self):
        mart, shape = self._legacy(pkless_fact_tables())
        self.assertTrue(shape.fact_dedupe)
        self.assertTrue(
            any(op.kind is MartOpKind.DEDUPE for op in mart.plan.ops),
            "plan must carry the dedupe op the prose describes",
        )
        by_name = {c.name: c for c in mart.columns}
        count = by_name["orders_count"]
        self.assertIn("DISTINCT", count.description)
        self.assertIn("count ONCE", count.description)
        total = by_name["total_amount"]
        self.assertIn("DISTINCT", total.description)

    def test_keyed_fact_star_keeps_the_raw_count_wording(self):
        raw = mixed_key_tables()
        mart, shape = self._legacy(raw)
        self.assertFalse(shape.fact_dedupe)
        self.assertFalse(
            any(op.kind is MartOpKind.DEDUPE for op in mart.plan.ops)
        )
        for col in mart.columns:
            self.assertNotIn("DISTINCT", col.description)

    # (A test asserting "every DEDUPE-bearing mart states the basis" was
    # removed: measured, `_build_marts` on this fixture emits one
    # mart with ZERO DEDUPE ops, so its loop body never executed — vacuous.
    # The invariant is really pinned by the shared plan library's wording
    # tests, which run on plans that DO dedupe.)


if __name__ == "__main__":
    unittest.main()

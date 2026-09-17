"""`adapters/evidence.py`: schema -> ChainEvidence -> plan-library shapes.

WHY THIS FILE EXISTS
The evidence extractor is where a pool's schema is turned into the facts a
library shape needs, and every rule in it exists because getting it wrong
produced a MEASURED corpus defect: a chain scored on features instead of on
shapes funded picked a bridge with a status column and no measure (killing the
roll-up and the argmax); a non-numeric countable key compiled to
`COALESCE(<varchar>, 0)` and failed the reference stage; a fan-out of two on
one parent was erased by the plan's own DEDUPE and left `wrong_grain` scoring
full reward. These tests pin all three.
"""

from __future__ import annotations

import unittest

from elt_taskgen.adapters import evidence as ev
from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.models import ColumnSpec, ColumnType, Relationship, TableSpec


def _col(name, type_=ColumnType.TEXT, nullable=True, enum=None):
    return ColumnSpec(
        name=name,
        type=type_,
        nullable=nullable,
        description=f"{name} column",
        enum_values=enum,
    )


def _chain_schema(*, bridge_measure=True, bridge_status=True, child=True):
    """parent <- bridge <- child, with the pieces switchable."""
    bridge_cols = [
        _col("order_id", ColumnType.INTEGER, nullable=False),
        _col("customer_id", ColumnType.INTEGER),
        _col("note"),
    ]
    if bridge_measure:
        bridge_cols.append(_col("amount", ColumnType.INTEGER))
    if bridge_status:
        bridge_cols.append(_col("status", enum=("open", "shut", "held")))
    if child:
        bridge_cols.append(_col("product_id", ColumnType.INTEGER))

    tables = [
        TableSpec(
            name="customers",
            description="dim",
            columns=(
                _col("customer_id", ColumnType.INTEGER, nullable=False),
                _col("customer_name"),
                _col("tier", enum=("gold", "silver", "bronze")),
            ),
            primary_key=("customer_id",),
        ),
        TableSpec(
            name="orders",
            description="fact",
            columns=tuple(bridge_cols),
            primary_key=("order_id",),
        ),
    ]
    rels = [
        Relationship(
            child_table="orders",
            child_columns=("customer_id",),
            parent_table="customers",
            parent_columns=("customer_id",),
            required=False,
        )
    ]
    if child:
        tables.append(
            TableSpec(
                name="products",
                description="dim2",
                columns=(
                    _col("product_id", ColumnType.INTEGER, nullable=False),
                    _col("product_name"),
                ),
                primary_key=("product_id",),
            )
        )
        rels.append(
            Relationship(
                child_table="orders",
                child_columns=("product_id",),
                parent_table="products",
                parent_columns=("product_id",),
                required=False,
            )
        )
    return tuple(tables), tuple(rels)


class SchemaShapeClusterTest(unittest.TestCase):
    def test_cluster_id_is_exact_and_order_independent(self) -> None:
        tables, rels = _chain_schema()
        expected = "sample__cluster_d58a22a4aa32"
        self.assertEqual(
            ev.schema_shape_cluster_id("sample", tables, rels), expected
        )
        self.assertEqual(
            ev.schema_shape_cluster_id(
                "sample", tuple(reversed(tables)), tuple(reversed(rels))
            ),
            expected,
        )


class ShapeRegistryTest(unittest.TestCase):
    def test_adapter_order_is_the_registry_policy_view(self) -> None:
        self.assertEqual(
            mp.registered_shape_builders(adapter_auto_select=True),
            ev.SHAPE_ORDER,
        )
        self.assertEqual(
            ("rollup", "cohorts", "by_period", "snapshot", "bands",
             "distribution", "top"),
            tuple(name for name, _builder in ev.SHAPE_ORDER),
        )

class ChainSelectionTest(unittest.TestCase):
    def test_a_full_chain_yields_the_bridge_and_the_second_hop(self) -> None:
        tables, rels = _chain_schema()
        e = ev.chain_evidence(tables, rels)
        self.assertIsNotNone(e)
        self.assertEqual((e.parent, e.bridge, e.child), ("customers", "orders", "products"))
        self.assertEqual(e.bridge_parent_fk, "customer_id")
        self.assertEqual(e.bridge_child_fk, "product_id")
        self.assertEqual(e.child_label, "product_name")

    def test_bridge_label_nullability_is_measured_from_the_selected_column(self) -> None:
        tables, rels = _chain_schema()
        evidence = ev.chain_evidence(tables, rels)
        self.assertEqual(evidence.bridge_label, "note")
        self.assertTrue(evidence.bridge_label_nullable)

        orders = next(table for table in tables if table.name == "orders")
        strict_orders = orders.model_copy(
            update={
                "columns": tuple(
                    column.model_copy(update={"nullable": False})
                    if column.name == "note"
                    else column
                    for column in orders.columns
                )
            }
        )
        strict_tables = tuple(
            strict_orders if table.name == "orders" else table for table in tables
        )
        strict = ev.chain_evidence(strict_tables, rels)
        self.assertEqual(strict.bridge_label, "note")
        self.assertFalse(strict.bridge_label_nullable)

    def test_distribution_grain_tracks_measured_amount_nullability(self) -> None:
        tables, rels = _chain_schema()
        nullable = ev.chain_evidence(tables, rels)
        self.assertIn(
            "absent state includes missing amount values",
            ev._grain_prose("distribution", nullable),
        )

        orders = next(table for table in tables if table.name == "orders")
        strict_orders = orders.model_copy(
            update={
                "columns": tuple(
                    column.model_copy(update={"nullable": False})
                    if column.name == "amount"
                    else column
                    for column in orders.columns
                )
            }
        )
        strict_tables = tuple(
            strict_orders if table.name == "orders" else table for table in tables
        )
        strict = ev.chain_evidence(strict_tables, rels)
        strict_grain = ev._grain_prose("distribution", strict)
        self.assertIn("amount is required", strict_grain)
        self.assertIn("no linked orders row belongs to the absent state", strict_grain)
        self.assertNotIn("missing amount values", strict_grain)

    def test_declared_domain_is_split_never_invented(self) -> None:
        tables, rels = _chain_schema()
        e = ev.chain_evidence(tables, rels)
        self.assertEqual(e.bridge_status, "status")
        self.assertEqual(
            set(e.bridge_status_pass) | set(e.bridge_status_fail),
            {"open", "shut", "held"},
        )
        self.assertFalse(set(e.bridge_status_pass) & set(e.bridge_status_fail))

    def test_ladder_reserves_one_declared_value_as_the_out_of_domain_witness(
        self,
    ) -> None:
        tables, rels = _chain_schema()
        e = ev.chain_evidence(tables, rels)
        self.assertEqual(e.parent_domain_column, "tier")
        self.assertIn(e.out_of_domain, e.domain)
        named = [v for v in e.domain if v != e.out_of_domain]
        self.assertGreaterEqual(len(named), 2)

    def test_selection_is_deterministic(self) -> None:
        tables, rels = _chain_schema()
        self.assertEqual(ev.chain_evidence(tables, rels), ev.chain_evidence(tables, rels))

    def test_a_chain_is_scored_by_how_many_SHAPES_it_funds(self) -> None:
        """Not by a hand-ordered feature list — the measured failure mode."""
        full, _ = ev.chain_candidates(*_chain_schema()), None
        thin = ev.chain_candidates(*_chain_schema(bridge_measure=False))
        self.assertGreater(
            len(ev.selectable_shapes(full[0])),
            len(ev.selectable_shapes(thin[0])) if thin else 0,
        )


class DisjointRolesTest(unittest.TestCase):
    """One source column never plays two roles: the released wikidbs mart
    shipped `given_name AS parent_key, given_name AS parent_name` and
    `SUM(pro_cycling_stats_cyclist_id)` as its measure; the released dlt mart
    keyed the argmax on the parent link."""

    def _wikidbs_like(self, *, second_text: bool = True, second_numeric: bool = True):
        """PK-less TEXT-keyed parent whose FIRST TEXT column is the referenced
        key; PK-less bridge with one NOT NULL int (the row key) and, optionally,
        a second numeric column."""
        parent_cols = [_col("team_name", ColumnType.TEXT, nullable=False)]
        if second_text:
            parent_cols.append(_col("team_label", ColumnType.TEXT, nullable=False))
        bridge_cols = [
            _col("player_label"),
            _col("player_id", ColumnType.INTEGER, nullable=False),
            _col("team_name", ColumnType.TEXT, nullable=False),
        ]
        if second_numeric:
            bridge_cols.append(_col("score", ColumnType.INTEGER, nullable=False))
        tables = (
            TableSpec(name="teams", description="parent", columns=tuple(parent_cols),
                      business_key=("team_name",)),
            TableSpec(name="players", description="bridge", columns=tuple(bridge_cols)),
        )
        rels = (
            Relationship(
                child_table="players", child_columns=("team_name",),
                parent_table="teams", parent_columns=("team_name",), required=False,
            ),
        )
        return tables, rels

    def test_parent_attr_is_never_the_referenced_parent_key(self) -> None:
        tables, rels = self._wikidbs_like()
        e = ev.chain_evidence(tables, rels)
        self.assertIsNotNone(e)
        self.assertEqual(e.parent_key, "team_name")
        self.assertEqual(e.parent_attr, "team_label")
        # No other TEXT column: no parent attribute, no chain (fail closed).
        tables, rels = self._wikidbs_like(second_text=False)
        self.assertEqual(ev.chain_candidates(tables, rels), ())

    def test_bridge_amount_is_never_the_bridge_key(self) -> None:
        tables, rels = self._wikidbs_like()
        e = ev.chain_evidence(tables, rels)
        self.assertEqual(e.bridge_key, "player_id")
        self.assertEqual(e.bridge_amount, "score")
        self.assertNotEqual(e.bridge_amount, e.bridge_key)
        # Only ONE numeric column: it is the key, so there is no measure and
        # the measure-hungry shapes are unfundable.
        tables, rels = self._wikidbs_like(second_numeric=False)
        e = ev.chain_evidence(tables, rels)
        self.assertEqual(e.bridge_key, "player_id")
        self.assertEqual(e.bridge_amount, "")
        names = {n for n, _ in ev.selectable_shapes(e)}
        self.assertNotIn("top", names)
        self.assertNotIn("rollup", names)

    def test_exclude_measures_removes_identifier_columns(self) -> None:
        """A measured identifier column (all-distinct numeric) is preferred as
        the row KEY and barred from the measure — never SUM(<id>)."""
        tables, rels = self._wikidbs_like()
        # Reorder so `score` comes first: without the exclusion it would be
        # picked as the countable key and player_id as the amount.
        players = tables[1]
        cols = list(players.columns)
        cols.insert(1, cols.pop(3))  # score before player_id
        tables = (tables[0], players.model_copy(update={"columns": tuple(cols)}))
        plain = ev.chain_evidence(tables, rels)
        self.assertEqual((plain.bridge_key, plain.bridge_amount), ("score", "player_id"))
        self.assertFalse(plain.bridge_key_is_unique)
        identifiers = {"players": frozenset({"player_id"})}
        e = ev.chain_candidates(tables, rels, exclude_measures=identifiers)[0]
        self.assertEqual((e.bridge_key, e.bridge_amount), ("player_id", "score"))
        self.assertTrue(e.bridge_key_is_unique)
        # Both numerics identifiers -> no measure at all.
        e = ev.chain_candidates(
            tables, rels, exclude_measures={"players": frozenset({"player_id", "score"})}
        )[0]
        self.assertEqual(e.bridge_amount, "")

    def test_bridge_key_on_the_parent_link_is_flagged_and_distribution_declines(self) -> None:
        """dlt workable: the only NOT NULL numeric column of the PK-less bridge
        is the parent link; the chain survives, flagged."""
        tables = (
            TableSpec(
                name="jobs", description="parent",
                columns=(
                    _col("job_id", ColumnType.BIGINT, nullable=False),
                    _col("title", ColumnType.TEXT, nullable=False),
                ),
                primary_key=("job_id",),
            ),
            TableSpec(
                name="jobs_stages", description="bridge",
                columns=(
                    _col("_jobs_id", ColumnType.BIGINT, nullable=False),
                    _col("stage_position", ColumnType.INTEGER, nullable=True),
                    _col("stage_slug"),
                ),
            ),
        )
        rels = (
            Relationship(
                child_table="jobs_stages", child_columns=("_jobs_id",),
                parent_table="jobs", parent_columns=("job_id",), required=True,
            ),
        )
        e = ev.chain_evidence(tables, rels)
        self.assertIsNotNone(e)
        self.assertEqual(e.bridge_key, "_jobs_id")
        self.assertTrue(e.bridge_key_is_parent_fk)
        self.assertEqual(e.bridge_amount, "stage_position")
        self.assertFalse(e.owner_key_nullable)
        names = {name for name, _builder in ev.selectable_shapes(e)}
        # A parent FK can still count rows for argmax, which drops all row-id
        # claims. It cannot fund the distribution's distinct-measure witness:
        # that witness needs equal parent-FK and different link-key values.
        self.assertNotIn("distribution", names)
        self.assertIn("top", names)


class KeyParentsDefaultTest(unittest.TestCase):
    """`key_parents=None` is the DECLARED keys, never a disarmed gate."""

    def _schema(self, *, target_is_business_key: bool = False):
        parent = TableSpec(
            name="objects", description="parent with a PK",
            columns=(
                _col("object_id", ColumnType.INTEGER, nullable=False),
                _col("object_name", ColumnType.TEXT, nullable=False),
                _col("object_label", ColumnType.TEXT, nullable=False),
            ),
            primary_key=("object_id",),
            business_key=("object_name",) if target_is_business_key else (),
        )
        bridge = TableSpec(
            name="commands", description="bridge",
            columns=(
                _col("command_id", ColumnType.INTEGER, nullable=False),
                _col("object", ColumnType.TEXT, nullable=True),
                _col("duration", ColumnType.INTEGER, nullable=True),
                _col("note"),
            ),
            primary_key=("command_id",),
        )
        rels = (
            Relationship(
                child_table="commands", child_columns=("object",),
                parent_table="objects", parent_columns=("object_name",),
                required=False,
            ),
        )
        return (parent, bridge), rels

    def test_key_parents_default_is_declared_keys_not_disarmed(self) -> None:
        # The FK targets a NON-key column of a table that has a PK: refused
        # (SynSQL froze link_count=28056 against 79 real children this way).
        tables, rels = self._schema()
        self.assertEqual(
            ev.declared_key_parents(tables),
            frozenset({("objects", "object_id"), ("commands", "command_id")}),
        )
        self.assertEqual(ev.chain_candidates(tables, rels), ())
        self.assertIsNone(ev.chain_evidence(tables, rels))
        # Declaring the target a business key restores the chain: the
        # generator mints business keys unique, so the grain is honest.
        tables, rels = self._schema(target_is_business_key=True)
        self.assertIn(("objects", "object_name"), ev.declared_key_parents(tables))
        self.assertTrue(ev.chain_candidates(tables, rels))
        # An explicit measured set still wins over the declaration.
        self.assertEqual(
            ev.chain_candidates(tables, rels, key_parents=frozenset()), ()
        )

    def test_hop2_onto_non_key_child_column_is_refused(self) -> None:
        """historical_economic_data_analysis-like: bridge FK -> child.non_pk_col.
        The hop-1 chain survives; the hop-2 join is dropped, not planned."""
        tables, rels = _chain_schema()
        # Re-target the products edge at a non-key column of products.
        products = next(t for t in tables if t.name == "products")
        products = products.model_copy(
            update={
                "columns": products.columns
                + (_col("sku", ColumnType.INTEGER, nullable=False),)
            }
        )
        tables = tuple(products if t.name == "products" else t for t in tables)
        rels = tuple(
            r.model_copy(update={"parent_columns": ("sku",)})
            if r.parent_table == "products"
            else r
            for r in rels
        )
        e = ev.chain_evidence(tables, rels)
        self.assertIsNotNone(e)
        self.assertEqual(e.child, "")
        self.assertEqual(e.bridge_child_fk, "")
        self.assertEqual(e.child_key, "")
        # And with the child key declared, the hop is back.
        keyed = tuple(
            t.model_copy(update={"primary_key": ("sku",)}) if t.name == "products" else t
            for t in tables
        )
        # `sku` must not also be the label; give products a second TEXT column.
        e = ev.chain_evidence(keyed, rels)
        self.assertEqual(e.child, "products")
        self.assertEqual(e.child_key, "sku")
        self.assertEqual(e.child_label, "product_name")


class NullabilityFactsTest(unittest.TestCase):
    """The evidence EXPOSES nullability; the shape decides (temporal_grid
    declines nullable grain inputs while snapshot can order them NULLS LAST)."""

    def test_chain_candidates_prefers_not_null_timestamps_and_flags_nullable_owner_key(
        self,
    ) -> None:
        tables, rels = _chain_schema()
        orders = next(t for t in tables if t.name == "orders")
        with_ts = orders.model_copy(
            update={
                "columns": orders.columns
                + (
                    _col("shipped_on", ColumnType.DATE, nullable=True),
                    _col("created_at", ColumnType.TIMESTAMP, nullable=False),
                )
            }
        )
        tables2 = tuple(with_ts if t.name == "orders" else t for t in tables)
        e = ev.chain_evidence(tables2, rels)
        self.assertEqual(e.bridge_timestamp, "created_at")
        self.assertFalse(e.bridge_timestamp_nullable)
        # Only a nullable temporal column: retain the evidence for snapshot,
        # while temporal_grid still declines the nullable period grain.
        only_nullable = orders.model_copy(
            update={
                "columns": orders.columns
                + (_col("shipped_on", ColumnType.DATE, nullable=True),)
            }
        )
        tables3 = tuple(only_nullable if t.name == "orders" else t for t in tables)
        e = ev.chain_evidence(tables3, rels)
        self.assertEqual(e.bridge_timestamp, "shipped_on")
        self.assertTrue(e.bridge_timestamp_nullable)
        self.assertNotIn("by_period", {n for n, _ in ev.selectable_shapes(e)})
        self.assertIn("snapshot", {n for n, _ in ev.selectable_shapes(e)})
        # nullable fk + required=False -> owner_key_nullable (the fixture's
        # customer_id is nullable and the link optional).
        self.assertTrue(e.owner_key_nullable)
        # NOT NULL fk (or a required link) -> False.
        strict = tuple(
            t.model_copy(
                update={
                    "columns": tuple(
                        c.model_copy(update={"nullable": False}) if c.name == "customer_id" else c
                        for c in t.columns
                    )
                }
            )
            if t.name == "orders"
            else t
            for t in tables
        )
        self.assertFalse(ev.chain_evidence(strict, rels).owner_key_nullable)
        required = tuple(
            r.model_copy(update={"required": True}) if r.parent_table == "customers" else r
            for r in rels
        )
        self.assertFalse(ev.chain_evidence(tables, required).owner_key_nullable)
        # amount nullability is exposed too (fixture amount is nullable).
        self.assertTrue(e.bridge_amount_nullable)
        # `e` retains the nullable timestamp fact for shapes that do not grain
        # on it; absence, by contrast, is still reported as None.
        self.assertEqual(e.bridge_timestamp, "shipped_on")
        self.assertTrue(e.bridge_timestamp_nullable)
        # ... and a chain without any numeric amount reports None too.
        e4 = ev.chain_evidence(*_chain_schema(bridge_measure=False))
        self.assertEqual(e4.bridge_amount, "")
        self.assertIsNone(e4.bridge_amount_nullable)


class ShapeSelectionTest(unittest.TestCase):
    def test_the_full_chain_funds_the_roll_up_and_the_argmax(self) -> None:
        e = ev.chain_evidence(*_chain_schema())
        names = {n for n, _ in ev.selectable_shapes(e)}
        self.assertIn("rollup", names)
        self.assertIn("distribution", names)
        self.assertIn("top", names)

    def test_no_measure_no_argmax_and_no_rollup(self) -> None:
        """Fail closed: an invented amount has a scale nobody declared."""
        e = ev.chain_evidence(*_chain_schema(bridge_measure=False))
        names = {n for n, _ in ev.selectable_shapes(e)}
        self.assertNotIn("top", names)
        self.assertNotIn("rollup", names)
        self.assertNotIn("distribution", names)

    def test_measure_distribution_needs_no_status_domain_or_timestamp(self) -> None:
        e = ev.chain_evidence(*_chain_schema(bridge_status=False))
        names = {n for n, _ in ev.selectable_shapes(e)}
        self.assertIn("distribution", names)
        self.assertIn("top", names)
        self.assertNotIn("cohorts", names)
        self.assertNotIn("snapshot", names)

    def test_status_cohorts_require_a_declared_two_sided_domain(self) -> None:
        e = ev.chain_evidence(*_chain_schema())
        self.assertIn("cohorts", {n for n, _ in ev.selectable_shapes(e)})
        no_status = ev.chain_evidence(*_chain_schema(bridge_status=False))
        self.assertNotIn(
            "cohorts", {n for n, _ in ev.selectable_shapes(no_status)}
        )

    def test_latest_snapshot_requires_a_timestamp_and_unique_row_key(self) -> None:
        tables, rels = _chain_schema(bridge_status=False)
        orders = next(t for t in tables if t.name == "orders")
        timestamped = orders.model_copy(
            update={
                "columns": orders.columns
                + (_col("created_at", ColumnType.TIMESTAMP, nullable=True),)
            }
        )
        tables = tuple(timestamped if t.name == "orders" else t for t in tables)
        e = ev.chain_evidence(tables, rels)
        self.assertTrue(e.bridge_key_is_unique)
        self.assertEqual(e.bridge_status, "")
        self.assertTrue(e.bridge_timestamp_nullable)
        self.assertIn("snapshot", {n for n, _ in ev.selectable_shapes(e)})
        self.assertNotIn("by_period", {n for n, _ in ev.selectable_shapes(e)})

        not_unique = e.__class__(
            **{**e.__dict__, "bridge_key_is_unique": False}
        )
        self.assertNotIn(
            "snapshot", {n for n, _ in ev.selectable_shapes(not_unique)}
        )

    def test_build_marts_puts_each_shape_on_the_first_chain_that_funds_it(self) -> None:
        tables, rels = _chain_schema()
        marts, shapes, names = ev.build_marts(
            ev.chain_candidates(tables, rels),
            prefix=lambda e, suffix: f"{e.parent}_{suffix}",
            max_marts=2,
        )
        self.assertEqual(len(marts), 2)
        self.assertEqual(len(shapes), 2)
        self.assertEqual(len(set(names)), 2)
        for mart in marts:
            self.assertTrue(set(mart.key_columns) <= {c.name for c in mart.columns})

    def test_two_marts_clear_the_anchor_minima(self) -> None:
        tables, rels = _chain_schema()
        marts, _shapes, _names = ev.build_marts(
            ev.chain_candidates(tables, rels),
            prefix=lambda e, suffix: f"{e.parent}_{suffix}",
            max_marts=2,
        )
        target = sum(len(m.columns) for m in marts)
        computed = sum(1 for m in marts for c in m.columns if c.computed)
        self.assertGreaterEqual(target, 6)   # anchor minimum
        self.assertGreaterEqual(computed, 4)  # anchor minimum


class CountableKeyTest(unittest.TestCase):
    def test_a_declared_numeric_primary_key_wins(self) -> None:
        tables, _ = _chain_schema()
        orders = next(t for t in tables if t.name == "orders")
        self.assertEqual(ev._countable_key(orders), "order_id")

    def test_a_pk_less_table_falls_back_to_a_not_null_numeric_column(self) -> None:
        table = TableSpec(
            name="t",
            description="no pk",
            columns=(
                _col("label", ColumnType.TEXT, nullable=False),
                _col("n", ColumnType.INTEGER, nullable=False),
            ),
        )
        # NOT the VARCHAR: `argmax_profile` COALESCEs this key to 0, and
        # COALESCE(VARCHAR, 0) is a DuckDB bind error.
        self.assertEqual(ev._countable_key(table), "n")

    def test_a_nullable_column_is_never_a_countable_key(self) -> None:
        table = TableSpec(
            name="t",
            description="no pk",
            columns=(_col("n", ColumnType.INTEGER, nullable=True),),
        )
        self.assertEqual(ev._countable_key(table), "")

    def test_a_declared_text_pk_beats_a_not_null_numeric_column(self) -> None:
        """The key's type travels with it (`bridge_key_type`), so a TEXT PK is
        a better row key than an arbitrary NOT NULL measure column."""
        table = TableSpec(
            name="t",
            description="text pk",
            columns=(
                _col("code", ColumnType.TEXT, nullable=False),
                _col("weight", ColumnType.INTEGER, nullable=False),
            ),
            primary_key=("code",),
        )
        self.assertEqual(ev._countable_key(table), "code")

    def test_a_parent_link_column_is_the_last_resort_key(self) -> None:
        """dlt-like PK-less transformer: `_jobs_id` (NOT NULL FK) is the only
        NOT NULL numeric column. The FK fallback is KEPT (it is still one per
        bridge row) but any NOT NULL numeric NON-FK column beats it, and the
        chain evidence flags the fallback so the argmax shape does not claim
        the key names WHICH row won."""
        rels = (
            Relationship(
                child_table="jobs_stages",
                child_columns=("_jobs_id",),
                parent_table="jobs",
                parent_columns=("job_id",),
                required=True,
            ),
        )
        fk_only = TableSpec(
            name="jobs_stages",
            description="no pk",
            columns=(
                _col("_jobs_id", ColumnType.BIGINT, nullable=False),
                _col("stage_position", ColumnType.INTEGER, nullable=True),
                _col("stage_slug"),
            ),
        )
        self.assertEqual(ev._countable_key(fk_only, rels), "_jobs_id")
        with_own = fk_only.model_copy(
            update={
                "columns": fk_only.columns
                + (_col("stage_id", ColumnType.INTEGER, nullable=False),)
            }
        )
        self.assertEqual(ev._countable_key(with_own, rels), "stage_id")
        # `prefer` (measured identifier columns) wins among non-FK candidates.
        two = with_own.model_copy(
            update={
                "columns": with_own.columns
                + (_col("row_no", ColumnType.INTEGER, nullable=False),)
            }
        )
        self.assertEqual(
            ev._countable_key(two, rels, prefer=frozenset({"row_no"})), "row_no"
        )
        self.assertEqual(
            ev._countable_key(two, rels, exclude=frozenset({"stage_id"})), "row_no"
        )


class RealRowEvidenceTest(unittest.TestCase):
    def _rows(self, fanout: int, childless: int):
        parents = [{"customer_id": i, "customer_name": f"c{i}", "tier": "gold"}
                   for i in range(1, 3 + childless)]
        orders = []
        oid = 0
        for _ in range(fanout):
            oid += 1
            orders.append(
                {
                    "order_id": oid,
                    "customer_id": 1,
                    "note": "n",
                    "amount": oid,
                    "status": "open",
                    "product_id": 1,
                }
            )
        return {"customers": parents, "orders": orders, "products": [
            {"product_id": 1, "product_name": "p"}
        ]}

    def test_link_statistics_report_fanout_and_childless_parents(self) -> None:
        tables, rels = _chain_schema()
        stats = ev.link_statistics(rels, self._rows(fanout=4, childless=2))
        self.assertEqual(
            stats[("orders", "customer_id", "customers", "customer_id")], (4, 3)
        )

    def test_a_thin_fanout_is_refused_on_a_real_row_pool(self) -> None:
        """A fan-out of two on one parent does not survive the DEDUPE step."""
        tables, rels = _chain_schema()
        rows = self._rows(fanout=2, childless=1)
        self.assertEqual(
            ev.chain_candidates(tables, rels, links=ev.link_statistics(rels, rows)),
            (),
        )

    def test_a_real_fanout_is_accepted(self) -> None:
        tables, rels = _chain_schema()
        rows = self._rows(fanout=5, childless=1)
        self.assertTrue(
            ev.chain_candidates(tables, rels, links=ev.link_statistics(rels, rows))
        )

    def test_a_chain_on_a_non_key_parent_is_refused(self) -> None:
        """The star grains on the hop-1 parent key and never DISTINCTs it.

        `build_star` projects the grain with no `DISTINCT`, so a parent column
        that repeats fans the join out, `GROUP BY` hides the duplication in the
        OUTPUT (grain rows stay unique) and every measure ships inflated.
        `key_parents=None` means the DECLARED keys (never disarmed); a
        real-row pool passes what it MEASURED, and an explicit empty set
        refuses everything.
        """
        tables, rels = _chain_schema()
        rows = self._rows(fanout=5, childless=1)
        links = ev.link_statistics(rels, rows)
        self.assertTrue(ev.chain_candidates(tables, rels, links=links))
        self.assertEqual(
            ev.chain_candidates(
                tables, rels, links=links, key_parents=frozenset()
            ),
            (),
        )
        # And the gate is about THIS column, not about the table's name.
        self.assertTrue(
            ev.chain_candidates(
                tables,
                rels,
                links=links,
                key_parents=frozenset(
                    (r.parent_table, r.parent_columns[0]) for r in rels
                ),
            )
        )

    def test_observed_domains_find_categoricals_a_schema_never_declares(self) -> None:
        table = TableSpec(
            name="t",
            description="real rows",
            columns=(_col("status"), _col("free_text")),
        )
        rows = {
            "t": [
                {"status": "a" if i % 2 else "b", "free_text": f"unique-{i}"}
                for i in range(10)
            ]
        }
        domains = ev.observed_domains((table,), rows)
        self.assertEqual(domains[("t", "status")], ("a", "b"))
        self.assertNotIn(("t", "free_text"), domains)


if __name__ == "__main__":
    unittest.main()

"""Focused contract tests for generation-policy-v6 adversarial source rows."""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import duckdb

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.generation import populations, source_data
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    PopulationName,
)
from elt_taskgen.reference.solution import load_sources_duckdb


P = PopulationName


def _wide_task():
    task = demo_task()
    tables = []
    for table in task.tables:
        if table.name == "order_items":
            table = table.model_copy(
                update={
                    "columns": table.columns
                    + (
                        ColumnSpec(name="large_count", type=ColumnType.BIGINT),
                        ColumnSpec(name="ratio", type=ColumnType.FLOAT),
                        ColumnSpec(name="precise_amount", type=ColumnType.DECIMAL),
                        ColumnSpec(name="is_active", type=ColumnType.BOOLEAN),
                        ColumnSpec(name="label", type=ColumnType.TEXT),
                        ColumnSpec(name="event_date", type=ColumnType.DATE),
                        ColumnSpec(name="event_at", type=ColumnType.TIMESTAMP),
                        ColumnSpec(name="payload", type=ColumnType.JSON),
                    )
                }
            )
        tables.append(table)

    # Keep the renderer round-trip test small while retaining PRIMARY's hidden,
    # adversarial policy.  Scales below 50 are intentionally realized exactly.
    specs = tuple(
        population.model_copy(
            update={"scale": {"customers": 12, "orders": 12, "order_items": 24}}
        )
        if population.name in (P.PRIMARY, P.RESAMPLED)
        else population
        for population in task.populations
    )
    # order_items carries the oversized BIGINT family, which only the Postgres
    # transport delivers exactly; every other backend rounds it at 2**53.
    backends = tuple(
        assignment.model_copy(update={"backend": Backend.POSTGRES})
        if assignment.table == "order_items"
        else assignment
        for assignment in task.backends
    )
    return task.model_copy(
        update={"tables": tuple(tables), "populations": specs, "backends": backends}
    )


def _with_backend(task, backend: Backend):
    options = {"page_size": "7"} if backend is Backend.REST else {}
    return task.model_copy(
        update={
            "backends": tuple(
                BackendAssignment(table=table.name, backend=backend, options=options)
                for table in task.tables
            )
        }
    )


class TestAdversarialSourceValues(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.task = _wide_task()
        cls.primary = source_data.generate_rows(cls.task, P.PRIMARY)
        cls.resampled = source_data.generate_rows(cls.task, P.RESAMPLED)
        cls.stress = source_data.generate_rows(cls.task, P.STRESS)

    def test_policy_version_is_reexported(self) -> None:
        self.assertEqual(source_data.GENERATION_POLICY_VERSION, 9)
        self.assertEqual(populations.GENERATION_POLICY_VERSION, 9)

    def test_oversized_bigints_stay_off_the_rounding_transports(self) -> None:
        # Measured on the type probe: REST, file, S3 and Mongo all deliver
        # 2**53 + 1 as 9007199254740992, so those tables keep small ids.
        for backend in (Backend.FILES, Backend.REST, Backend.S3, Backend.MONGODB):
            with self.subTest(backend=backend.value):
                task = _wide_task()
                backends = tuple(
                    a.model_copy(update={"backend": backend, "options": ({"page_size": "7"} if backend is Backend.REST else {})})
                    if a.table == "order_items" else a
                    for a in task.backends
                )
                rows = source_data.generate_rows(
                    task.model_copy(update={"backends": backends}), P.PRIMARY
                )["order_items"]
                self.assertTrue(rows)
                self.assertTrue(all(row["large_count"] <= 2**53 for row in rows))

    def test_primary_contains_every_portable_adversarial_family(self) -> None:
        rows = self.primary["order_items"]

        bigints = {row["large_count"] for row in rows}
        self.assertTrue(any(value > 2**53 for value in bigints))
        self.assertTrue(all(value <= 2**63 - 1 for value in bigints))

        decimals = {row["precise_amount"] for row in rows}
        for value in source_data._ADVERSARIAL_DECIMAL_VALUES[:2]:
            self.assertIn(value, decimals)

        floats = {row["ratio"] for row in rows}
        self.assertTrue(floats)
        self.assertTrue(all(isinstance(value, float) for value in floats))

        # Exact binary fractions, so SUM over the column returns the same total
        # from the reference engine and from the warehouse.
        for column, grid in ((decimals, source_data._DECIMAL_GRID), (floats, source_data._FLOAT_GRID)):
            self.assertTrue(
                all(
                    (value * grid).is_integer() and abs(value) < source_data._FRACTION_CEILING
                    for value in column
                )
            )
        # A decimal keeps to the nine fractional digits DECIMAL(38,9) admits.
        self.assertTrue(
            all(len(repr(value).partition(".")[2]) <= 9 for value in decimals)
        )
        self.assertTrue(
            any(len(repr(value).partition(".")[2]) == 9 for value in decimals)
        )
        # A float fits the nine fractional digits Redshift's numeric storage
        # keeps (Databricks keeps ten), so every destination carries it back
        # unchanged.
        self.assertTrue(
            all(len(repr(value).partition(".")[2]) <= 9 for value in floats)
        )

        booleans = {row["is_active"] for row in rows}
        self.assertEqual(booleans, {False, True})

        texts = {row["label"] for row in rows}
        self.assertIn("MiXeD_Case", texts)
        self.assertIn("nUlL", texts)
        self.assertIn("Null", texts)
        self.assertIn("München 東京 Δ", texts)
        self.assertIn("Cafe\u0301", texts)
        self.assertNotIn("", texts)
        # The Snowflake destination trims a loaded string, so a padded value
        # could never arrive intact.
        self.assertTrue(
            all(v == v.strip() for v in source_data._ADVERSARIAL_TEXT_VALUES)
        )

        dates = {row["event_date"] for row in rows}
        self.assertIn("2024-02-29", dates)
        self.assertIn("2024-03-10", dates)
        self.assertIn("2024-11-03", dates)
        self.assertIn("2024-12-31", dates)

        timestamps = {row["event_at"] for row in rows}
        self.assertIn("2024-02-29 23:59:59.999999", timestamps)
        self.assertIn("2024-03-10 01:59:59", timestamps)
        self.assertIn("2024-03-10 03:00:00", timestamps)
        self.assertIn("2024-11-03 01:30:00", timestamps)

        payloads = [json.loads(row["payload"]) for row in rows]
        self.assertTrue(
            any(
                isinstance(value.get("meta"), dict)
                and "label" in value["meta"]
                and value["meta"]["label"] is None
                for value in payloads
            )
        )
        self.assertTrue(
            any(
                isinstance(value.get("meta"), dict)
                and "label" not in value["meta"]
                for value in payloads
            )
        )

    def test_resampled_rotates_fixed_cases_without_losing_coverage(self) -> None:
        primary = self.primary["order_items"]
        resampled = self.resampled["order_items"]
        for column in (
            "large_count",
            "ratio",
            "precise_amount",
            "is_active",
            "label",
            "event_date",
            "event_at",
            "payload",
        ):
            left = [row[column] for row in primary]
            right = [row[column] for row in resampled]
            self.assertNotEqual(left, right, column)
        self.assertEqual(
            {row["label"] for row in primary} & set(source_data._ADVERSARIAL_TEXT_VALUES),
            set(source_data._ADVERSARIAL_TEXT_VALUES),
        )
        self.assertEqual(
            {row["label"] for row in self.resampled["order_items"]}
            & set(source_data._ADVERSARIAL_TEXT_VALUES),
            set(source_data._ADVERSARIAL_TEXT_VALUES),
        )

    def test_stress_has_constructed_tie_runs_and_multi_hot_skew(self) -> None:
        item_rows = self.stress["order_items"]
        non_special = [
            row["large_count"]
            for row in item_rows
            if row["large_count"] <= 2**53
        ]
        frequencies = Counter(non_special)
        self.assertGreaterEqual(max(frequencies.values()), 4)
        self.assertLessEqual(set(non_special), {1, 2, 3})

        orders = self.stress["orders"]
        counts = Counter(
            row["customer_id"]
            for row in orders
            if row["customer_id"] is not None
        )
        shares = sorted(counts.values(), reverse=True)
        self.assertGreater(shares[0] / len(orders), 0.45)
        self.assertGreater(shares[1] / len(orders), 0.08)

    def test_all_five_renderers_round_trip_the_values(self) -> None:
        expected = {table: len(rows) for table, rows in self.primary.items()}
        for backend in Backend:
            with self.subTest(backend=backend.value), tempfile.TemporaryDirectory() as tmp:
                task = _with_backend(self.task, backend)
                rendered = Path(tmp) / "rendered"
                source_data.render_population(task, P.PRIMARY, self.primary, rendered)
                con = duckdb.connect(":memory:")
                try:
                    loaded = load_sources_duckdb(task, rendered, con)
                    self.assertEqual(loaded.counts, expected)
                    maximum = con.execute(
                        'SELECT MAX("large_count") FROM "order_items"'
                    ).fetchone()[0]
                    labels = {
                        row[0]
                        for row in con.execute(
                            'SELECT "label" FROM "order_items"'
                        ).fetchall()
                    }
                    payloads = {
                        row[0]
                        for row in con.execute(
                            'SELECT "payload" FROM "order_items"'
                        ).fetchall()
                    }
                    booleans = {
                        row[0]
                        for row in con.execute(
                            'SELECT "is_active" FROM "order_items"'
                        ).fetchall()
                    }
                finally:
                    con.close()
                self.assertGreater(maximum, 2**53)
                self.assertIn("München 東京 Δ", labels)
                self.assertEqual(booleans, {False, True})
                self.assertTrue(any('"label":null' in value for value in payloads))
                self.assertTrue(
                    any(
                        '"tags"' in value and '"label"' not in value
                        for value in payloads
                    )
                )


class TestBigintIdentityPortability(unittest.TestCase):
    def _customer_types(self, parent_type: ColumnType, child_type: ColumnType):
        task = demo_task()
        tables = []
        for table in task.tables:
            if table.name == "customers":
                columns = tuple(
                    column.model_copy(update={"type": parent_type})
                    if column.name == "customer_id"
                    else column
                    for column in table.columns
                )
                table = table.model_copy(update={"columns": columns})
            elif table.name == "orders":
                columns = tuple(
                    column.model_copy(update={"type": child_type})
                    if column.name == "customer_id"
                    else column
                    for column in table.columns
                )
                table = table.model_copy(update={"columns": columns})
            tables.append(table)
        return task.model_copy(update={"tables": tuple(tables)})

    def _on_backend(self, task, table: str, backend: Backend):
        """Move one table onto another backend, leaving the rest alone."""
        backends = tuple(
            assignment.model_copy(update={"backend": backend})
            if assignment.table == table
            else assignment
            for assignment in task.backends
        )
        return task.model_copy(update={"backends": backends})

    def test_bigint_to_bigint_identity_crosses_json_exactness_boundary(self) -> None:
        task = self._customer_types(ColumnType.BIGINT, ColumnType.BIGINT)
        # This case is about the link, so put the child on an exact backend.
        task = self._on_backend(task, "orders", Backend.POSTGRES)
        rows = source_data.generate_rows(task, P.PRIMARY)
        parent_ids = {row["customer_id"] for row in rows["customers"]}
        child_ids = {
            row["customer_id"]
            for row in rows["orders"]
            if row["customer_id"] is not None
        }
        self.assertGreater(min(parent_ids), 2**53)
        self.assertLessEqual(child_ids, parent_ids)

    def test_a_mongodb_child_keeps_the_parent_in_its_safe_range(self) -> None:
        task = self._customer_types(ColumnType.BIGINT, ColumnType.BIGINT)
        task = self._on_backend(task, "orders", Backend.MONGODB)
        rows = source_data.generate_rows(task, P.PRIMARY)
        parent_ids = {row["customer_id"] for row in rows["customers"]}
        self.assertLessEqual(max(parent_ids), 2**53)

    def test_a_mongodb_table_carries_no_bigint_measure_above_the_boundary(self) -> None:
        task = self._customer_types(ColumnType.BIGINT, ColumnType.BIGINT)
        task = self._on_backend(task, "orders", Backend.MONGODB)
        rows = source_data.generate_rows(task, P.PRIMARY)
        bigint_columns = [
            column.name
            for column in task.table("orders").columns
            if column.type is ColumnType.BIGINT
        ]
        for column in bigint_columns:
            values = [
                row[column] for row in rows["orders"]
                if isinstance(row.get(column), int)
            ]
            if values:
                self.assertLessEqual(max(values), 2**53, column)

    def test_an_exact_backend_still_carries_the_boundary_measure(self) -> None:
        task = self._customer_types(ColumnType.BIGINT, ColumnType.BIGINT)
        task = self._on_backend(task, "orders", Backend.POSTGRES)
        rows = source_data.generate_rows(task, P.PRIMARY)
        values = [
            value
            for row in rows["orders"]
            for key, value in row.items()
            if isinstance(value, int)
        ]
        self.assertTrue(any(value > 2**53 for value in values))

    def test_a_mongodb_parent_keeps_its_own_identity_in_range(self) -> None:
        task = self._customer_types(ColumnType.BIGINT, ColumnType.BIGINT)
        task = self._on_backend(task, "customers", Backend.MONGODB)
        rows = source_data.generate_rows(task, P.PRIMARY)
        self.assertLessEqual(
            max(row["customer_id"] for row in rows["customers"]), 2**53
        )

    def test_narrow_child_keeps_the_parent_in_its_safe_range(self) -> None:
        task = self._customer_types(ColumnType.BIGINT, ColumnType.INTEGER)
        rows = source_data.generate_rows(task, P.PRIMARY)
        parent_ids = {row["customer_id"] for row in rows["customers"]}
        child_ids = {
            row["customer_id"]
            for row in rows["orders"]
            if row["customer_id"] is not None
        }
        self.assertLess(max(parent_ids), 2**31)
        self.assertLessEqual(child_ids, parent_ids)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

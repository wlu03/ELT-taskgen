"""Typed cross-runtime canonicalization used by parity observations."""

from __future__ import annotations

import datetime as dt
import decimal
import unittest

from pydantic import ValidationError

from elt_taskgen.models import ColumnType
from elt_taskgen.verification.canonical_fingerprint import (
    CANONICAL_FINGERPRINT_VERSION,
    CanonicalFingerprintError,
    CanonicalRelationFingerprint,
    CanonicalRowOrder,
    NaiveTimestampPolicy,
    canonical_cell,
    canonical_relation_fingerprint,
)


class CanonicalFingerprintTests(unittest.TestCase):
    expected = (
        ("event_id", ColumnType.BIGINT),
        ("amount", ColumnType.DECIMAL),
        ("occurred_at", ColumnType.TIMESTAMP),
        ("event_date", ColumnType.DATE),
        ("active", ColumnType.BOOLEAN),
        ("payload", ColumnType.JSON),
        ("label", ColumnType.TEXT),
    )

    def test_cross_driver_shapes_produce_one_typed_fingerprint(self) -> None:
        snowflake_columns = (
            "EVENT_ID",
            "AMOUNT",
            "OCCURRED_AT",
            "EVENT_DATE",
            "ACTIVE",
            "PAYLOAD",
            "LABEL",
            "_AIRBYTE_RAW_ID",
        )
        snowflake_rows = [
            (
                decimal.Decimal("9007199254740993"),
                decimal.Decimal("1.230000000"),
                dt.datetime(
                    2024,
                    2,
                    1,
                    4,
                    5,
                    6,
                    123456,
                    tzinfo=dt.timezone(dt.timedelta(hours=-8)),
                ),
                dt.date(2024, 2, 1),
                True,
                {"b": [1, None], "a": "x"},
                "Case Sensitive",
                "ignored-metadata",
            )
        ]
        string_driver_columns = tuple(name for name, _ in self.expected)
        string_driver_rows = [
            (
                "9007199254740993",
                "1.23",
                "2024-02-01T12:05:06.123456Z",
                "2024-02-01",
                1,
                '{"a":"x","b":[1,null]}',
                "Case Sensitive",
            )
        ]

        snowflake = canonical_relation_fingerprint(
            actual_columns=snowflake_columns,
            expected_columns=self.expected,
            rows=snowflake_rows,
            allowed_extra_columns=("_AIRBYTE_RAW_ID",),
        )
        string_driver = canonical_relation_fingerprint(
            actual_columns=string_driver_columns,
            expected_columns=self.expected,
            rows=string_driver_rows,
            allowed_extra_columns=("_AIRBYTE_RAW_ID",),
        )
        self.assertEqual(snowflake.version, CANONICAL_FINGERPRINT_VERSION)
        self.assertEqual(snowflake, string_driver)

    def test_row_order_is_ignored_but_duplicates_are_preserved(self) -> None:
        expected = (("id", ColumnType.INTEGER), ("value", ColumnType.TEXT))
        rows = [(2, "b"), (1, "a")]
        first = canonical_relation_fingerprint(
            actual_columns=("id", "value"),
            expected_columns=expected,
            rows=rows,
        )
        reordered = canonical_relation_fingerprint(
            actual_columns=("ID", "VALUE"),
            expected_columns=expected,
            rows=list(reversed(rows)),
        )
        duplicated = canonical_relation_fingerprint(
            actual_columns=("id", "value"),
            expected_columns=expected,
            rows=rows + [rows[0]],
        )
        self.assertEqual(first, reordered)
        self.assertNotEqual(first.result_digest, duplicated.result_digest)
        self.assertEqual(duplicated.row_count, 3)

        ordered = canonical_relation_fingerprint(
            actual_columns=("id", "value"),
            expected_columns=expected,
            rows=rows,
            row_order=CanonicalRowOrder.ORDERED,
        )
        ordered_reversed = canonical_relation_fingerprint(
            actual_columns=("id", "value"),
            expected_columns=expected,
            rows=list(reversed(rows)),
            row_order="ordered",
        )
        self.assertNotEqual(ordered.result_digest, ordered_reversed.result_digest)
        self.assertNotEqual(first.schema_digest, ordered.schema_digest)

    def test_decimal_equivalence_and_boundaries(self) -> None:
        equivalents = (
            decimal.Decimal("1.230000000"),
            decimal.Decimal("1.23"),
            "1.230000000",
            0,
            "-0.000000000",
        )
        canonical = [
            canonical_cell(value, ColumnType.DECIMAL, label="decimal")
            for value in equivalents
        ]
        self.assertEqual(canonical[0], canonical[1])
        self.assertEqual(canonical[0], canonical[2])
        self.assertEqual(canonical[3], canonical[4])
        self.assertEqual(
            canonical_cell(
                "99999999999999999999999999999.123456789",
                ColumnType.DECIMAL,
                label="max",
            ),
            ["decimal", "99999999999999999999999999999.123456789"],
        )
        for value in (
            "0.0000000001",
            "999999999999999999999999999999.123456789",
            "NaN",
            True,
        ):
            with self.subTest(value=value):
                with self.assertRaises(CanonicalFingerprintError):
                    canonical_cell(value, ColumnType.DECIMAL, label="bad")

    def test_integer_and_bigint_widths_are_enforced(self) -> None:
        for value, column_type in (
            (-(2**31), ColumnType.INTEGER),
            (2**31 - 1, ColumnType.INTEGER),
            (-(2**63), ColumnType.BIGINT),
            (2**63 - 1, ColumnType.BIGINT),
        ):
            canonical_cell(value, column_type, label="boundary")
        for value, column_type in (
            (-(2**31) - 1, ColumnType.INTEGER),
            (2**31, ColumnType.INTEGER),
            (-(2**63) - 1, ColumnType.BIGINT),
            (2**63, ColumnType.BIGINT),
        ):
            with self.subTest(value=value, column_type=column_type.value):
                with self.assertRaises(CanonicalFingerprintError):
                    canonical_cell(value, column_type, label="out-of-range")

    def test_timestamp_offsets_normalize_to_the_same_microsecond_utc_value(self) -> None:
        pacific = dt.datetime(
            2024,
            3,
            10,
            1,
            30,
            0,
            654321,
            tzinfo=dt.timezone(dt.timedelta(hours=-8)),
        )
        utc = "2024-03-10T09:30:00.654321Z"
        naive_utc = dt.datetime(2024, 3, 10, 9, 30, 0, 654321)
        self.assertEqual(
            canonical_cell(pacific, ColumnType.TIMESTAMP, label="ts"),
            canonical_cell(utc, ColumnType.TIMESTAMP, label="ts"),
        )
        with self.assertRaisesRegex(CanonicalFingerprintError, "naive timestamp"):
            canonical_cell(naive_utc, ColumnType.TIMESTAMP, label="ts")
        self.assertEqual(
            canonical_cell(
                naive_utc,
                ColumnType.TIMESTAMP,
                label="ts",
                naive_timestamp_policy=NaiveTimestampPolicy.ASSUME_UTC,
            ),
            canonical_cell(utc, ColumnType.TIMESTAMP, label="ts"),
        )
        for invalid in (
            "2024-03-10",
            "2024-03-10T09:30:00.1234567Z",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CanonicalFingerprintError):
                    canonical_cell(invalid, ColumnType.TIMESTAMP, label="ts")

    def test_sql_null_json_null_missing_and_text_null_remain_distinct(self) -> None:
        expected = (("payload", ColumnType.JSON),)
        fingerprints = [
            canonical_relation_fingerprint(
                actual_columns=("payload",),
                expected_columns=expected,
                rows=[(value,)],
            ).result_digest
            for value in (None, "null", {}, '{"value":null}', '"null"')
        ]
        self.assertEqual(len(fingerprints), len(set(fingerprints)))

    def test_json_key_order_is_ignored_but_array_order_and_key_case_are_not(self) -> None:
        equivalent = (
            canonical_cell(
                {"a": 1, "b": [2, 3]}, ColumnType.JSON, label="json"
            ),
            canonical_cell(
                '{"b":[2,3],"a":1}', ColumnType.JSON, label="json"
            ),
        )
        self.assertEqual(*equivalent)
        self.assertNotEqual(
            canonical_cell(
                {"a": 1, "b": [2, 3]}, ColumnType.JSON, label="json"
            ),
            canonical_cell(
                {"a": 1, "b": [3, 2]}, ColumnType.JSON, label="json"
            ),
        )
        self.assertNotEqual(
            canonical_cell({"A": 1}, ColumnType.JSON, label="json"),
            canonical_cell({"a": 1}, ColumnType.JSON, label="json"),
        )

    def test_duplicate_non_text_and_nonstandard_json_values_fail_closed(self) -> None:
        for value in (
            '{"a":1,"a":2}',
            {"a": 1, 2: "not-text"},
            '{"a":NaN}',
            '{"a":Infinity}',
        ):
            with self.subTest(value=value):
                with self.assertRaises(CanonicalFingerprintError):
                    canonical_cell(value, ColumnType.JSON, label="json")

    def test_airbyte_metadata_is_ignored_only_when_explicitly_allowed(self) -> None:
        expected = (("id", ColumnType.INTEGER),)
        columns = ("id", "_airbyte_raw_id", "_AIRBYTE_EXTRACTED_AT")
        rows = [(1, "raw-id", "timestamp")]
        with self.assertRaisesRegex(
            CanonicalFingerprintError, "unexpected business columns"
        ):
            canonical_relation_fingerprint(
                actual_columns=columns,
                expected_columns=expected,
                rows=rows,
            )
        allowed = canonical_relation_fingerprint(
            actual_columns=columns,
            expected_columns=expected,
            rows=rows,
            allowed_extra_columns=("_airbyte_raw_id", "_airbyte_extracted_at"),
        )
        plain = canonical_relation_fingerprint(
            actual_columns=("id",),
            expected_columns=expected,
            rows=[(1,)],
            allowed_extra_columns=("_airbyte_raw_id", "_airbyte_extracted_at"),
        )
        self.assertEqual(allowed, plain)

        with self.assertRaisesRegex(
            CanonicalFingerprintError, "unexpected business columns"
        ):
            canonical_relation_fingerprint(
                actual_columns=("id", "other_system_column"),
                expected_columns=expected,
                rows=[(1, "not-airbyte")],
                allowed_extra_columns=("_airbyte_raw_id",),
            )
        with self.assertRaisesRegex(
            CanonicalFingerprintError, "unexpected business columns"
        ):
            canonical_relation_fingerprint(
                actual_columns=("id", "_airbyte_unexpected"),
                expected_columns=expected,
                rows=[(1, "metadata")],
                allowed_extra_columns=("_airbyte_raw_id",),
            )
        with self.assertRaisesRegex(
            CanonicalFingerprintError, "only explicit _airbyte_"
        ):
            canonical_relation_fingerprint(
                actual_columns=("id", "other"),
                expected_columns=expected,
                rows=[(1, "hidden")],
                allowed_extra_columns=("other",),
            )

        source_metadata = canonical_relation_fingerprint(
            actual_columns=("id", "_AB_SOURCE_FILE_URL"),
            expected_columns=expected,
            rows=[(1, "s3://bucket/object.csv")],
            allowed_extra_columns=("_ab_source_file_url",),
        )
        source_plain = canonical_relation_fingerprint(
            actual_columns=("id",),
            expected_columns=expected,
            rows=[(1,)],
            allowed_extra_columns=("_ab_source_file_url",),
        )
        self.assertEqual(source_metadata, source_plain)

        with self.assertRaisesRegex(
            CanonicalFingerprintError, "reviewed source metadata"
        ):
            canonical_relation_fingerprint(
                actual_columns=("id", "_ab_unreviewed"),
                expected_columns=expected,
                rows=[(1, "hidden")],
                allowed_extra_columns=("_ab_unreviewed",),
            )

    def test_case_collisions_missing_columns_and_bad_row_width_fail_closed(self) -> None:
        expected = (("id", ColumnType.INTEGER),)
        with self.assertRaisesRegex(CanonicalFingerprintError, "collision"):
            canonical_relation_fingerprint(
                actual_columns=("id", "ID"),
                expected_columns=expected,
                rows=[(1, 1)],
            )
        with self.assertRaisesRegex(CanonicalFingerprintError, "missing columns"):
            canonical_relation_fingerprint(
                actual_columns=("other",),
                expected_columns=expected,
                rows=[(1,)],
            )
        with self.assertRaisesRegex(CanonicalFingerprintError, "row width"):
            canonical_relation_fingerprint(
                actual_columns=("id",),
                expected_columns=expected,
                rows=[(1, 2)],
            )

    def test_nonfinite_float_invalid_json_and_non_text_fail_closed(self) -> None:
        for value, column_type in (
            (float("nan"), ColumnType.FLOAT),
            (float("inf"), ColumnType.FLOAT),
            ("{bad json", ColumnType.JSON),
            (123, ColumnType.TEXT),
            ("not-a-date", ColumnType.DATE),
            ("not-a-timestamp", ColumnType.TIMESTAMP),
            (2, ColumnType.BOOLEAN),
        ):
            with self.subTest(value=value, column_type=column_type.value):
                with self.assertRaises(CanonicalFingerprintError):
                    canonical_cell(value, column_type, label="bad")

        with self.assertRaisesRegex(
            CanonicalFingerprintError, "row-order policy"
        ):
            canonical_relation_fingerprint(
                actual_columns=("id",),
                expected_columns=(("id", ColumnType.INTEGER),),
                rows=[(1,)],
                row_order="driver_default",
            )

    def test_fingerprint_record_rejects_forged_versions_and_digests(self) -> None:
        base = {
            "row_order": CanonicalRowOrder.UNORDERED,
            "naive_timestamp_policy": NaiveTimestampPolicy.REJECT,
            "allowed_extra_columns": (),
            "row_count": 1,
            "schema_digest": "a" * 64,
            "result_digest": "b" * 64,
        }
        with self.assertRaises(ValidationError):
            CanonicalRelationFingerprint(**(base | {"version": "old"}))
        with self.assertRaises(ValidationError):
            CanonicalRelationFingerprint(**(base | {"result_digest": "bad"}))
        with self.assertRaises(ValidationError):
            CanonicalRelationFingerprint(**(base | {"row_count": -1}))
        with self.assertRaises(ValidationError):
            CanonicalRelationFingerprint(**(base | {"row_count": True}))
        with self.assertRaises(ValidationError):
            CanonicalRelationFingerprint(**(base | {"row_count": "1"}))


if __name__ == "__main__":
    unittest.main()

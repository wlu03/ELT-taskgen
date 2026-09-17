"""Unit matrix for the strict typed diagnostic (IR-002).

Every strict verdict is asserted NEXT TO the unchanged legacy verdict: the
legacy comparator and the legacy loader map must keep their exact behavior
while the strict diagnostic reports what they erase.  A failure here means
either the diagnostic regressed or — far worse — the frozen reward moved.
"""

from __future__ import annotations

import datetime
import decimal
import tempfile
import unittest
from pathlib import Path

from elt_taskgen.models import (
    ColumnSpec,
    ColumnType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    TableSpec,
)
from elt_taskgen.reference import solution
from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection
from elt_taskgen.verification import strict_diagnostic as sd
from elt_taskgen.verification import upstream_eval

UTC = datetime.timezone.utc


def _mart(name: str, columns: tuple[tuple[str, ColumnType], ...], keys=("id",)) -> MartSpec:
    cols = tuple(
        MartColumn(name=n, type=t, description=f"{n} column") for n, t in columns
    )
    plan = MartPlan(
        mart=name,
        ops=(
            MartOp(
                kind=MartOpKind.SOURCE,
                description="strict diagnostic test view",
                tables=(name,),
                columns=tuple(c.name for c in cols),
            ),
        ),
    )
    return MartSpec(
        name=name,
        description="strict diagnostic test mart",
        grain="one row per id",
        key_columns=keys,
        columns=cols,
        plan=plan,
    )


class StrictCellMatrixTests(unittest.TestCase):
    """The review's value classes: legacy verdict unchanged, strict distinct."""

    def test_large_ints_beyond_double_precision(self):
        g, a = 2**53, 2**53 + 1
        # Legacy: both columns coerce, floats collapse them to the same double.
        self.assertTrue(upstream_eval._vectors_match([g], [a]))
        self.assertNotEqual(sd.strict_cell(g), sd.strict_cell(a))
        self.assertEqual(sd.strict_cell(a), ("int", str(2**53 + 1)))

    def test_case_fold_and_trim_are_not_strict_matches(self):
        for gold, actual in (("ACME", "acme"), (" x ", "x")):
            with self.subTest(gold=gold, actual=actual):
                self.assertTrue(upstream_eval._vectors_match([gold], [actual]))
                self.assertNotEqual(sd.strict_cell(gold), sd.strict_cell(actual))
        self.assertEqual(sd.strict_cell(" x "), ("text", " x "))  # verbatim

    def test_null_vs_na_tokens_and_empty_string(self):
        for token in ("null", "NULL", "None", "NaN", ""):
            with self.subTest(token=token):
                self.assertTrue(upstream_eval._vectors_match([None], [token]))
                self.assertNotEqual(sd.strict_cell(None), sd.strict_cell(token))
        self.assertEqual(sd.strict_cell(None), ("null", ""))
        self.assertEqual(sd.strict_cell(""), ("text", ""))
        self.assertEqual(sd.strict_cell("null"), ("text", "null"))

    def test_decimal_scale_and_float_are_distinct_tags(self):
        d_scaled = decimal.Decimal("1.50")
        d_plain = decimal.Decimal("1.5")
        # Legacy result coercion erases all three into the same float.
        self.assertEqual(solution.to_scalar(d_scaled), solution.to_scalar(d_plain))
        self.assertEqual(solution.to_scalar(d_scaled), 1.5)
        cells = {sd.strict_cell(d_scaled), sd.strict_cell(d_plain), sd.strict_cell(1.5)}
        self.assertEqual(len(cells), 3)
        self.assertEqual(sd.strict_cell(d_scaled), ("decimal", "1.50"))
        self.assertEqual(sd.strict_cell(1.5), ("float", "1.5"))
        self.assertEqual(sd.strict_cell(float("nan")), ("float", "nan"))

    def test_timestamp_timezone_tag_and_text_survive(self):
        naive = datetime.datetime(2020, 1, 1)
        aware = datetime.datetime(2020, 1, 1, tzinfo=UTC)
        self.assertEqual(sd.strict_cell(naive)[0], "timestamp")
        self.assertEqual(sd.strict_cell(aware)[0], "timestamptz")
        self.assertEqual(sd.strict_cell(aware)[1], "2020-01-01T00:00:00+00:00")
        self.assertEqual(sd.strict_cell(datetime.date(2020, 1, 2)), ("date", "2020-01-02"))

    def test_bool_text_matches_the_legacy_cell_text_convention(self):
        self.assertEqual(sd.strict_cell(True), ("bool", "True"))
        self.assertEqual(sd.strict_text(True), upstream_eval.cell_text(True))
        self.assertEqual(sd.strict_text(False), upstream_eval.cell_text(False))
        # ... but never coerces cross-type: bool 1 is not int 1.
        self.assertNotEqual(sd.strict_cell(True), sd.strict_cell(1))

    def test_json_null_vs_missing_and_key_case_are_distinct(self):
        self.assertNotEqual(sd.strict_cell({"a": None}), sd.strict_cell({}))
        self.assertNotEqual(sd.strict_cell({"A": 1}), sd.strict_cell({"a": 1}))
        self.assertEqual(sd.strict_cell({"a": None})[0], "json")

    def test_strict_sort_key_is_total_and_deterministic(self):
        rows = [(1, "a"), (1, "A"), (None, ""), (1, 1)]
        ordered = sorted(rows, key=sd.strict_sort_key)
        self.assertEqual(sorted(rows, key=sd.strict_sort_key), ordered)  # stable
        self.assertEqual(len({sd.strict_sort_key(r) for r in rows}), 4)


class StrictTypeMapTests(unittest.TestCase):
    """Strict DDL convention next to a proof the legacy map is untouched."""

    def test_strict_map_types_and_legacy_map_unchanged(self):
        self.assertEqual(sd.strict_duckdb_type(ColumnType.DECIMAL), "DECIMAL(38,9)")
        self.assertEqual(sd.STRICT_DECIMAL_TYPE, "DECIMAL(38,9)")
        self.assertEqual(sd.strict_duckdb_type(ColumnType.JSON), "JSON")
        # The legacy map keeps the exact widenings the reward depends on.
        self.assertEqual(solution.duckdb_type(ColumnType.DECIMAL), "DOUBLE")
        self.assertEqual(solution.duckdb_type(ColumnType.JSON), "VARCHAR")
        for column_type in ColumnType:
            if column_type in (ColumnType.DECIMAL, ColumnType.JSON):
                continue
            with self.subTest(column_type=column_type.value):
                self.assertEqual(
                    sd.strict_duckdb_type(column_type),
                    solution.duckdb_type(column_type),
                )

    def test_describe_columns_reports_full_logical_types(self):
        con = sandboxed_memory_connection()
        try:
            fingerprints = sd.describe_columns(
                con,
                "SELECT 1::BIGINT AS a, 1.0::DOUBLE AS b, 'x' AS c, "
                "'{}'::JSON AS d, TIMESTAMP '2020-01-01' AS e, "
                "TIMESTAMPTZ '2020-01-01 00:00:00+00' AS f",
            )
            self.assertEqual(
                [(f.name, f.declared_type) for f in fingerprints],
                [
                    ("a", "BIGINT"),
                    ("b", "DOUBLE"),
                    ("c", "VARCHAR"),
                    ("d", "JSON"),
                    ("e", "TIMESTAMP"),
                    ("f", "TIMESTAMP WITH TIME ZONE"),
                ],
            )
            with self.assertRaisesRegex(ValueError, "describe_failed"):
                sd.describe_columns(con, "SELECT nonsense FROM nowhere")
        finally:
            con.close()


class StrictMartDiagnosticTests(unittest.TestCase):
    """End-to-end strict verdicts on a real sandboxed connection."""

    def setUp(self):
        self.con = sandboxed_memory_connection()
        self.addCleanup(self.con.close)
        self.con.execute('CREATE TABLE "m" (id INTEGER, val DOUBLE)')
        self.con.execute('INSERT INTO "m" VALUES (1, 100.0), (2, 200.0)')
        self.mart = _mart(
            "m", (("id", ColumnType.INTEGER), ("val", ColumnType.DECIMAL))
        )
        self.reference_sql = 'SELECT id, val FROM "m" ORDER BY id'

    def _diag(self, submission_sql, **kwargs):
        limits = {"max_rows": 1000, "max_bytes": 1 << 20}
        limits.update(kwargs)
        return sd.strict_mart_diagnostic(
            self.con, self.mart, self.reference_sql, submission_sql, **limits
        )

    def _legacy_score(self, submission_sql) -> bool:
        gold_rows = solution.execute_mart(self.con, self.mart, self.reference_sql)
        gold_csv = upstream_eval.rows_to_canonical_csv(
            upstream_eval.sort_rows(gold_rows, ("id",), ("id", "val")),
            ("id", "val"),
        )
        actual = solution.execute_mart(self.con, self.mart, submission_sql)
        return upstream_eval.compare_mart(gold_csv, actual, self.mart)

    def test_tolerance_abuse_passes_legacy_but_fails_strict(self):
        submission = 'SELECT id, val * 1.005 AS val FROM "m" ORDER BY id'
        self.assertTrue(self._legacy_score(submission))
        diag = self._diag(submission)
        self.assertFalse(diag.strict_match)
        self.assertEqual(diag.mismatch_code, "values:val")
        # Fingerprints are reported on mismatch too.
        self.assertEqual(
            [(f.name, f.declared_type) for f in diag.submission_columns],
            [("id", "INTEGER"), ("val", "DOUBLE")],
        )

    def test_identical_sql_matches_with_equal_fingerprints(self):
        diag = self._diag(self.reference_sql)
        self.assertTrue(diag.strict_match)
        self.assertEqual(diag.mismatch_code, "")
        self.assertEqual(diag.submission_columns, diag.reference_columns)

    def test_result_type_change_is_column_types(self):
        diag = self._diag('SELECT id, val::VARCHAR AS val FROM "m"')
        self.assertFalse(diag.strict_match)
        self.assertEqual(diag.mismatch_code, "column_types")

    def test_row_count_and_ordering_ties(self):
        diag = self._diag('SELECT id, val FROM "m" WHERE id = 1')
        self.assertFalse(diag.strict_match)
        self.assertEqual(diag.mismatch_code, "row_count")
        # Fold-equal but strictly distinct rows must compare deterministically
        # whatever order each side returns them in.
        self.con.execute('CREATE TABLE "t" (id INTEGER, v VARCHAR)')
        self.con.execute("INSERT INTO \"t\" VALUES (1, 'A'), (1, 'a')")
        mart = _mart("t", (("id", ColumnType.INTEGER), ("v", ColumnType.TEXT)))
        reordered = sd.strict_mart_diagnostic(
            self.con,
            mart,
            'SELECT id, v FROM "t" ORDER BY v',
            'SELECT id, v FROM "t" ORDER BY v DESC',
            max_rows=10,
            max_bytes=4096,
        )
        self.assertTrue(reordered.strict_match)
        folded = sd.strict_mart_diagnostic(
            self.con,
            mart,
            'SELECT id, v FROM "t" ORDER BY v',
            'SELECT id, lower(v) AS v FROM "t" ORDER BY v',
            max_rows=10,
            max_bytes=4096,
        )
        self.assertFalse(folded.strict_match)
        self.assertEqual(folded.mismatch_code, "values:v")

    def test_error_paths_collapse_to_stable_codes(self):
        broken = self._diag("SELECT nonsense FROM nowhere")
        self.assertEqual(broken.mismatch_code, "error:describe_failed")
        wrong_columns = self._diag('SELECT id AS wrong, val FROM "m"')
        self.assertEqual(wrong_columns.mismatch_code, "error:query_failed")
        too_many = self._diag(self.reference_sql, max_rows=1)
        self.assertEqual(too_many.mismatch_code, "error:query_failed")
        too_big = self._diag(self.reference_sql, max_bytes=1)
        self.assertEqual(too_big.mismatch_code, "error:output_limit")
        for diag in (broken, wrong_columns, too_many, too_big):
            self.assertFalse(diag.strict_match)


class StrictTextCompareTests(unittest.TestCase):
    """Runtime-tier strict text pass against frozen gold text."""

    MART = _mart("m", (("id", ColumnType.INTEGER), ("val", ColumnType.TEXT)))

    def test_exact_text_match_across_case_insensitive_column_names(self):
        ok, code = sd.strict_text_compare(
            "id,val\n1,Acme\n",
            [{"ID": 1, "VAL": "Acme"}],
            ("ID", "VAL"),
            self.MART,
        )
        self.assertTrue(ok)
        self.assertEqual(code, "")

    def test_na_tokens_and_folding_are_strict_mismatches(self):
        cases = (
            ("id,val\n1,null\n", [{"id": 1, "val": None}]),  # NULL vs 'null'
            ("id,val\n1,acme\n", [{"id": 1, "val": "ACME"}]),  # case fold
            ("id,val\n1,x\n", [{"id": 1, "val": "x "}]),  # trailing space
        )
        for gold_csv, actual in cases:
            with self.subTest(gold=gold_csv):
                self.assertTrue(
                    upstream_eval.compare_mart(gold_csv, list(actual), self.MART)
                )
                ok, code = sd.strict_text_compare(
                    gold_csv, list(actual), ("id", "val"), self.MART
                )
                self.assertFalse(ok)
                self.assertEqual(code, "values:val")

    def test_structural_codes(self):
        ok, code = sd.strict_text_compare(
            "id,val\n1,a\n", [], ("id", "val"), self.MART
        )
        self.assertEqual((ok, code), (False, "row_count"))
        ok, code = sd.strict_text_compare(
            "id,val\n1,a\n", [{"id": 1, "other": "a"}], ("id", "other"), self.MART
        )
        self.assertEqual((ok, code), (False, "column_names"))
        ok, code = sd.strict_text_compare(
            'id,val\n"1\n', [{"id": 1, "val": "a"}], ("id", "val"), self.MART
        )
        self.assertEqual((ok, code), (False, "error:gold_unreadable"))
        ok, code = sd.strict_text_compare(
            "", [{"id": 1, "val": "a"}], ("id", "val"), self.MART
        )
        self.assertEqual((ok, code), (False, "error:gold_unreadable"))

    def test_typed_driver_values_compare_by_strict_text(self):
        gold = "id,val\n1,10.55\n"
        ok, code = sd.strict_text_compare(
            gold, [{"id": 1, "val": decimal.Decimal("10.55")}], ("id", "val"), self.MART
        )
        self.assertTrue(ok)
        ok, code = sd.strict_text_compare(
            gold, [{"id": 1, "val": decimal.Decimal("10.550")}], ("id", "val"), self.MART
        )
        self.assertEqual((ok, code), (False, "values:val"))


class DescriptionFingerprintTests(unittest.TestCase):
    def test_tuple_and_attribute_descriptions(self):
        class _Column:
            def __init__(self, name, type_code):
                self.name = name
                self.type_code = type_code

        self.assertEqual(
            sd.description_fingerprint((("ID", 3), ("TOTAL", 0, None, None, 38, 9, True))),
            (("ID", "3"), ("TOTAL", "0")),
        )
        self.assertEqual(
            sd.description_fingerprint([_Column("a", "NUMBER"), _Column("b", None)]),
            (("a", "NUMBER"), ("b", "")),
        )
        with self.assertRaises(ValueError):
            sd.description_fingerprint(None)
        with self.assertRaises(ValueError):
            sd.description_fingerprint([(None, 3)])


class StrictShadowLoaderTests(unittest.TestCase):
    """The strict loader keeps digits and JSON typing; the legacy loader is untouched."""

    TABLE = TableSpec(
        name="t",
        columns=(
            ColumnSpec(name="id", type=ColumnType.BIGINT, description="row id"),
            ColumnSpec(
                name="amount", type=ColumnType.DECIMAL, nullable=True,
                description="decimal amount",
            ),
            ColumnSpec(
                name="payload", type=ColumnType.JSON, nullable=True,
                description="json payload",
            ),
        ),
    )

    def _fixture_rows(self) -> list[dict]:
        """Rows read through the SAME reader the legacy loader uses."""
        text = (
            "id,amount,payload\n"
            f"{2**63 - 1},0.123456789,\"{{\"\"a\"\": null}}\"\n"
            f"{2**53 + 1},,\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.csv"
            path.write_text(text, encoding="utf-8")
            return solution._read_csv(path)

    def test_strict_load_keeps_digits_and_types(self):
        rows = self._fixture_rows()
        con = sandboxed_memory_connection()
        try:
            sd.create_strict_table(con, self.TABLE)
            self.assertEqual(sd._strict_insert_rows(con, self.TABLE, rows), 2)
            described = {
                row[0]: row[1]
                for row in con.execute('DESCRIBE "t"').fetchall()
            }
            self.assertEqual(described["id"], "BIGINT")
            self.assertEqual(described["amount"], "DECIMAL(38,9)")
            self.assertEqual(described["payload"], "JSON")
            fetched = con.execute('SELECT id, amount FROM "t" ORDER BY id DESC').fetchall()
            self.assertEqual(fetched[0][0], 2**63 - 1)  # exact, never floated
            self.assertEqual(fetched[0][1], decimal.Decimal("0.123456789"))
            self.assertIsNone(fetched[1][1])
        finally:
            con.close()

    def test_legacy_load_of_the_same_fixture_is_unchanged(self):
        rows = self._fixture_rows()
        con = sandboxed_memory_connection()
        try:
            solution.create_table(con, self.TABLE)
            self.assertEqual(solution._insert_rows(con, self.TABLE, rows), 2)
            described = {
                row[0]: row[1]
                for row in con.execute('DESCRIBE "t"').fetchall()
            }
            self.assertEqual(described["amount"], "DOUBLE")  # historical widening
            self.assertEqual(described["payload"], "VARCHAR")
            fetched = con.execute('SELECT amount FROM "t" ORDER BY id DESC').fetchall()
            self.assertEqual(fetched[0][0], float("0.123456789"))
        finally:
            con.close()

    def test_strict_decimal_convention_fails_closed(self):
        col = self.TABLE.columns[1]
        self.assertEqual(
            sd.strict_coerce_value("1.50", col, table="t"), decimal.Decimal("1.50")
        )
        self.assertIsNone(sd.strict_coerce_value(None, col, table="t"))
        self.assertIsNone(sd.strict_coerce_value("", col, table="t"))
        for raw in ("0.1234567891", "1" + "0" * 29, "NaN", "not a number", True):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    sd.strict_coerce_value(raw, col, table="t")
        # Non-decimal columns delegate to the legacy coercion unchanged.
        id_col = self.TABLE.columns[0]
        self.assertEqual(sd.strict_coerce_value("7", id_col, table="t"), 7)

    def _postgres_artifact(self, tmp: str, amount_literal: str) -> Path:
        path = Path(tmp) / "t.sql"
        path.write_text(
            'INSERT INTO "t" ("id", "amount", "payload") '
            f"VALUES (1, {amount_literal}, NULL);\n",
            encoding="utf-8",
        )
        return path

    def test_postgres_shadow_load_stages_exact_decimal_text(self):
        """A direct load into DECIMAL(38,9) would let DuckDB round the
        literal silently; the VARCHAR staging keeps the exact digits so the
        strict convention check sees them."""
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._postgres_artifact(tmp, "0.123456789")
            con = sandboxed_memory_connection()
            try:
                staged = sd._staged_postgres_rows(con, self.TABLE, artifact)
                self.assertEqual(staged[0]["amount"], "0.123456789")
                sd.create_strict_table(con, self.TABLE)
                sd._strict_insert_rows(con, self.TABLE, staged)
                (value,) = con.execute('SELECT amount FROM "t"').fetchone()
                self.assertEqual(value, decimal.Decimal("0.123456789"))
                # Staging dropped its table before the strict CREATE reused
                # the name, and no second table remains.
                tables = con.execute("SHOW TABLES").fetchall()
                self.assertEqual([row[0] for row in tables], ["t"])
            finally:
                con.close()

    def test_postgres_shadow_load_fails_closed_on_downscale_decimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._postgres_artifact(tmp, "0.1234567891")
            con = sandboxed_memory_connection()
            try:
                staged = sd._staged_postgres_rows(con, self.TABLE, artifact)
                # The exact scale-10 text survives staging ...
                self.assertEqual(staged[0]["amount"], "0.1234567891")
                sd.create_strict_table(con, self.TABLE)
                # ... and the strict insert refuses it instead of rounding.
                with self.assertRaisesRegex(ValueError, "does not fit"):
                    sd._strict_insert_rows(con, self.TABLE, staged)
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()

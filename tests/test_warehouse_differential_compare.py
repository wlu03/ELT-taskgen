"""The differential collector compares typed values, then the reward rule.

Collector version 1 compared display strings (SQL NULL matched the text 'NULL',
TRUE matched 'true') and called a symmetric tolerance "within tolerance" even
where the scorer rejects (reference 100, candidate 99). These tests run the
collector's own comparison, and its real pinned-DuckDB collection path, without
any warehouse.
"""
from __future__ import annotations

import inspect
import json
import math
import sys
import unittest
from decimal import Decimal
from pathlib import Path

from elt_taskgen.destinations import Destination
from elt_taskgen.training import dbt_runner
from elt_taskgen.verification import upstream_eval

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "warehouse_differential"
sys.path.insert(0, str(FIXTURES))

import collect  # noqa: E402
from compare import compare, display, encode, error_kind, outcome, reward_rule_agreement, typed  # noqa: E402
from native_probes import DESTINATIONS, NATIVE_PROBES  # noqa: E402

PINNED_DUCKDB = collect.RUNTIME_PY.is_file()


def _pair(warehouse, duckdb, *, warehouse_column="", duckdb_column="", engine="snowflake"):
    return compare(encode(warehouse, warehouse_column), encode(duckdb, duckdb_column), engine)


class TypedAgreementTests(unittest.TestCase):
    def test_values_that_share_a_display_string_are_different_values(self) -> None:
        cases = (
            ("SQL NULL vs text NULL", None, "NULL", "", ""),
            ("SQL NULL vs empty text", None, "", "", ""),
            ("empty text vs text NULL", "", "NULL", "", ""),
            ("TRUE vs text true", True, "true", "13", "VARCHAR"),
            ("FALSE vs text false", False, "false", "13", "VARCHAR"),
            ("JSON null vs SQL NULL", "null", None, "5", "JSON"),
            ("JSON null vs text null", "null", "null", "5", "VARCHAR"),
            ("number vs its text", 1, "1", "0", "VARCHAR"),
        )
        for label, warehouse, duckdb, w_column, d_column in cases:
            with self.subTest(label):
                result = _pair(warehouse, duckdb, warehouse_column=w_column, duckdb_column=d_column)
                self.assertFalse(result["exact_agreement"])
                self.assertEqual(result["verdict"], "DIFFERENT")
        self.assertEqual(display(typed(encode(None), "duckdb")), display(typed(encode("NULL"), "duckdb")))

    def test_json_null_is_neither_sql_null_nor_text(self) -> None:
        json_null = typed(encode("null", "JSON"), "duckdb")
        self.assertEqual(json_null, ["json", '["null"]'])
        self.assertNotEqual(json_null, typed(encode(None, "JSON"), "duckdb"))
        self.assertNotEqual(json_null, typed(encode("null", "VARCHAR"), "duckdb"))
        # Each engine's JSON column type is recognized, not only DuckDB's.
        for engine, column in (("snowflake", 5), ("redshift", 4000), ("databricks", "variant")):
            with self.subTest(engine=engine):
                self.assertEqual(typed(encode("null", column), engine), json_null)

    def test_equal_numbers_agree_whatever_the_driver_type(self) -> None:
        cases = (
            (5, Decimal("5.0")),
            (1, 1.0),
            (Decimal("1.50"), 1.5),
            (Decimal("123456789012345678901234567890"), 123456789012345678901234567890),
            (0, Decimal("-0.00")),
        )
        for warehouse, duckdb in cases:
            with self.subTest(warehouse=warehouse, duckdb=duckdb):
                result = _pair(warehouse, duckdb)
                self.assertTrue(result["exact_agreement"])
                self.assertEqual(result["verdict"], "identical")
        # Genuinely different numbers stay different exactly.
        long = _pair(
            Decimal("123456789012345678901234567890"),
            Decimal("123456789012345678901234567891"),
        )
        self.assertFalse(long["exact_agreement"])

    def test_a_missing_side_is_not_comparable(self) -> None:
        self.assertEqual(compare(None, encode(1), "snowflake"), {"verdict": "not_comparable"})
        self.assertEqual(compare(encode(1), None, "snowflake"), {"verdict": "not_comparable"})

    @unittest.skipUnless(PINNED_DUCKDB, "the pinned runtime-images/dbt-duckdb venv is not provisioned")
    def test_the_pinned_duckdb_collection_path_keeps_types(self) -> None:
        statements = {
            "sql_null": "SELECT NULL",
            "text_null": "SELECT 'NULL'",
            "empty": "SELECT ''",
            "json_null": "SELECT 'null'::JSON",
            "true": "SELECT TRUE",
            "text_true": "SELECT 'true'",
            "int": "SELECT 5",
            "decimal": "SELECT CAST(5.0 AS DECIMAL(10, 1))",
            "double": "SELECT CAST(1.5 AS DOUBLE)",
        }
        records = collect.run_duckdb(statements)
        values = {key: typed(record["value"], "duckdb") for key, record in records.items()}
        self.assertEqual(values["sql_null"], ["sql_null"])
        self.assertEqual(values["text_null"], ["text", "NULL"])
        self.assertEqual(values["empty"], ["text", ""])
        self.assertEqual(values["json_null"], ["json", '["null"]'])
        self.assertEqual(values["true"], ["boolean", True])
        self.assertEqual(values["text_true"], ["text", "true"])
        self.assertEqual(values["int"], values["decimal"])
        self.assertEqual(values["double"], ["number", "1.5"])
        self.assertEqual(len({str(value) for value in values.values()}), len(values) - 1)


def _scorer(reference, candidate) -> bool:
    """The production comparison, called the way compare_mart calls it."""
    _columns, gold = upstream_eval.parse_canonical_csv(
        upstream_eval.rows_to_canonical_csv([{"v": reference}], ("v",))
    )
    return upstream_eval._vectors_match([gold[0]["v"]], [upstream_eval.cell_text(candidate)])


class RewardRuleAgreementTests(unittest.TestCase):
    CASES = (
        # boundary and argument order: the tolerance scales with the candidate
        (100, 99, False),
        (99, 100, True),
        (101, 100, True),
        (100.0, 98.9, False),
        # zero and the absolute floor
        (0, 0, True),
        (0, 1e-9, True),
        (0, 2e-9, False),
        (1e-9, 0, True),
        (Decimal("0.00"), 0, True),
        # nulls: the reward rule folds SQL NULL and the pandas null tokens
        (None, None, True),
        (None, 0, False),
        (0, None, False),
        (None, "NULL", True),
        ("", None, True),
        # non-finite values
        (math.inf, math.inf, True),
        (math.inf, 1e308, False),
        (1.0, math.inf, False),
        (-math.inf, math.inf, False),
        (math.nan, math.nan, True),
        (math.nan, 0.0, False),
        # text: trimmed and case-insensitive under the reward rule
        ("A", "a", True),
        ("a", "b", False),
    )

    def test_the_collector_asks_the_scorers_comparator(self) -> None:
        for reference, candidate, expected in self.CASES:
            with self.subTest(reference=reference, candidate=candidate):
                self.assertEqual(_scorer(reference, candidate), expected)
                self.assertEqual(
                    reward_rule_agreement(encode(reference), encode(candidate)), expected
                )

    def test_within_tolerance_is_the_reward_rules_numeric_verdict(self) -> None:
        # The reported counterexample: the scorer rejects 99 against 100.
        self.assertEqual(_pair(100, 99)["verdict"], "DIFFERENT")
        self.assertEqual(_pair(99, 100)["verdict"], "within_tolerance")
        self.assertEqual(_pair(Decimal("0.3"), 0.1 + 0.2)["verdict"], "within_tolerance")
        # Folded nulls and letter case agree under the reward rule but are
        # different values, so the verdict stays DIFFERENT.
        for warehouse, duckdb in ((None, "NULL"), ("A", "a"), (True, "true")):
            with self.subTest(warehouse=warehouse, duckdb=duckdb):
                result = _pair(warehouse, duckdb)
                self.assertTrue(result["reward_rule_agreement"])
                self.assertEqual(result["verdict"], "DIFFERENT")

    def test_the_collector_has_no_tolerance_formula_of_its_own(self) -> None:
        source = inspect.getsource(collect)
        self.assertNotIn("numerically_close", source)
        self.assertNotIn("REL_TOL", source)
        self.assertIn("outcome(warehouse_value, warehouse_error_kind, duckdb_value, name)", source)


class OutcomeTests(unittest.TestCase):
    """An outage is unmeasured; a native SQL error with local success is a
    disagreement."""

    def test_error_kinds(self) -> None:
        for name in ("InterfaceError", "OperationalError", "RequestError", "ConnectionError", "TimeoutError"):
            with self.subTest(name=name):
                self.assertEqual(error_kind(type(name, (Exception,), {})()), "unmeasured")
        # Databricks reports a rejected statement as ServerOperationError.
        for name in ("ProgrammingError", "ServerOperationError", "DatabaseError", "DataError"):
            with self.subTest(name=name):
                self.assertEqual(error_kind(type(name, (Exception,), {})()), "sql")

    def test_outcomes(self) -> None:
        value = encode(1)
        self.assertEqual(outcome(None, "unmeasured", value, "redshift"), {"verdict": "unmeasured"})
        self.assertEqual(
            outcome(None, "sql", value, "redshift"),
            {"verdict": "DIFFERENT", "disagreement": "native_error_local_success"},
        )
        self.assertEqual(outcome(None, "sql", None, "redshift"), {"verdict": "not_comparable"})
        self.assertEqual(outcome(value, None, value, "redshift")["verdict"], "identical")


class NativeProbeTests(unittest.TestCase):
    def test_every_native_probe_is_written_for_every_destination(self) -> None:
        ids = [probe["id"] for probe in NATIVE_PROBES]
        self.assertEqual(len(ids), len(set(ids)))
        for probe in NATIVE_PROBES:
            with self.subTest(probe=probe["id"]):
                self.assertEqual(set(probe["native"]), set(DESTINATIONS))

    @unittest.skipUnless(PINNED_DUCKDB, "the pinned runtime-images/dbt-duckdb venv is not provisioned")
    def test_every_native_probe_reaches_the_pinned_engine_or_is_refused(self) -> None:
        from elt_taskgen.destinations import Destination

        for destination in DESTINATIONS:
            prepared = {
                probe["id"]: collect.native_duckdb_sql(probe, Destination(destination))
                for probe in NATIVE_PROBES
            }
            records = collect.run_duckdb({pid: sql for pid, (_s, sql) in prepared.items() if sql})
            for pid, (status, sql) in prepared.items():
                with self.subTest(destination=destination, probe=pid):
                    self.assertTrue(sql, f"{pid} has no DuckDB statement ({status})")
                    self.assertIn(pid, records)


class NativeEvidenceTests(unittest.TestCase):
    """The native probes collected on 2026-09-18 (collector 2, rewrite v4),
    bound to the current rewrite. The files keep the labels they were
    collected under."""

    def _evidence(self, destination: str) -> dict:
        return json.loads((FIXTURES / f"native_{destination}.json").read_text(encoding="utf-8"))

    def test_the_native_evidence_is_complete_and_measured(self) -> None:
        ids = [probe["id"] for probe in NATIVE_PROBES]
        for destination in DESTINATIONS:
            with self.subTest(destination=destination):
                document = self._evidence(destination)
                self.assertEqual(document["collector_version"], "2")
                self.assertEqual(document["rewrite"]["subset_version"], "portable-dbt-sql-v4")
                self.assertEqual([entry["id"] for entry in document["probes"]], ids)
                self.assertNotIn("unmeasured", {entry["verdict"] for entry in document["probes"]})

    @unittest.skipUnless(PINNED_DUCKDB, "the pinned runtime-images/dbt-duckdb venv is not provisioned")
    def test_every_native_disagreement_is_now_refused_or_reproduced(self) -> None:
        probes = {probe["id"]: probe for probe in NATIVE_PROBES}
        checked = refused = 0
        for destination in DESTINATIONS:
            pending = {}
            for entry in self._evidence(destination)["probes"]:
                if entry["verdict"] == "identical":
                    continue
                native = probes[entry["id"]]["native"][destination]
                try:
                    pending[entry["id"]] = (
                        entry,
                        dbt_runner.rewrite_model_sql(native, Destination(destination)),
                    )
                except dbt_runner.DbtPolicyFailure:
                    refused += 1
            records = collect.run_duckdb({pid: sql for pid, (_entry, sql) in pending.items()})
            for pid, (entry, _sql) in pending.items():
                checked += 1
                with self.subTest(destination=destination, probe=pid):
                    local = records[pid]
                    if entry.get("warehouse_error"):
                        self.assertFalse(
                            local["ok"], "the destination rejected this SQL but the grader runs it"
                        )
                    else:
                        self.assertTrue(local["ok"], local.get("error"))
                        self.assertEqual(
                            typed(local["value"], "duckdb"),
                            typed(entry["warehouse_value"], destination),
                        )
        self.assertGreater(checked, 0)
        self.assertGreater(refused, 0)


if __name__ == "__main__":
    unittest.main()

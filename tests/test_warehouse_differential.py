"""Real-destination differential evidence for the portable dbt subset.

The grader never runs a warehouse: it rewrites the solver's destination SQL
into a closed DuckDB subset and executes that. Every admitted rule is therefore
a CLAIM that the two engines agree. Until 2026-09-16 no rule had ever been
executed on a real warehouse.

`tests/fixtures/warehouse_differential/` holds the measurement: each probe's
expression rendered for the destination, run on a live Snowflake account and a
live Databricks SQL warehouse and a live Redshift Serverless workgroup,
beside the same expression rewritten by
`rewrite_model_sql` and run on the PINNED grading engine (DuckDB 1.4.5).
`collect.py` and `probes.py` beside them are the collector, so the evidence can
be refreshed with credentials and without guesswork.

These tests need no warehouse. They bind the POLICY to the EVIDENCE: anything
measured as different must be refused by the subset or repaired by the rewrite,
and anything measured as identical must stay admitted, so a later widening of
the subset cannot silently re-admit a construct a real destination disagrees
with.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from elt_taskgen.destinations import Destination
from elt_taskgen.training.dbt_runner import (
    DBT_COMPATIBILITY_SUBSET_VERSION,
    DbtPolicyFailure,
    rewrite_model_sql,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "warehouse_differential"
#: Every destination the evidence covers.
DESTINATIONS = ("snowflake", "databricks", "redshift")
SOURCE = "{{ source('raw', 't') }}"

#: Declared destination/probe rewrites. Empty because the current Databricks
#: DATE_TRUNC cast makes every probe agree; future repairs must be listed here.
REPAIRED_BY_REWRITE: dict[tuple[str, str], str] = {}


def _model(candidate_sql: str) -> str:
    return candidate_sql if " FROM " in candidate_sql else f"{candidate_sql} FROM {SOURCE}"


def _evidence(destination: str) -> list[dict]:
    document = json.loads((FIXTURES / f"differential_{destination}.json").read_text(encoding="utf-8"))
    return document["probes"]


class WarehouseDifferentialTests(unittest.TestCase):
    def test_evidence_exists_for_both_measured_destinations(self) -> None:
        for destination in DESTINATIONS:
            probes = _evidence(destination)
            self.assertGreaterEqual(len(probes), 40, destination)
            verdicts = {probe["verdict"] for probe in probes}
            self.assertTrue(verdicts <= {"identical", "DIFFERENT", "within_tolerance", "not_comparable"})

    def test_every_measured_difference_is_refused_or_repaired(self) -> None:
        """A construct a real destination computes differently must never be
        graded locally as if it agreed."""
        for destination in DESTINATIONS:
            for probe in _evidence(destination):
                if probe["verdict"] != "DIFFERENT":
                    continue
                with self.subTest(destination=destination, probe=probe["id"]):
                    repair = REPAIRED_BY_REWRITE.get((destination, probe["id"]))
                    try:
                        rewritten = rewrite_model_sql(
                            _model(probe["candidate_sql"]), Destination(destination)
                        )
                    except DbtPolicyFailure:
                        self.assertIsNone(
                            repair,
                            f"{probe['id']} is documented as repaired but is refused",
                        )
                        continue
                    self.assertIsNotNone(
                        repair,
                        f"{probe['id']} measured {probe['warehouse']!r} on {destination} but "
                        f"{probe['duckdb']!r} on the grading engine, and the subset still admits it",
                    )
                    self.assertIn(repair, rewritten)

    def test_every_agreeing_construct_stays_admitted(self) -> None:
        """The evidence is also a floor: a construct that agrees with EVERY
        measured destination must keep working, so a tightening cannot quietly
        remove a usable one.

        The subset is one shared surface, so a construct is legitimately
        refused when ANY measured destination disagrees: Snowflake renders a
        double or a timestamp as text differently, which is why those casts are
        refused for Databricks too.
        """
        differs_somewhere = {
            probe["id"]
            for destination in ("snowflake", "databricks")
            for probe in _evidence(destination)
            if probe["verdict"] == "DIFFERENT"
        }
        expected_refusals = {
            # Databricks DATEDIFF with a unit counts whole elapsed units while
            # DuckDB counts boundary crossings; measured 0 against 1.
            ("databricks", "datediff_day_dates"),
            ("databricks", "datediff_hour_ts"),
            ("redshift", "datediff_day_dates"),
            ("redshift", "datediff_hour_ts"),
            # Redshift AVG over integers divides as integers: 1 against 1.5.
            ("redshift", "avg_of_ints"),
            # Redshift refuses to cast a boolean to text at all.
            ("redshift", "cast_bool_text"),
            ("redshift", "string_case_equality"),
            ("redshift", "ilike"),
        }
        for destination in DESTINATIONS:
            for probe in _evidence(destination):
                if probe["verdict"] != "identical" or probe["id"] in differs_somewhere:
                    continue
                with self.subTest(destination=destination, probe=probe["id"]):
                    try:
                        rewrite_model_sql(_model(probe["candidate_sql"]), Destination(destination))
                    except DbtPolicyFailure:
                        self.assertIn(
                            (destination, probe["id"]),
                            expected_refusals,
                            f"{probe['id']} agrees with {destination} but the subset refuses it",
                        )

    def test_redshift_specific_divergences_are_on_record(self) -> None:
        """Redshift was measured last and moved three rules on its own."""
        probes = {probe["id"]: probe for probe in _evidence("redshift")}
        self.assertEqual(probes["avg_of_ints"]["warehouse"], "1")
        self.assertEqual(probes["avg_of_ints"]["duckdb"], "1.5")
        # REGEXP_REPLACE replaces every match there and only the first on
        # DuckDB; the rewrite adds the global flag, so the probe now agrees.
        self.assertEqual(probes["regexp_replace_digits"]["verdict"], "identical")
        # DATE_TRUNC returns a TIMESTAMP for every operand, including a DATE.
        self.assertEqual(probes["date_trunc_month_date"]["verdict"], "identical")
        # Three engines, three answers for the sharp S.
        self.assertEqual(probes["upper_unicode"]["warehouse"], "STRAßE")
        self.assertEqual(
            {_evidence(name)[0]["id"] for name in DESTINATIONS}, {"md5_text_cast"}
        )

    def test_databricks_datediff_units_are_refused_with_evidence(self) -> None:
        """The reason is on record: two minutes apart across an hour boundary
        is 1 on DuckDB and 0 on Databricks."""
        probes = {probe["id"]: probe for probe in _evidence("databricks")}
        hour = probes["datediff_hour_ts"]
        self.assertEqual(hour["warehouse"], "0")
        self.assertEqual(hour["subset"], "dbt_compatibility_unsupported")
        snowflake = {probe["id"]: probe for probe in _evidence("snowflake")}["datediff_hour_ts"]
        self.assertEqual(snowflake["warehouse"], snowflake["duckdb"])
        self.assertEqual(snowflake["verdict"], "identical")

    def test_the_subset_version_moved_with_the_evidence(self) -> None:
        self.assertEqual(DBT_COMPATIBILITY_SUBSET_VERSION, "portable-dbt-sql-v3")


if __name__ == "__main__":
    unittest.main()

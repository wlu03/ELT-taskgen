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

These tests need no warehouse. They bind the POLICY to the EVIDENCE:

  * a construct a destination COMPUTES differently is refused or repaired;
  * a construct a destination REFUSES TO COMPILE is refused for that
    destination, so a solver cannot be rewarded for SQL its own warehouse
    would reject;
  * a construct that agrees everywhere stays admitted unless its refusal is
    recorded here with a reason;
  * every construct the subset admits has a probe behind it.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import sqlglot

from elt_taskgen.destinations import Destination
from elt_taskgen.training.dbt_runner import (
    DBT_COMPATIBILITY_SUBSET_VERSION,
    DbtPolicyFailure,
    _ALLOWED_AST_NODE_NAMES,
    rewrite_model_sql,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "warehouse_differential"
#: Every destination the evidence covers.
DESTINATIONS = ("snowflake", "databricks", "redshift")
SOURCE = "{{ source('raw', 't') }}"

#: Declared destination/probe rewrites. Empty because the current Databricks
#: DATE_TRUNC cast makes every probe agree; future repairs must be listed here.
REPAIRED_BY_REWRITE: dict[tuple[str, str], str] = {}

#: (destination, probe) -> why the subset refuses a construct the destination
#: agrees on. Every entry is a deliberate non-admission, not an oversight: no
#: task in the corpus uses any of them, and admitting one needs its own subset
#: version bump.
REFUSED_THOUGH_AGREED: dict[tuple[str, str], str] = {
    ("snowflake", "filter_clause_count"): (
        "Snowflake folds COUNT(*) FILTER (WHERE ...) into COUNT_IF, which the "
        "subset does not admit"
    ),
    ("snowflake", "bool_and_or_agg"): "BOOL_AND and BOOL_OR are not admitted",
    ("databricks", "bool_and_or_agg"): "BOOL_AND and BOOL_OR are not admitted",
    ("redshift", "bool_and_or_agg"): "BOOL_AND and BOOL_OR are not admitted",
    ("snowflake", "split_part_middle"): "SPLIT_PART is not admitted",
    ("databricks", "split_part_middle"): "SPLIT_PART is not admitted",
    ("redshift", "split_part_middle"): "SPLIT_PART is not admitted",
    ("snowflake", "json_extract_missing_key"): "JSON navigation over a literal is not admitted",
    ("databricks", "json_extract_missing_key"): "JSON navigation over a literal is not admitted",
    ("redshift", "json_extract_missing_key"): "JSON navigation over a literal is not admitted",
    ("snowflake", "json_array_element"): "JSON navigation over a literal is not admitted",
    ("databricks", "json_array_element"): "JSON navigation over a literal is not admitted",
    ("redshift", "json_array_element"): "JSON navigation over a literal is not admitted",
    ("databricks", "datediff_day_dates"): (
        "DATEDIFF with a unit counts whole elapsed units on Databricks and "
        "boundary crossings on DuckDB, so the spelling is refused for both "
        "units even where one agrees"
    ),
    ("redshift", "datediff_day_dates"): (
        "Redshift's DATEDIFF spelling carries the unit as a bare identifier, "
        "which the subset does not admit"
    ),
}


def _model(candidate_sql: str) -> str:
    return candidate_sql if " FROM " in candidate_sql else f"{candidate_sql} FROM {SOURCE}"


def _evidence(destination: str) -> list[dict]:
    document = json.loads((FIXTURES / f"differential_{destination}.json").read_text(encoding="utf-8"))
    return document["probes"]


class WarehouseDifferentialTests(unittest.TestCase):
    def test_every_destination_measured_the_same_probe_matrix(self) -> None:
        matrices = {}
        for destination in DESTINATIONS:
            probes = _evidence(destination)
            self.assertGreaterEqual(len(probes), 90, destination)
            verdicts = {probe["verdict"] for probe in probes}
            self.assertTrue(verdicts <= {"identical", "DIFFERENT", "within_tolerance", "not_comparable"})
            matrices[destination] = tuple(probe["id"] for probe in probes)
        self.assertEqual(len(set(matrices.values())), 1, "destinations measured different probes")

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
        } | set(REFUSED_THOUGH_AGREED)
        unusable = {
            (destination, probe["id"])
            for destination in DESTINATIONS
            for probe in _evidence(destination)
            if probe.get("warehouse_error")
        }
        for destination in DESTINATIONS:
            for probe in _evidence(destination):
                if probe["verdict"] != "identical" or probe["id"] in differs_somewhere:
                    continue
                if (destination, probe["id"]) in unusable:
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

    def test_a_construct_the_destination_cannot_compile_is_refused(self) -> None:
        """The sharpest failure this evidence can catch: the grading engine
        runs the SQL and rewards it while the solver's own warehouse rejects
        it outright. Anything the warehouse refused to compile must be refused
        by the subset for that destination."""
        checked = 0
        for destination in DESTINATIONS:
            for probe in _evidence(destination):
                if not probe.get("warehouse_error") or probe.get("duckdb") is None:
                    continue
                checked += 1
                with self.subTest(destination=destination, probe=probe["id"]):
                    with self.assertRaises(
                        DbtPolicyFailure,
                        msg=(
                            f"{destination} refused to compile {probe['id']} "
                            f"({probe['warehouse_error'][:80]}) but the subset admits it"
                        ),
                    ):
                        rewrite_model_sql(
                            _model(probe["candidate_sql"]), Destination(destination)
                        )
        self.assertGreater(checked, 0, "no destination refused any probe")

    def test_the_measured_availability_gaps_are_the_ones_on_record(self) -> None:
        """Name them, so a later collection that loses one is visible."""
        unusable = {
            destination: sorted(
                probe["id"]
                for probe in _evidence(destination)
                if probe.get("warehouse_error")
            )
            for destination in DESTINATIONS
        }
        self.assertEqual(unusable["snowflake"], [])
        self.assertEqual(
            unusable["databricks"], ["count_distinct_over", "dateadd_day", "to_char_month"]
        )
        # Redshift refuses to cast any boolean-valued expression to text, has
        # no aggregate FILTER clause, needs an explicit frame on an aggregate
        # window with an ORDER BY, and rejects a DISTINCT window aggregate.
        self.assertIn("filter_clause_count", unusable["redshift"])
        self.assertIn("window_sum_default_frame", unusable["redshift"])
        self.assertIn("last_value_default_frame", unusable["redshift"])
        self.assertIn("count_distinct_over", unusable["redshift"])
        self.assertIn("boolean_literal_text", unusable["redshift"])

    def test_the_window_constructs_the_corpus_relies_on_agree(self) -> None:
        """Half the tasks that carry reference SQL use a window function, and
        QUALIFY was the construct most likely to be missing. All three ran it
        and agreed with the grading engine."""
        for destination in DESTINATIONS:
            probes = {probe["id"]: probe for probe in _evidence(destination)}
            for probe_id in (
                "qualify_row_number",
                "row_number_null_rank",
                "row_number_desc_null_rank",
                "rank_versus_dense_rank",
                "lag_lead_edges",
                "window_rows_frame_explicit",
            ):
                with self.subTest(destination=destination, probe=probe_id):
                    self.assertEqual(probes[probe_id]["verdict"], "identical")
    def test_the_null_ordering_defaults_differ_and_the_rewrite_tracks_them(self) -> None:
        """Where an unordered NULL lands is not the same on every destination:
        Databricks ranks it first ascending, Snowflake and Redshift last. The
        rewrite reproduces each destination's own answer, which is what makes
        a window function gradeable locally at all."""
        ascending = {}
        descending = {}
        for destination in DESTINATIONS:
            probes = {probe["id"]: probe for probe in _evidence(destination)}
            ascending[destination] = probes["row_number_null_rank"]
            descending[destination] = probes["row_number_desc_null_rank"]
            for probe in (ascending[destination], descending[destination]):
                self.assertEqual(probe["warehouse"], probe["duckdb"], destination)
        self.assertEqual(ascending["snowflake"]["warehouse"], "2")
        self.assertEqual(ascending["redshift"]["warehouse"], "2")
        self.assertEqual(ascending["databricks"]["warehouse"], "1")
        self.assertEqual(descending["snowflake"]["warehouse"], "1")
        self.assertEqual(descending["redshift"]["warehouse"], "1")
        self.assertEqual(descending["databricks"]["warehouse"], "2")

    def test_the_subset_version_moved_with_the_evidence(self) -> None:
        self.assertEqual(DBT_COMPATIBILITY_SUBSET_VERSION, "portable-dbt-sql-v4")


class AdmittedSurfaceCoverageTests(unittest.TestCase):
    """Every construct the subset admits must have a probe behind it.

    Without this the probe matrix drifts away from the policy: the first three
    rounds measured MD5 eight ways while nothing exercised a window function,
    which half the tasks with reference SQL use.
    """

    #: Admitted node types no probe can produce, with the reason. Both are
    #: unreachable rather than unmeasured, so neither widens the surface.
    UNREACHABLE = {
        "When": (
            "sqlglot emits When only for MERGE branches, and every mutating "
            "statement is refused before the node check runs"
        ),
        "Anonymous": (
            "under the pinned sqlglot every name in _ALLOWED_ANONYMOUS_FUNCTIONS "
            "parses into a modelled node in all three destination dialects, so "
            "the Anonymous branch never fires"
        ),
    }

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(FIXTURES))
        from probes import PROBES, candidate_sql

        cls.probes = PROBES
        cls.exercised = {}
        for destination in DESTINATIONS:
            names = set()
            for probe in PROBES:
                try:
                    tree = sqlglot.parse_one(candidate_sql(probe, destination), read=destination)
                except Exception:  # noqa: BLE001 - an unparsable rendering covers nothing
                    continue
                names |= {type(node).__name__ for node in tree.walk()}
            cls.exercised[destination] = names

    def test_every_admitted_construct_is_exercised_by_a_probe(self) -> None:
        union = set().union(*self.exercised.values())
        missing = sorted(name for name in _ALLOWED_AST_NODE_NAMES if name not in union)
        self.assertEqual(
            missing,
            sorted(self.UNREACHABLE),
            "admitted constructs with no measured evidence; add a probe or "
            "record why the node is unreachable",
        )

    def test_the_unreachable_constructs_really_are_unreachable(self) -> None:
        for name in self.UNREACHABLE:
            with self.subTest(node=name):
                for destination in DESTINATIONS:
                    self.assertNotIn(name, self.exercised[destination])

    def test_each_destination_carries_most_of_the_surface(self) -> None:
        """A dialect legitimately spells some constructs into other nodes, so
        per-destination coverage is not total; it must not collapse either."""
        for destination in DESTINATIONS:
            with self.subTest(destination=destination):
                covered = len(_ALLOWED_AST_NODE_NAMES & self.exercised[destination])
                self.assertGreaterEqual(covered, 85, f"{destination} exercises only {covered}")

    def test_every_probe_is_rendered_for_every_destination(self) -> None:
        from probes import candidate_sql

        for probe in self.probes:
            for destination in DESTINATIONS:
                with self.subTest(probe=probe["id"], destination=destination):
                    self.assertTrue(candidate_sql(probe, destination).startswith("SELECT "))

    def test_the_probe_matrix_matches_the_recorded_evidence(self) -> None:
        recorded = {probe["id"] for probe in _evidence("snowflake")}
        self.assertEqual({probe["id"] for probe in self.probes}, recorded)


if __name__ == "__main__":
    unittest.main()

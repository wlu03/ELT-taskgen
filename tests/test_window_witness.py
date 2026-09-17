"""Row P, the period-ordered mirror of row A, and the rule that a wrong_window
claim ships with a winner-first mirror witness.

The batch20 leak (schemapile esse4): latest_snapshot claimed wrong_window with
witnesses that all listed the ordered winner LAST, so an unordered window that
happened to emit the last-arrived row reproduced gold on the counterfactual.
Whether it did depended on DuckDB's join orientation, which flipped between the
counterfactual and stress populations. The bar here is execution under BOTH
scan orders: the mutant must lose on the counterfactual whether the rows arrive
as listed or reversed.
"""
from __future__ import annotations

import unittest
from unittest import mock

import duckdb

from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.generation import populations as pops
from elt_taskgen.generation.source_data import generate_rows
from elt_taskgen.models import PopulationName
from elt_taskgen.reference import solution as ref
from elt_taskgen.verification import attacks, upstream_eval
from tests.test_plan_library import EVIDENCE, TABLES, RELATIONSHIPS, _fetch, build_task


def _load_ordered(con, task, rows, *, reverse: bool) -> None:
    for table in task.tables:
        ref.create_table(con, table)
        payload = list(rows.get(table.name) or [])
        if reverse:
            payload.reverse()
        if payload:
            ref._insert_rows(con, table, payload)


class RowPTests(unittest.TestCase):
    def test_latest_snapshot_declares_the_period_mirror(self) -> None:
        built = mp.latest_snapshot(EVIDENCE, mart="snapshot_mart")
        self.assertIn(mp.WITNESS_LATEST_FIRST, built.shape.witnesses)
        self.assertIn("wrong_window", built.shape.attack_claims)
        self.assertIsNone(
            mp.wrong_window_mirror_problem(built.shape.attack_claims, built.shape.witnesses)
        )

    def test_a_wrong_window_claim_without_a_mirror_is_refused(self) -> None:
        problem = mp.wrong_window_mirror_problem(
            ("inner_join", "wrong_window"), (mp.WITNESS_CONTROL, mp.WITNESS_SECOND_PERIOD)
        )
        self.assertIsNotNone(problem)
        self.assertIn("mirror", problem)
        self.assertIsNone(mp.wrong_window_mirror_problem(("inner_join",), ()))
        self.assertIsNone(
            mp.wrong_window_mirror_problem(("wrong_window@drop_frame",), (mp.WITNESS_TIE,))
        )
        # The guard is applied by build_rollup itself: a library shape whose
        # claim the guard rejects cannot be built.
        with mock.patch.object(mp, "wrong_window_mirror_problem", return_value="no mirror"):
            with self.assertRaisesRegex(ValueError, "unbacked attack claim"):
                mp.latest_snapshot(EVIDENCE, mart="unbacked")

    def test_row_p_lists_the_later_period_first_and_mirrors_row_a(self) -> None:
        built = mp.latest_snapshot(EVIDENCE, mart="snapshot_mart")
        rows = pops.witness_literal_rows(TABLES, RELATIONSHIPS, built.shape)
        shape = built.shape
        anchor_table, anchor_key, bridge_table, bridge_fk = shape.witness_anchor()
        ordered = pops._effective_witnesses(TABLES, shape, RELATIONSHIPS)
        anchors = rows[anchor_table]
        by_witness = dict(zip(ordered, anchors))
        period = shape.roles.period_column
        bridge_rows = rows[bridge_table]

        def pair(witness):
            key = by_witness[witness][anchor_key]
            return [r for r in bridge_rows if r[bridge_fk] == key]

        control = pair(mp.WITNESS_CONTROL)
        latest_first = pair(mp.WITNESS_LATEST_FIRST)
        self.assertEqual(2, len(control))
        self.assertEqual(2, len(latest_first))
        self.assertLess(control[0][period], control[1][period])  # winner listed last
        self.assertGreater(latest_first[0][period], latest_first[1][period])  # winner listed first
        prose = pops.witness_conditions(shape, tables=TABLES, relationships=RELATIONSHIPS)
        self.assertTrue(any("row P" in line for line in prose))

    def test_wrong_window_loses_on_the_counterfactual_under_both_scan_orders(self) -> None:
        built = mp.latest_snapshot(EVIDENCE, mart="snapshot_mart")
        task = build_task(built, "proof__snapshot_both_orders")
        mart = task.marts[0]
        sql = ref.compile_plan_sql(task, mart)
        cols = tuple(c.name for c in mart.columns)
        kind, variant = attacks.split_kind_directive("wrong_window")
        mutant = attacks._apply_kind(kind, sql, mart, variant)
        self.assertIsNotNone(mutant)
        rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
        for reverse in (False, True):
            with self.subTest(scan_order="reversed" if reverse else "as listed"):
                con = duckdb.connect(":memory:")
                try:
                    _load_ordered(con, task, rows, reverse=reverse)
                    gold_rows = _fetch(con, sql)
                    gold_csv = upstream_eval.rows_to_canonical_csv(gold_rows, cols)
                    self.assertTrue(upstream_eval.compare_mart(gold_csv, gold_rows, mart))
                    actual = _fetch(con, mutant)
                    self.assertFalse(
                        upstream_eval.compare_mart(gold_csv, actual, mart),
                        "the ORDER-BY-stripped window reproduced gold on the counterfactual",
                    )
                finally:
                    con.close()

    def test_the_verifier_refuses_a_lost_or_reordered_pair(self) -> None:
        a1, a2 = {"fk": 1, "p": 1}, {"fk": 1, "p": 2}
        p1, p2 = {"fk": 2, "p": 2}, {"fk": 2, "p": 1}
        ok = {"bridge": [a1, a2, p1, p2]}
        pops._verify_window_order_witnesses(
            ok, bridge_table="bridge", bridge_fk="fk", period_column="p",
            control_group=(a1, a2), latest_first_group=(p1, p2),
        )
        with self.assertRaisesRegex(ValueError, "lost one of its two rows"):
            pops._verify_window_order_witnesses(
                {"bridge": [a1, p1, p2]}, bridge_table="bridge", bridge_fk="fk", period_column="p",
                control_group=(a1, a2), latest_first_group=(p1, p2),
            )
        with self.assertRaisesRegex(ValueError, "listing order"):
            pops._verify_window_order_witnesses(
                {"bridge": [a1, a2, p2, p1]}, bridge_table="bridge", bridge_fk="fk", period_column="p",
                control_group=(a1, a2), latest_first_group=(p1, p2),
            )
        split = {"bridge": [a1, a2, p1, {"fk": 3, "p": 1}]}
        with self.assertRaisesRegex(ValueError, "split across parents"):
            pops._verify_window_order_witnesses(
                split, bridge_table="bridge", bridge_fk="fk", period_column="p",
                control_group=(a1, a2), latest_first_group=(p1, split["bridge"][3]),
            )


if __name__ == "__main__":
    unittest.main()

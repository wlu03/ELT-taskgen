"""Compatibility contract for the extracted witness identifier package."""

from __future__ import annotations

import unittest

from elt_taskgen.generation import mart_plan, populations, witness
from elt_taskgen.generation.witness import constants


WITNESS_IDENTIFIERS = (
    ("WITNESS_CONTROL", "A_control"),
    ("WITNESS_CHILDLESS", "B_childless"),
    ("WITNESS_DUPLICATE", "C_duplicate"),
    ("WITNESS_SAME_CHILD", "D_same_child"),
    ("WITNESS_ALL_FAIL", "E_all_fail"),
    ("WITNESS_TIE", "F_tie"),
    ("WITNESS_OUT_OF_DOMAIN", "G_out_of_domain"),
    ("WITNESS_ON_THRESHOLD", "H_on_threshold"),
    ("WITNESS_BRIDGE_NO_CHILD", "I_bridge_no_child"),
    ("WITNESS_SECOND_PERIOD", "J_second_period"),
    ("WITNESS_BELOW_THRESHOLD", "K_below_threshold"),
    ("WITNESS_NULL_MEASURE", "L_null_measure"),
    ("WITNESS_ARGMAX_CASE_ORDER", "M_argmax_case_order"),
    ("WITNESS_ARGMAX_NULL_ORDER", "N_argmax_null_order"),
    ("WITNESS_DISTINCT_MEASURE", "O_distinct_measure"),
    ("WITNESS_LATEST_FIRST", "P_latest_first"),
)
PUBLIC_NAMES = tuple(name for name, _value in WITNESS_IDENTIFIERS) + (
    "WITNESS_ORDER",
    "WINDOW_MIRROR_WITNESSES",
)


class WitnessPackageCompatibilityTest(unittest.TestCase):
    def test_identifiers_and_order_are_stable(self) -> None:
        expected = tuple(value for _name, value in WITNESS_IDENTIFIERS)
        self.assertEqual(expected, witness.WITNESS_ORDER)
        self.assertEqual(len(expected), len(set(expected)))
        for name, value in WITNESS_IDENTIFIERS:
            with self.subTest(witness=name):
                self.assertEqual(value, getattr(witness, name))
        self.assertEqual(
            frozenset({witness.WITNESS_TIE, witness.WITNESS_LATEST_FIRST}),
            witness.WINDOW_MIRROR_WITNESSES,
        )
        self.assertEqual(PUBLIC_NAMES, tuple(constants.__all__))
        self.assertEqual(PUBLIC_NAMES, tuple(witness.__all__))

    def test_compatibility_surfaces_reexport_canonical_objects(self) -> None:
        for name in PUBLIC_NAMES:
            with self.subTest(export=name):
                self.assertIs(getattr(constants, name), getattr(witness, name))
                self.assertIs(getattr(mart_plan, name), getattr(witness, name))
        for name, _value in WITNESS_IDENTIFIERS:
            with self.subTest(population_import=name):
                self.assertIs(getattr(populations, name), getattr(witness, name))
        self.assertIs(populations.WITNESS_ORDER, witness.WITNESS_ORDER)

    def test_every_identifier_has_validation_and_prose_metadata(self) -> None:
        expected = set(witness.WITNESS_ORDER)
        self.assertEqual(expected, set(mart_plan._WITNESS_REQUIREMENTS))
        self.assertEqual(expected, set(populations._WITNESS_PROSE))


if __name__ == "__main__":
    unittest.main()

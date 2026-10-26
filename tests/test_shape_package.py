"""Compatibility contract for the extracted mart-plan shape package."""

from __future__ import annotations

import unittest

from elt_taskgen.generation import mart_plan
from elt_taskgen.generation import shapes


SHAPE_NAMES = (
    "fan_out_rollup",
    "aggregate_then_filter",
    "status_cohort_union",
    "temporal_grid",
    "latest_snapshot",
    "categorical_ladder",
    "measure_state_distribution",
    "argmax_profile",
    "orphan_coverage",
)


class ShapePackageCompatibilityTest(unittest.TestCase):
    def test_mart_plan_aliases_are_package_exports(self) -> None:
        for name in SHAPE_NAMES:
            with self.subTest(shape=name):
                self.assertIs(getattr(mart_plan, name), getattr(shapes, name))

    def test_registry_preserves_shape_policy_order(self) -> None:
        self.assertEqual(
            SHAPE_NAMES,
            tuple(registration.shape_name for registration in mart_plan.SHAPE_REGISTRY),
        )
        self.assertEqual(
            tuple((name, getattr(shapes, name)) for name in SHAPE_NAMES),
            mart_plan.registered_shape_builders(),
        )


if __name__ == "__main__":
    unittest.main()

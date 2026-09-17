"""Policy-v4 row-A coverage for nullable argmax labels."""

from __future__ import annotations

import unittest

from elt_taskgen.generation import mart_plan, populations
from elt_taskgen.models import (
    ColumnSpec,
    ColumnType,
    PopulationName,
    Relationship,
    TableSpec,
)


def fixture(nullable: bool):
    vessels = TableSpec(
        name="vessels",
        columns=(ColumnSpec(name="vessel_id", type=ColumnType.INTEGER),),
        primary_key=("vessel_id",),
    )
    landings = TableSpec(
        name="landings",
        columns=(
            ColumnSpec(name="landing_id", type=ColumnType.INTEGER),
            ColumnSpec(name="vessel_id", type=ColumnType.INTEGER),
            ColumnSpec(name="year", type=ColumnType.INTEGER),
            ColumnSpec(
                name="landing_method",
                type=ColumnType.TEXT,
                nullable=nullable,
            ),
        ),
        primary_key=("landing_id",),
    )
    relationship = Relationship(
        child_table="landings",
        child_columns=("vessel_id",),
        parent_table="vessels",
        parent_columns=("vessel_id",),
        required=True,
    )
    shape = mart_plan.StarShape(
        mart="vessels_landings_distribution",
        parent="vessels",
        parent_keys=("vessel_id",),
        key_columns=("vessel_id",),
        fact="landings",
        fact_link_columns=("vessel_id",),
        has_join=True,
        ranked_measures=("top_label",),
        witnesses=(mart_plan.WITNESS_CONTROL,),
        roles=mart_plan.FactRoles(
            link_key="landing_id",
            measure="year",
            label="landing_method",
        ),
    )
    return (vessels, landings), (relationship,), shape


class NullableArgmaxWitnessTest(unittest.TestCase):
    def test_nullable_label_places_null_on_higher_measure_winner_and_documents_it(self) -> None:
        tables, relationships, shape = fixture(nullable=True)
        rows = populations.witness_literal_rows(tables, relationships, shape)["landings"]
        self.assertEqual([row["year"] for row in rows], [10, 40])
        self.assertEqual(
            [row["landing_method"] for row in rows],
            ["alpha", None],
        )
        conditions = populations.witness_conditions(shape, tables=tables)
        scope = (
            "WITNESS SCOPE [mart=vessels_landings_distribution; shape=star; "
            "anchor=vessels; bridge=landings; child=(none)]: "
        )
        self.assertTrue(all(line.startswith(scope) for line in conditions))
        self.assertTrue(
            any(
                "HIGHER-measure winning row carries NULL in landing_method" in condition
                for condition in conditions
            )
        )

        derived, _attacks = populations.derive_populations_and_attacks(
            task_id="synsql__nullable_argmax_fixture",
            tables=tables,
            relationships=relationships,
            shapes=(shape,),
            scale_hint={"vessels": 10, "landings": 20},
        )
        counterfactual = next(
            spec for spec in derived if spec.name is PopulationName.COUNTERFACTUAL
        )
        self.assertIsNone(counterfactual.literal_rows["landings"][1]["landing_method"])
        self.assertIn(conditions[-1], counterfactual.conditions)

    def test_nonnullable_label_keeps_original_bytes_and_no_nullable_claim(self) -> None:
        tables, relationships, shape = fixture(nullable=False)
        rows = populations.witness_literal_rows(tables, relationships, shape)["landings"]
        self.assertEqual([row["year"] for row in rows], [10, 40])
        self.assertEqual(
            [row["landing_method"] for row in rows],
            ["alpha", "beta"],
        )
        conditions = populations.witness_conditions(shape, tables=tables)
        self.assertFalse(any("nullable-label boundary" in line for line in conditions))
        # Omitting tables is the compatibility form: shape-only callers keep
        # the pre-v4 condition set because nullability is unknowable there.
        self.assertEqual(
            populations.witness_conditions(shape),
            populations.witness_conditions(shape, tables=()),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

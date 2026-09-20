"""Executable coverage for the two contract-sensitive argmax label orders."""

from __future__ import annotations

import unittest

from elt_taskgen.generation import mart_plan, populations
from elt_taskgen.models import ColumnSpec, ColumnType, PopulationName, Relationship, TableSpec
from elt_taskgen.reference import solution as reference
from tests.test_nullable_measure_witness import (
    fixture,
    run_counterfactual_sql,
    task_for,
)


def _group_with_labels(rows, labels: tuple[object, object]) -> int:
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["account_id"], []).append(row)
    wanted = sorted(labels, key=lambda value: (value is None, str(value)))
    matches = [
        key
        for key, group in grouped.items()
        if len(group) == 2
        and len({row["amount"] for row in group}) == 1
        and sorted(
            (row["event_label"] for row in group),
            key=lambda value: (value is None, str(value)),
        )
        == wanted
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one group for labels {labels!r}, got {matches!r}")
    return matches[0]


def _with_label_dimension(*, required: bool):
    tables, relationships, evidence = fixture(False, label_nullable=True)
    labels = TableSpec(
        name="event_labels",
        columns=(ColumnSpec(name="label", type=ColumnType.TEXT),),
        primary_key=("label",),
    )
    label_relationship = Relationship(
        child_table="events",
        child_columns=("event_label",),
        parent_table="event_labels",
        parent_columns=("label",),
        required=required,
    )
    return tables + (labels,), relationships + (label_relationship,), evidence


class ArgmaxOrderWitnessTest(unittest.TestCase):
    def test_reference_and_order_mutants_choose_different_winners(self) -> None:
        tables, relationships, evidence = fixture(False, label_nullable=True)
        built = mart_plan.argmax_profile(evidence, mart="argmax_order_contract")
        task = task_for(
            built,
            tables,
            relationships,
            "proof__argmax_order_contract",
        )
        self.assertEqual(populations.validate_population_coverage(task), [])

        counterfactual = task.population(PopulationName.COUNTERFACTUAL)
        events = counterfactual.literal_rows["events"]
        case_parent = _group_with_labels(events, ("Zeta", "alpha"))
        null_parent = _group_with_labels(events, ("kept", None))
        self.assertEqual(
            len({row["event_id"] for row in events}),
            len(events),
            "primary-key enforcement must retain every witness row",
        )

        sql = reference.compile_plan_sql(task, task.marts[0])
        order = '"f_label" ASC NULLS LAST'
        self.assertEqual(sql.count(order), 1)
        key = built.column_named("parent_key")
        label = built.column_named("top_label")
        correct = {row[key]: row for row in run_counterfactual_sql(task, sql)}
        lower_mutant = {
            row[key]: row
            for row in run_counterfactual_sql(
                task,
                sql.replace(order, 'LOWER("f_label") ASC NULLS LAST'),
            )
        }
        nulls_first_mutant = {
            row[key]: row
            for row in run_counterfactual_sql(
                task,
                sql.replace(order, '"f_label" ASC NULLS FIRST'),
            )
        }

        self.assertEqual(correct[case_parent][label], "Zeta")
        self.assertEqual(lower_mutant[case_parent][label], "alpha")
        self.assertEqual(correct[null_parent][label], "kept")
        self.assertEqual(nulls_first_mutant[null_parent][label], "(none)")

        scope = populations._witness_scope_prefix(built.shape)
        order_conditions = tuple(
            condition
            for condition in counterfactual.conditions
            if "case-order" in condition or "NULL-order" in condition
        )
        self.assertTrue(order_conditions)
        self.assertTrue(all(condition.startswith(scope) for condition in order_conditions))
        self.assertTrue(any("LOWER(event_label)" in line for line in order_conditions))
        self.assertTrue(any("ASC NULLS FIRST" in line for line in order_conditions))

        regenerated, _attacks = populations.derive_populations_and_attacks(
            task_id=task.task_id,
            tables=tables,
            relationships=relationships,
            shapes=(built.shape,),
            scale_hint={"accounts": 10, "events": 20},
        )
        self.assertEqual(task.populations, regenerated)

    def test_nonnullable_label_activates_case_but_not_null_order(self) -> None:
        tables, relationships, evidence = fixture(False, label_nullable=False)
        built = mart_plan.argmax_profile(evidence, mart="strict_label_argmax")
        rows = populations.witness_literal_rows(tables, relationships, built.shape)
        events = rows["events"]
        _group_with_labels(events, ("Zeta", "alpha"))
        self.assertFalse(any(row["event_label"] is None for row in events))
        conditions = populations.witness_conditions(
            built.shape,
            tables=tables,
            relationships=relationships,
        )
        self.assertTrue(any("ARGMAX-H case-order" in line for line in conditions))
        self.assertFalse(any("ARGMAX-J NULL-order" in line for line in conditions))

    def test_optional_label_fk_is_closed_without_erasing_either_witness(self) -> None:
        tables, relationships, evidence = _with_label_dimension(required=False)
        built = mart_plan.argmax_profile(evidence, mart="optional_label_fk_argmax")
        task = task_for(
            built,
            tables,
            relationships,
            "proof__optional_label_fk_argmax",
        )
        self.assertEqual(populations.validate_population_coverage(task), [])
        counterfactual = task.population(PopulationName.COUNTERFACTUAL)
        events = counterfactual.literal_rows["events"]
        _group_with_labels(events, ("Zeta", "alpha"))
        _group_with_labels(events, ("kept", None))
        parent_labels = {
            row["label"] for row in counterfactual.literal_rows["event_labels"]
        }
        self.assertTrue(
            all(
                row["event_label"] is None or row["event_label"] in parent_labels
                for row in events
            )
        )

    def test_required_label_fk_suppresses_only_illegal_null_witness(self) -> None:
        tables, relationships, evidence = _with_label_dimension(required=True)
        built = mart_plan.argmax_profile(evidence, mart="required_label_fk_argmax")
        task = task_for(
            built,
            tables,
            relationships,
            "proof__required_label_fk_argmax",
        )
        self.assertEqual(populations.validate_population_coverage(task), [])
        counterfactual = task.population(PopulationName.COUNTERFACTUAL)
        events = counterfactual.literal_rows["events"]
        _group_with_labels(events, ("Zeta", "alpha"))
        self.assertFalse(any(row["event_label"] is None for row in events))
        self.assertFalse(
            any("ARGMAX-J NULL-order" in line for line in counterfactual.conditions)
        )
        parent_labels = {
            row["label"] for row in counterfactual.literal_rows["event_labels"]
        }
        self.assertTrue(all(row["event_label"] in parent_labels for row in events))

    def test_two_marts_keep_every_order_condition_in_its_own_scope(self) -> None:
        tables, relationships, evidence = fixture(False, label_nullable=True)
        first = mart_plan.argmax_profile(evidence, mart="first_argmax")
        second = mart_plan.argmax_profile(evidence, mart="second_argmax")
        generated, _attacks = populations.derive_populations_and_attacks(
            task_id="proof__two_scoped_argmax_marts",
            tables=tables,
            relationships=relationships,
            shapes=(first.shape, second.shape),
            scale_hint={"accounts": 10, "events": 20},
        )
        counterfactual = next(
            spec for spec in generated if spec.name is PopulationName.COUNTERFACTUAL
        )
        for shape in (first.shape, second.shape):
            scope = populations._witness_scope_prefix(shape)
            lines = tuple(
                line
                for line in counterfactual.conditions
                if line.startswith(scope) and "ARGMAX" in line
            )
            self.assertEqual(len(lines), 4)
            self.assertTrue(any("case-order values" in line for line in lines))
            self.assertTrue(any("NULL-order values" in line for line in lines))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

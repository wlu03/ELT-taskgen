from __future__ import annotations

import unittest
from unittest.mock import patch

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.generation.challenging_policy import (
    SemanticCoveragePolicy,
    validate_challenging_cohort,
)
from elt_taskgen.models import (
    ColumnType,
    MartColumn,
    MartColumnKind,
    SemanticPattern,
)


def _deep_classified_task():
    task = demo_task()
    mart = task.marts[0]
    columns = tuple(
        column.model_copy(update={"kind": MartColumnKind.PASSTHROUGH})
        for column in mart.columns
    ) + tuple(
        MartColumn(
            name=f"policy_extra_{index}",
            type=ColumnType.BIGINT,
            description="Additional classified policy-test column.",
            kind=MartColumnKind.DERIVED,
        )
        for index in range(3)
    )
    mart = mart.model_copy(update={"columns": columns})
    return task.model_copy(update={"marts": (mart,)})


def _rename_single_mart(task, name: str):
    mart = task.marts[0]
    plan = mart.plan.model_copy(update={"mart": name})
    renamed = mart.model_copy(update={"name": name, "plan": plan})
    return task.model_copy(update={"marts": (renamed,)})


def _tag_single_mart(task, template_id: str, *patterns: SemanticPattern):
    mart = task.marts[0]
    plan = mart.plan.model_copy(
        update={
            "template_id": template_id,
            "semantic_patterns": tuple(
                sorted(patterns, key=lambda pattern: pattern.value)
            ),
        }
    )
    return task.model_copy(update={"marts": (mart.model_copy(update={"plan": plan}),)})


class ChallengingCohortPolicyTests(unittest.TestCase):
    def test_empty_cohort_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no tasks were provided"):
            validate_challenging_cohort(())

    def test_rejects_shallow_and_unclassified_marts(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "3 mart columns") as caught:
            validate_challenging_cohort((("dbt_01", demo_task()),))
        self.assertIn("unclassified mart columns", str(caught.exception))
        self.assertIn("before reference work", str(caught.exception))

    def test_rejects_multi_mart_task_that_repeats_one_template(self) -> None:
        task = _deep_classified_task()
        second = _rename_single_mart(task, "repeated_summary").marts[0]
        task = task.model_copy(update={"marts": task.marts + (second,)})
        with self.assertRaisesRegex(
            RuntimeError, "all 2 marts repeat one plan-template signature"
        ):
            validate_challenging_cohort((("dbt_01", task),))

    def test_exactly_twenty_five_percent_template_share_is_allowed(self) -> None:
        tasks = tuple(
            (
                f"task_{index}",
                _rename_single_mart(_deep_classified_task(), f"mart_{index}"),
            )
            for index in range(4)
        )
        signatures = {f"mart_{index}": f"shape_{index}" for index in range(4)}
        with patch(
            "elt_taskgen.generation.challenging_policy.plan_template_signature",
            side_effect=lambda plan: signatures[plan.mart],
        ):
            report = validate_challenging_cohort(tasks)
        self.assertEqual(report.mart_count, 4)
        self.assertEqual(report.distinct_template_count, 4)
        self.assertEqual(report.largest_template_share, 0.25)

    def test_rejects_template_above_twenty_five_percent_of_corpus(self) -> None:
        tasks = tuple(
            (
                f"task_{index}",
                _rename_single_mart(_deep_classified_task(), f"mart_{index}"),
            )
            for index in range(5)
        )
        signatures = {
            "mart_0": "repeated",
            "mart_1": "repeated",
            "mart_2": "shape_2",
            "mart_3": "shape_3",
            "mart_4": "shape_4",
        }
        with patch(
            "elt_taskgen.generation.challenging_policy.plan_template_signature",
            side_effect=lambda plan: signatures[plan.mart],
        ), self.assertRaisesRegex(
            RuntimeError, "2/5 marts, exceeding the 25% cap"
        ):
            validate_challenging_cohort(tasks)

    def test_configured_semantic_coverage_is_returned_on_success(self) -> None:
        tasks = tuple(
            (
                f"task_{index}",
                _tag_single_mart(
                    _rename_single_mart(
                        _deep_classified_task().model_copy(
                            update={"task_id": f"demo__policy_{index}"}
                        ),
                        f"mart_{index}",
                    ),
                    "conditional_pivot",
                    SemanticPattern.PIVOT,
                ),
            )
            for index in range(4)
        )
        policy = SemanticCoveragePolicy(
            known_template_ids=("conditional_pivot",),
            known_patterns=(SemanticPattern.PIVOT,),
            require_explicit_template_id=True,
            require_explicit_patterns=True,
            minimum_pattern_marts=((SemanticPattern.PIVOT, 4),),
            minimum_pattern_tasks=((SemanticPattern.PIVOT, 4),),
        )
        signatures = {f"mart_{index}": f"shape_{index}" for index in range(4)}
        with patch(
            "elt_taskgen.generation.challenging_policy.plan_template_signature",
            side_effect=lambda plan: signatures[plan.mart],
        ):
            report = validate_challenging_cohort(tasks, coverage_policy=policy)

        self.assertEqual(report.semantic_coverage.patterns[0].name, "pivot")
        self.assertEqual(report.semantic_coverage.patterns[0].mart_count, 4)
        self.assertEqual(report.semantic_coverage.patterns[0].task_count, 4)
        self.assertEqual(report.semantic_coverage.deficits, ())

    def test_configured_semantic_coverage_fails_closed(self) -> None:
        tasks = tuple(
            (
                f"task_{index}",
                _rename_single_mart(
                    _deep_classified_task().model_copy(
                        update={"task_id": f"demo__missing_policy_{index}"}
                    ),
                    f"mart_{index}",
                ),
            )
            for index in range(4)
        )
        policy = SemanticCoveragePolicy(
            known_template_ids=("conditional_pivot",),
            known_patterns=(SemanticPattern.PIVOT,),
            require_explicit_template_id=True,
            require_explicit_patterns=True,
            minimum_pattern_marts=((SemanticPattern.PIVOT, 1),),
        )
        signatures = {f"mart_{index}": f"shape_{index}" for index in range(4)}
        with patch(
            "elt_taskgen.generation.challenging_policy.plan_template_signature",
            side_effect=lambda plan: signatures[plan.mart],
        ), self.assertRaisesRegex(
            RuntimeError, "missing semantic_patterns"
        ) as caught:
            validate_challenging_cohort(tasks, coverage_policy=policy)

        self.assertIn("missing template_id", str(caught.exception))
        self.assertIn("pattern 'pivot' has 0 mart owners", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

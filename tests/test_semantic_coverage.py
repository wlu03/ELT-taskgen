from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.generation.coverage import (
    MISSING_TEMPLATE_ID,
    SemanticCoveragePolicy,
    measure_semantic_coverage,
    write_semantic_coverage,
)
from elt_taskgen.models import SemanticPattern, sha256_hex


def _tagged_task(
    task_id: str,
    *,
    mart_names: tuple[str, ...],
    template_id: str,
    patterns: tuple[SemanticPattern, ...],
):
    task = demo_task()
    base = task.marts[0]
    marts = []
    for mart_name in mart_names:
        plan = base.plan.model_copy(
            update={
                "mart": mart_name,
                "template_id": template_id,
                "semantic_patterns": tuple(
                    sorted(patterns, key=lambda pattern: pattern.value)
                ),
            }
        )
        marts.append(base.model_copy(update={"name": mart_name, "plan": plan}))
    return task.model_copy(
        update={
            "task_id": task_id,
            "cluster_id": f"cluster_{task_id}",
            "marts": tuple(marts),
        }
    )


class SemanticCoveragePolicyTests(unittest.TestCase):
    def test_policy_is_canonical_and_rejects_invalid_configuration(self) -> None:
        left = SemanticCoveragePolicy(
            known_patterns=(
                SemanticPattern.WEIGHTED_AVERAGE,
                SemanticPattern.PIVOT,
            ),
            minimum_pattern_marts=((SemanticPattern.PIVOT, 2),),
        )
        right = SemanticCoveragePolicy(
            known_patterns=(
                SemanticPattern.PIVOT.value,
                SemanticPattern.WEIGHTED_AVERAGE.value,
            ),
            minimum_pattern_marts=((SemanticPattern.PIVOT.value, 2),),
        )
        self.assertEqual(left.canonical_contract(), right.canonical_contract())
        self.assertEqual(left.digest, right.digest)

        with self.assertRaisesRegex(ValueError, "duplicate names"):
            SemanticCoveragePolicy(known_patterns=("pivot", "pivot"))
        with self.assertRaisesRegex(ValueError, "outside the configured vocabulary"):
            SemanticCoveragePolicy(
                known_patterns=("pivot",),
                minimum_pattern_marts=(("weighted_average", 1),),
            )
        with self.assertRaisesRegex(ValueError, "positive minimums"):
            SemanticCoveragePolicy(minimum_pattern_tasks=(("pivot", 0),))

    def test_measurement_counts_mart_and_distinct_task_owners(self) -> None:
        first = _tagged_task(
            "demo__coverage_first",
            mart_names=("pivot_one", "pivot_two"),
            template_id="conditional_pivot",
            patterns=(SemanticPattern.PIVOT,),
        )
        second = _tagged_task(
            "demo__coverage_second",
            mart_names=("pivot_three",),
            template_id="conditional_pivot",
            patterns=(SemanticPattern.PIVOT,),
        )
        entries = (("second", second), ("first", first))

        report = measure_semantic_coverage(entries)

        self.assertEqual(report.canonical_contract()["schema_version"], "1.0.0")
        self.assertEqual(report.task_count, 2)
        self.assertEqual(report.mart_count, 3)
        self.assertEqual(len(report.templates), 1)
        self.assertEqual(report.templates[0].name, "conditional_pivot")
        self.assertEqual(report.templates[0].mart_count, 3)
        self.assertEqual(report.templates[0].task_count, 2)
        self.assertEqual(report.patterns[0].name, "pivot")
        self.assertEqual(report.patterns[0].mart_count, 3)
        self.assertEqual(report.patterns[0].task_count, 2)
        self.assertEqual(report.patterns[0].mart_owners, (
            "first/pivot_one",
            "first/pivot_two",
            "second/pivot_three",
        ))
        self.assertEqual(report.problems, ())

        reversed_report = measure_semantic_coverage(tuple(reversed(entries)))
        self.assertEqual(
            report.to_canonical_json(), reversed_report.to_canonical_json()
        )
        self.assertEqual(report.digest, reversed_report.digest)

    def test_strict_policy_reports_missing_and_unknown_tags(self) -> None:
        missing = demo_task().model_copy(update={"task_id": "demo__missing_tags"})
        unknown = _tagged_task(
            "demo__unknown_tags",
            mart_names=("unknown",),
            template_id="unregistered_template",
            patterns=(SemanticPattern.WEIGHTED_AVERAGE,),
        )
        policy = SemanticCoveragePolicy(
            known_template_ids=("conditional_pivot",),
            known_patterns=(SemanticPattern.PIVOT,),
            require_explicit_template_id=True,
            require_explicit_patterns=True,
        )

        report = measure_semantic_coverage(
            (("missing", missing), ("unknown", unknown)), policy=policy
        )

        self.assertTrue(
            any("missing template_id" in problem for problem in report.problems)
        )
        self.assertTrue(
            any("missing semantic_patterns" in problem for problem in report.problems)
        )
        self.assertTrue(
            any("unknown template_id" in problem for problem in report.problems)
        )
        self.assertTrue(
            any("unknown semantic_patterns" in problem for problem in report.problems)
        )
        missing_bucket = next(
            item for item in report.templates if item.name == MISSING_TEMPLATE_ID
        )
        self.assertEqual(missing_bucket.mart_count, 1)

    def test_strict_policy_refuses_a_pattern_that_only_reports_itself(self) -> None:
        dishonest = _tagged_task(
            "demo__dishonest_pattern",
            mart_names=("dishonest",),
            template_id="aggregate_then_filter",
            patterns=(SemanticPattern.AGGREGATE_THEN_FILTER,),
        )
        policy = SemanticCoveragePolicy(
            known_template_ids=("aggregate_then_filter",),
            known_patterns=(SemanticPattern.AGGREGATE_THEN_FILTER,),
            require_explicit_template_id=True,
            require_explicit_patterns=True,
            require_certified_patterns=True,
        )

        report = measure_semantic_coverage((("dishonest", dishonest),), policy=policy)

        self.assertTrue(
            any("declared but not certified" in problem for problem in report.problems),
            report.problems,
        )

    def test_mart_and_task_owner_minimums_are_independent(self) -> None:
        task = _tagged_task(
            "demo__one_owner",
            mart_names=("pivot_one", "pivot_two"),
            template_id="conditional_pivot",
            patterns=(SemanticPattern.PIVOT,),
        )
        policy = SemanticCoveragePolicy(
            known_template_ids=("conditional_pivot",),
            known_patterns=(SemanticPattern.PIVOT,),
            minimum_pattern_marts=((SemanticPattern.PIVOT, 2),),
            minimum_pattern_tasks=((SemanticPattern.PIVOT, 2),),
        )

        report = measure_semantic_coverage((("only", task),), policy=policy)

        self.assertEqual(len(report.deficits), 1)
        deficit = report.deficits[0]
        self.assertEqual(deficit.name, SemanticPattern.PIVOT.value)
        self.assertEqual(deficit.owner_kind, "task")
        self.assertEqual(deficit.observed, 1)
        self.assertEqual(deficit.required, 2)
        self.assertEqual(deficit.missing, 1)

    def test_report_bytes_and_digest_are_recomputable(self) -> None:
        task = _tagged_task(
            "demo__write_coverage",
            mart_names=("pivot",),
            template_id="conditional_pivot",
            patterns=(SemanticPattern.PIVOT,),
        )
        report = measure_semantic_coverage((("demo", task),))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "semantic_coverage.json"
            written_digest = write_semantic_coverage(path, report)
            text = path.read_text(encoding="utf-8")

        self.assertEqual(text, report.to_canonical_json() + "\n")
        self.assertEqual(written_digest, sha256_hex(text))

    def test_duplicate_owner_alias_fails_closed_in_report(self) -> None:
        task = _tagged_task(
            "demo__duplicate",
            mart_names=("pivot",),
            template_id="conditional_pivot",
            patterns=(SemanticPattern.PIVOT,),
        )
        report = measure_semantic_coverage((("same", task), ("same", task)))
        self.assertIn("cohort: duplicate task aliases ['same']", report.problems)


if __name__ == "__main__":
    unittest.main()

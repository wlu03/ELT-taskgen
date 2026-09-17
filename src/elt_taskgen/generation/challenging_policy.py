"""Reject structurally shallow or template-collapsed cohorts before reference work."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable

from elt_taskgen.generation.coverage import (
    PERMISSIVE_SEMANTIC_COVERAGE_POLICY,
    SemanticCoveragePolicy,
    SemanticCoverageReport,
    measure_semantic_coverage,
)
from elt_taskgen.generation.mart_plan import (
    MIN_MART_COLUMNS,
    plan_template_signature,
    unclassified_columns,
)
from elt_taskgen.models import TaskIR


# Compare integer products rather than rounded percentages.  A template may
# occupy exactly one quarter of the mart corpus, but never more than one quarter.
CHALLENGING_TEMPLATE_SHARE_NUMERATOR = 1
CHALLENGING_TEMPLATE_SHARE_DENOMINATOR = 4


@dataclass(frozen=True)
class ChallengingCohortPolicyReport:
    """Deterministic measurements returned by a passing policy check."""

    task_count: int
    mart_count: int
    distinct_template_count: int
    largest_template_count: int
    semantic_coverage: SemanticCoverageReport

    @property
    def largest_template_share(self) -> float:
        return (
            self.largest_template_count / self.mart_count
            if self.mart_count
            else 0.0
        )


def validate_challenging_cohort(
    tasks: Iterable[tuple[str, TaskIR]],
    *,
    coverage_policy: SemanticCoveragePolicy = PERMISSIVE_SEMANTIC_COVERAGE_POLICY,
) -> ChallengingCohortPolicyReport:
    """Validate a challenging cohort and report deterministic, aliased errors."""

    entries = tuple(sorted(tasks, key=lambda item: item[0]))
    problems: list[str] = []
    signature_owners: dict[str, list[str]] = defaultdict(list)
    coverage_report = measure_semantic_coverage(entries, policy=coverage_policy)
    problems.extend(coverage_report.problems)
    if not entries:
        problems.append("cohort: no tasks were provided")

    for alias, task in entries:
        task_signatures: list[str] = []
        if not task.marts:
            problems.append(f"{alias}: task declares no marts")
        for mart in sorted(task.marts, key=lambda item: item.name):
            if len(mart.columns) < MIN_MART_COLUMNS:
                problems.append(
                    f"{alias}/{mart.name}: {len(mart.columns)} mart columns; "
                    f"challenging minimum is {MIN_MART_COLUMNS}"
                )
            unclassified = unclassified_columns(mart)
            if unclassified:
                problems.append(
                    f"{alias}/{mart.name}: unclassified mart columns "
                    f"{unclassified}"
                )

            signature = plan_template_signature(mart.plan)
            task_signatures.append(signature)
            signature_owners[signature].append(f"{alias}/{mart.name}")

        if len(task.marts) > 1 and len(set(task_signatures)) == 1:
            signature = task_signatures[0] if task_signatures else "<none>"
            problems.append(
                f"{alias}: all {len(task.marts)} marts repeat one plan-template "
                f"signature {signature!r}"
            )

    signature_counts = Counter(
        {signature: len(owners) for signature, owners in signature_owners.items()}
    )
    mart_count = sum(signature_counts.values())
    for signature, count in sorted(signature_counts.items()):
        if (
            count * CHALLENGING_TEMPLATE_SHARE_DENOMINATOR
            > mart_count * CHALLENGING_TEMPLATE_SHARE_NUMERATOR
        ):
            owners = ", ".join(sorted(signature_owners[signature]))
            problems.append(
                f"cohort: plan-template signature {signature!r} occupies "
                f"{count}/{mart_count} marts, exceeding the 25% cap "
                f"({owners})"
            )

    if problems:
        raise RuntimeError(
            "challenging cohort policy failed before reference work: "
            + "; ".join(sorted(problems))
        )

    return ChallengingCohortPolicyReport(
        task_count=len(entries),
        mart_count=mart_count,
        distinct_template_count=len(signature_counts),
        largest_template_count=max(signature_counts.values(), default=0),
        semantic_coverage=coverage_report,
    )


__all__ = [
    "CHALLENGING_TEMPLATE_SHARE_DENOMINATOR",
    "CHALLENGING_TEMPLATE_SHARE_NUMERATOR",
    "ChallengingCohortPolicyReport",
    "SemanticCoveragePolicy",
    "validate_challenging_cohort",
]

"""Focused truthfulness contracts for configured-run reporting."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import demo_fixture
from elt_taskgen.engine import Engine, StageName
from elt_taskgen.pipeline_readiness import (
    RUN_PREFLIGHT_BLOCKED_STATE,
    AttemptReadiness,
    CandidateRunAction,
    Coverage,
    GenerationRunSpec,
    ReadinessFailureClass,
    ReadinessProfile,
    TaskReadiness,
    cost_breakdown_from_budget_snapshot,
    make_run_report,
    render_run_report_markdown,
    write_run_report,
)
from elt_taskgen.review.budget_ledger import DurableBudgetLedger


class PipelineReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.engine.register(demo_fixture.demo_task())
        self.task = self.engine.load_task(demo_fixture.DEMO_TASK_ID)
        self.run_id = "reporting-run"

    def _snapshot(self):
        ledger = DurableBudgetLedger.initialize(
            self.run_id,
            10.0,
            self.workspace,
            per_task_limit_usd=7.0,
        )
        ledger.reserve(
            self.task.task_id,
            "independent_loader",
            0.4,
            "call-uncertain",
        )
        ledger.mark_outstanding_uncertain()
        ledger.reserve(
            self.task.task_id,
            "semantic_author",
            1.0,
            "call-author",
        )
        ledger.commit(
            "call-author",
            1.2,
            task_id=self.task.task_id,
            role="semantic_author",
        )
        ledger.reserve(
            self.task.task_id,
            "ambiguity_critic",
            0.3,
            "call-reserved",
        )
        return ledger.snapshot()

    def test_durable_snapshot_reconciles_task_role_stage_and_overrun(self) -> None:
        costs = cost_breakdown_from_budget_snapshot(
            self._snapshot(),
            task_ids=(self.task.task_id,),
            expected_run_id=self.run_id,
        )

        self.assertTrue(costs.reconciled)
        self.assertEqual(costs.accounting_source, "durable_budget_ledger")
        self.assertAlmostEqual(costs.recorded_cost, 1.2)
        self.assertAlmostEqual(costs.reserved_cost, 0.3)
        self.assertAlmostEqual(costs.uncertain_cost, 0.4)
        self.assertAlmostEqual(costs.reserved_or_uncertain_cost, 0.7)
        self.assertAlmostEqual(costs.final_request_overrun, 0.2)
        self.assertAlmostEqual(
            costs.by_task[self.task.task_id].recorded_cost, 1.2
        )
        self.assertAlmostEqual(
            costs.by_role["ambiguity_critic"].reserved_cost, 0.3
        )
        self.assertAlmostEqual(
            costs.by_stage[StageName.AUTHOR.value].recorded_cost, 1.2
        )
        self.assertAlmostEqual(
            costs.by_stage[StageName.REVIEW.value].reserved_cost, 0.3
        )
        self.assertAlmostEqual(
            costs.by_stage[StageName.GATES_EXTRACT_LOAD.value].uncertain_cost,
            0.4,
        )
        self.assertEqual(
            {row["attributed_stage"] for row in costs.reservations},
            {
                StageName.AUTHOR.value,
                StageName.REVIEW.value,
                StageName.GATES_EXTRACT_LOAD.value,
            },
        )

    def test_foreign_ledger_task_and_wrong_run_fail_closed(self) -> None:
        snapshot = self._snapshot()
        with self.assertRaisesRegex(ValueError, "outside the exact run roster"):
            cost_breakdown_from_budget_snapshot(
                snapshot,
                task_ids=("synthetic__another",),
            )
        with self.assertRaisesRegex(ValueError, "differs"):
            cost_breakdown_from_budget_snapshot(
                snapshot,
                task_ids=(self.task.task_id,),
                expected_run_id="another-run",
            )

    def test_report_uses_cumulative_ledger_and_per_candidate_actions(self) -> None:
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.LOCAL_READY,
            budget_per_task=7.0,
            budget_total=10.0,
        )
        action = CandidateRunAction(
            selected=True,
            registration_action="resumed",
            processing_attempted=True,
            adapter_taskir_rederived=True,
            source_provenance_equivalence_attested=True,
            reports_locally_revalidated_and_superseded=2,
            evidence_paths=("state/revalidation.json",),
        )
        report = make_run_report(
            engine=self.engine,
            run_id=self.run_id,
            spec=spec,
            task_ids=(self.task.task_id,),
            state="RUNNING",
            worker_results=(
                {"task_id": self.task.task_id, "state": "running", "usd": 0.01},
            ),
            budget_snapshot=self._snapshot(),
            task_actions={self.task.task_id: action},
        )

        task = report.tasks[0]
        self.assertEqual(task.task_revision, self.task.current_revision)
        self.assertEqual(task.revision, self.task.current_revision)
        self.assertEqual(task.source_family, self.task.origin.value)
        self.assertAlmostEqual(task.recorded_cost, 1.2)
        self.assertAlmostEqual(task.reserved_cost, 0.3)
        self.assertAlmostEqual(task.uncertain_cost, 0.4)
        self.assertAlmostEqual(task.reserved_or_uncertain_cost, 0.7)
        self.assertAlmostEqual(task.final_request_overrun, 0.2)
        self.assertEqual(report.counts.selected, 1)
        self.assertEqual(report.counts.generation_attempts, 1)
        self.assertEqual(report.counts.created_this_invocation, 0)
        self.assertEqual(report.counts.resumed_existing, 1)
        self.assertEqual(report.counts.adapter_taskirs_rederived, 1)
        self.assertEqual(
            report.counts.source_provenance_equivalence_attested, 1
        )
        self.assertEqual(
            report.counts.reports_locally_revalidated_and_superseded, 2
        )
        self.assertTrue(report.costs["reconciled"])
        self.assertAlmostEqual(report.costs["spent_usd"], 1.2)
        self.assertAlmostEqual(report.costs["reserved_usd"], 0.3)
        self.assertAlmostEqual(report.costs["uncertain_usd"], 0.4)

    def test_attempt_count_is_not_the_selected_roster_size(self) -> None:
        spec = GenerationRunSpec(
            candidate_count=2,
            profile=ReadinessProfile.LOCAL_READY,
            budget_per_task=7.0,
        )
        report = make_run_report(
            engine=self.engine,
            run_id="partial-attempt",
            spec=spec,
            task_ids=("synthetic__one", "synthetic__two"),
            state="RUNNING",
            worker_results=(
                {"task_id": "synthetic__one", "state": "blocked"},
            ),
        )
        self.assertEqual(report.counts.selected, 2)
        self.assertEqual(report.counts.generation_attempts, 1)

    def test_run_preflight_block_preserves_terminal_evidence_dispositions(
        self,
    ) -> None:
        task_ids = (
            "synthetic__accepted_one",
            "synthetic__accepted_two",
            "synthetic__rejected",
            "synthetic__infrastructure_failed",
            "synthetic__awaiting_provider",
        )

        def readiness(task_id: str, disposition: str, **updates) -> TaskReadiness:
            return TaskReadiness(
                task_id=task_id,
                origin="synthetic",
                family_id=task_id,
                task_content_hash="1" * 64,
                disposition=disposition,
                stages=(),
                coverage=Coverage(),
                **updates,
            )

        reports = {
            task_ids[0]: readiness(
                task_ids[0], "accepted", locally_evaluator_ready=True
            ),
            task_ids[1]: readiness(
                task_ids[1], "accepted", locally_evaluator_ready=True
            ),
            task_ids[2]: readiness(
                task_ids[2],
                "rejected",
                failure_class=ReadinessFailureClass.QUALITY_REJECTION,
            ),
            task_ids[3]: readiness(
                task_ids[3],
                "failed",
                failure_class=ReadinessFailureClass.INFRASTRUCTURE_FAILURE,
                attempts=(
                    AttemptReadiness(
                        attempt_id="infrastructure-attempt",
                        target_stage=StageName.GATES_EXTRACT_LOAD,
                        state="infrastructure",
                        evidence_path="attempt.json",
                    ),
                ),
            ),
            # A stale prerequisite can look failed before the provider work that
            # would refresh it; the run-level preflight stop owns this outcome.
            task_ids[4]: readiness(task_ids[4], "failed"),
        }
        detail = "provider preflight: council admission is stale"
        spec = GenerationRunSpec(
            candidate_count=len(task_ids),
            profile=ReadinessProfile.LOCAL_READY,
            budget_per_task=7.0,
        )

        with (
            mock.patch.object(
                self.engine,
                "load_task",
                side_effect=lambda task_id: SimpleNamespace(task_id=task_id),
            ),
            mock.patch(
                "elt_taskgen.pipeline_readiness.task_readiness",
                side_effect=lambda _engine, task, **_kwargs: reports[task.task_id],
            ),
        ):
            report = make_run_report(
                engine=self.engine,
                run_id="preflight-dispositions",
                spec=spec,
                task_ids=task_ids,
                state="BLOCKED",
                worker_results=tuple(
                    {
                        "task_id": task_id,
                        "state": RUN_PREFLIGHT_BLOCKED_STATE,
                        "detail": detail,
                    }
                    for task_id in task_ids
                ),
            )

        self.assertEqual(report.counts.accepted, 2)
        self.assertEqual(report.counts.rejected, 1)
        self.assertEqual(report.counts.failed, 1)
        self.assertEqual(report.counts.blocked, 1)
        self.assertEqual(report.counts.quality_rejected, 1)
        self.assertEqual(report.counts.infrastructure_failed, 1)
        self.assertEqual(report.counts.operationally_blocked, 1)
        self.assertEqual(
            {task.task_id: task.disposition for task in report.tasks},
            {
                task_ids[0]: "accepted",
                task_ids[1]: "accepted",
                task_ids[2]: "rejected",
                task_ids[3]: "failed",
                task_ids[4]: "blocked",
            },
        )
        self.assertTrue(
            all(detail in task.precise_blockers for task in report.tasks)
        )

    def test_aggregate_actions_must_match_candidate_facts(self) -> None:
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.LOCAL_READY,
            budget_per_task=7.0,
        )
        with self.assertRaisesRegex(ValueError, "disagrees"):
            make_run_report(
                engine=self.engine,
                run_id="contradictory-actions",
                spec=spec,
                task_ids=(self.task.task_id,),
                state="RUNNING",
                task_actions={
                    self.task.task_id: CandidateRunAction(
                        registration_action="resumed"
                    )
                },
                run_actions={"resumed_existing": 0},
            )

    def test_json_and_markdown_are_written_from_the_same_report(self) -> None:
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.LOCAL_READY,
            budget_per_task=7.0,
        )
        report = make_run_report(
            engine=self.engine,
            run_id="rendered-report",
            spec=spec,
            task_ids=(self.task.task_id,),
            state="RUNNING",
        )
        json_path = write_run_report(report, workspace=self.workspace)
        markdown_path = json_path.with_suffix(".md")
        self.assertTrue(json_path.is_file())
        self.assertTrue(markdown_path.is_file())
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertEqual(markdown, render_run_report_markdown(report))
        self.assertIn(self.task.task_id, markdown)
        self.assertIn(f"| requested | {report.counts.requested} |", markdown)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

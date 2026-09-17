"""Focused contracts for configured-run status and package reporting."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import cli as cli_mod
from elt_taskgen import demo_fixture
from elt_taskgen.engine import Engine, FINAL_ACCEPTED, STAGE_ORDER, StageName
from elt_taskgen.ingest_manifest import (
    CandidateIngestOutcome,
    FiveSourceIngestResult,
)
from elt_taskgen.models import Origin
from elt_taskgen.pipeline_readiness import (
    CandidateRunAction,
    GenerationRunSpec,
    PipelineReadinessReport,
    ReadinessProfile,
    ReadinessState,
    StageReadiness,
    make_run_report,
    write_run_report,
)
from elt_taskgen.review.budget_ledger import initialize


class _SelectedRoster:
    """Small selected-manifest double with the coordinator's public surface."""

    def __init__(self, task_ids: tuple[str, ...]) -> None:
        self._entries = tuple(
            SimpleNamespace(expected_task_id=task_id, pool=Origin.DBT.value)
            for task_id in task_ids
        )
        self.expected_task_count = len(task_ids)
        self.source_allocation = {
            origin: len(task_ids) if origin is Origin.DBT else 0
            for origin in (
                Origin.DBT,
                Origin.DLT,
                Origin.SYNSQL,
                Origin.SCHEMAPILE,
                Origin.WIKIDBS,
            )
        }

    def ordered_entries(self):
        return self._entries

    def manifest_sha256(self) -> str:
        return hashlib.sha256(
            "\0".join(
                entry.expected_task_id for entry in self._entries
            ).encode()
        ).hexdigest()


class PipelineStatusReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @staticmethod
    def _register_tasks(workspace: Path, task_ids: tuple[str, ...]):
        tasks = tuple(
            demo_fixture.demo_task().model_copy(
                update={
                    "task_id": task_id,
                    "origin": Origin.SYNTHETIC,
                    "family_id": task_id,
                    "cluster_id": task_id,
                }
            )
            for task_id in task_ids
        )
        engine = Engine(workspace)
        try:
            for task in tasks:
                engine.register(task)
            return tuple(engine.load_task(task.task_id) for task in tasks)
        finally:
            engine.close()

    @staticmethod
    def _status_args(workspace: Path, run_id: str):
        return cli_mod.build_parser().parse_args(
            [
                "pipeline-status",
                "--workspace",
                str(workspace),
                "--run-id",
                run_id,
            ]
        )

    @staticmethod
    def _pipeline_args(
        workspace: Path,
        pool_path: Path,
        export_dir: Path,
        run_id: str,
        count: int,
    ):
        return cli_mod.build_parser().parse_args(
            [
                "pipeline",
                "--workspace",
                str(workspace),
                "--ingest-manifest",
                str(pool_path),
                "--candidate-count",
                str(count),
                "--readiness-profile",
                "packaged",
                "--workers",
                "1",
                "--run-id",
                run_id,
                "--export-dir",
                str(export_dir),
            ]
        )

    @staticmethod
    def _ingested(selected: _SelectedRoster, tasks) -> FiveSourceIngestResult:
        return FiveSourceIngestResult(
            manifest_sha256=selected.manifest_sha256(),
            tasks=tuple(tasks),
            receipt=None,
            dry_run=False,
            candidate_outcomes=tuple(
                CandidateIngestOutcome(
                    origin=Origin.DBT,
                    task_id=task.task_id,
                    state="created",
                )
                for task in tasks
            ),
        )

    @staticmethod
    def _stage_result(_engine, _task, stage, *, spec=None):
        del spec
        state = (
            ReadinessState.PASS
            if STAGE_ORDER.index(stage)
            <= STAGE_ORDER.index(StageName.GATES_TRANSFORM)
            else ReadinessState.NOT_RUN
        )
        return StageReadiness(stage=stage, state=state)

    def _status_fixture(
        self,
        *,
        run_id: str,
        task_id: str,
        receipt_relative: str | None = None,
    ) -> dict:
        workspace = self.root / run_id
        task = self._register_tasks(workspace, (task_id,))[0]
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.DRAFT,
            workers=1,
            budget_per_task=3.0,
            budget_total=5.0,
        )
        ledger = initialize(
            run_id,
            5.0,
            workspace,
            per_task_limit_usd=3.0,
        )
        ledger.reserve(task_id, "semantic_author", 1.5, "committed-call")
        ledger.commit(
            "committed-call",
            1.25,
            task_id=task_id,
            role="semantic_author",
        )
        ledger.reserve(task_id, "ambiguity_critic", 0.4, "reserved-call")

        attempt_path = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "attempts"
            / task_id
            / "attempt-001.json"
        )
        attempt_path.parent.mkdir(parents=True)
        attempt_path.write_text(
            json.dumps(
                {
                    "attempt_id": "attempt-001",
                    "task_id": task_id,
                    "target_stage": StageName.INTAKE.value,
                    "state": "PASS",
                    "task_content_hash": task.content_hash(),
                    "detail": "durable attempt",
                    "usd": 1.25,
                }
            ),
            encoding="utf-8",
        )
        action = CandidateRunAction(
            selected=True,
            registration_action="resumed",
            processing_attempted=True,
            adapter_taskir_rederived=True,
            source_provenance_equivalence_attested=True,
            reports_locally_revalidated_and_superseded=2,
            evidence_paths=("state/generator-revalidation/equivalence.json",),
        )
        engine = Engine(workspace)
        try:
            report = make_run_report(
                engine=engine,
                run_id=run_id,
                spec=spec,
                task_ids=(task_id,),
                state="COMPLETE",
                budget_snapshot=ledger.snapshot(),
                task_actions={task_id: action},
            )
        finally:
            engine.close()

        package_path = self.root / "packages" / task_id
        package_path.mkdir(parents=True)
        if receipt_relative is None:
            receipt_relative = (
                f"state/pipeline_runs/{run_id}/package_verifications/"
                f"{task_id}.json"
            )
        receipt_path = workspace.joinpath(*Path(receipt_relative).parts)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text("{}\n", encoding="utf-8")

        stages = tuple(
            StageReadiness(
                stage=stage,
                state=(
                    ReadinessState.PASS
                    if stage is StageName.INTAKE
                    else ReadinessState.NOT_RUN
                ),
            )
            for stage in STAGE_ORDER
        )
        recorded = report.tasks[0].model_copy(
            update={
                "stages": stages,
                "actions": action,
                "package_path": str(package_path.resolve()),
                "package_path_if_verified": str(package_path.resolve()),
                "package_verification_receipt": receipt_relative,
                "package_verification_sha256": "a" * 64,
                "package_status": ReadinessState.PASS,
                "packaged_for_evaluation": True,
                "blocker": "",
                "precise_blockers": (),
            }
        )
        report = report.model_copy(update={"tasks": (recorded,), "blockers": ()})
        write_run_report(report, workspace=workspace)
        return {
            "workspace": workspace,
            "task_id": task_id,
            "report": report,
            "recorded": recorded,
            "action": action,
            "attempts": recorded.attempts,
            "receipt": receipt_path,
            "ledger": ledger,
        }

    def test_status_forwards_canonical_receipt_actions_attempts_and_ledger_cost(
        self,
    ) -> None:
        fixture = self._status_fixture(
            run_id="status-current",
            task_id="synthetic__status_current",
        )
        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.pipeline_readiness.task_readiness",
                return_value=fixture["recorded"],
            ) as readiness,
            contextlib.redirect_stdout(output),
        ):
            code = cli_mod.cmd_pipeline_status(
                self._status_args(fixture["workspace"], "status-current")
            )

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["valid"], payload["problems"])
        self.assertEqual(payload["costs"]["recorded_cost"], 1.25)
        self.assertEqual(payload["costs"]["reserved_cost"], 0.4)
        self.assertEqual(payload["costs"]["reserved_or_uncertain_cost"], 0.4)

        forwarded = readiness.call_args.kwargs
        self.assertEqual(
            forwarded["package_receipt_path"], fixture["receipt"].resolve()
        )
        self.assertEqual(forwarded["action"], fixture["action"])
        self.assertEqual(forwarded["attempts"], fixture["attempts"])
        self.assertEqual(len(forwarded["attempts"]), 1)
        self.assertEqual(forwarded["recorded_cost"], 1.25)
        self.assertEqual(forwarded["reserved_cost"], 0.4)
        self.assertEqual(forwarded["cost_breakdown"].recorded_cost, 1.25)
        self.assertEqual(forwarded["cost_breakdown"].reserved_cost, 0.4)

    def test_status_rejects_noncanonical_recorded_receipt_path_precisely(
        self,
    ) -> None:
        run_id = "status-wrong-receipt"
        task_id = "synthetic__status_wrong_receipt"
        fixture = self._status_fixture(
            run_id=run_id,
            task_id=task_id,
            receipt_relative=(
                f"state/pipeline_runs/{run_id}/package_verifications/"
                "some-other-task.json"
            ),
        )
        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.pipeline_readiness.task_readiness",
                return_value=fixture["recorded"],
            ) as readiness,
            contextlib.redirect_stdout(output),
        ):
            code = cli_mod.cmd_pipeline_status(
                self._status_args(fixture["workspace"], run_id)
            )

        self.assertEqual(code, 2)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["valid"])
        self.assertIn(
            f"{task_id}: package verification receipt path is not the "
            "canonical run/task location",
            payload["problems"],
        )
        self.assertIsNone(readiness.call_args.kwargs["package_receipt_path"])

    def test_status_rejects_ledger_drift_and_reports_current_costs(self) -> None:
        run_id = "status-ledger-drift"
        task_id = "synthetic__status_ledger_drift"
        fixture = self._status_fixture(run_id=run_id, task_id=task_id)
        fixture["ledger"].reserve(
            task_id, "feasibility_reviewer", 0.75, "later-call"
        )
        fixture["ledger"].commit(
            "later-call",
            0.5,
            task_id=task_id,
            role="feasibility_reviewer",
        )

        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.pipeline_readiness.task_readiness",
                return_value=fixture["recorded"],
            ) as readiness,
            contextlib.redirect_stdout(output),
        ):
            code = cli_mod.cmd_pipeline_status(
                self._status_args(fixture["workspace"], run_id)
            )

        self.assertEqual(code, 2)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["valid"])
        self.assertIn(
            "durable budget ledger has changed since the readiness report was written",
            payload["problems"],
        )
        self.assertEqual(payload["costs"]["recorded_cost"], 1.75)
        self.assertEqual(
            readiness.call_args.kwargs["cost_breakdown"].recorded_cost,
            1.75,
        )

    def test_package_verifier_failure_does_not_claim_package_and_stdout_uses_report_counts(
        self,
    ) -> None:
        workspace = self.root / "package-failure-workspace"
        export_dir = self.root / "package-failure-output"
        run_id = "package-verifier-failure"
        task_ids = ("synthetic__package_verifier_failure",)
        selected = _SelectedRoster(task_ids)
        tasks = self._register_tasks(workspace, task_ids)
        ingested = self._ingested(selected, tasks)
        worker_results = [
            {
                "task_id": task_ids[0],
                "ok": True,
                "state": "ready",
                "detail": "",
                "usd": 0.5,
            }
        ]

        def freeze(_engine, task, destination):
            destination.mkdir(parents=True)
            (destination / "package_manifest.json").write_text(
                task.task_id, encoding="utf-8"
            )

        args = self._pipeline_args(
            workspace,
            self.root / "pool.yaml",
            export_dir,
            run_id,
            1,
        )
        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.ingest_manifest.load_five_source_batch_manifest",
                return_value=mock.sentinel.pool,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.select_candidate_manifest",
                return_value=selected,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.ingest_selected_sources",
                return_value=(selected, ingested),
            ),
            mock.patch.object(
                cli_mod, "_configured_results", return_value=worker_results
            ),
            mock.patch.object(
                cli_mod, "_configured_live_provider_problems", return_value=()
            ),
            mock.patch.object(
                cli_mod,
                "_repository_run_identity",
                return_value={"head": "test", "working_tree_dirty": False},
            ),
            mock.patch(
                "elt_taskgen.export.local_package.freeze_local_package",
                side_effect=freeze,
            ),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_fresh_local_package",
                return_value=SimpleNamespace(
                    verified=False,
                    failures=("positive control failed",),
                ),
            ) as verifier,
            mock.patch(
                "elt_taskgen.pipeline_readiness.stage_readiness",
                side_effect=self._stage_result,
            ),
            mock.patch.object(
                Engine, "final_verdict", return_value=FINAL_ACCEPTED
            ),
            contextlib.redirect_stdout(output),
        ):
            code = cli_mod.cmd_pipeline(args)

        self.assertEqual(code, 2)
        verifier.assert_called_once()
        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / run_id
                / "readiness.json"
            ).read_bytes()
        )
        self.assertEqual(report.counts.accepted, 1)
        self.assertEqual(report.counts.packaged, 0)
        self.assertEqual(report.counts.exported, 0)
        self.assertEqual(report.counts.failed, 1)
        self.assertFalse(report.tasks[0].packaged_for_evaluation)
        self.assertEqual(report.tasks[0].package_path, "")
        self.assertEqual(report.tasks[0].package_path_if_verified, "")
        self.assertEqual(report.tasks[0].package_verification_receipt, "")
        self.assertIn(
            "fresh-copy package verification failed: positive control failed",
            report.tasks[0].precise_blockers,
        )
        self.assertIn(
            "configured pipeline: requested=1 created=1 selected=1 "
            "accepted=1 packaged=0 rejected=0 failed=1 blocked=0",
            output.getvalue(),
        )

    def test_stale_provider_preflight_hydrates_only_current_canonical_packages(
        self,
    ) -> None:
        workspace = self.root / "hydrate-preflight-workspace"
        export_dir = self.root / "hydrate-preflight-packages"
        run_id = "hydrate-preflight"
        task_ids = (
            "synthetic__hydrate_current",
            "synthetic__hydrate_stale",
            "synthetic__hydrate_foreign",
        )
        selected = _SelectedRoster(task_ids)
        tasks = self._register_tasks(workspace, task_ids)
        ingested = self._ingested(selected, tasks)
        receipt_root = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "package_verifications"
        )
        for task_id in task_ids:
            (export_dir / task_id).mkdir(parents=True)
            receipt_root.mkdir(parents=True, exist_ok=True)
            (receipt_root / f"{task_id}.json").write_text(
                "{}\n", encoding="utf-8"
            )
        # An otherwise plausible receipt outside the exact roster is never
        # discovered: adoption constructs canonical paths from task_ids rather
        # than scanning either package directory.
        (receipt_root / "synthetic__not_in_roster.json").write_text(
            "{}\n", encoding="utf-8"
        )
        transcript = workspace / "transcripts" / "fixture" / "unchanged.json"
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes(b'{"provider_calls": 0}\n')
        transcript_before = transcript.read_bytes()

        args = self._pipeline_args(
            workspace,
            self.root / "pool.yaml",
            export_dir,
            run_id,
            len(task_ids),
        )
        args.budget_total = 30.0
        spec = cli_mod._configured_run_spec(args)
        ledger = initialize(
            run_id,
            30.0,
            workspace,
            per_task_limit_usd=spec.budget_per_task,
        )
        budget_before = ledger.snapshot()
        observed_writes: list[dict] = []
        original_write = cli_mod._write_configured_report

        def capture_write(**kwargs):
            observed_writes.append(
                {
                    "state": kwargs["state"],
                    "packages": tuple(sorted((kwargs.get("packages") or {}).keys())),
                    "receipts": tuple(
                        sorted((kwargs.get("package_receipts") or {}).keys())
                    ),
                }
            )
            return original_write(**kwargs)

        task_hashes = {task.task_id: task.content_hash() for task in tasks}

        def persisted_receipt(
            _receipt,
            *,
            package_path,
            expected_task_id,
            expected_task_content_hash,
        ):
            self.assertEqual(package_path, export_dir.resolve() / expected_task_id)
            self.assertEqual(
                expected_task_content_hash, task_hashes[expected_task_id]
            )
            if expected_task_id == task_ids[0]:
                return SimpleNamespace(
                    ok=True, receipt_sha256="a" * 64, failures=()
                )
            if expected_task_id == task_ids[1]:
                return SimpleNamespace(
                    ok=False,
                    receipt_sha256="",
                    failures=(
                        "package verification evaluator runtime identity drifted: "
                        "['generator_sha256']",
                    ),
                )
            return SimpleNamespace(
                ok=False,
                receipt_sha256="",
                failures=(
                    "package verification receipt names another task_id",
                    "package verification receipt is bound to another path",
                ),
            )

        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.ingest_manifest.load_five_source_batch_manifest",
                return_value=mock.sentinel.pool,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.select_candidate_manifest",
                return_value=selected,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.ingest_selected_sources",
                return_value=(selected, ingested),
            ),
            mock.patch.object(
                cli_mod, "_assert_configured_run_identity", return_value=None
            ),
            mock.patch.object(
                cli_mod,
                "_configured_live_provider_problems",
                return_value=("council admission is stale",),
            ),
            mock.patch.object(cli_mod, "_configured_results") as workers,
            mock.patch.object(cli_mod, "_resolve_provider") as provider,
            mock.patch.object(
                cli_mod,
                "_repository_run_identity",
                return_value={"head": "test", "working_tree_dirty": False},
            ),
            mock.patch.object(
                cli_mod, "_write_configured_report", side_effect=capture_write
            ),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_persisted_package_receipt",
                side_effect=persisted_receipt,
            ) as verifier,
            mock.patch(
                "elt_taskgen.pipeline_readiness.stage_readiness",
                side_effect=self._stage_result,
            ),
            mock.patch.object(Engine, "final_verdict", return_value=FINAL_ACCEPTED),
            contextlib.redirect_stdout(output),
        ):
            code = cli_mod.cmd_pipeline(args)

        self.assertEqual(code, 2, output.getvalue())
        workers.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(
            {call.kwargs["expected_task_id"] for call in verifier.call_args_list},
            set(task_ids),
        )
        self.assertEqual(
            observed_writes,
            [
                {
                    "state": "RUNNING",
                    "packages": (task_ids[0],),
                    "receipts": (task_ids[0],),
                },
                {
                    "state": "BLOCKED",
                    "packages": (task_ids[0],),
                    "receipts": (task_ids[0],),
                },
            ],
        )
        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / run_id
                / "readiness.json"
            ).read_bytes()
        )
        self.assertEqual(report.state, "BLOCKED")
        self.assertEqual(report.counts.accepted, len(task_ids))
        self.assertEqual(report.counts.packaged, 1)
        self.assertEqual(report.counts.blocked, 0)
        self.assertTrue(report.tasks[0].packaged_for_evaluation)
        self.assertFalse(report.tasks[1].packaged_for_evaluation)
        self.assertFalse(report.tasks[2].packaged_for_evaluation)
        self.assertTrue(
            any("evaluator runtime identity drifted" in row for row in report.blockers)
        )
        self.assertTrue(
            any("names another task_id" in row for row in report.blockers)
        )
        self.assertEqual(ledger.snapshot(), budget_before)
        self.assertEqual(transcript.read_bytes(), transcript_before)

    def test_fully_adopted_packaged_resume_skips_live_preflight_and_workers(
        self,
    ) -> None:
        workspace = self.root / "fully-adopted-workspace"
        export_dir = self.root / "fully-adopted-packages"
        run_id = "fully-adopted"
        task_ids = (
            "synthetic__fully_adopted_0",
            "synthetic__fully_adopted_1",
        )
        selected = _SelectedRoster(task_ids)
        tasks = self._register_tasks(workspace, task_ids)
        ingested = self._ingested(selected, tasks)
        receipt_root = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "package_verifications"
        )
        receipt_root.mkdir(parents=True)
        for task_id in task_ids:
            (export_dir / task_id).mkdir(parents=True)
            (receipt_root / f"{task_id}.json").write_text(
                "{}\n", encoding="utf-8"
            )

        args = self._pipeline_args(
            workspace,
            self.root / "pool.yaml",
            export_dir,
            run_id,
            len(task_ids),
        )
        terminal_results: list[dict] = []
        original_write = cli_mod._write_configured_report

        def capture_write(**kwargs):
            terminal_results[:] = list(kwargs.get("results", ()))
            return original_write(**kwargs)

        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.ingest_manifest.load_five_source_batch_manifest",
                return_value=mock.sentinel.pool,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.select_candidate_manifest",
                return_value=selected,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.ingest_selected_sources",
                return_value=(selected, ingested),
            ),
            mock.patch.object(
                cli_mod, "_assert_configured_run_identity", return_value=None
            ),
            mock.patch.object(
                cli_mod, "_configured_live_provider_problems"
            ) as provider_preflight,
            mock.patch.object(cli_mod, "_configured_results") as workers,
            mock.patch.object(
                cli_mod, "_write_configured_report", side_effect=capture_write
            ),
            mock.patch(
                "elt_taskgen.export.local_package.freeze_local_package"
            ) as freezer,
            mock.patch(
                "elt_taskgen.export.package_verification.verify_persisted_package_receipt",
                return_value=SimpleNamespace(
                    ok=True, receipt_sha256="a" * 64, failures=()
                ),
            ),
            mock.patch(
                "elt_taskgen.pipeline_readiness.stage_readiness",
                side_effect=self._stage_result,
            ),
            mock.patch.object(Engine, "final_verdict", return_value=FINAL_ACCEPTED),
            contextlib.redirect_stdout(output),
        ):
            code = cli_mod.cmd_pipeline(args)

        self.assertEqual(code, 0, output.getvalue())
        provider_preflight.assert_not_called()
        workers.assert_not_called()
        freezer.assert_not_called()
        self.assertEqual(
            [(row["task_id"], row["state"], row["usd"]) for row in terminal_results],
            [(task_id, "packaged", 0.0) for task_id in task_ids],
        )
        self.assertTrue(all(row["package_adopted"] for row in terminal_results))
        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / run_id
                / "readiness.json"
            ).read_bytes()
        )
        self.assertEqual(tuple(task.task_id for task in report.tasks), task_ids)
        self.assertEqual(report.counts.packaged, len(task_ids))
        self.assertEqual(
            [
                report.costs["by_task"][task_id]["recorded_cost"]
                for task_id in task_ids
            ],
            [0.0, 0.0],
        )

    def test_terminal_rejection_is_preserved_without_provider_or_worker(self) -> None:
        workspace = self.root / "terminal-rejection-workspace"
        export_dir = self.root / "terminal-rejection-packages"
        run_id = "terminal-rejection"
        task_id = "synthetic__terminal_rejection"
        selected = _SelectedRoster((task_id,))
        tasks = self._register_tasks(workspace, (task_id,))
        ingested = self._ingested(selected, tasks)
        (export_dir / task_id).mkdir(parents=True)
        receipt_path = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "package_verifications"
            / f"{task_id}.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text("{}\n", encoding="utf-8")
        args = self._pipeline_args(
            workspace,
            self.root / "pool.yaml",
            export_dir,
            run_id,
            1,
        )
        terminal_results: list[dict] = []
        original_write = cli_mod._write_configured_report

        def capture_write(**kwargs):
            terminal_results[:] = list(kwargs.get("results", ()))
            return original_write(**kwargs)

        def rejected_stage(_engine, _task, stage, *, spec=None):
            del spec
            return StageReadiness(
                stage=stage,
                state=(
                    ReadinessState.FAIL
                    if stage is StageName.GENERATE
                    else ReadinessState.NOT_RUN
                ),
            )

        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.ingest_manifest.load_five_source_batch_manifest",
                return_value=mock.sentinel.pool,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.select_candidate_manifest",
                return_value=selected,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.ingest_selected_sources",
                return_value=(selected, ingested),
            ),
            mock.patch.object(
                cli_mod, "_assert_configured_run_identity", return_value=None
            ),
            mock.patch.object(
                cli_mod, "_configured_live_provider_problems"
            ) as provider_preflight,
            mock.patch.object(cli_mod, "_configured_results") as workers,
            mock.patch.object(
                cli_mod, "_write_configured_report", side_effect=capture_write
            ),
            mock.patch(
                "elt_taskgen.export.local_package.freeze_local_package"
            ) as freezer,
            mock.patch(
                "elt_taskgen.export.package_verification.verify_persisted_package_receipt"
            ) as verifier,
            mock.patch(
                "elt_taskgen.pipeline_readiness.stage_readiness",
                side_effect=rejected_stage,
            ),
            mock.patch.object(Engine, "final_verdict", return_value="rejected"),
            contextlib.redirect_stdout(output),
        ):
            code = cli_mod.cmd_pipeline(args)

        self.assertEqual(code, 1, output.getvalue())
        provider_preflight.assert_not_called()
        workers.assert_not_called()
        freezer.assert_not_called()
        verifier.assert_not_called()
        self.assertEqual(len(terminal_results), 1)
        self.assertEqual(terminal_results[0]["state"], "rejected")
        self.assertEqual(terminal_results[0]["usd"], 0.0)
        self.assertTrue(terminal_results[0]["terminal_rejection_preserved"])
        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / run_id
                / "readiness.json"
            ).read_bytes()
        )
        self.assertEqual(tuple(task.task_id for task in report.tasks), (task_id,))
        self.assertEqual(report.counts.rejected, 1)
        self.assertEqual(report.costs["by_task"][task_id]["recorded_cost"], 0.0)

    def test_mixed_packaged_resume_sends_only_pending_task_to_workers(self) -> None:
        workspace = self.root / "mixed-adoption-workspace"
        export_dir = self.root / "mixed-adoption-packages"
        run_id = "mixed-adoption"
        task_ids = (
            "synthetic__already_packaged",
            "synthetic__still_pending",
        )
        selected = _SelectedRoster(task_ids)
        tasks = self._register_tasks(workspace, task_ids)
        ingested = self._ingested(selected, tasks)
        (export_dir / task_ids[0]).mkdir(parents=True)
        receipt_path = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "package_verifications"
            / f"{task_ids[0]}.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text("{}\n", encoding="utf-8")

        args = self._pipeline_args(
            workspace,
            self.root / "pool.yaml",
            export_dir,
            run_id,
            len(task_ids),
        )
        worker_results = [
            {
                "task_id": task_ids[1],
                "ok": True,
                "state": "ready",
                "detail": "",
                "usd": 0.0,
            }
        ]

        def freeze(_engine, task, destination):
            self.assertEqual(task.task_id, task_ids[1])
            destination.mkdir(parents=True)
            (destination / "package_manifest.json").write_text(
                task.task_id, encoding="utf-8"
            )

        def verify_fresh(_package, *, receipt_path):
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text("{}\n", encoding="utf-8")
            return SimpleNamespace(verified=True, failures=())

        provider_preflight = mock.Mock(return_value=())
        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.ingest_manifest.load_five_source_batch_manifest",
                return_value=mock.sentinel.pool,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.select_candidate_manifest",
                return_value=selected,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.ingest_selected_sources",
                return_value=(selected, ingested),
            ),
            mock.patch.object(
                cli_mod, "_assert_configured_run_identity", return_value=None
            ),
            mock.patch.object(
                cli_mod,
                "_configured_live_provider_problems",
                new=provider_preflight,
            ),
            mock.patch.object(
                cli_mod, "_configured_results", return_value=worker_results
            ) as workers,
            mock.patch(
                "elt_taskgen.export.local_package.freeze_local_package",
                side_effect=freeze,
            ) as freezer,
            mock.patch(
                "elt_taskgen.export.package_verification.verify_fresh_local_package",
                side_effect=verify_fresh,
            ),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_persisted_package_receipt",
                return_value=SimpleNamespace(
                    ok=True, receipt_sha256="b" * 64, failures=()
                ),
            ),
            mock.patch(
                "elt_taskgen.pipeline_readiness.stage_readiness",
                side_effect=self._stage_result,
            ),
            mock.patch.object(Engine, "final_verdict", return_value=FINAL_ACCEPTED),
            contextlib.redirect_stdout(output),
        ):
            code = cli_mod.cmd_pipeline(args)

        self.assertEqual(code, 0, output.getvalue())
        provider_preflight.assert_called_once()
        workers.assert_called_once()
        self.assertEqual(workers.call_args.kwargs["task_ids"], (task_ids[1],))
        self.assertEqual(freezer.call_count, 1)
        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / run_id
                / "readiness.json"
            ).read_bytes()
        )
        self.assertEqual(tuple(task.task_id for task in report.tasks), task_ids)
        self.assertEqual(report.counts.packaged, len(task_ids))

    def test_package_receipt_cannot_override_current_rejection(self) -> None:
        workspace = self.root / "rejected-package-workspace"
        export_dir = self.root / "rejected-package-output"
        run_id = "rejected-package"
        task_id = "synthetic__rejected_package"
        self._register_tasks(workspace, (task_id,))
        (export_dir / task_id).mkdir(parents=True)
        receipt_path = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "package_verifications"
            / f"{task_id}.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text("{}\n", encoding="utf-8")
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.PACKAGED,
            workers=1,
            budget_per_task=3.0,
            export_dir=str(export_dir),
        )

        with (
            mock.patch.object(Engine, "final_verdict", return_value="rejected"),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_persisted_package_receipt"
            ) as verifier,
        ):
            packages, receipts, blockers = cli_mod._hydrate_configured_packages(
                workspace=workspace,
                run_id=run_id,
                spec=spec,
                task_ids=(task_id,),
            )

        self.assertEqual(packages, {})
        self.assertEqual(receipts, {})
        verifier.assert_not_called()
        self.assertEqual(len(blockers), 1)
        self.assertIn("engine verdict 'rejected'", blockers[0])

    def test_package_receipt_cannot_override_stale_transform_acceptance(self) -> None:
        workspace = self.root / "stale-gates-package-workspace"
        export_dir = self.root / "stale-gates-package-output"
        run_id = "stale-gates-package"
        task_id = "synthetic__stale_gates_package"
        self._register_tasks(workspace, (task_id,))
        (export_dir / task_id).mkdir(parents=True)
        receipt_path = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "package_verifications"
            / f"{task_id}.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text("{}\n", encoding="utf-8")
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.PACKAGED,
            workers=1,
            budget_per_task=3.0,
            export_dir=str(export_dir),
        )

        def transform_readiness(_engine, _task, stage, *, spec=None):
            del spec
            if stage is StageName.GATES_TRANSFORM:
                return StageReadiness(
                    stage=stage,
                    state=ReadinessState.STALE,
                    reason="configured input fingerprint changed",
                )
            return StageReadiness(stage=stage, state=ReadinessState.PASS)

        with (
            mock.patch.object(Engine, "final_verdict", return_value=FINAL_ACCEPTED),
            mock.patch(
                "elt_taskgen.pipeline_readiness.stage_readiness",
                side_effect=transform_readiness,
            ),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_persisted_package_receipt"
            ) as verifier,
        ):
            packages, receipts, blockers = cli_mod._hydrate_configured_packages(
                workspace=workspace,
                run_id=run_id,
                spec=spec,
                task_ids=(task_id,),
            )

        self.assertEqual(packages, {})
        self.assertEqual(receipts, {})
        verifier.assert_not_called()
        self.assertEqual(len(blockers), 1)
        self.assertIn("complete prerequisite chain", blockers[0])
        self.assertIn(
            "gates_transform: configured input fingerprint changed", blockers[0]
        )
        self.assertIn("configured input fingerprint changed", blockers[0])

    def test_package_receipt_cannot_skip_stale_upstream_stage(self) -> None:
        workspace = self.root / "stale-author-package-workspace"
        export_dir = self.root / "stale-author-package-output"
        run_id = "stale-author-package"
        task_id = "synthetic__stale_author_package"
        self._register_tasks(workspace, (task_id,))
        (export_dir / task_id).mkdir(parents=True)
        receipt_path = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "package_verifications"
            / f"{task_id}.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text("{}\n", encoding="utf-8")
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.PACKAGED,
            workers=1,
            budget_per_task=3.0,
            export_dir=str(export_dir),
        )

        def readiness(_engine, _task, stage, *, spec=None):
            del spec
            if stage is StageName.AUTHOR:
                return StageReadiness(
                    stage=stage,
                    state=ReadinessState.STALE,
                    reason="author prompt identity changed",
                )
            return StageReadiness(stage=stage, state=ReadinessState.PASS)

        with (
            mock.patch.object(Engine, "final_verdict", return_value=FINAL_ACCEPTED),
            mock.patch(
                "elt_taskgen.pipeline_readiness.stage_readiness",
                side_effect=readiness,
            ),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_persisted_package_receipt"
            ) as verifier,
        ):
            packages, receipts, blockers = cli_mod._hydrate_configured_packages(
                workspace=workspace,
                run_id=run_id,
                spec=spec,
                task_ids=(task_id,),
            )

        self.assertEqual(packages, {})
        self.assertEqual(receipts, {})
        verifier.assert_not_called()
        self.assertEqual(len(blockers), 1)
        self.assertIn("author: author prompt identity changed", blockers[0])

    @unittest.skipUnless(os.name == "posix", "receipt-store symlink requires POSIX")
    def test_stale_provider_preflight_refuses_symlinked_receipt_store(
        self,
    ) -> None:
        workspace = self.root / "hydrate-symlink-workspace"
        export_dir = self.root / "hydrate-symlink-packages"
        external_receipts = self.root / "external-receipts"
        run_id = "hydrate-symlink"
        task_id = "synthetic__hydrate_symlink"
        selected = _SelectedRoster((task_id,))
        tasks = self._register_tasks(workspace, (task_id,))
        ingested = self._ingested(selected, tasks)
        (export_dir / task_id).mkdir(parents=True)
        external_receipts.mkdir()
        (external_receipts / f"{task_id}.json").write_text(
            "{}\n", encoding="utf-8"
        )
        run_root = workspace / "state" / "pipeline_runs" / run_id
        run_root.mkdir(parents=True)
        (run_root / "package_verifications").symlink_to(
            external_receipts, target_is_directory=True
        )

        args = self._pipeline_args(
            workspace,
            self.root / "pool.yaml",
            export_dir,
            run_id,
            1,
        )
        args.budget_total = 30.0
        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.ingest_manifest.load_five_source_batch_manifest",
                return_value=mock.sentinel.pool,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.select_candidate_manifest",
                return_value=selected,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.ingest_selected_sources",
                return_value=(selected, ingested),
            ),
            mock.patch.object(
                cli_mod, "_assert_configured_run_identity", return_value=None
            ),
            mock.patch.object(
                cli_mod,
                "_configured_live_provider_problems",
                return_value=("council admission is stale",),
            ),
            mock.patch.object(cli_mod, "_configured_results") as workers,
            mock.patch.object(cli_mod, "_resolve_provider") as provider,
            mock.patch.object(
                cli_mod,
                "_repository_run_identity",
                return_value={"head": "test", "working_tree_dirty": False},
            ),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_persisted_package_receipt"
            ) as verifier,
            mock.patch(
                "elt_taskgen.pipeline_readiness.stage_readiness",
                side_effect=self._stage_result,
            ),
            mock.patch.object(Engine, "final_verdict", return_value=FINAL_ACCEPTED),
            contextlib.redirect_stdout(output),
        ):
            code = cli_mod.cmd_pipeline(args)

        self.assertEqual(code, 2, output.getvalue())
        workers.assert_not_called()
        provider.assert_not_called()
        verifier.assert_not_called()
        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / run_id
                / "readiness.json"
            ).read_bytes()
        )
        self.assertEqual(report.counts.accepted, 1)
        self.assertEqual(report.counts.packaged, 0)
        self.assertTrue(
            any(
                "unsafe receipt ancestor" in blocker
                and "package_verifications" in blocker
                for blocker in report.blockers
            ),
            report.blockers,
        )

    def test_each_package_outcome_gets_a_running_readiness_checkpoint(self) -> None:
        workspace = self.root / "package-checkpoint-workspace"
        export_dir = self.root / "package-checkpoint-output"
        run_id = "package-checkpoints"
        task_ids = (
            "synthetic__package_checkpoint_0",
            "synthetic__package_checkpoint_1",
        )
        selected = _SelectedRoster(task_ids)
        tasks = self._register_tasks(workspace, task_ids)
        ingested = self._ingested(selected, tasks)
        worker_results = [
            {
                "task_id": task_id,
                "ok": True,
                "state": "ready",
                "detail": "",
                "usd": 0.0,
            }
            for task_id in task_ids
        ]
        observed_writes: list[dict] = []
        original_write = cli_mod._write_configured_report

        def capture_write(**kwargs):
            observed_writes.append(
                {
                    "state": kwargs["state"],
                    "packages": tuple(sorted((kwargs.get("packages") or {}).keys())),
                    "receipts": tuple(
                        sorted((kwargs.get("package_receipts") or {}).keys())
                    ),
                    "results": tuple(
                        (row["task_id"], row.get("state"), bool(row.get("ok")))
                        for row in kwargs.get("results", ())
                    ),
                }
            )
            return original_write(**kwargs)

        def freeze(_engine, task, destination):
            destination.mkdir(parents=True)
            (destination / "package_manifest.json").write_text(
                task.task_id, encoding="utf-8"
            )

        def verify_fresh(_package, *, receipt_path):
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text("{}\n", encoding="utf-8")
            return SimpleNamespace(verified=True, failures=())

        args = self._pipeline_args(
            workspace,
            self.root / "pool.yaml",
            export_dir,
            run_id,
            2,
        )
        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.ingest_manifest.load_five_source_batch_manifest",
                return_value=mock.sentinel.pool,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.select_candidate_manifest",
                return_value=selected,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.ingest_selected_sources",
                return_value=(selected, ingested),
            ),
            mock.patch.object(
                cli_mod, "_configured_results", return_value=worker_results
            ),
            mock.patch.object(
                cli_mod, "_configured_live_provider_problems", return_value=()
            ),
            mock.patch.object(
                cli_mod,
                "_repository_run_identity",
                return_value={"head": "test", "working_tree_dirty": False},
            ),
            mock.patch.object(
                cli_mod, "_write_configured_report", side_effect=capture_write
            ),
            mock.patch(
                "elt_taskgen.export.local_package.freeze_local_package",
                side_effect=freeze,
            ),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_fresh_local_package",
                side_effect=verify_fresh,
            ),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_persisted_package_receipt",
                return_value=SimpleNamespace(
                    ok=True, receipt_sha256="b" * 64, failures=()
                ),
            ),
            mock.patch(
                "elt_taskgen.pipeline_readiness.stage_readiness",
                side_effect=self._stage_result,
            ),
            mock.patch.object(
                Engine, "final_verdict", return_value=FINAL_ACCEPTED
            ),
            contextlib.redirect_stdout(output),
        ):
            code = cli_mod.cmd_pipeline(args)

        self.assertEqual(code, 0, output.getvalue())
        package_checkpoints = [
            row
            for row in observed_writes
            if row["state"] == "RUNNING" and row["packages"]
        ]
        self.assertEqual(
            [len(row["packages"]) for row in package_checkpoints],
            [1, 2],
        )
        self.assertEqual(
            package_checkpoints[0]["packages"],
            (task_ids[0],),
        )
        self.assertEqual(
            package_checkpoints[0]["receipts"],
            (task_ids[0],),
        )
        self.assertEqual(
            dict(
                (task_id, state)
                for task_id, state, _ok in package_checkpoints[0]["results"]
            ),
            {task_ids[0]: "packaged", task_ids[1]: "ready"},
        )
        self.assertEqual(
            package_checkpoints[1]["packages"],
            tuple(sorted(task_ids)),
        )
        self.assertEqual(
            package_checkpoints[1]["receipts"],
            tuple(sorted(task_ids)),
        )

        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / run_id
                / "readiness.json"
            ).read_bytes()
        )
        self.assertEqual(report.state, "COMPLETE")
        self.assertEqual(report.counts.packaged, 2)
        self.assertIn("accepted=2 packaged=2", output.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

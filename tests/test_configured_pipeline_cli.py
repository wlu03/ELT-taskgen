"""Coordinator contracts for arbitrary-count configured pipeline runs."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml
from pydantic import ValidationError

from elt_taskgen import cli as cli_mod
from elt_taskgen import demo_fixture
from elt_taskgen.engine import (
    Engine,
    FINAL_ACCEPTED,
    FINAL_REJECTED,
    STAGE_ORDER,
    StageName,
)
from elt_taskgen.ingest_manifest import (
    CandidateIngestOutcome,
    FiveSourceIngestError,
    FiveSourceIngestResult,
)
from elt_taskgen.models import Origin
from elt_taskgen.pipeline_readiness import (
    GenerationRunSpec,
    PipelineReadinessReport,
    ReadinessFailureClass,
    ReadinessProfile,
    ReadinessState,
    StageReadiness,
    make_run_report,
    seal_configured_stage,
    write_run_report,
)


class _SelectedRoster:
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
            "\0".join(entry.expected_task_id for entry in self._entries).encode()
        ).hexdigest()


class ConfiguredPipelineCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _args(
        self,
        workspace: Path,
        count: int,
        *,
        profile: str = "draft",
        run_id: str = "configured-test",
        export_dir: Path | None = None,
        budget_total: float | None = None,
    ):
        argv = [
            "pipeline",
            "--workspace",
            str(workspace),
            "--ingest-manifest",
            str(self.root / "pool.yaml"),
            "--candidate-count",
            str(count),
            "--readiness-profile",
            profile,
            "--workers",
            "1",
            "--run-id",
            run_id,
        ]
        if export_dir is not None:
            argv.extend(("--export-dir", str(export_dir)))
        if budget_total is not None:
            argv.extend(("--budget-total", str(budget_total)))
        return cli_mod.build_parser().parse_args(argv)

    @staticmethod
    def _tasks(workspace: Path, task_ids: tuple[str, ...]):
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

    def _patch_selection(self, selected, ingested):
        return (
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
        )

    def test_draft_cli_reports_exact_arbitrary_counts_one_two_and_seven(self) -> None:
        for count in (1, 2, 7):
            with self.subTest(count=count):
                workspace = self.root / f"workspace-{count}"
                task_ids = tuple(
                    f"synthetic__configured_{count}_{index}"
                    for index in range(count)
                )
                selected = _SelectedRoster(task_ids)
                tasks = self._tasks(workspace, task_ids)
                ingested = self._ingested(selected, tasks)
                args = self._args(
                    workspace, count, run_id=f"arbitrary-{count}"
                )
                patches = self._patch_selection(selected, ingested)
                with (
                    patches[0] as loader,
                    patches[1] as selector,
                    patches[2] as ingest,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(cli_mod.cmd_pipeline(args), 0)

                loader.assert_called_once()
                self.assertEqual(selector.call_args.kwargs["candidate_count"], count)
                self.assertEqual(ingest.call_args.kwargs["candidate_count"], count)
                report = PipelineReadinessReport.model_validate_json(
                    (
                        workspace
                        / "state"
                        / "pipeline_runs"
                        / f"arbitrary-{count}"
                        / "readiness.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(report.state, "COMPLETE")
                self.assertEqual(report.counts.requested, count)
                self.assertEqual(report.counts.generation_attempts, count)
                self.assertEqual(report.counts.candidates_created, count)
                self.assertEqual(len(report.tasks), count)
                self.assertEqual(
                    tuple(task.task_id for task in report.tasks), task_ids
                )

    def test_global_ingest_blocker_still_reports_every_uncreated_candidate(self) -> None:
        workspace = self.root / "blocked-workspace"
        task_ids = ("synthetic__uncreated_0", "synthetic__uncreated_1")
        selected = _SelectedRoster(task_ids)
        args = self._args(workspace, 2, run_id="global-ingest-block")

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
                side_effect=FiveSourceIngestError("catalog digest changed"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli_mod.cmd_pipeline(args), 2)

        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / "global-ingest-block"
                / "readiness.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(report.state, "BLOCKED")
        self.assertEqual(report.counts.requested, 2)
        self.assertEqual(report.counts.generation_attempts, 2)
        self.assertEqual(report.counts.candidates_created, 0)
        self.assertEqual(report.counts.blocked, 2)
        self.assertEqual(tuple(task.task_id for task in report.tasks), task_ids)
        self.assertTrue(all(task.disposition == "blocked" for task in report.tasks))
        self.assertTrue(all(not task.task_content_hash for task in report.tasks))
        for task_id in task_ids:
            self.assertTrue(
                (
                    workspace
                    / "state"
                    / "pipeline_runs"
                    / "global-ingest-block"
                    / "candidates"
                    / f"{task_id}.json"
                ).is_file()
            )

    def test_candidate_local_ingest_failure_controls_exit_and_run_state(self) -> None:
        workspace = self.root / "partial-ingest-workspace"
        task_ids = ("synthetic__created", "synthetic__ingest_failed")
        selected = _SelectedRoster(task_ids)
        tasks = self._tasks(workspace, task_ids[:1])
        ingested = FiveSourceIngestResult(
            manifest_sha256=selected.manifest_sha256(),
            tasks=tasks,
            receipt=None,
            dry_run=False,
            candidate_outcomes=(
                CandidateIngestOutcome(
                    origin=Origin.DBT,
                    task_id=task_ids[0],
                    state="created",
                ),
                CandidateIngestOutcome(
                    origin=Origin.DBT,
                    task_id=task_ids[1],
                    state="failed",
                    detail="source descriptor was invalid",
                ),
            ),
        )
        patches = self._patch_selection(selected, ingested)
        args = self._args(
            workspace,
            2,
            profile="local-ready",
            run_id="partial-ingest",
        )
        # The configured worker is mocked below, so mint the intake seal that
        # a real configured worker would persist. Every stage now has an
        # implementation-bound seal; an unsealed PASS is intentionally stale.
        engine = Engine(workspace)
        try:
            seal_configured_stage(
                engine,
                tasks[0],
                StageName.INTAKE,
                cli_mod._configured_run_spec(args),
            )
        finally:
            engine.close()
        output = io.StringIO()
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch.object(
                cli_mod,
                "_configured_results",
                return_value=[
                    {
                        "task_id": task_ids[0],
                        "ok": True,
                        "state": "ready",
                        "detail": "",
                        "usd": 0.0,
                    }
                ],
            ),
            mock.patch.object(
                cli_mod, "_configured_live_provider_problems", return_value=()
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli_mod.cmd_pipeline(args), 2)

        self.assertIn("selected 2 candidate(s), registered 1", output.getvalue())
        self.assertIn("requested=2 created=1", output.getvalue())

        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / "partial-ingest"
                / "readiness.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(report.state, "COMPLETE_WITH_ISSUES")
        self.assertEqual(report.counts.requested, 2)
        self.assertEqual(report.counts.candidates_created, 1)
        self.assertEqual(report.counts.failed, 1)
        self.assertEqual(len(report.tasks), 2)

    def test_typed_nonterminal_worker_states_always_exit_operationally(self) -> None:
        for worker_state in (
            "protocol_failure",
            "incomplete_protocol",
            "pending_adjudication",
            "stale",
        ):
            with self.subTest(worker_state=worker_state):
                suffix = worker_state.replace("_", "-")
                workspace = self.root / f"typed-state-{suffix}"
                task_id = f"synthetic__typed_{worker_state}"
                selected = _SelectedRoster((task_id,))
                tasks = self._tasks(workspace, (task_id,))
                ingested = self._ingested(selected, tasks)
                patches = self._patch_selection(selected, ingested)
                args = self._args(
                    workspace,
                    1,
                    profile="local-ready",
                    run_id=f"typed-{suffix}",
                )
                with (
                    patches[0],
                    patches[1],
                    patches[2],
                    mock.patch.object(
                        cli_mod,
                        "_configured_results",
                        return_value=[
                            {
                                "task_id": task_id,
                                "ok": False,
                                "state": worker_state,
                                "detail": "typed recovery is still required",
                                "usd": 0.0,
                            }
                        ],
                    ),
                    mock.patch.object(
                        cli_mod,
                        "_configured_live_provider_problems",
                        return_value=(),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    code = cli_mod.cmd_pipeline(args)

                self.assertEqual(code, 2)
                report = PipelineReadinessReport.model_validate_json(
                    (
                        workspace
                        / "state"
                        / "pipeline_runs"
                        / f"typed-{suffix}"
                        / "readiness.json"
                    ).read_bytes()
                )
                self.assertEqual(report.state, "COMPLETE_WITH_ISSUES")

    def test_live_configured_derives_a_shared_total_before_provider_or_workers(
        self,
    ) -> None:
        """A live configured run always carries a finite shared total: one
        the operator declared, else DEFAULT_BUDGET_TOTAL_PER_CANDIDATE_USD per
        requested candidate, derived while the spec is built and therefore
        before any provider is constructed or worker dispatched."""
        workspace = self.root / "derived-shared-budget-workspace"
        args = self._args(
            workspace,
            3,
            profile="local-ready",
            run_id="derived-shared-budget",
        )
        args.budget_total = None
        spec = cli_mod._configured_run_spec(args)
        self.assertEqual(
            spec.budget_total, cli_mod.DEFAULT_BUDGET_TOTAL_PER_CANDIDATE_USD * 3
        )
        explicit = self._args(
            workspace, 3, profile="local-ready", run_id="explicit-shared-budget"
        )
        explicit.budget_total = 12.5
        self.assertEqual(cli_mod._configured_run_spec(explicit).budget_total, 12.5)

    def test_live_configured_with_shared_total_reaches_workers(self) -> None:
        workspace = self.root / "explicit-shared-budget-workspace"
        task_ids = ("synthetic__explicit_shared_budget",)
        selected = _SelectedRoster(task_ids)
        tasks = self._tasks(workspace, task_ids)
        ingested = self._ingested(selected, tasks)
        args = self._args(
            workspace,
            1,
            profile="local-ready",
            run_id="explicit-shared-budget",
            budget_total=3.0,
        )
        patches = self._patch_selection(selected, ingested)
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_API_KEY": "present-for-test",
                    "ELT_TASKGEN_OSS_BASE_URL": "https://gateway.example/v1",
                    "ELT_TASKGEN_OSS_API_KEY": "present-for-test",
                    "ELT_TASKGEN_OSS_MODEL": "moonshotai/kimi-k2.7-code",
                },
                clear=False,
            ),
            mock.patch.object(
                cli_mod,
                "_configured_results",
                return_value=[
                    {
                        "task_id": task_ids[0],
                        "ok": True,
                        "state": "ready",
                        "detail": "",
                        "usd": 0.0,
                    }
                ],
            ) as workers,
            mock.patch.object(cli_mod, "_resolve_provider") as provider,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli_mod.cmd_pipeline(args), 0)

        workers.assert_called_once()
        self.assertEqual(workers.call_args.kwargs["spec"].budget_total, 3.0)
        provider.assert_not_called()
        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / "explicit-shared-budget"
                / "readiness.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(report.state, "COMPLETE")

    def test_packaged_preflights_all_live_credentials_before_workers(self) -> None:
        workspace = self.root / "credential-preflight-workspace"
        task_ids = ("synthetic__credential_preflight",)
        selected = _SelectedRoster(task_ids)
        tasks = self._tasks(workspace, task_ids)
        ingested = self._ingested(selected, tasks)
        args = self._args(
            workspace,
            1,
            profile="packaged",
            run_id="credential-preflight",
            export_dir=self.root / "credential-preflight-packages",
            budget_total=3.0,
        )
        patches = self._patch_selection(selected, ingested)
        output = io.StringIO()
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch(
                "elt_taskgen.review.providers.load_role_routing",
                return_value=mock.sentinel.routing,
            ) as load_routing,
            mock.patch(
                "elt_taskgen.review.providers.credential_problems",
                return_value=[
                    "ELT_TASKGEN_OSS_API_KEY is not set (openai_compat provider)"
                ],
            ) as credentials,
            mock.patch.object(cli_mod, "_configured_results") as workers,
            mock.patch.object(cli_mod, "_resolve_provider") as provider,
            mock.patch.object(
                cli_mod,
                "_write_configured_report",
                wraps=cli_mod._write_configured_report,
            ) as report_writer,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli_mod.cmd_pipeline(args), 2)

        load_routing.assert_called_once_with(None)
        checked_roles = credentials.call_args.args[1]
        self.assertEqual(
            set(checked_roles),
            {
                "semantic_author",
                "ambiguity_critic",
                "population_adversary",
                "shortcut_attacker",
                "feasibility_reviewer",
                "independent_implementer",
                "independent_loader",
                "repair_proposer",
            },
        )
        workers.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(
            report_writer.call_args_list[-1].kwargs["results"][0]["state"],
            "run_preflight_blocked",
        )
        self.assertIn("blocked before provider workers", output.getvalue())
        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / "credential-preflight"
                / "readiness.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(report.state, "BLOCKED")
        self.assertEqual(report.counts.blocked, 1)
        self.assertEqual(report.tasks[0].disposition, "blocked")
        self.assertTrue(
            any("ELT_TASKGEN_OSS_API_KEY" in row for row in report.blockers)
        )

    def test_packaged_refuses_same_family_or_unpriced_witness_before_provider(self) -> None:
        scenarios = (
            ("same-family", "anthropic/claude-opus-5", "cross-family"),
            ("unpriced", "vendor/unpriced-model", "no pricing entry"),
        )
        for label, model, expected in scenarios:
            with self.subTest(label=label):
                workspace = self.root / f"{label}-preflight-workspace"
                task_ids = (f"synthetic__{label}_preflight",)
                selected = _SelectedRoster(task_ids)
                tasks = self._tasks(workspace, task_ids)
                ingested = self._ingested(selected, tasks)
                args = self._args(
                    workspace,
                    1,
                    profile="packaged",
                    run_id=f"{label}-preflight",
                    export_dir=self.root / f"{label}-packages",
                    budget_total=3.0,
                )
                patches = self._patch_selection(selected, ingested)
                output = io.StringIO()
                with (
                    patches[0],
                    patches[1],
                    patches[2],
                    mock.patch.dict(
                        "os.environ",
                        {
                            "ANTHROPIC_API_KEY": "present-for-test",
                            "ELT_TASKGEN_OSS_BASE_URL": "https://gateway.example/v1",
                            "ELT_TASKGEN_OSS_API_KEY": "present-for-test",
                            "ELT_TASKGEN_OSS_MODEL": model,
                        },
                        clear=False,
                    ),
                    mock.patch.object(cli_mod, "_configured_results") as workers,
                    mock.patch.object(cli_mod, "_resolve_provider") as provider,
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(cli_mod.cmd_pipeline(args), 2)

                workers.assert_not_called()
                provider.assert_not_called()
                self.assertIn(expected, output.getvalue())
                report = PipelineReadinessReport.model_validate_json(
                    (
                        workspace
                        / "state"
                        / "pipeline_runs"
                        / f"{label}-preflight"
                        / "readiness.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(report.state, "BLOCKED")
                self.assertEqual(report.tasks[0].disposition, "blocked")
                self.assertTrue(any(expected in row for row in report.blockers))

    def test_draft_and_replay_only_skip_live_provider_preflight(self) -> None:
        for profile, replay_only in (
            (ReadinessProfile.DRAFT, False),
            (ReadinessProfile.PACKAGED, True),
        ):
            with self.subTest(profile=profile.value, replay_only=replay_only):
                spec = GenerationRunSpec(
                    candidate_count=1,
                    profile=profile,
                    budget_per_task=7.0,
                    export_dir=(
                        str((self.root / "replay-packages").resolve())
                        if profile is ReadinessProfile.PACKAGED
                        else ""
                    ),
                )
                args = SimpleNamespace(
                    replay_only=replay_only,
                    repair_proposer=True,
                )
                with mock.patch(
                    "elt_taskgen.review.providers.load_role_routing"
                ) as load_routing:
                    self.assertEqual(
                        cli_mod._configured_live_provider_problems(args, spec),
                        (),
                    )
                load_routing.assert_not_called()

    def test_live_preflight_refuses_stale_explicit_admission(self) -> None:
        from elt_taskgen.review import metrology

        admission = self.root / "council.live_admitted"
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.PACKAGED,
            budget_per_task=7.0,
            budget_total=20.0,
            export_dir=str((self.root / "packages").resolve()),
            admission_reference=str(admission),
        )
        args = SimpleNamespace(
            workspace=self.root / "workspace",
            replay_only=False,
            repair_proposer=False,
        )
        route = SimpleNamespace(provider="fixture", model="fixture-model")
        routing = SimpleNamespace(
            for_role=lambda _role: route,
            provider_config={"fixture": {}},
        )
        stale = metrology.AdmissionStatus(
            ok=False,
            reason="critic tool surface is stale",
            path=admission,
        )
        with (
            mock.patch(
                "elt_taskgen.review.providers.load_role_routing",
                return_value=routing,
            ),
            mock.patch(
                "elt_taskgen.review.providers.credential_problems",
                return_value=[],
            ),
            mock.patch(
                "elt_taskgen.reference.independent._refuse_same_family"
            ),
            mock.patch(
                "elt_taskgen.review.providers.rate_card_for",
                return_value=mock.sentinel.rate,
            ),
            mock.patch(
                "elt_taskgen.review.metrology.council_routing_fingerprint",
                return_value="a" * 64,
            ),
            mock.patch(
                "elt_taskgen.review.metrology.admission_status",
                return_value=stale,
            ) as status,
        ):
            problems = cli_mod._configured_live_provider_problems(args, spec)

        self.assertEqual(len(problems), 1)
        self.assertIn("critic tool surface is stale", problems[0])
        status.assert_called_once_with(
            Path(args.workspace).resolve(),
            routing_fingerprint="a" * 64,
            agents_config=None,
            record_path=admission,
        )

    def test_live_preflight_refuses_invalid_configured_rate(self) -> None:
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.PACKAGED,
            budget_per_task=7.0,
            budget_total=20.0,
            export_dir=str((self.root / "packages").resolve()),
        )
        args = SimpleNamespace(
            workspace=self.root / "workspace",
            replay_only=False,
            repair_proposer=False,
        )
        author_route = SimpleNamespace(
            provider="anthropic", model="claude-sonnet-5"
        )
        witness_route = SimpleNamespace(
            provider="openai_compat", model="fixture-witness"
        )
        routing = SimpleNamespace(
            for_role=lambda role: (
                witness_route if role.startswith("independent_") else author_route
            ),
            provider_config={
                "anthropic": {},
                "openai_compat": {
                    "pricing": {
                        "fixture-witness": {
                            "usd_per_mtok_input": float("nan"),
                            "usd_per_mtok_output": 2.0,
                        }
                    }
                },
            },
        )
        with (
            mock.patch(
                "elt_taskgen.review.providers.load_role_routing",
                return_value=routing,
            ),
            mock.patch(
                "elt_taskgen.review.providers.credential_problems",
                return_value=[],
            ),
        ):
            problems = cli_mod._configured_live_provider_problems(args, spec)

        self.assertEqual(len(problems), 1)
        self.assertIn("unpriced provider route", problems[0])
        self.assertIn("finite, non-negative", problems[0])

    def test_mixed_outcomes_package_only_the_accepted_candidate(self) -> None:
        workspace = self.root / "mixed-workspace"
        export_dir = self.root / "packages"
        task_ids = tuple(f"synthetic__mixed_{index}" for index in range(4))
        selected = _SelectedRoster(task_ids)
        tasks = self._tasks(workspace, task_ids[:3])
        ingested = FiveSourceIngestResult(
            manifest_sha256=selected.manifest_sha256(),
            tasks=tasks,
            receipt=None,
            dry_run=False,
            candidate_outcomes=(
                *(
                    CandidateIngestOutcome(
                        origin=Origin.DBT,
                        task_id=task.task_id,
                        state="created",
                    )
                    for task in tasks
                ),
                CandidateIngestOutcome(
                    origin=Origin.DBT,
                    task_id=task_ids[3],
                    state="failed",
                    detail="adapter rejected malformed source",
                ),
            ),
        )
        worker_results = [
            {
                "task_id": task_ids[0],
                "ok": True,
                "state": "ready",
                "detail": "",
                "usd": 0.5,
            },
            {
                "task_id": task_ids[1],
                "ok": False,
                "state": FINAL_REJECTED,
                "detail": "candidate failed semantic gates",
                "usd": 0.25,
            },
            {
                "task_id": task_ids[2],
                "ok": False,
                "state": "blocked",
                "detail": "provider unavailable",
                "usd": 0.0,
            },
        ]

        def stage_result(_engine, task, stage, *, spec=None):
            if task.task_id == task_ids[0]:
                state = (
                    ReadinessState.PASS
                    if STAGE_ORDER.index(stage)
                    <= STAGE_ORDER.index(StageName.GATES_TRANSFORM)
                    else ReadinessState.NOT_RUN
                )
            elif task.task_id == task_ids[1] and stage is StageName.GENERATE:
                state = ReadinessState.FAIL
            else:
                state = ReadinessState.NOT_RUN
            return StageReadiness(stage=stage, state=state)

        original_final = Engine.final_verdict

        def final_verdict(engine, task_id):
            if task_id == task_ids[0]:
                return FINAL_ACCEPTED
            if task_id == task_ids[1]:
                return FINAL_REJECTED
            return original_final(engine, task_id)

        def freeze(_engine, task, destination):
            destination.mkdir(parents=True)
            (destination / "package_manifest.json").write_text(
                task.task_id, encoding="utf-8"
            )

        def verify_fresh(_package, *, receipt_path):
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text("verified\n", encoding="utf-8")
            return SimpleNamespace(verified=True, failures=())

        patches = self._patch_selection(selected, ingested)
        args = self._args(
            workspace,
            4,
            profile="packaged",
            run_id="mixed-run",
            export_dir=export_dir,
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch.object(
                cli_mod, "_configured_results", return_value=worker_results
            ),
            mock.patch.object(
                cli_mod, "_configured_live_provider_problems", return_value=()
            ),
            mock.patch(
                "elt_taskgen.export.local_package.freeze_local_package",
                side_effect=freeze,
            ) as freezer,
            mock.patch(
                "elt_taskgen.export.local_package.verify_local_package",
                return_value=SimpleNamespace(ok=True),
            ),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_fresh_local_package",
                side_effect=verify_fresh,
            ),
            mock.patch(
                "elt_taskgen.export.package_verification.verify_persisted_package_receipt",
                return_value=SimpleNamespace(
                    ok=True, receipt_sha256="a" * 64, failures=()
                ),
            ),
            mock.patch(
                "elt_taskgen.pipeline_readiness.stage_readiness",
                side_effect=stage_result,
            ),
            mock.patch.object(Engine, "final_verdict", new=final_verdict),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli_mod.cmd_pipeline(args), 2)

        self.assertEqual(freezer.call_count, 1)
        self.assertEqual(freezer.call_args.args[1].task_id, task_ids[0])
        self.assertEqual(
            freezer.call_args.args[2], export_dir.resolve() / task_ids[0]
        )
        self.assertFalse((export_dir / task_ids[1]).exists())
        self.assertFalse((export_dir / task_ids[2]).exists())

        report = PipelineReadinessReport.model_validate_json(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / "mixed-run"
                / "readiness.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(len(report.tasks), 4)
        self.assertEqual(report.counts.requested, 4)
        self.assertEqual(report.counts.candidates_created, 3)
        self.assertEqual(report.counts.accepted, 1)
        self.assertEqual(report.counts.exported, 1)
        self.assertEqual(report.counts.rejected, 1)
        self.assertEqual(report.counts.failed, 1)
        self.assertEqual(report.counts.blocked, 1)
        self.assertEqual(
            {task.task_id: task.disposition for task in report.tasks},
            {
                task_ids[0]: "accepted",
                task_ids[1]: "rejected",
                task_ids[2]: "blocked",
                task_ids[3]: "failed",
            },
        )
        self.assertEqual(
            [task.task_id for task in report.tasks if task.packaged_for_evaluation],
            [task_ids[0]],
        )
        self.assertTrue(
            any(task_ids[3] in blocker for blocker in report.blockers),
            report.blockers,
        )

    def test_single_worker_exception_pauses_untouched_siblings(self) -> None:
        workspace = self.root / "worker-isolation"
        task_ids = (
            "synthetic__worker_0",
            "synthetic__worker_1",
            "synthetic__worker_2",
        )
        self._tasks(workspace, task_ids)
        spec = GenerationRunSpec(
            candidate_count=3,
            profile=ReadinessProfile.LOCAL_READY,
            workers=1,
            budget_per_task=7.0,
        )
        args = self._args(workspace, 3, profile="local-ready")
        dispatched: list[str] = []

        def worker(payload):
            dispatched.append(payload["task_id"])
            if payload["task_id"] == task_ids[0]:
                attempt_dir = (
                    workspace
                    / "state"
                    / "pipeline_runs"
                    / "worker-isolation"
                    / "attempts"
                    / task_ids[0]
                )
                attempt_dir.mkdir(parents=True)
                (attempt_dir / "crashed.json").write_text(
                    json.dumps(
                        {
                            "attempt_id": "crashed",
                            "task_id": task_ids[0],
                            "target_stage": StageName.GATES_TRANSFORM.value,
                            "state": "RUNNING",
                        }
                    ),
                    encoding="utf-8",
                )
                raise RuntimeError("task-local crash")
            raise AssertionError("a later sequential worker was dispatched")

        with (
            mock.patch.object(
                cli_mod,
                "_pipeline_batch_intake_preflight",
                return_value=(set(), []),
            ),
            mock.patch.object(cli_mod, "_pipeline_worker", side_effect=worker),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            results = cli_mod._configured_results(
                args,
                spec=spec,
                task_ids=task_ids,
                run_id="worker-isolation",
                target_stage=StageName.GATES_TRANSFORM,
            )

        self.assertEqual([row["task_id"] for row in results], list(task_ids))
        self.assertEqual(dispatched, [task_ids[0]])
        self.assertEqual(results[0]["state"], "infrastructure")
        self.assertIn("RuntimeError: task-local crash", results[0]["detail"])
        self.assertEqual(
            [row["state"] for row in results[1:]], ["blocked", "blocked"]
        )
        for row in results[1:]:
            self.assertFalse(row["ok"])
            self.assertEqual(row["usd"], 0.0)
            self.assertIn("shared safety pause", row["detail"])
            self.assertIn(task_ids[0], row["detail"])
            self.assertIn("infrastructure", row["detail"])
            self.assertIn("not dispatched", row["detail"])
        crashed = json.loads(
            (
                workspace
                / "state"
                / "pipeline_runs"
                / "worker-isolation"
                / "attempts"
                / task_ids[0]
                / "crashed.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(crashed["state"], "INTERRUPTED")
        self.assertIn("RuntimeError: task-local crash", crashed["detail"])

    def test_single_worker_usage_error_pauses_without_dispatching_later_tasks(
        self,
    ) -> None:
        workspace = self.root / "worker-usage-pause"
        task_ids = tuple(f"synthetic__usage_{index}" for index in range(3))
        self._tasks(workspace, task_ids)
        spec = GenerationRunSpec(
            candidate_count=3,
            profile=ReadinessProfile.LOCAL_READY,
            workers=1,
            budget_per_task=7.0,
        )
        args = self._args(workspace, 3, profile="local-ready")
        dispatched: list[str] = []

        def worker(payload):
            dispatched.append(payload["task_id"])
            return {
                "task_id": payload["task_id"],
                "ok": False,
                "state": "usage_error",
                "detail": "shared provider configuration is invalid",
                "usd": 0.0,
            }

        with (
            mock.patch.object(
                cli_mod,
                "_pipeline_batch_intake_preflight",
                return_value=(set(), []),
            ),
            mock.patch.object(cli_mod, "_pipeline_worker", side_effect=worker),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            results = cli_mod._configured_results(
                args,
                spec=spec,
                task_ids=task_ids,
                run_id="worker-usage-pause",
                target_stage=StageName.GATES_TRANSFORM,
            )

        self.assertEqual(dispatched, [task_ids[0]])
        self.assertEqual([row["task_id"] for row in results], list(task_ids))
        self.assertEqual(
            [row["state"] for row in results],
            ["usage_error", "blocked", "blocked"],
        )
        self.assertTrue(
            all("not dispatched" in row["detail"] for row in results[1:])
        )

    def test_single_worker_task_budget_failure_does_not_pause_siblings(
        self,
    ) -> None:
        workspace = self.root / "worker-task-budget"
        task_ids = tuple(f"synthetic__task_budget_{index}" for index in range(3))
        self._tasks(workspace, task_ids)
        spec = GenerationRunSpec(
            candidate_count=3,
            profile=ReadinessProfile.LOCAL_READY,
            workers=1,
            budget_per_task=7.0,
        )
        args = self._args(workspace, 3, profile="local-ready")
        dispatched: list[str] = []

        def worker(payload):
            dispatched.append(payload["task_id"])
            if payload["task_id"] == task_ids[0]:
                return {
                    "task_id": payload["task_id"],
                    "ok": False,
                    "state": "infrastructure",
                    "detail": "this task exhausted its own budget",
                    "infrastructure_scope": "task",
                    "usd": 0.0,
                }
            return {
                "task_id": payload["task_id"],
                "ok": True,
                "state": "ready",
                "detail": "",
                "usd": 0.0,
            }

        with (
            mock.patch.object(
                cli_mod,
                "_pipeline_batch_intake_preflight",
                return_value=(set(), []),
            ),
            mock.patch.object(cli_mod, "_pipeline_worker", side_effect=worker),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            results = cli_mod._configured_results(
                args,
                spec=spec,
                task_ids=task_ids,
                run_id="worker-task-budget",
                target_stage=StageName.GATES_TRANSFORM,
            )

        self.assertEqual(dispatched, list(task_ids))
        self.assertEqual([row["task_id"] for row in results], list(task_ids))
        self.assertEqual(
            [row["state"] for row in results],
            ["infrastructure", "ready", "ready"],
        )
        self.assertEqual(results[0]["infrastructure_scope"], "task")

    def test_single_worker_total_budget_failure_pauses_untouched_siblings(
        self,
    ) -> None:
        workspace = self.root / "worker-total-budget"
        task_ids = tuple(f"synthetic__total_budget_{index}" for index in range(3))
        self._tasks(workspace, task_ids)
        spec = GenerationRunSpec(
            candidate_count=3,
            profile=ReadinessProfile.LOCAL_READY,
            workers=1,
            budget_per_task=7.0,
        )
        args = self._args(workspace, 3, profile="local-ready")
        dispatched: list[str] = []

        def worker(payload):
            dispatched.append(payload["task_id"])
            if payload["task_id"] != task_ids[0]:
                raise AssertionError("a later sequential worker was dispatched")
            return {
                "task_id": payload["task_id"],
                "ok": False,
                "state": "infrastructure",
                "detail": "the shared run budget is exhausted",
                "infrastructure_scope": "total",
                "usd": 0.0,
            }

        with (
            mock.patch.object(
                cli_mod,
                "_pipeline_batch_intake_preflight",
                return_value=(set(), []),
            ),
            mock.patch.object(cli_mod, "_pipeline_worker", side_effect=worker),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            results = cli_mod._configured_results(
                args,
                spec=spec,
                task_ids=task_ids,
                run_id="worker-total-budget",
                target_stage=StageName.GATES_TRANSFORM,
            )

        self.assertEqual(dispatched, [task_ids[0]])
        self.assertEqual([row["task_id"] for row in results], list(task_ids))
        self.assertEqual(
            [row["state"] for row in results],
            ["infrastructure", "blocked", "blocked"],
        )
        for row in results[1:]:
            self.assertIn("shared safety pause", row["detail"])
            self.assertIn("budget scope 'total'", row["detail"])
            self.assertIn("not dispatched", row["detail"])

    def test_single_worker_task_local_outcomes_do_not_pause_siblings(self) -> None:
        workspace = self.root / "worker-task-local-outcomes"
        task_ids = tuple(f"synthetic__local_{index}" for index in range(4))
        self._tasks(workspace, task_ids)
        spec = GenerationRunSpec(
            candidate_count=4,
            profile=ReadinessProfile.LOCAL_READY,
            workers=1,
            budget_per_task=7.0,
        )
        args = self._args(workspace, 4, profile="local-ready")
        states = (FINAL_REJECTED, "blocked", "blocked_limit", "ready")
        dispatched: list[str] = []

        def worker(payload):
            dispatched.append(payload["task_id"])
            state = states[len(dispatched) - 1]
            return {
                "task_id": payload["task_id"],
                "ok": state == "ready",
                "state": state,
                "detail": "task-local outcome",
                "usd": 0.0,
            }

        with (
            mock.patch.object(
                cli_mod,
                "_pipeline_batch_intake_preflight",
                return_value=(set(), []),
            ),
            mock.patch.object(cli_mod, "_pipeline_worker", side_effect=worker),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            results = cli_mod._configured_results(
                args,
                spec=spec,
                task_ids=task_ids,
                run_id="worker-task-local-outcomes",
                target_stage=StageName.GATES_TRANSFORM,
            )

        self.assertEqual(dispatched, list(task_ids))
        self.assertEqual([row["state"] for row in results], list(states))

    def test_explicit_run_id_cannot_be_rebound_to_config_or_roster(self) -> None:
        workspace = self.root / "run-identity-workspace"
        run_id = "stable-run-id"
        initial_ids = ("synthetic__identity_initial",)
        initial = _SelectedRoster(initial_ids)
        tasks = self._tasks(workspace, initial_ids)
        ingested = self._ingested(initial, tasks)
        args = self._args(workspace, 1, run_id=run_id)
        patches = self._patch_selection(initial, ingested)
        with (
            patches[0],
            patches[1],
            patches[2],
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli_mod.cmd_pipeline(args), 0)

        report_path = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "readiness.json"
        )
        original_report = report_path.read_bytes()

        changed_config = _SelectedRoster(
            ("synthetic__identity_other_0", "synthetic__identity_other_1")
        )
        changed_args = self._args(workspace, 2, run_id=run_id)
        with (
            mock.patch(
                "elt_taskgen.ingest_manifest.load_five_source_batch_manifest",
                return_value=mock.sentinel.pool,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.select_candidate_manifest",
                return_value=changed_config,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.ingest_selected_sources"
            ) as changed_ingest,
            self.assertRaisesRegex(
                cli_mod.CliUsageError, "different run identity"
            ),
        ):
            cli_mod.cmd_pipeline(changed_args)
        changed_ingest.assert_not_called()
        self.assertEqual(report_path.read_bytes(), original_report)

        changed_roster = _SelectedRoster(("synthetic__identity_reselected",))
        with (
            mock.patch(
                "elt_taskgen.ingest_manifest.load_five_source_batch_manifest",
                return_value=mock.sentinel.pool,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.select_candidate_manifest",
                return_value=changed_roster,
            ),
            mock.patch(
                "elt_taskgen.ingest_manifest.ingest_selected_sources"
            ) as roster_ingest,
            self.assertRaisesRegex(
                cli_mod.CliUsageError, "selected-roster fingerprint differs"
            ),
        ):
            cli_mod.cmd_pipeline(args)
        roster_ingest.assert_not_called()
        self.assertEqual(report_path.read_bytes(), original_report)

    def test_run_config_paths_are_relative_to_config_and_cli_paths_override(self) -> None:
        config_dir = self.root / "portable-config"
        config_dir.mkdir()
        config_path = config_dir / "run.yaml"
        task_id = "synthetic__runtime_evidence"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "generation-run-v1",
                    "candidate_count": 1,
                    "profile": "release",
                    "budget_per_task": 7.0,
                    "empirical": True,
                    "agents_config": "routing/agents.yaml",
                    "admission_reference": "evidence/admission.json",
                    "export_dir": "outputs/release",
                    "runtime_certification_store": "runtime/store",
                    "runtime_difficulty_reports": {
                        task_id: "runtime/difficulty.json"
                    },
                }
            ),
            encoding="utf-8",
        )
        parser = cli_mod.build_parser()
        args = parser.parse_args(
            [
                "pipeline",
                "--workspace",
                str(self.root / "path-workspace"),
                "--run-config",
                str(config_path),
            ]
        )
        spec = cli_mod._configured_run_spec(args)
        self.assertEqual(
            spec.agents_config,
            str((config_dir / "routing" / "agents.yaml").resolve()),
        )
        self.assertEqual(
            spec.admission_reference,
            str((config_dir / "evidence" / "admission.json").resolve()),
        )
        self.assertEqual(
            spec.export_dir,
            str((config_dir / "outputs" / "release").resolve()),
        )
        self.assertEqual(
            spec.runtime_certification_store,
            str((config_dir / "runtime" / "store").resolve()),
        )
        self.assertEqual(
            spec.runtime_difficulty_reports,
            {task_id: str((config_dir / "runtime" / "difficulty.json").resolve())},
        )

        cli_agents = (self.root / "cli-agents.yaml").resolve()
        cli_admission = (self.root / "cli-admission.json").resolve()
        cli_export = (self.root / "cli-release").resolve()
        overridden = parser.parse_args(
            [
                "pipeline",
                "--workspace",
                str(self.root / "path-workspace"),
                "--run-config",
                str(config_path),
                "--agents-config",
                str(cli_agents),
                "--admission-reference",
                str(cli_admission),
                "--export-dir",
                str(cli_export),
            ]
        )
        overridden_spec = cli_mod._configured_run_spec(overridden)
        self.assertEqual(overridden_spec.agents_config, str(cli_agents))
        self.assertEqual(
            overridden_spec.admission_reference, str(cli_admission)
        )
        self.assertEqual(overridden_spec.export_dir, str(cli_export))

    def test_typed_run_config_never_coerces_boolean_counts_to_integers(self) -> None:
        for field in (
            "candidate_count",
            "seed",
            "workers",
            "max_repair_rounds",
            "repair_attempts",
            "http_retries",
            "schema_retries",
        ):
            values = {
                "candidate_count": 1,
                "profile": ReadinessProfile.LOCAL_READY,
                "budget_per_task": 7.0,
                field: True,
            }
            with self.subTest(field=field), self.assertRaisesRegex(
                ValidationError, "must not be booleans"
            ):
                GenerationRunSpec.model_validate(values)

        with self.assertRaisesRegex(ValidationError, "must not be booleans"):
            GenerationRunSpec(
                candidate_count=1,
                profile=ReadinessProfile.LOCAL_READY,
                budget_per_task=7.0,
                source_families=("dbt",),
                source_allocation={"dbt": True},
            )

    def test_pipeline_status_release_requires_current_release_stage(self) -> None:
        workspace = self.root / "release-status-workspace"
        run_id = "release-status"
        task_id = "synthetic__release_status"
        self._tasks(workspace, (task_id,))
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.RELEASE,
            budget_per_task=7.0,
            empirical=True,
            export_dir=str((self.root / "release-status-output").resolve()),
        )
        engine = Engine(workspace)
        try:
            report = make_run_report(
                engine=engine,
                run_id=run_id,
                spec=spec,
                task_ids=(task_id,),
                state="COMPLETE",
            )
        finally:
            engine.close()
        write_run_report(report, workspace=workspace)

        # Audit/current release readiness is deliberately true, but the final
        # immutable release stage itself has no current PASS evidence.
        refreshed = report.tasks[0].model_copy(
            update={
                "release_ready": True,
                "stages": tuple(
                    StageReadiness(
                        stage=stage,
                        state=(
                            ReadinessState.NOT_RUN
                            if stage is StageName.RELEASE
                            else ReadinessState.PASS
                        ),
                    )
                    for stage in STAGE_ORDER
                ),
            }
        )
        args = cli_mod.build_parser().parse_args(
            [
                "pipeline-status",
                "--workspace",
                str(workspace),
                "--run-id",
                run_id,
            ]
        )
        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.pipeline_readiness.task_readiness",
                return_value=refreshed,
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli_mod.cmd_pipeline_status(args), 2)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["valid"])
        self.assertTrue(
            any("not at 'release' milestone" in row for row in payload["problems"])
        )

    def test_pipeline_status_invalid_configured_runtime_evidence_is_nonzero(
        self,
    ) -> None:
        workspace = self.root / "runtime-status-workspace"
        run_id = "runtime-status"
        task_id = "synthetic__runtime_status"
        self._tasks(workspace, (task_id,))
        release = (self.root / "runtime-status-release").resolve()
        release.mkdir()
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.RELEASE,
            budget_per_task=7.0,
            empirical=True,
            export_dir=str(release),
            runtime_certification_store=str(
                (self.root / "missing-certification-store").resolve()
            ),
            runtime_difficulty_reports={
                task_id: str((self.root / "missing-difficulty.json").resolve())
            },
        )
        engine = Engine(workspace)
        try:
            report = make_run_report(
                engine=engine,
                run_id=run_id,
                spec=spec,
                task_ids=(task_id,),
                state="COMPLETE",
                package_paths={task_id: release},
            )
        finally:
            engine.close()
        write_run_report(report, workspace=workspace)
        refreshed = report.tasks[0].model_copy(
            update={
                "release_ready": True,
                "stages": tuple(
                    StageReadiness(
                        stage=stage,
                        state=ReadinessState.PASS,
                    )
                    for stage in STAGE_ORDER
                ),
            }
        )
        self.assertTrue(refreshed.runtime_evidence.configured)
        self.assertFalse(refreshed.runtime_evidence.certification_verified)
        self.assertFalse(refreshed.runtime_evidence.difficulty_verified)

        args = cli_mod.build_parser().parse_args(
            [
                "pipeline-status",
                "--workspace",
                str(workspace),
                "--run-id",
                run_id,
            ]
        )
        output = io.StringIO()
        with (
            mock.patch(
                "elt_taskgen.pipeline_readiness.task_readiness",
                return_value=refreshed,
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli_mod.cmd_pipeline_status(args), 2)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["valid"])
        self.assertTrue(
            any("runtime certification" in row for row in payload["problems"])
        )
        self.assertTrue(
            any(
                "runtime certification did not verify" in row
                or "runtime-certified difficulty" in row
                for row in payload["problems"]
            )
        )

    def test_resume_preserves_interrupted_and_new_attempt_history(self) -> None:
        workspace = self.root / "attempt-workspace"
        run_id = "resume-history"
        task_id = "synthetic__attempt"
        attempt_dir = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "attempts"
            / task_id
        )
        attempt_dir.mkdir(parents=True)
        prior_path = attempt_dir / "000-prior.json"
        prior_path.write_text(
            json.dumps(
                {
                    "attempt_id": "prior",
                    "task_id": task_id,
                    "target_stage": StageName.GENERATE.value,
                    "state": "RUNNING",
                }
            ),
            encoding="utf-8",
        )

        with mock.patch.object(
            cli_mod,
            "_resolve_provider",
            side_effect=cli_mod.CliUsageError("offline provider"),
        ):
            result = cli_mod._pipeline_worker(
                {
                    "workspace": str(workspace),
                    "task_id": task_id,
                    "until_stage": StageName.GENERATE.value,
                    "run_id": run_id,
                }
            )
        self.assertEqual(result["state"], "usage_error")
        self.assertEqual(
            json.loads(prior_path.read_text(encoding="utf-8"))["state"],
            "INTERRUPTED",
        )

        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.LOCAL_READY,
            budget_per_task=7.0,
        )
        engine = Engine(workspace)
        try:
            report = make_run_report(
                engine=engine,
                run_id=run_id,
                spec=spec,
                task_ids=(task_id,),
                state="COMPLETE_WITH_ISSUES",
                worker_results=(result,),
            )
        finally:
            engine.close()
        self.assertEqual(len(report.tasks), 1)
        self.assertEqual(report.tasks[0].disposition, "failed")
        self.assertEqual(
            {attempt.state for attempt in report.tasks[0].attempts},
            {"INTERRUPTED", "usage_error"},
        )
        self.assertTrue(
            all(attempt.evidence_path for attempt in report.tasks[0].attempts)
        )

    def test_worker_and_run_report_preserve_typed_block_cause(self) -> None:
        cases = (
            (
                "protocol_failure",
                "critic_attack_handoff_invalid",
                "provider",
            ),
            (
                "pending_adjudication",
                "independent_gold_disagreement",
                "human",
            ),
        )
        for index, (failure_class, failure_code, blocked_on) in enumerate(cases):
            with self.subTest(failure_class=failure_class):
                workspace = self.root / f"typed-block-{index}"
                run_id = f"typed-block-{index}"
                task_id = f"synthetic__typed_block_{index}"
                task = self._tasks(workspace, (task_id,))[0]
                provider = mock.Mock()
                provider.meter.total_usd = 0.0
                worker_engine = mock.Mock()
                worker_engine.require_empirical_difficulty = False
                worker_engine.load_task.side_effect = (task, task)
                worker_engine.blocked_stage.return_value = SimpleNamespace(
                    payload_json=json.dumps(
                        {
                            "error": f"typed {failure_class} hold",
                            "data": {
                                "failure_class": failure_class,
                                "failure_code": failure_code,
                                "blocked_on": blocked_on,
                                "retry_guard": "explicit",
                                "recovery_prerequisite": "replace_evidence",
                            },
                        }
                    )
                )
                worker_engine.latest_report.return_value = mock.sentinel.report
                worker_engine.report_is_current.return_value = (False, "not ready")
                worker_engine.final_verdict.return_value = "blocked"

                with (
                    mock.patch.object(
                        cli_mod, "_resolve_provider", return_value=provider
                    ),
                    mock.patch.object(
                        cli_mod, "_make_engine", return_value=worker_engine
                    ),
                ):
                    result = cli_mod._pipeline_worker(
                        {
                            "workspace": str(workspace),
                            "task_id": task_id,
                            "until_stage": StageName.GENERATE.value,
                            "run_id": run_id,
                        }
                    )

                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["failure_class"], failure_class)
                self.assertEqual(result["failure_code"], failure_code)
                self.assertEqual(result["blocked_on"], blocked_on)
                self.assertEqual(result["retry_guard"], "explicit")

                attempt_path = next(
                    (
                        workspace
                        / "state"
                        / "pipeline_runs"
                        / run_id
                        / "attempts"
                        / task_id
                    ).glob("*.json")
                )
                attempt_payload = json.loads(attempt_path.read_text())
                self.assertEqual(attempt_payload["failure_class"], failure_class)
                self.assertEqual(attempt_payload["failure_code"], failure_code)
                self.assertEqual(attempt_payload["blocked_on"], blocked_on)

                spec = GenerationRunSpec(
                    candidate_count=1,
                    profile=ReadinessProfile.LOCAL_READY,
                    budget_per_task=7.0,
                )
                report_engine = Engine(workspace)
                try:
                    report = make_run_report(
                        engine=report_engine,
                        run_id=run_id,
                        spec=spec,
                        task_ids=(task_id,),
                        state="COMPLETE_WITH_ISSUES",
                        worker_results=(result,),
                    )
                finally:
                    report_engine.close()
                task_report = report.tasks[0]
                self.assertEqual(task_report.disposition, "blocked")
                self.assertEqual(
                    task_report.failure_class,
                    ReadinessFailureClass(failure_class),
                )
                self.assertEqual(task_report.failure_code, failure_code)
                self.assertEqual(task_report.blocked_on, blocked_on)
                self.assertEqual(
                    task_report.attempts[-1].failure_class,
                    ReadinessFailureClass(failure_class),
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class CandidateCountDefaultsTests(unittest.TestCase):
    """`pipeline --candidate-count N` needs nothing else: every other setting
    resolves to the value the packaged runs used."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.admission = self.root / "council.live_admitted"
        self.admission.write_text("{}", encoding="utf-8")
        self.parser = cli_mod.build_parser()

    def _spec(self, *argv: str):
        args = self.parser.parse_args(
            ["pipeline", "--workspace", str(self.root / "ws"), *argv]
        )
        with mock.patch.dict(
            "os.environ", {"ELT_TASKGEN_ADMISSION": str(self.admission)}
        ):
            return cli_mod._configured_run_spec(args)

    def test_bare_candidate_count_resolves_every_default(self) -> None:
        spec = self._spec("--candidate-count", "3")
        self.assertEqual(spec.candidate_count, 3)
        self.assertIs(spec.profile, ReadinessProfile.PACKAGED)
        self.assertEqual(spec.budget_per_task, 25.0)
        self.assertEqual(spec.budget_total, 40.0 * 3)
        self.assertEqual(spec.max_repair_rounds, 3)
        self.assertEqual(spec.workers, 4)
        self.assertTrue(spec.resume)
        self.assertEqual(spec.destinations, ("snowflake", "databricks", "redshift"))
        self.assertEqual(spec.extra_destinations, ("databricks", "redshift"))
        self.assertEqual(
            spec.export_dir, str((self.root / "ws").resolve() / "packages")
        )
        self.assertEqual(spec.admission_reference, str(self.admission.resolve()))

    def test_explicit_flags_still_win(self) -> None:
        spec = self._spec(
            "--candidate-count", "2",
            "--budget-per-task", "9",
            "--destination", "databricks,snowflake",
            "--export-dir", str(self.root / "out"),
            "--admission-reference", str(self.root / "other.live_admitted"),
            "--max-repair-rounds", "1",
        )
        self.assertEqual(spec.budget_per_task, 9.0)
        self.assertEqual(spec.destinations, ("databricks", "snowflake"))
        self.assertEqual(spec.destination, "databricks")
        self.assertEqual(spec.export_dir, str((self.root / "out").resolve()))
        self.assertEqual(
            spec.admission_reference, str((self.root / "other.live_admitted").resolve())
        )
        self.assertEqual(spec.max_repair_rounds, 1)

    def test_unknown_destination_is_a_usage_error(self) -> None:
        with self.assertRaisesRegex(cli_mod.CliUsageError, "destinations"):
            self._spec("--candidate-count", "1", "--destination", "bigquery")

    def test_legacy_pipeline_budget_default_is_unchanged(self) -> None:
        args = self.parser.parse_args(["pipeline"])
        self.assertEqual(args.budget_per_task, 7.0)
        self.assertFalse(args.budget_per_task_explicit)

    def test_default_pool_is_the_shipped_candidate_pool(self) -> None:
        pool = cli_mod._default_candidate_pool()
        self.assertEqual(pool.name, "candidate_pool.ingest.yaml")
        self.assertTrue(pool.is_file())

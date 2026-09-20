"""Focused regression tests for the concurrent pipeline's CLI policy boundary."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen import cli as cli_mod
from elt_taskgen import demo_fixture
from elt_taskgen.cli import (
    CliUsageError,
    _pipeline_worker,
    _pipeline_worker_payload,
    _registered_task_ids,
    build_parser,
    cmd_pipeline,
)
from elt_taskgen.corpus.difficulty import structural_difficulty, with_empirical
from elt_taskgen.corpus.selection import SelectionResult
from elt_taskgen.engine import (
    FINAL_ACCEPTED,
    FINAL_REJECTED,
    STAGE_ORDER,
    VERDICT_BLOCKED,
    VERDICT_FATAL,
    VERDICT_PASS,
    Engine,
    InfrastructureFailure,
    ReportRow,
    StageName,
    StageOutcome,
    StagePayload,
    variant_gate_stage,
)
from elt_taskgen.export import release as release_mod
from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    AcceptanceReport,
    ColumnType,
    EmpiricalDifficulty,
    GateResult,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    SolverTierResult,
    TaskStatus,
    TaskVariant,
    VariantCalibration,
    canonical_json,
    solver_roster_fingerprint,
    variant_task_id,
)
from elt_taskgen.verification import gates as gates_mod
from elt_taskgen.verification import variant_battery


def _blocked_row(error: str) -> ReportRow:
    """A blocked SELECT row, as `Engine.blocked_stage` would return one."""
    return ReportRow(
        id=1,
        task_id="",
        revision=1,
        stage=StageName.SELECT.value,
        verdict=VERDICT_BLOCKED,
        payload_json=json.dumps({"error": error, "data": {"blocked_on": "human"}}),
        content_hash="0" * 64,
        created_at="2026-01-01T00:00:00+00:00",
    )


class PipelineParserPolicyTests(unittest.TestCase):
    def _parse(self, *extra: str):
        return build_parser().parse_args(
            ["pipeline", "--workspace", "/tmp/elt-taskgen-pipeline", *extra]
        )

    def test_pipeline_defaults_are_strict_and_cover_all_sources(self) -> None:
        args = self._parse()

        self.assertIs(args.func, cmd_pipeline)
        self.assertFalse(args.allow_structural_difficulty)
        self.assertFalse(args.development_release)
        self.assertEqual(args.budget_per_task, 7.0)
        self.assertEqual(
            args.require_sources,
            "dbt,dlt,synsql,schemapile,wikidbs",
        )

    def test_pipeline_budget_override_does_not_change_other_commands(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "generate",
                "--workspace",
                "/tmp/elt-taskgen-generate",
                "--task-id",
                "dbt__one",
            ]
        )

        self.assertEqual(args.budget_per_task, 5.0)

        implicit, extras = parser.parse_known_args(["pipeline"])
        self.assertEqual(extras, [])
        self.assertEqual(implicit.budget_per_task, 7.0)
        self.assertFalse(hasattr(implicit, "_budget_per_task_was_explicit"))

        explicit, extras = parser.parse_known_args(
            ["pipeline", "--budget-per-task", "5"]
        )
        self.assertEqual(extras, [])
        self.assertEqual(explicit.budget_per_task, 5.0)
        self.assertFalse(hasattr(explicit, "_budget_per_task_was_explicit"))

        other, extras = parser.parse_known_args(
            ["generate", "--task-id", "dbt__one"]
        )
        self.assertEqual(extras, [])
        self.assertEqual(other.budget_per_task, 5.0)
        self.assertFalse(hasattr(other, "_budget_per_task_was_explicit"))

    def test_pipeline_help_states_its_local_budget_default(self) -> None:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            build_parser().parse_args(["pipeline", "--help"])

        self.assertEqual(raised.exception.code, 0)
        rendered = " ".join(output.getvalue().split())
        self.assertIn(
            "Most commands default to 5.00; pipeline defaults to 7.00",
            rendered,
        )

    def test_development_escape_hatches_parse_explicitly(self) -> None:
        args = self._parse(
            "--allow-structural-difficulty",
            "--development-release",
            "--require-sources",
            "",
        )

        self.assertTrue(args.allow_structural_difficulty)
        self.assertTrue(args.development_release)
        self.assertEqual(args.require_sources, "")

    def test_worker_payload_is_closed_and_pickle_serializable(self) -> None:
        args = self._parse(
            "--task-id",
            "dbt__one",
            "--budget-per-task",
            "5",
            "--budget-total",
            "4",
        )

        payload = _pipeline_worker_payload(args, "dbt__one", task_budget=2.0)

        self.assertEqual(payload["task_id"], "dbt__one")
        self.assertEqual(payload["budget_per_task"], 2.0)
        self.assertEqual(payload["budget_total"], 2.0)
        self.assertTrue(payload["empirical"])
        self.assertTrue(payload["require_empirical"])
        self.assertEqual(payload["release_mode"], "development")
        self.assertEqual(pickle.loads(pickle.dumps(payload)), payload)

    def test_worker_payload_uses_per_task_cap_without_aggregate_cap(self) -> None:
        args = self._parse("--budget-per-task", "3.5")

        payload = _pipeline_worker_payload(args, "dbt__one", task_budget=None)

        self.assertEqual(payload["budget_per_task"], 3.5)
        self.assertIsNone(payload["budget_total"])

    def test_aggregate_slice_never_raises_the_per_task_cap(self) -> None:
        args = self._parse("--budget-per-task", "3.5", "--budget-total", "100")

        payload = _pipeline_worker_payload(args, "dbt__one", task_budget=50.0)

        self.assertEqual(payload["budget_per_task"], 3.5)
        self.assertEqual(payload["budget_total"], 50.0)

    def test_non_finite_budgets_and_out_of_range_fraction_fail_in_parser(self) -> None:
        invalid = (
            ("--budget-per-task", "nan"),
            ("--budget-total", "inf"),
            ("--budget-total", "0"),
            ("--val-fraction", "-0.1"),
            ("--val-fraction", "1.1"),
            ("--val-fraction", "nan"),
        )
        for flag, value in invalid:
            with self.subTest(flag=flag, value=value):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    self._parse(flag, value)
                self.assertEqual(raised.exception.code, 2)

    def test_budget_help_describes_partitioned_circuit_breaker_not_hard_cap(
        self,
    ) -> None:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            self._parse("--help")

        self.assertEqual(raised.exception.code, 0)
        # argparse re-wraps help at the terminal width; compare the prose,
        # not its line breaks.
        rendered = " ".join(output.getvalue().split())
        self.assertIn("circuit-breaker target", rendered)
        self.assertIn("partitions it evenly", rendered)
        self.assertIn("not a prepaid hard cap", rendered)

    def test_pipeline_help_does_not_describe_a_nonexistent_embedded_fallback(
        self,
    ) -> None:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            self._parse("--help")

        self.assertEqual(raised.exception.code, 0)
        rendered = " ".join(output.getvalue().split())
        self.assertIn("default: packaged config/agents.yaml", rendered)
        self.assertNotIn("else embedded defaults", rendered)
        self.assertIn("does not reduce worker scheduling or live-call spend", rendered)


class PipelineWorkerBoundaryTests(unittest.TestCase):
    def test_worker_stops_at_post_contamination(self) -> None:
        task = demo_fixture.demo_task()
        provider = mock.Mock()
        provider.meter.total_usd = 0.25
        engine = mock.Mock()
        engine.require_empirical_difficulty = False
        engine.load_task.side_effect = (task, task)
        engine.blocked_stage.return_value = None
        engine.latest_report.return_value = mock.sentinel.prepared
        engine.report_is_current.return_value = (True, "")
        engine.final_verdict.return_value = FINAL_ACCEPTED

        with (
            mock.patch.object(cli_mod, "_resolve_provider", return_value=provider),
            mock.patch.object(cli_mod, "_make_engine", return_value=engine),
        ):
            result = _pipeline_worker(
                {"workspace": "/tmp/worker", "task_id": task.task_id}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "prepared")
        engine.run.assert_called_once_with(
            task.task_id, until=StageName.CONTAMINATION_POST.value
        )
        engine.close.assert_called_once_with()

    def test_worker_returns_typed_infrastructure_failure(self) -> None:
        task = demo_fixture.demo_task()
        provider = mock.Mock()
        provider.meter.total_usd = 0.5
        engine = mock.Mock()
        engine.require_empirical_difficulty = False
        engine.load_task.return_value = task
        engine.run.side_effect = InfrastructureFailure(
            task.task_id, StageName.REVIEW.value, "providerfault"
        )

        with (
            mock.patch.object(cli_mod, "_resolve_provider", return_value=provider),
            mock.patch.object(cli_mod, "_make_engine", return_value=engine),
        ):
            result = _pipeline_worker(
                {"workspace": "/tmp/worker", "task_id": task.task_id}
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "infrastructure")
        self.assertEqual(result["infrastructure_scope"], "")
        self.assertIn("providerfault", result["detail"])
        engine.close.assert_called_once_with()

    def test_worker_preserves_typed_budget_scope_from_engine_evidence(self) -> None:
        from elt_taskgen.review.providers import BudgetExceededError

        cases = (
            ("task", "message falsely says total budget"),
            ("total", "message falsely says task budget"),
        )
        for scope, message in cases:
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                task = demo_fixture.demo_task()
                provider = mock.Mock()
                provider.meter.total_usd = 0.0

                def budget_failure(_engine, _task):
                    try:
                        raise BudgetExceededError(message, scope=scope)
                    except BudgetExceededError as exc:
                        raise RuntimeError("outer wrapper") from exc

                engine = Engine(
                    workspace,
                    stage_runners={
                        StageName.CONTAMINATION_PRE.value: budget_failure
                    },
                )
                engine.register(task)
                with (
                    mock.patch.object(
                        cli_mod, "_resolve_provider", return_value=provider
                    ),
                    mock.patch.object(cli_mod, "_make_engine", return_value=engine),
                ):
                    result = _pipeline_worker(
                        {"workspace": str(workspace), "task_id": task.task_id}
                    )

                self.assertEqual(result["state"], "infrastructure")
                self.assertEqual(result["infrastructure_scope"], scope)
                reopened = Engine(workspace)
                try:
                    row = reopened.latest_report(
                        task.task_id, StageName.CONTAMINATION_PRE.value
                    )
                    self.assertIsNotNone(row)
                    payload = json.loads(row.payload_json)
                    self.assertEqual(payload["infrastructure"], "budgetexceedederror")
                    self.assertEqual(payload["budget_scope"], scope)
                    self.assertEqual(payload["data"]["budget_scope"], scope)
                finally:
                    reopened.close()

    def test_extract_load_gate_re_raises_task_and_total_budget_faults(self) -> None:
        from elt_taskgen.export import eltbench as eltbench_mod
        from elt_taskgen.reference import gold as gold_mod
        from elt_taskgen.reference import independent
        from elt_taskgen.review.providers import BudgetExceededError
        from elt_taskgen.verification import el_probes

        for scope in ("task", "total"):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                task = demo_fixture.demo_task()
                engine = Engine(workspace)
                fault = BudgetExceededError(
                    "message deliberately names the other budget",
                    scope=scope,
                )
                runner = cli_mod.make_variant_gates_runner(
                    TaskVariant.EXTRACT_LOAD,
                    provider=object(),
                )
                try:
                    with (
                        mock.patch.object(gold_mod, "load_gold", return_value=object()),
                        mock.patch.object(eltbench_mod, "emit_variant"),
                        mock.patch.object(
                            cli_mod, "_population_drift_failure", return_value=None
                        ),
                        mock.patch.object(el_probes, "record_artifact_census"),
                        mock.patch.object(
                            independent,
                            "run_independent_load_build",
                            side_effect=fault,
                        ),
                        self.assertRaises(BudgetExceededError) as raised,
                    ):
                        runner(engine, task)
                    self.assertIs(raised.exception, fault)
                    self.assertEqual(raised.exception.scope, scope)
                finally:
                    engine.close()

    def test_worker_does_not_hide_programming_error_during_setup(self) -> None:
        with (
            mock.patch.object(
                cli_mod,
                "_resolve_provider",
                side_effect=RuntimeError("programming bug"),
            ),
            self.assertRaisesRegex(RuntimeError, "programming bug"),
        ):
            _pipeline_worker({"workspace": "/tmp/worker", "task_id": "t"})


class RegisteredPipelineTaskTests(unittest.TestCase):
    def test_discovery_is_sorted_and_requested_ids_are_stably_deduplicated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for task_id in ("dlt__b", "dbt__a"):
                task_dir = workspace / "tasks" / task_id
                task_dir.mkdir(parents=True)
                (task_dir / "task_ir.json").touch()

            self.assertEqual(
                _registered_task_ids(workspace, None),
                ("dbt__a", "dlt__b"),
            )
            self.assertEqual(
                _registered_task_ids(workspace, ["dlt__b", "dbt__a", "dlt__b"]),
                ("dlt__b", "dbt__a"),
            )

    def test_missing_requested_task_fails_as_cli_usage(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(CliUsageError, "not registered"),
        ):
            _registered_task_ids(Path(tmp), ["dbt__missing"])

    def test_unsafe_requested_task_ids_fail_before_any_workspace_join(self) -> None:
        invalid_ids = (
            "../../outside",
            "nested/task",
            r"nested\task",
            ".",
            "..",
            "/absolute/path",
            r"C:\absolute\path",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            for task_id in invalid_ids:
                with self.subTest(task_id=task_id):
                    with self.assertRaisesRegex(CliUsageError, "unsafe pipeline task id"):
                        _registered_task_ids(workspace, [task_id])


def _battery(task, variant) -> AcceptanceReport:
    roster = variant_battery.gate_roster(variant)
    return AcceptanceReport.from_gates(
        task_id=variant_task_id(task.task_id, variant),
        revision=task.current_revision,
        task_content_hash=task.content_hash(),
        gates=tuple(
            GateResult(gate=name, passed=True, details="test pass") for name in roster
        ),
        scorer_version=gates_mod.SCORER_VERSION,
        roster_digest=gates_mod.ROSTER_DIGEST,
        roster=roster,
    )


def _prepare_candidate(engine: Engine, task) -> None:
    engine.register(task)
    task = engine.load_task(task.task_id).with_status(TaskStatus.ACCEPTED)
    engine.save_task(task)
    for stage in STAGE_ORDER:
        if stage is StageName.INTAKE:
            continue
        if stage in {
            StageName.GATES_EXTRACT_LOAD,
            StageName.GATES_TRANSFORM,
        }:
            variant = (
                RLVR_TASK_VARIANTS[0]
                if stage is StageName.GATES_EXTRACT_LOAD
                else RLVR_TASK_VARIANTS[1]
            )
            engine.record_report(
                task, stage.value, VERDICT_PASS, _battery(task, variant)
            )
        else:
            engine.record_report(
                task,
                stage.value,
                VERDICT_PASS,
                StagePayload(detail="prepared test stage"),
            )
        if stage is StageName.CONTAMINATION_POST:
            break
    reports = engine.task_dir(task.task_id) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "difficulty.json").write_text(
        structural_difficulty(task).model_dump_json(), encoding="utf-8"
    )


def _different_plan_candidate(task_id: str):
    """A valid demo-shaped task well below the production duplicate threshold."""
    mart = MartSpec(
        name="order_backlog",
        description="Backlog of unfulfilled orders by status.",
        grain="One row per order status.",
        key_columns=("status",),
        columns=(
            MartColumn(
                name="status", type=ColumnType.TEXT, description="Order status."
            ),
            MartColumn(
                name="backlog_orders",
                type=ColumnType.INTEGER,
                description="Orders in the status.",
            ),
        ),
        plan=MartPlan(
            mart="order_backlog",
            ops=(
                MartOp(
                    kind=MartOpKind.AGGREGATE,
                    description="Count orders grouped by status.",
                    tables=("orders",),
                    columns=("status", "order_id"),
                ),
                MartOp(
                    kind=MartOpKind.TIE_BREAK,
                    description="Deterministic order by status.",
                    columns=("status",),
                ),
            ),
        ),
    )
    return demo_fixture.demo_task().model_copy(
        update={
            "task_id": task_id,
            "family_id": task_id,
            "cluster_id": task_id,
            "origin": Origin.DBT,
            "marts": (mart,),
            "reference": None,
            "attack_cases": (),
        }
    )


class PipelineCoordinatorPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"

    def _args(self, *extra: str):
        return build_parser().parse_args(
            [
                "pipeline",
                "--workspace",
                str(self.workspace),
                "--workers",
                "1",
                *extra,
            ]
        )

    def test_release_only_flags_require_a_release_directory_before_discovery(
        self,
    ) -> None:
        cases = (
            ("--development-release",),
            ("--sandbox-attestation", str(Path(self._tmp.name) / "attestation.json")),
            ("--allow-unlocked-env",),
        )
        for flags in cases:
            with self.subTest(flags=flags), self.assertRaisesRegex(
                CliUsageError, "require --release-dir"
            ):
                cmd_pipeline(self._args(*flags))

    def test_required_source_failure_happens_before_worker_spend(self) -> None:
        task = demo_fixture.demo_task().model_copy(update={"origin": Origin.DBT})
        engine = Engine(self.workspace)
        try:
            engine.register(task)
        finally:
            engine.close()

        args = self._args(
            "--task-id", task.task_id, "--require-sources", Origin.WIKIDBS.value
        )
        with (
            mock.patch.object(cli_mod, "_pipeline_worker") as worker,
            self.assertRaisesRegex(CliUsageError, "missing required sources"),
        ):
            cmd_pipeline(args)
        worker.assert_not_called()

    def test_certified_release_refuses_development_difficulty_before_workers(
        self,
    ) -> None:
        task = demo_fixture.demo_task().model_copy(update={"origin": Origin.DBT})
        engine = Engine(self.workspace)
        try:
            engine.register(task)
        finally:
            engine.close()

        args = self._args(
            "--task-id",
            task.task_id,
            "--require-sources",
            Origin.DBT.value,
            "--allow-structural-difficulty",
            "--release-dir",
            str(Path(self._tmp.name) / "release"),
        )
        with (
            mock.patch.object(cli_mod, "_pipeline_worker") as worker,
            self.assertRaisesRegex(CliUsageError, "development-only"),
        ):
            cmd_pipeline(args)
        worker.assert_not_called()

    def test_rejected_candidate_is_excluded_before_global_selection(
        self,
    ) -> None:
        prepared = demo_fixture.demo_task().model_copy(
            update={"task_id": "synthetic__prepared", "cluster_id": "prepared"}
        )
        rejected = demo_fixture.demo_task().model_copy(
            update={
                "task_id": "synthetic__rejected",
                "family_id": "synthetic__rejected",
                "cluster_id": "rejected",
            }
        )
        engine = Engine(self.workspace)
        try:
            _prepare_candidate(engine, prepared)
            engine.register(rejected)
        finally:
            engine.close()

        def worker(payload: dict) -> dict:
            task_id = payload["task_id"]
            if task_id == rejected.task_id:
                return {
                    "task_id": task_id,
                    "ok": False,
                    "state": FINAL_REJECTED,
                    "detail": "candidate rejected in preparation",
                    "usd": 0.0,
                }
            return {
                "task_id": task_id,
                "ok": True,
                "state": "prepared",
                "detail": "",
                "usd": 0.0,
            }

        args = self._args(
            "--task-id",
            prepared.task_id,
            "--task-id",
            rejected.task_id,
            "--size",
            "1",
            "--allow-structural-difficulty",
            "--require-sources",
            "",
        )
        with (
            mock.patch.object(cli_mod, "_pipeline_worker", side_effect=worker),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cmd_pipeline(args), 0)

        check = Engine(self.workspace)
        try:
            self.assertIsNotNone(
                check.latest_report(prepared.task_id, StageName.SELECT.value)
            )
            self.assertIsNone(
                check.latest_report(rejected.task_id, StageName.SELECT.value)
            )
        finally:
            check.close()

    def test_batch_duplicate_winner_and_refusal_do_not_depend_on_worker_count(
        self,
    ) -> None:
        class ImmediateFuture:
            def __init__(self, result: dict) -> None:
                self._result = result

            def result(self) -> dict:
                return self._result

        class ImmediateExecutor:
            def __init__(self, *, max_workers: int) -> None:
                self.max_workers = max_workers

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def submit(self, fn, payload: dict) -> ImmediateFuture:
                return ImmediateFuture(fn(payload))

        observations: list[dict] = []
        for workers in (1, 4):
            workspace = Path(self._tmp.name) / f"workers-{workers}"
            winner = demo_fixture.demo_task().model_copy(
                update={
                    "task_id": "dbt__a_winner",
                    "family_id": "dbt__a_winner",
                    "cluster_id": "dbt__a_winner",
                    "origin": Origin.DBT,
                }
            )
            clone = winner.model_copy(
                update={
                    "task_id": "dbt__b_clone",
                    "family_id": "dbt__b_clone",
                    "cluster_id": "dbt__b_clone",
                }
            )
            different = _different_plan_candidate("dbt__c_different")
            engine = Engine(workspace)
            try:
                # Registration and CLI request order intentionally disagree with
                # lexical order: the coordinator's stable order owns admission.
                for task in (clone, different, winner):
                    engine.register(task)
            finally:
                engine.close()

            worker_calls: list[str] = []

            def worker(payload: dict) -> dict:
                task_id = payload["task_id"]
                worker_calls.append(task_id)
                worker_engine = Engine(workspace)
                try:
                    _prepare_candidate(
                        worker_engine, worker_engine.load_task(task_id)
                    )
                finally:
                    worker_engine.close()
                return {
                    "task_id": task_id,
                    "ok": True,
                    "state": "prepared",
                    "detail": "",
                    "usd": 0.0,
                }

            args = build_parser().parse_args(
                [
                    "pipeline",
                    "--workspace",
                    str(workspace),
                    "--workers",
                    str(workers),
                    "--task-id",
                    clone.task_id,
                    "--task-id",
                    different.task_id,
                    "--task-id",
                    winner.task_id,
                    "--size",
                    "3",
                    "--allow-structural-difficulty",
                    "--require-sources",
                    "",
                ]
            )
            with (
                mock.patch.object(
                    cli_mod, "_pipeline_worker", side_effect=worker
                ),
                mock.patch.object(
                    cli_mod,
                    "run_contamination_pre",
                    return_value=StageOutcome(
                        VERDICT_PASS, StagePayload(detail="offline preflight pass")
                    ),
                ),
                mock.patch.object(cli_mod, "_resolve_provider") as provider,
                mock.patch(
                    "concurrent.futures.ProcessPoolExecutor", ImmediateExecutor
                ),
                mock.patch(
                    "concurrent.futures.as_completed", side_effect=lambda values: values
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                # One of three requested tasks is deterministically refused, so
                # the requested three-task cohort cannot be filled.
                self.assertEqual(cmd_pipeline(args), 1)
            provider.assert_not_called()

            check = Engine(workspace)
            try:
                statuses = {
                    task_id: check.load_task(task_id).status.value
                    for task_id in (winner.task_id, clone.task_id, different.task_id)
                }
            finally:
                check.close()
            duplicate_report = json.loads(
                (
                    workspace
                    / "tasks"
                    / clone.task_id
                    / "reports"
                    / "filters"
                    / "near-duplicate-intake.json"
                ).read_text(encoding="utf-8")
            )
            different_report = json.loads(
                (
                    workspace
                    / "tasks"
                    / different.task_id
                    / "reports"
                    / "filters"
                    / "near-duplicate-intake.json"
                ).read_text(encoding="utf-8")
            )
            observations.append(
                {
                    "worker_calls": sorted(worker_calls),
                    "statuses": statuses,
                    "duplicate_passed": duplicate_report["passed"],
                    "duplicate_evidence": duplicate_report["evidence"],
                    "different_evidence": different_report["evidence"],
                }
            )
            for task in (winner, clone, different):
                self.assertTrue(
                    (
                        workspace
                        / "tasks"
                        / task.task_id
                        / "reports"
                        / "filters"
                        / "intake-format-blacklist.json"
                    ).is_file()
                )

        self.assertEqual(observations[0], observations[1])
        self.assertEqual(
            observations[0]["worker_calls"],
            ["dbt__a_winner", "dbt__c_different"],
        )
        self.assertEqual(
            observations[0]["statuses"]["dbt__b_clone"],
            TaskStatus.REJECTED.value,
        )
        self.assertFalse(observations[0]["duplicate_passed"])
        self.assertEqual(
            observations[0]["duplicate_evidence"]["max_similarity_task"],
            "dbt__a_winner",
        )
        self.assertEqual(
            observations[0]["duplicate_evidence"]["max_similarity"], "1.0000"
        )
        self.assertEqual(
            observations[0]["different_evidence"]["compared_against"], "1"
        )
        self.assertIn(
            "score:dbt__a_winner", observations[0]["different_evidence"]
        )

    def test_blocked_selection_never_publishes_global_selection(self) -> None:
        task = demo_fixture.demo_task().model_copy(update={"origin": Origin.DBT})
        engine = Engine(self.workspace)
        try:
            _prepare_candidate(engine, task)
        finally:
            engine.close()
        worker_result = {
            "task_id": task.task_id,
            "ok": True,
            "state": "prepared",
            "detail": "",
            "usd": 0.0,
        }
        args = self._args(
            "--task-id",
            task.task_id,
            "--size",
            "1",
            "--allow-structural-difficulty",
            "--require-sources",
            "",
        )
        with (
            mock.patch.object(cli_mod, "_pipeline_worker", return_value=worker_result),
            mock.patch.object(
                Engine,
                "blocked_stage",
                return_value=_blocked_row("waiting for human adjudication"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cmd_pipeline(args), 2)
        self.assertFalse(
            (self.workspace / "state" / "corpus_selection.json").exists()
        )

    def test_rejected_selection_never_publishes_global_selection(self) -> None:
        task = demo_fixture.demo_task().model_copy(update={"origin": Origin.DBT})
        engine = Engine(self.workspace)
        try:
            _prepare_candidate(engine, task)
        finally:
            engine.close()
        worker_result = {
            "task_id": task.task_id,
            "ok": True,
            "state": "prepared",
            "detail": "",
            "usd": 0.0,
        }
        args = self._args(
            "--task-id",
            task.task_id,
            "--size",
            "1",
            "--allow-structural-difficulty",
            "--require-sources",
            "",
        )
        with (
            mock.patch.object(cli_mod, "_pipeline_worker", return_value=worker_result),
            mock.patch.object(
                Engine, "final_verdict", return_value=FINAL_REJECTED
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cmd_pipeline(args), 1)
        self.assertFalse(
            (self.workspace / "state" / "corpus_selection.json").exists()
        )

    def test_selection_evidence_drift_before_publication_fails_closed(self) -> None:
        task = demo_fixture.demo_task().model_copy(update={"origin": Origin.DBT})
        engine = Engine(self.workspace)
        try:
            _prepare_candidate(engine, task)
        finally:
            engine.close()
        worker_result = {
            "task_id": task.task_id,
            "ok": True,
            "state": "prepared",
            "detail": "",
            "usd": 0.0,
        }

        real_coordinator_lock = Engine.coordinator_lock

        def mutate_measurement(_engine: Engine):
            """Drift the recorded measurement after the tentative selection is
            derived and before it is re-derived under the publication lock."""
            current_task = _engine.load_task(task.task_id)
            path = cli_mod._evidence_dir(_engine, current_task) / "difficulty.json"
            measurement = structural_difficulty(current_task)
            changed = measurement.model_copy(
                update={
                    "structural": {
                        **measurement.structural,
                        "concurrent_edit_probe": 1.0,
                    }
                }
            )
            path.write_text(changed.model_dump_json(), encoding="utf-8")
            return real_coordinator_lock(_engine)

        args = self._args(
            "--task-id",
            task.task_id,
            "--size",
            "1",
            "--allow-structural-difficulty",
            "--require-sources",
            "",
        )
        with (
            mock.patch.object(cli_mod, "_pipeline_worker", return_value=worker_result),
            mock.patch.object(
                Engine, "coordinator_lock", mutate_measurement
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(CliUsageError, "selection inputs changed"),
        ):
            cmd_pipeline(args)
        self.assertFalse(
            (self.workspace / "state" / "corpus_selection.json").exists()
        )

    def test_matching_existing_release_is_reverified_and_adopted(self) -> None:
        task = demo_fixture.demo_task().model_copy(update={"origin": Origin.DBT})
        engine = Engine(self.workspace)
        try:
            _prepare_candidate(engine, task)
        finally:
            engine.close()
        measurement = structural_difficulty(task)
        difficulty_digest = hashlib.sha256(
            canonical_json(measurement.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        out = Path(self._tmp.name) / "release"
        out.mkdir()
        manifest = release_mod.ReleaseManifest(
            schema_version=release_mod.RELEASE_SCHEMA_VERSION,
            corpus_profile=release_mod.COMBINED_CORPUS_PROFILE,
            public_layout=release_mod.COMBINED_PUBLIC_LAYOUT,
            release_mode="development",
            release_id="release-recovery-test",
            tasks={task.task_id: task.content_hash()},
            difficulty_measurements={task.task_id: difficulty_digest},
            splits={task.task_id: "train"},
            families={task.task_id: task.family_id},
            licenses={task.task_id: task.license},
            checksums={},
            variants={
                task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)
            },
            scorer_version="test",
            generator_version="test",
        )
        (out / "release_manifest.json").write_text(
            manifest.model_dump_json(), encoding="utf-8"
        )
        worker_result = {
            "task_id": task.task_id,
            "ok": True,
            "state": "prepared",
            "detail": "",
            "usd": 0.0,
        }
        verification = mock.Mock(ok=True, files_checked=7, failures=())
        args = self._args(
            "--task-id",
            task.task_id,
            "--size",
            "1",
            "--val-fraction",
            "0",
            "--allow-structural-difficulty",
            "--development-release",
            "--require-sources",
            "",
            "--release-dir",
            str(out),
        )
        output = io.StringIO()
        with (
            mock.patch.object(cli_mod, "_pipeline_worker", return_value=worker_result),
            mock.patch.object(
                release_mod, "verify_release", return_value=verification
            ) as verify,
            mock.patch.object(release_mod, "freeze_release") as freeze,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cmd_pipeline(args), 0)
        freeze.assert_not_called()
        verify.assert_called_once_with(out.resolve())
        self.assertIn("adopted and re-verified", output.getvalue())
        release_state = json.loads(
            (self.workspace / "state" / "corpus_release.json").read_text()
        )
        self.assertEqual(release_state["release_id"], manifest.release_id)

    def test_short_selection_is_not_published_as_current_corpus(self) -> None:
        task = demo_fixture.demo_task().model_copy(update={"origin": Origin.DBT})
        engine = Engine(self.workspace)
        try:
            _prepare_candidate(engine, task)
        finally:
            engine.close()

        worker_result = {
            "task_id": task.task_id,
            "ok": True,
            "state": "prepared",
            "detail": "",
            "usd": 0.0,
        }
        empty = SelectionResult(
            train=(), val=(), rejected={task.task_id: "quota miss"}, variants={}
        )
        args = self._args(
            "--task-id",
            task.task_id,
            "--size",
            "1",
            "--allow-structural-difficulty",
            "--require-sources",
            "",
        )
        with (
            mock.patch.object(cli_mod, "_pipeline_worker", return_value=worker_result),
            mock.patch("elt_taskgen.corpus.selection.select", return_value=empty),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cmd_pipeline(args), 1)

        self.assertFalse(
            (self.workspace / "state" / "corpus_selection.json").exists()
        )

    def test_source_incomplete_selection_is_not_published_as_current_corpus(
        self,
    ) -> None:
        dbt_task = demo_fixture.demo_task().model_copy(
            update={"task_id": "dbt__candidate", "origin": Origin.DBT}
        )
        wiki_task = demo_fixture.demo_task().model_copy(
            update={
                "task_id": "wikidbs__candidate",
                "family_id": "wikidbs__candidate",
                "cluster_id": "wikidbs__candidate",
                "origin": Origin.WIKIDBS,
            }
        )
        engine = Engine(self.workspace)
        try:
            _prepare_candidate(engine, dbt_task)
            _prepare_candidate(engine, wiki_task)
        finally:
            engine.close()

        def worker(payload: dict) -> dict:
            return {
                "task_id": payload["task_id"],
                "ok": True,
                "state": "prepared",
                "detail": "",
                "usd": 0.0,
            }

        variants = tuple(variant.value for variant in RLVR_TASK_VARIANTS)
        dbt_only = SelectionResult(
            train=(dbt_task.task_id,),
            val=(),
            rejected={wiki_task.task_id: "quota miss"},
            variants={dbt_task.task_id: variants},
        )
        args = self._args(
            "--task-id",
            dbt_task.task_id,
            "--task-id",
            wiki_task.task_id,
            "--size",
            "1",
            "--allow-structural-difficulty",
            "--require-sources",
            Origin.WIKIDBS.value,
        )
        with (
            mock.patch.object(cli_mod, "_pipeline_worker", side_effect=worker),
            mock.patch("elt_taskgen.corpus.selection.select", return_value=dbt_only),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cmd_pipeline(args), 1)

        self.assertFalse(
            (self.workspace / "state" / "corpus_selection.json").exists()
        )

    def test_default_source_policy_selects_one_candidate_from_all_five_sources(
        self,
    ) -> None:
        origins = (
            Origin.DBT,
            Origin.DLT,
            Origin.SYNSQL,
            Origin.SCHEMAPILE,
            Origin.WIKIDBS,
        )
        engine = Engine(self.workspace)
        try:
            for origin in origins:
                task_id = f"{origin.value}__candidate"
                task = demo_fixture.demo_task().model_copy(
                    update={
                        "task_id": task_id,
                        "family_id": task_id,
                        "cluster_id": task_id,
                        "origin": origin,
                    }
                )
                _prepare_candidate(engine, task)
        finally:
            engine.close()

        def worker(payload: dict) -> dict:
            return {
                "task_id": payload["task_id"],
                "ok": True,
                "state": "prepared",
                "detail": "",
                "usd": 0.0,
            }

        args = self._args("--allow-structural-difficulty")
        with (
            mock.patch.object(cli_mod, "_pipeline_worker", side_effect=worker),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cmd_pipeline(args), 0)

        selection = SelectionResult.model_validate_json(
            (self.workspace / "state" / "corpus_selection.json").read_text(
                encoding="utf-8"
            )
        )
        selected_ids = set(selection.train) | set(selection.val)
        self.assertEqual(
            selected_ids, {f"{origin.value}__candidate" for origin in origins}
        )


class ExistingReleasePolicyTests(unittest.TestCase):
    def test_certified_run_does_not_reuse_existing_development_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            engine = Engine(workspace)
            self.addCleanup(engine.close)
            task = demo_fixture.demo_task()
            engine.register(task)
            for variant in RLVR_TASK_VARIANTS:
                engine.record_report(
                    task,
                    variant_gate_stage(variant).value,
                    VERDICT_PASS,
                    _battery(task, variant),
                )

            model_key = "test:solver"
            tiers = {
                TaskVariant.EXTRACT_LOAD: SolverTierResult(
                    model_key=model_key,
                    k=2,
                    successes=1,
                    stage1_failures=1,
                ),
                TaskVariant.TRANSFORM: SolverTierResult(
                    model_key=model_key,
                    k=2,
                    successes=1,
                    stage2_failures=1,
                ),
            }
            empirical = EmpiricalDifficulty(
                solver_config="test",
                n_attempts=4,
                success_rate=0.5,
                stage1_failure_rate=0.5,
                stage2_failure_rate=0.5,
                variants={
                    variant: VariantCalibration(variant=variant, tiers=(tiers[variant],))
                    for variant in RLVR_TASK_VARIANTS
                },
                roster_fingerprint=solver_roster_fingerprint((model_key,)),
                campaign_fingerprint="1" * 64,
                measured_at_content_hash=task.content_hash(),
            )
            reports = cli_mod._evidence_dir(engine, task)
            measurement = with_empirical(structural_difficulty(task), empirical)
            (reports / "difficulty.json").write_text(
                measurement.model_dump_json(), encoding="utf-8"
            )
            (reports / "selection.json").write_text(
                SelectionResult(
                    train=(task.task_id,),
                    val=(),
                    rejected={},
                    variants={task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)},
                    empirical_required=True,
                ).model_dump_json(),
                encoding="utf-8",
            )

            out = workspace / "release"
            out.mkdir()
            (out / "release_manifest.json").write_text(
                canonical_json(
                    {
                        "schema_version": release_mod.RELEASE_SCHEMA_VERSION,
                        "release_id": "release-existing",
                        "release_mode": "development",
                        "corpus_profile": release_mod.COMBINED_CORPUS_PROFILE,
                        "public_layout": release_mod.COMBINED_PUBLIC_LAYOUT,
                        "tasks": {task.task_id: task.content_hash()},
                        "variants": {
                            task.task_id: [v.value for v in RLVR_TASK_VARIANTS]
                        },
                        "el_sources": {task.task_id: {"development": "source"}},
                    }
                ),
                encoding="utf-8",
            )
            engine.release_mode = "certified"
            engine.require_empirical_difficulty = True
            engine.sandbox_attestation = None

            with mock.patch.object(release_mod, "verify_release") as verify:
                outcome = cli_mod.run_release(engine, task)

            self.assertEqual(outcome.verdict, cli_mod.VERDICT_BLOCKED)
            self.assertIn("existing release mode", outcome.payload.error)
            self.assertIn("move it aside", outcome.payload.error)
            verify.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

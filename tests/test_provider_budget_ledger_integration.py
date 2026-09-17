"""Provider-side integration tests for the durable global budget ledger."""

from __future__ import annotations

import argparse
import multiprocessing
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import cli
from elt_taskgen.models import CouncilRole, Origin
from elt_taskgen.pipeline_readiness import GenerationRunSpec, ReadinessProfile
from elt_taskgen.review import providers as P
from elt_taskgen.review.budget_ledger import (
    DurableBudgetLedger,
    ReservationConflictError,
    ReservationState,
)
from tests.test_providers import (
    FakeTransport,
    VALID_FINDING,
    _routed,
    anthropic_tool_response,
    anthropic_bad_tool_response,
)


def _process_reserve(
    workspace: str,
    start: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
    index: int,
) -> None:
    """Spawn-safe provider-ledger contender for the process-level test."""

    try:
        ledger = DurableBudgetLedger.initialize("process-run", 1.0, workspace)
        trajectory = P.CostMeter(
            budget_per_task_usd=10.0,
            durable_ledger=ledger,
        ).trajectory(f"task-{index}", "semantic_author")
        if not start.wait(timeout=15):
            raise TimeoutError("parent did not release budget contenders")
        try:
            trajectory.reserve(0.25)
        except P.BudgetExceededError:
            result.put((index, False, ""))
        else:
            result.put((index, True, ""))
    except BaseException as exc:  # pragma: no cover - asserted in parent
        result.put((index, False, f"{type(exc).__name__}: {exc}"))


class ProviderDurableBudgetIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _ledger(self, limit: float = 1.0) -> DurableBudgetLedger:
        return DurableBudgetLedger.initialize(
            "provider-run", limit, self.root / "workspace"
        )

    def test_concurrent_cost_meters_share_one_transactional_cap(self) -> None:
        ledger = self._ledger(1.0)
        workers = 10
        barrier = threading.Barrier(workers)

        def reserve(index: int) -> bool:
            # Pipeline workers have independent in-memory meters but reopen
            # the same durable run ledger.
            worker_ledger = DurableBudgetLedger.initialize(
                "provider-run", 1.0, self.root / "workspace"
            )
            meter = P.CostMeter(
                budget_per_task_usd=10.0,
                durable_ledger=worker_ledger,
            )
            trajectory = meter.trajectory(f"task-{index}", "semantic_author")
            barrier.wait(timeout=10)
            try:
                trajectory.reserve(0.2)
            except P.BudgetExceededError as exc:
                self.assertEqual(exc.scope, "total")
                return False
            return True

        with ThreadPoolExecutor(max_workers=workers) as executor:
            admitted = list(executor.map(reserve, range(workers)))

        self.assertEqual(sum(admitted), 5)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.reserved_count, 5)
        self.assertEqual(snapshot.reserved_usd, 1.0)
        self.assertEqual(snapshot.available_usd, 0.0)

    def test_successful_provider_call_commits_actual_cost(self) -> None:
        ledger = self._ledger()
        transport = FakeTransport([anthropic_tool_response([VALID_FINDING])])
        meter = P.CostMeter(
            budget_per_task_usd=100.0,
            durable_ledger=ledger,
            durable_reservation_multiplier=1.25,
        )
        provider = _routed(self.root / "success", transport, meter=meter)

        text = provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")

        self.assertIn("findings", text)
        snapshot = ledger.snapshot()
        expected = (1000 * 2.0 + 200 * 10.0) / 1_000_000.0
        self.assertAlmostEqual(snapshot.committed_usd, expected)
        self.assertEqual(snapshot.committed_count, 1)
        self.assertEqual(snapshot.reserved_count, 0)
        self.assertEqual(snapshot.uncertain_count, 0)
        self.assertEqual(snapshot.reservations[0].state, ReservationState.COMMITTED)
        self.assertAlmostEqual(meter.total_usd, expected)

    def test_schema_retry_uses_one_reservation_and_commits_every_attempt(self) -> None:
        ledger = self._ledger()
        transport = FakeTransport(
            [
                anthropic_bad_tool_response(),
                anthropic_tool_response([VALID_FINDING]),
            ]
        )
        meter = P.CostMeter(budget_per_task_usd=100.0, durable_ledger=ledger)
        provider = _routed(self.root / "schema-retry", transport, meter=meter)

        provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")

        snapshot = ledger.snapshot()
        per_attempt = (1000 * 2.0 + 200 * 10.0) / 1_000_000.0
        self.assertEqual(snapshot.reservation_count, 1)
        self.assertEqual(snapshot.committed_count, 1)
        self.assertEqual(snapshot.reserved_count, 0)
        self.assertAlmostEqual(snapshot.committed_usd, 2 * per_attempt)
        self.assertAlmostEqual(meter.total_usd, 2 * per_attempt)

    def test_successful_attempt_batch_commits_all_cost_before_budget_error(self) -> None:
        ledger = self._ledger()
        transport = FakeTransport(
            [
                anthropic_bad_tool_response(),
                anthropic_bad_tool_response(),
                anthropic_tool_response([VALID_FINDING]),
            ]
        )
        meter = P.CostMeter(
            budget_per_task_usd=0.005,
            durable_ledger=ledger,
        )
        provider = _routed(self.root / "successful-breach", transport, meter=meter)

        with self.assertRaises(P.BudgetExceededError):
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")

        per_attempt = (1000 * 2.0 + 200 * 10.0) / 1_000_000.0
        snapshot = ledger.snapshot()
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(meter.attempt_count, 3)
        self.assertAlmostEqual(meter.total_usd, 3 * per_attempt)
        self.assertEqual(snapshot.committed_count, 1)
        self.assertEqual(snapshot.reserved_count, 0)
        self.assertAlmostEqual(snapshot.committed_usd, 3 * per_attempt)

    def test_raising_attempt_batch_commits_all_cost_before_budget_error(self) -> None:
        ledger = self._ledger()
        transport = FakeTransport(
            [anthropic_bad_tool_response() for _ in range(3)]
        )
        meter = P.CostMeter(
            budget_per_task_usd=0.005,
            durable_ledger=ledger,
        )
        provider = _routed(self.root / "raising-breach", transport, meter=meter)

        with self.assertRaises(P.BudgetExceededError) as caught:
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")

        self.assertIsInstance(caught.exception.__context__, P.ProviderProtocolError)
        per_attempt = (1000 * 2.0 + 200 * 10.0) / 1_000_000.0
        snapshot = ledger.snapshot()
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(meter.attempt_count, 3)
        self.assertAlmostEqual(meter.total_usd, 3 * per_attempt)
        self.assertEqual(snapshot.committed_count, 1)
        self.assertEqual(snapshot.reserved_count, 0)
        self.assertAlmostEqual(snapshot.committed_usd, 3 * per_attempt)

    def test_memo_hit_reconciles_recorded_exchange_after_commit_interruption(self) -> None:
        ledger = self._ledger()
        first_transport = FakeTransport(
            [anthropic_tool_response([VALID_FINDING])]
        )
        first = _routed(
            self.root / "commit-interruption",
            first_transport,
            meter=P.CostMeter(
                budget_per_task_usd=100.0,
                durable_ledger=ledger,
            ),
        )
        with mock.patch.object(
            ledger, "commit", side_effect=OSError("simulated commit interruption")
        ):
            with self.assertRaisesRegex(OSError, "simulated commit interruption"):
                first.complete(CouncilRole.AMBIGUITY_CRITIC, "p")

        interrupted = ledger.snapshot()
        self.assertEqual(interrupted.reserved_count, 1)
        reservation_id = interrupted.reservations[0].call_id
        key = first.transcript_key_for(CouncilRole.AMBIGUITY_CRITIC, "p")
        stored = first.store.lookup("ambiguity_critic", key)
        self.assertEqual(stored["budget_reservation_id"], reservation_id)
        ledger.mark_outstanding_uncertain()

        second_transport = FakeTransport([])
        second = _routed(
            self.root / "commit-interruption",
            second_transport,
            meter=P.CostMeter(
                budget_per_task_usd=100.0,
                durable_ledger=ledger,
            ),
        )
        second.complete(CouncilRole.AMBIGUITY_CRITIC, "p")

        self.assertEqual(second_transport.calls, [])
        recovered = ledger.snapshot()
        self.assertEqual(recovered.uncertain_count, 0)
        self.assertEqual(recovered.committed_count, 1)
        self.assertAlmostEqual(recovered.committed_usd, 0.004)

    def test_forged_transcript_reservation_id_cannot_charge_another_task(self) -> None:
        ledger = self._ledger()
        ledger.reserve("victim-task", "ambiguity_critic", 0.2, "victim-call")
        provider = _routed(
            self.root / "forged-id",
            FakeTransport([]),
            meter=P.CostMeter(
                budget_per_task_usd=100.0,
                durable_ledger=ledger,
            ),
        )
        route = provider.routing.for_role("ambiguity_critic")
        forged = {
            "budget_reservation_id": "victim-call",
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        }

        with self.assertRaisesRegex(ReservationConflictError, "belongs to task"):
            provider._reconcile_cached_durable_reservation(
                forged, "ambiguity_critic", route
            )
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.reserved_count, 1)
        self.assertEqual(snapshot.committed_count, 0)

    def test_exception_before_measured_attempt_stays_reserved_then_uncertain(self) -> None:
        ledger = self._ledger()
        fault = RuntimeError("provider transport failed without a response")

        def failed_transport(*_args, **_kwargs):
            raise fault

        meter = P.CostMeter(budget_per_task_usd=100.0, durable_ledger=ledger)
        provider = _routed(self.root / "failure", failed_transport, meter=meter)

        with self.assertRaises(RuntimeError) as caught:
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
        self.assertIs(caught.exception, fault)
        # No response means the local meter cannot price an actual attempt.
        # The prospective reservation is intentionally not released: the
        # provider may have accepted the request before transport failed.
        before_resume = ledger.snapshot()
        self.assertEqual(before_resume.reserved_count, 1)
        self.assertEqual(before_resume.committed_count, 0)
        self.assertGreater(before_resume.reserved_usd, 0.0)
        self.assertEqual(meter.total_usd, 0.0)

        self.assertEqual(ledger.mark_outstanding_uncertain(), 1)
        after_resume = ledger.snapshot()
        self.assertEqual(after_resume.reserved_count, 0)
        self.assertEqual(after_resume.uncertain_count, 1)
        self.assertEqual(after_resume.available_usd, before_resume.available_usd)

    def test_local_refusal_creates_no_durable_reservation(self) -> None:
        ledger = self._ledger()
        meter = P.CostMeter(
            budget_per_task_usd=0.1,
            durable_ledger=ledger,
        )
        trajectory = meter.trajectory("task", "semantic_author")
        with self.assertRaises(P.BudgetExceededError) as caught:
            trajectory.reserve(0.2)
        self.assertEqual(caught.exception.scope, "task")
        self.assertEqual(ledger.snapshot().reservation_count, 0)

    def test_new_meter_on_resume_cannot_reset_durable_per_task_cap(self) -> None:
        workspace = self.root / "resume-task-cap"
        ledger = DurableBudgetLedger.initialize(
            "resume-task-cap-run",
            350.0,
            workspace,
            per_task_limit_usd=7.0,
        )
        first_meter = P.CostMeter(
            budget_per_task_usd=7.0,
            durable_ledger=ledger,
            durable_reservation_multiplier=15.0,
        )
        first = first_meter.trajectory("task-1", "semantic_author")
        first.reserve(0.4)
        P.RoutedProvider._charge_attempts(
            first,
            P.RateCard(1.0, 1.0, 1.0, 1.0),
            (
                P.AttemptRecord(
                    usage=P.Usage(),
                    elapsed_ms=0,
                    served_model="test-model",
                    reported_usd=6.0,
                ),
            ),
            model="test-model",
        )
        self.assertEqual(ledger.snapshot().committed_usd, 6.0)

        # A resumed worker has an intentionally fresh local CostMeter. The
        # shared ledger, not that volatile meter, still sees the prior task's
        # $6 and refuses another retry-sized $6 reservation.
        resumed_ledger = DurableBudgetLedger.initialize(
            "resume-task-cap-run",
            350.0,
            workspace,
            per_task_limit_usd=7.0,
        )
        resumed = P.CostMeter(
            budget_per_task_usd=7.0,
            durable_ledger=resumed_ledger,
            durable_reservation_multiplier=15.0,
        ).trajectory("task-1", "ambiguity_critic")
        with self.assertRaises(P.BudgetExceededError) as caught:
            resumed.reserve(0.4)
        self.assertEqual(caught.exception.scope, "task")
        snapshot = resumed_ledger.snapshot()
        self.assertEqual(snapshot.per_task_limit_usd, 7.0)
        self.assertEqual(snapshot.committed_usd, 6.0)
        self.assertEqual(snapshot.reservation_count, 1)

    def test_session_init_guard_does_not_double_reserve_transport(self) -> None:
        ledger = self._ledger(2.0)
        meter = P.CostMeter(budget_per_task_usd=2.0, durable_ledger=ledger)
        trajectory = meter.trajectory("task", "semantic_author", max_usd=1.0)

        # This is the aggregate INIT guard used by `_run_session_core`.
        trajectory.reserve(1.0, durable=False)
        self.assertEqual(ledger.snapshot().reservation_count, 0)

        # The actual turn owns exactly one reservation, reconciled once after
        # all of that transport exchange's attempts have been metered.
        trajectory.reserve(0.2)
        P.RoutedProvider._charge_attempts(
            trajectory,
            P.RateCard(1.0, 1.0, 1.0, 1.0),
            (
                P.AttemptRecord(
                    usage=P.Usage(input_tokens=100_000),
                    elapsed_ms=0,
                    served_model="test-model",
                ),
            ),
            model="test-model",
        )
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.reservation_count, 1)
        self.assertEqual(snapshot.committed_count, 1)
        self.assertEqual(snapshot.reserved_count, 0)
        self.assertEqual(trajectory._durable_reservations, [])

    def test_spawned_workers_cannot_race_past_shared_cap(self) -> None:
        workspace = self.root / "process-workspace"
        ledger = DurableBudgetLedger.initialize("process-run", 1.0, workspace)
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_process_reserve,
                args=(str(workspace), start, results, index),
            )
            for index in range(8)
        ]
        try:
            for process in processes:
                process.start()
            start.set()
            outcomes = [results.get(timeout=20) for _ in processes]
        finally:
            for process in processes:
                process.join(timeout=20)
                if process.is_alive():  # pragma: no cover - defensive cleanup
                    process.terminate()
                    process.join(timeout=5)
        self.assertEqual([error for _, _, error in outcomes if error], [])
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        self.assertEqual(sum(admitted for _, admitted, _ in outcomes), 4)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.reserved_count, 4)
        self.assertEqual(snapshot.reserved_usd, 1.0)

    def test_resolve_provider_multiplies_nested_retry_allowances(self) -> None:
        ledger_workspace = self.root / "resolved"
        DurableBudgetLedger.initialize("resolve-run", 3.0, ledger_workspace)
        args = argparse.Namespace(
            agents_config=None,
            http_retries=2,
            schema_retries=3,
            http_timeout_seconds=17.0,
            http_backoff_seconds=0.25,
            global_budget_total=3.0,
            global_budget_run_id="resolve-run",
            budget_per_task=2.0,
            budget_total=None,
            replay_only=False,
            record=False,
        )
        with mock.patch.object(P, "configure_retry_policy") as configured:
            provider = cli._resolve_provider(args, ledger_workspace)
        configured.assert_called_once_with(
            http_retries=2,
            schema_retries=3,
            http_timeout_seconds=17.0,
            http_backoff_seconds=0.25,
        )
        self.assertEqual(provider.meter.durable_reservation_multiplier, 12.0)
        self.assertEqual(provider.meter.budget_total_usd, None)
        self.assertEqual(provider.meter.durable_ledger.run_id, "resolve-run")
        self.assertEqual(
            provider.meter.durable_ledger.snapshot().per_task_limit_usd,
            2.0,
        )

    def test_resolve_provider_can_share_parent_total_without_candidate_cap(self) -> None:
        parent_workspace = self.root / "parent"
        parent = DurableBudgetLedger.initialize(
            "parent-run",
            3.0,
            parent_workspace,
            per_task_limit_usd=0.5,
        )
        parent.reserve("candidate-a", "author", 0.4, "historical")
        parent.commit("historical", 0.4)
        args = argparse.Namespace(
            agents_config=None,
            http_retries=0,
            schema_retries=0,
            http_timeout_seconds=17.0,
            http_backoff_seconds=0.25,
            global_budget_workspace=parent_workspace,
            global_budget_total=3.0,
            global_budget_run_id="parent-run",
            global_budget_per_task=None,
            global_budget_enforce_task_limit=False,
            budget_per_task=2.0,
            budget_total=1.0,
            replay_only=False,
            record=False,
        )
        provider_workspace = self.root / "metrology"
        with mock.patch.object(P, "configure_retry_policy"):
            provider = cli._resolve_provider(args, provider_workspace)
        shared = provider.meter.durable_ledger
        self.assertEqual(shared.database_path, parent.database_path)
        self.assertFalse(shared.enforce_task_limit)
        shared.reserve("council-metrology", "critic", 1.0, "fresh-metrology")
        self.assertEqual(shared.snapshot().committed_usd, 0.4)
        self.assertEqual(shared.snapshot().reserved_usd, 1.0)

    def test_retry_reconfiguration_invalidates_behavior_digest_cache(self) -> None:
        original = (
            P._HTTP_RETRIES,
            P.SCHEMA_RETRIES,
            P._HTTP_TIMEOUT_SECONDS,
            P._HTTP_BACKOFF_BASE_SECONDS,
        )
        self.addCleanup(
            lambda: P.configure_retry_policy(
                http_retries=original[0],
                schema_retries=original[1],
                http_timeout_seconds=original[2],
                http_backoff_seconds=original[3],
            )
        )
        P.configure_retry_policy(schema_retries=2)
        two_retry_digest = P.role_behavior_sha256("ambiguity_critic")
        # This call must not return `_behavior_sha_cached` under the previous
        # module-level retry policy.
        P.configure_retry_policy(schema_retries=1)
        one_retry_digest = P.role_behavior_sha256("ambiguity_critic")

        self.assertNotEqual(one_retry_digest, two_retry_digest)
        self.assertEqual(
            P.role_behavior_manifest("ambiguity_critic")["schema_retries"], 1
        )

    def test_release_ready_delegation_propagates_run_config_retries(self) -> None:
        spec = GenerationRunSpec(
            candidate_count=1,
            source_families=(Origin.DBT.value,),
            source_allocation={Origin.DBT.value: 1},
            profile=ReadinessProfile.RELEASE_READY,
            empirical=True,
            budget_per_task=2.0,
            http_retries=1,
            schema_retries=4,
            http_timeout_seconds=23.0,
            http_backoff_seconds=0.75,
        )
        selected = SimpleNamespace(
            source_allocation={Origin.DBT: 1},
            ordered_entries=lambda: (SimpleNamespace(expected_task_id="task-1"),),
        )
        ingested = SimpleNamespace(task_ids=("task-1",), candidate_outcomes=())
        args = argparse.Namespace(
            workspace=str(self.root / "configured"),
            ingest_manifest=self.root / "pool.yaml",
            task_id=None,
            size=None,
            run_id="configured-run",
            reingest=False,
            http_retries=None,
            schema_retries=None,
            http_timeout_seconds=None,
            http_backoff_seconds=None,
        )
        delegated: list[argparse.Namespace] = []

        def capture(namespace: argparse.Namespace) -> int:
            delegated.append(namespace)
            return 0

        with mock.patch.object(cli, "_configured_run_spec", return_value=spec), mock.patch(
            "elt_taskgen.ingest_manifest.load_five_source_batch_manifest",
            return_value=object(),
        ), mock.patch(
            "elt_taskgen.ingest_manifest.select_candidate_manifest",
            return_value=selected,
        ), mock.patch(
            "elt_taskgen.ingest_manifest.ingest_selected_sources",
            return_value=(selected, ingested),
        ), mock.patch.object(
            cli, "_write_configured_report", return_value=self.root / "readiness.json"
        ), mock.patch.object(
            cli, "_configured_live_provider_problems", return_value=()
        ), mock.patch.object(cli, "_cmd_pipeline_legacy", side_effect=capture):
            self.assertEqual(cli._cmd_pipeline_configured(args), 0)

        self.assertEqual(len(delegated), 1)
        worker_args = delegated[0]
        self.assertEqual(worker_args.http_retries, 1)
        self.assertEqual(worker_args.schema_retries, 4)
        self.assertEqual(worker_args.http_timeout_seconds, 23.0)
        self.assertEqual(worker_args.http_backoff_seconds, 0.75)
        self.assertIsNotNone(worker_args.configured_run_spec)

    def test_terminal_readiness_report_marks_abandoned_reservations_uncertain(self) -> None:
        from elt_taskgen.pipeline_readiness import PipelineReadinessReport

        spec = GenerationRunSpec(
            candidate_count=1,
            source_families=(Origin.DBT.value,),
            source_allocation={Origin.DBT.value: 1},
            profile=ReadinessProfile.LOCAL_READY,
            budget_per_task=2.0,
            budget_total=1.0,
        )
        workspace = self.root / "terminal-report"
        ledger = DurableBudgetLedger.initialize("terminal-run", 1.0, workspace)
        ledger.reserve("task-1", "semantic_author", 0.2, "ambiguous-call")
        report = mock.Mock(spec=PipelineReadinessReport)
        report.model_copy.return_value = report
        selected = mock.Mock()
        selected.manifest_sha256.return_value = "a" * 64
        engine = mock.Mock()

        with mock.patch.object(cli, "_open_engine", return_value=engine), mock.patch(
            "elt_taskgen.pipeline_readiness.make_run_report", return_value=report
        ) as make_report, mock.patch(
            "elt_taskgen.pipeline_readiness.write_run_report",
            return_value=workspace / "readiness.json",
        ):
            cli._write_configured_report(
                workspace=workspace,
                run_id="terminal-run",
                spec=spec,
                task_ids=("task-1",),
                state="COMPLETE_WITH_ISSUES",
                selected_manifest=selected,
            )

        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.reserved_count, 0)
        self.assertEqual(snapshot.uncertain_count, 1)
        self.assertEqual(snapshot.uncertain_usd, 0.2)
        passed_snapshot = make_report.call_args.kwargs["budget_snapshot"]
        self.assertEqual(passed_snapshot.reserved_count, 0)
        self.assertEqual(passed_snapshot.uncertain_count, 1)
        self.assertEqual(passed_snapshot.uncertain_usd, 0.2)
        report.model_copy.assert_not_called()
        engine.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

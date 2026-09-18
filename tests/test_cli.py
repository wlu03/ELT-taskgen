"""CLI + stage-wiring integration tests.

WHY THIS EXISTS
The parser contract and the stage-wiring contract are cheap to check and easy
to break: a renamed subcommand, a default that drifts, a stage runner that is
never wired. Those checks run unconditionally here.

The end-to-end acceptance tests that used to live in this file drove
`elt-taskgen demo`, which was removed. Exercising the pipeline
end to end now means driving a real pool; see the README ladder.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import cli, demo_fixture, repair
from elt_taskgen import workspace as workspace_mod
from elt_taskgen.engine import (
    Engine,
    StageName,
    StageOutcome,
    StagePayload,
    STAGE_ORDER,
    VERDICT_FAIL,
    VERDICT_PASS,
)
from elt_taskgen.models import CouncilRole, Finding, RepairRoute, Severity

from elt_taskgen.review import council
from elt_taskgen.review import providers as providers_mod
from elt_taskgen.review.budget_ledger import DurableBudgetLedger


class CannedFindingsProvider:
    """Protocol-valid stub for stage-wiring tests (transport-free): returns a
    fixed findings payload so make_review_runner's logic can be exercised
    without any provider backend."""

    def complete(self, role, prompt):
        role_name = getattr(role, "value", role)
        if role_name in {"ambiguity_critic", "feasibility_reviewer"}:
            return '{"findings": []}'
        expected = {name: True for name in (
            "development", "primary", "resampled", "counterfactual", "stress"
        )}
        return json.dumps(
            {
                "findings": [
                    {
                        "severity": "major",
                        "summary": "constants shortcut must lose reward",
                        "detail": "compile a constants mutant",
                        "route_hint": None,
                        "suggested_attack": "constants", "disposition": "active",
                        "proposed_case": {
                            "kind": "constants",
                            "params": "{}",
                            "expected_pass_by_stage": {
                                "extract_load": expected,
                                "transform": expected,
                            },
                            "rationale": (
                                "the constants mutant is an explicit executable "
                                "shortcut probe"
                            ),
                        },
                    }
                ]
            }
        )


class TestWorkspaceIsNotAContainer(unittest.TestCase):
    """A stray `--workspace` must not be able to sit on shipped artifacts.

    The admission record, 140 metrology transcripts and 5 quarantined
    markers were once destroyed by a process that targeted the directory
    holding them. `runs/` holds every release and `council/` the one admission
    record; neither is a workspace, and naming one is refused before any stage
    can write.
    """

    def setUp(self):
        # Resolve the containers against a TEMPORARY repo root, so the test is
        # independent of the caller's cwd (it used to pass only from the repo
        # root, i.e. the suite was not portable to a clean checkout).
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        for name in workspace_mod.NEVER_A_WORKSPACE:
            (self.root / name).mkdir()
        patcher = mock.patch.object(workspace_mod, "repo_root", lambda: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _refuses(self, *args, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cli._assert_workspace_is_not_a_container(*args, **kwargs)
        return ctx.exception

    def test_the_containers_are_refused(self):
        for name in workspace_mod.NEVER_A_WORKSPACE:
            with self.subTest(container=name):
                exc = self._refuses(self.root / name)
                self.assertIn("is a container", str(exc))
                self.assertIn(name, str(exc))

    def test_refusal_exit_code_is_two(self):
        """1 already means "EMPTY RESULT" (ingest) and "task rejected"
        (stages); a refusal is neither, and `raise SystemExit(str)` exits 1."""
        exc = self._refuses(self.root / "runs")
        self.assertEqual(exc.code, 2)
        self.assertIn("is a container", str(exc))

    def test_council_is_allowed_only_when_named(self):
        """metrology's home IS council/ (README documents it); nothing else."""
        cli._assert_workspace_is_not_a_container(
            self.root / "council", allow=("council",)
        )
        self._refuses(self.root / "runs", allow=("council",))
        parser = cli.build_parser()
        self.assertEqual(
            parser.parse_args(["metrology"]).container_allow, ("council",)
        )
        self.assertEqual(
            getattr(
                parser.parse_args(["generate", "--task-id", "t"]),
                "container_allow",
                (),
            ),
            (),
        )

    def test_a_child_and_an_unrelated_path_are_allowed(self):
        """The discriminating half: the guard must not refuse everything."""
        for ok in (
            self.root / "runs" / "default",
            self.root / "council" / "scratch",
            Path("/tmp/ws"),
        ):
            with self.subTest(path=str(ok)):
                cli._assert_workspace_is_not_a_container(ok)

    def test_the_default_workspace_is_not_a_container(self):
        cli._assert_workspace_is_not_a_container(cli.DEFAULT_WORKSPACE)
        self.assertEqual(cli.DEFAULT_WORKSPACE, Path("runs") / "default")

    def test_every_engine_in_cli_is_opened_through_the_guard(self):
        """Regression lint for the 13 bare `Engine(...)` call sites.

        `Engine.__init__` creates state/ and tasks/ on construction, so any
        construction that skips the guard is a writer that can land on a
        container (measured: `ingest-synsql --workspace runs` exited 0 after
        creating runs/state and runs/tasks beside the releases)."""
        source = Path(cli.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name == "_open_engine":
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "Engine"
                ):
                    offenders.append(f"{node.name}:{inner.lineno}")
        self.assertEqual(
            offenders, [], "construct engines via cli._open_engine (guarded)"
        )

    def test_ingest_and_export_refuse_the_containers_before_writing(self):
        """The guard runs in main(), so it also covers the writers that never
        build an Engine (measure-target, every ingest's contamination seed)."""
        for argv in (
            ["audit", "list"],
            ["triage"],
            ["export", "--task-id", "nope"],
            ["ingest-dlt", "--list"],
        ):
            for container in workspace_mod.NEVER_A_WORKSPACE:
                target = self.root / container
                with self.subTest(argv=argv[0], container=container):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as ctx:
                            cli.main([*argv, "--workspace", str(target)])
                    self.assertEqual(ctx.exception.code, 2)
                    self.assertFalse((target / "state").exists())
                    self.assertFalse((target / "tasks").exists())


class TestParser(unittest.TestCase):
    def test_all_contract_subcommands_exist(self):
        parser = cli.build_parser()
        sub = next(
            a for a in parser._actions
            if isinstance(a, cli.argparse._SubParsersAction)  # type: ignore[attr-defined]
        )
        expected = {
            "ingest-dbt", "ingest-synsql", "ingest-anchor", "generate",
            "reference-run", "review", "attack", "validate", "validate-el",
            "validate-t", "calibrate", "select", "export", "release",
            "record-transcripts",
            # X3/X5: scoring a released unit and byte-verifying a release tree
            # are taskgen's own jobs — the harness is a thin client and the
            # documented `shasum -c` remedy skips every census-pinned warehouse.
            "score", "verify",
        }
        self.assertTrue(
            expected.issubset(set(sub.choices)),
            sorted(expected - set(sub.choices)),
        )

    def test_missing_subcommand_is_an_error(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.build_parser().parse_args([])

    def test_workspace_default(self):
        args = cli.build_parser().parse_args(["generate", "--task-id", "t"])
        self.assertEqual(Path(args.workspace), cli.DEFAULT_WORKSPACE)

    def test_provider_flags_default(self):
        args = cli.build_parser().parse_args(["generate", "--task-id", "t"])
        self.assertFalse(args.replay_only)
        self.assertFalse(args.record)
        # Cost T3 / roadmap 0.D: 5.00 for every profile (the former 2.00
        # already breached a one-shot task with `calibrate --empirical`).
        self.assertEqual(args.budget_per_task, 5.00)
        self.assertEqual(args.budget_per_task, providers_mod.DEFAULT_BUDGET_PER_TASK_USD)
        self.assertIsNone(args.budget_total)

    def test_repair_proposer_defaults_to_bounded_and_has_explicit_rollback(self):
        """The CLI enables the bounded proposer by default.

        ``one_shot`` remains an explicit compatibility mode and
        ``--no-repair-proposer`` is the complete offline/cost rollback.
        """
        from elt_taskgen.review import repair_proposer as rp

        parser = cli.build_parser()
        args = parser.parse_args(["generate", "--task-id", "t"])
        self.assertTrue(args.repair_proposer)
        self.assertEqual(args.repair_proposer_mode, "bounded")
        self.assertEqual(rp.DEFAULT_REPAIR_PROPOSER_MODE, "one_shot")
        self.assertEqual(rp.REPAIR_PROPOSER_MODES, ("one_shot", "bounded"))
        self.assertIsInstance(
            cli._make_repair_proposer(args, object()), rp.AgenticRepairProposer
        )
        one_shot = parser.parse_args(
            ["generate", "--task-id", "t", "--repair-proposer-mode", "one_shot"]
        )
        self.assertIsInstance(
            cli._make_repair_proposer(one_shot, object()), rp.RepairProposer
        )
        disabled = parser.parse_args(
            ["generate", "--task-id", "t", "--no-repair-proposer"]
        )
        self.assertIsNone(cli._make_repair_proposer(disabled, object()))
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["generate", "--task-id", "t", "--repair-proposer-mode", "agentic"])
        # The bounded-profile budget is documented on the flag, the default stays 5.00.
        generate = next(
            a for a in parser._actions if isinstance(a, cli.argparse._SubParsersAction)  # type: ignore[attr-defined]
        ).choices["generate"]
        self.assertIn("7.00", generate._option_string_actions["--budget-per-task"].help)
        self.assertEqual(args.budget_per_task, 5.00)


class TestIngestContaminationReporting(unittest.TestCase):
    def test_zero_borderline_and_fatal_results_use_one_output_contract(self):
        from elt_taskgen.verification.contamination import Collision

        borderline = Collision(
            kind="schema-table",
            against="eltbench",
            detail="shared table shape",
            fatal=False,
        )
        fatal = Collision(
            kind="family",
            against="spider2_dbt",
            detail="reserved family",
            fatal=True,
        )
        cases = (
            ((), 0, ""),
            (
                (borderline,),
                0,
                "  contamination borderline [schema-table/eltbench] "
                "shared table shape\n",
            ),
            (
                (borderline, fatal),
                1,
                "  contamination borderline [schema-table/eltbench] "
                "shared table shape\n"
                "  contamination FATAL [family/spider2_dbt] reserved family\n"
                "REFUSED: 1 fatal contamination collision(s) — "
                "candidate__task was NOT registered\n",
            ),
        )
        for collisions, expected_count, expected_output in cases:
            with self.subTest(collisions=len(collisions)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    count = cli._report_ingest_collisions(
                        "candidate__task", collisions
                    )
                self.assertEqual(count, expected_count)
                self.assertEqual(output.getvalue(), expected_output)


class TestStageWiring(unittest.TestCase):
    def test_every_stage_but_intake_is_wired(self):
        runners = cli.build_stage_runners(CannedFindingsProvider())
        wired = set(runners)
        expected = set(STAGE_ORDER) - {StageName.INTAKE}
        self.assertEqual(wired, expected)

    def test_require_pass_payload_fails_closed_on_missing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(Path(tmp))
            try:
                task = demo_fixture.demo_task()
                engine.register(task)
                with self.assertRaises(RuntimeError):
                    cli._require_pass_payload(engine, task, StageName.REVIEW)
            finally:
                engine.close()


class TestReviewStageDiligence(unittest.TestCase):
    """run_review fail-closed contract: provider protocol failures BLOCK
    without judging the task, and an empty shortcut review remains a diligence
    failure rather than a clean pass."""

    def test_unparseable_provider_output_blocks_with_structured_protocol_failure(self):
        class GarbageProvider:
            def complete(self, role, prompt):
                return "not json at all {{{"

        run_review = cli.make_review_runner(GarbageProvider())
        first = run_review(None, demo_fixture.demo_task())
        second = run_review(None, demo_fixture.demo_task())
        self.assertEqual(first.verdict, cli.VERDICT_BLOCKED)
        self.assertEqual(first.payload.data["failure_class"], "protocol_failure")
        self.assertEqual(
            first.payload.data["failure_code"],
            "critic_attack_handoff_invalid",
        )
        self.assertEqual(first.payload.data["blocked_on"], "provider")
        self.assertEqual(first.payload.data["retry_guard"], "explicit")
        self.assertEqual(
            first.payload.data["recovery_prerequisite"],
            "correct_or_replace_critic_handoff",
        )
        self.assertEqual(len(first.payload.data["handoff_digest"]), 64)
        self.assertEqual(
            first.payload.data["handoff_digest"],
            second.payload.data["handoff_digest"],
        )

    def test_review_transcript_manifest_integrity_is_protocol_not_task_quality(self):
        run_review = cli.make_review_runner(CannedFindingsProvider())
        with mock.patch.object(
            cli,
            "_validated_review_manifest",
            return_value=((), ["shortcut_attacker: behavior hash drifted"]),
        ):
            outcome = run_review(None, demo_fixture.demo_task())

        self.assertEqual(outcome.verdict, cli.VERDICT_BLOCKED)
        self.assertIsNone(outcome.route)
        self.assertEqual(outcome.payload.data["failure_class"], "protocol_failure")
        self.assertEqual(
            outcome.payload.data["failure_code"],
            "critic_transcript_manifest_invalid",
        )
        self.assertEqual(outcome.payload.data["blocked_on"], "provider")
        self.assertEqual(outcome.payload.data["retry_guard"], "explicit")
        self.assertEqual(
            outcome.payload.data["recovery_prerequisite"],
            "rebuild_or_correct_review_transcript_manifest",
        )
        self.assertIn("transcript manifest integrity", outcome.payload.error)

    def test_manifest_protocol_failure_precedes_fatal_task_judgment(self):
        finding = Finding(
            finding_id="feasibility_reviewer-fatal-manifest",
            role=CouncilRole.FEASIBILITY_REVIEWER,
            severity=Severity.FATAL,
            summary="The public specification is internally inconsistent",
            detail="customer_summary declares incompatible total_spend rules",
            route_hint=RepairRoute.SPECIFICATION,
            proposed_case=None,
        )
        with (
            mock.patch.object(council, "run_council", return_value=[finding]),
            mock.patch.object(
                cli,
                "_validated_review_manifest",
                return_value=((), ["feasibility_reviewer: trajectory hash drifted"]),
            ),
        ):
            outcome = cli.make_review_runner(CannedFindingsProvider())(
                None, demo_fixture.demo_task()
            )

        self.assertEqual(outcome.verdict, cli.VERDICT_BLOCKED)
        self.assertIsNone(outcome.route)
        self.assertEqual(
            outcome.payload.data["failure_code"],
            "critic_transcript_manifest_invalid",
        )

    def test_silent_shortcut_attacker_fails_diligence(self):
        class SilentProvider:
            def complete(self, role, prompt):
                return '{"findings": []}'

        run_review = cli.make_review_runner(SilentProvider())
        outcome = run_review(None, demo_fixture.demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_BLOCKED)
        self.assertEqual(outcome.payload.data["failure_class"], "protocol_failure")
        self.assertEqual(
            outcome.payload.data["failure_code"],
            "critic_shortcut_diligence_incomplete",
        )
        self.assertEqual(outcome.payload.data["retry_guard"], "explicit")
        self.assertIn("shortcut_attacker", outcome.payload.error)

    def test_diligence_failure_after_an_in_session_correction_fails_on_the_route(self):
        """Review finding 1-3 (batch-repair round 2): when the shortcut
        attacker's harness-validated session already TOLD the seat (a
        compile correction, or its accepted payload still red and voided
        post-session) and no executable probe survived, the review stage
        FAILs on the attacker's route (SPECIFICATION: the review re-runs on
        the revised prose with a fresh session, bounded by the repair
        attempts) instead of the `retry_guard=explicit` block that held the
        task forever.  Without that session evidence — a one-shot row, a
        session never corrected, a duck-typed provider — the diligence
        failure stays the protocol block it was."""
        class SilentProvider:
            def complete(self, role, prompt):
                return '{"findings": []}'

        def row(**counts):
            base = {"role": "shortcut_attacker", "entry_schema": 3,
                    "correction_kinds": {"schema": 0, "compile": 0},
                    "submitted_with_red_validators": 0}
            base.update(counts)
            return base

        corrected = [
            ("compile correction", row(correction_kinds={"schema": 0, "compile": 1})),
            ("red at submit", row(submitted_with_red_validators=1)),
        ]
        for label, attacker_row in corrected:
            with self.subTest(label=label), mock.patch.object(
                cli, "_validated_review_manifest", return_value=((attacker_row,), [])
            ):
                outcome = cli.make_review_runner(SilentProvider())(None, demo_fixture.demo_task())
            self.assertEqual(outcome.verdict, cli.VERDICT_FAIL, label)
            self.assertIs(outcome.route, RepairRoute.SPECIFICATION)
            self.assertIsInstance(outcome.payload, cli.ReviewPayload)
            self.assertIn("zero executable probes", outcome.payload.detail)
            self.assertIn("corrected in-session", outcome.payload.detail)
            self.assertEqual(outcome.payload.transcript_manifest, (attacker_row,))
            self.assertFalse(outcome.infrastructure)
        not_corrected = [
            ("session never corrected", row()),
            ("one-shot row", {"role": "shortcut_attacker", "entry_schema": 2,
                              "attempt_count": 2, "correction_count": 1}),
            ("another seat's row", {"role": "population_adversary", "entry_schema": 3,
                                    "correction_kinds": {"schema": 0, "compile": 1},
                                    "submitted_with_red_validators": 1}),
            ("no rows", None),
        ]
        for label, attacker_row in not_corrected:
            rows = () if attacker_row is None else (attacker_row,)
            with self.subTest(label=label), mock.patch.object(
                cli, "_validated_review_manifest", return_value=(rows, [])
            ):
                outcome = cli.make_review_runner(SilentProvider())(None, demo_fixture.demo_task())
            self.assertEqual(outcome.verdict, cli.VERDICT_BLOCKED, label)
            self.assertEqual(outcome.payload.data["failure_code"], "critic_shortcut_diligence_incomplete")
            self.assertEqual(outcome.payload.data["retry_guard"], "explicit")
        self.assertFalse(cli._attacker_corrected_in_session(()))
        self.assertFalse(cli._attacker_corrected_in_session(("not a row",)))
        self.assertTrue(cli._attacker_corrected_in_session((row(submitted_with_red_validators=1),)))

    def test_info_suggestion_without_a_proposal_cannot_satisfy_diligence(self):
        class InformationalSuggestionProvider:
            def complete(self, role, prompt):
                if getattr(role, "value", role) != "shortcut_attacker":
                    return '{"findings": []}'
                return json.dumps(
                    {
                        "findings": [
                            {
                                "severity": "info",
                                "summary": "Constants are a candidate shortcut probe",
                                "detail": (
                                    "Set customer_summary completed_order_count "
                                    "and total_spend to zero."
                                ),
                                "route_hint": None,
                                "suggested_attack": "constants", "disposition": "active",
                                "proposed_case": None,
                            }
                        ]
                    }
                )

        outcome = cli.make_review_runner(InformationalSuggestionProvider())(
            None, demo_fixture.demo_task()
        )
        self.assertEqual(outcome.verdict, cli.VERDICT_BLOCKED)
        self.assertEqual(outcome.payload.data["failure_class"], "protocol_failure")
        self.assertEqual(
            outcome.payload.data["failure_code"],
            "critic_shortcut_diligence_incomplete",
        )
        self.assertIn("zero executable probes", outcome.payload.error)

    def test_valid_minor_shortcut_proposal_satisfies_diligence(self):
        class ValidatedProbeProvider:
            def complete(self, role, prompt):
                if getattr(role, "value", role) != "shortcut_attacker":
                    return '{"findings": []}'
                populations = (
                    "development",
                    "primary",
                    "resampled",
                    "counterfactual",
                    "stress",
                )
                return json.dumps(
                    {
                        "findings": [
                            {
                                "severity": "minor",
                                "summary": "Constants are an executable shortcut probe",
                                "detail": (
                                    "Set customer_summary completed_order_count "
                                    "and total_spend to zero."
                                ),
                                "route_hint": None,
                                "suggested_attack": "constants", "disposition": "active",
                                "proposed_case": {
                                    "kind": "constants",
                                    "params": "{}",
                                    "expected_pass_by_stage": {
                                        "extract_load": {
                                            population: True
                                            for population in populations
                                        },
                                        "transform": {
                                            population: False
                                            for population in populations
                                        },
                                    },
                                    "rationale": (
                                        "The constants shortcut must lose "
                                        "transform reward on every population."
                                    ),
                                },
                            }
                        ]
                    }
                )

        run_review = cli.make_review_runner(ValidatedProbeProvider())
        outcome = run_review(None, demo_fixture.demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)

    def test_unexecutable_ambiguity_and_gold_disputes_await_adjudication(self):
        class DisputeProvider:
            def __init__(self, target_role, route_hint, severity):
                self.target_role = target_role
                self.route_hint = route_hint
                self.severity = severity
                self.fallback = CannedFindingsProvider()

            def complete(self, role, prompt):
                role_name = getattr(role, "value", role)
                if role_name != self.target_role:
                    return self.fallback.complete(role, prompt)
                return json.dumps(
                    {
                        "findings": [
                            {
                                "severity": self.severity,
                                "summary": (
                                    "customer_summary has an unresolved "
                                    "semantic interpretation"
                                ),
                                "detail": (
                                    "Rule 2 and the order_items inputs admit "
                                    "two incompatible total_spend readings"
                                ),
                                "route_hint": self.route_hint,
                                "suggested_attack": None, "disposition": "active",
                                "proposed_case": None,
                            }
                        ]
                    }
                )

        cases = (
            (
                "ambiguity_critic",
                None,
                "fatal",
                "critic_ambiguity_requires_adjudication",
            ),
            (
                "feasibility_reviewer",
                "reference",
                "major",
                "critic_gold_disagreement",
            ),
        )
        for role, route_hint, severity, code in cases:
            with self.subTest(role=role):
                run_review = cli.make_review_runner(
                    DisputeProvider(role, route_hint, severity)
                )
                outcome = run_review(None, demo_fixture.demo_task())
                self.assertEqual(outcome.verdict, cli.VERDICT_BLOCKED)
                self.assertIsNone(outcome.route)
                self.assertEqual(
                    outcome.payload.data["failure_class"],
                    "pending_adjudication",
                )
                self.assertEqual(outcome.payload.data["failure_code"], code)
                self.assertEqual(outcome.payload.data["blocked_on"], "human")
                self.assertEqual(outcome.payload.data["retry_guard"], "explicit")
                self.assertEqual(
                    outcome.payload.data["recovery_prerequisite"],
                    "bound_adjudication_or_revised_critic_evidence",
                )
                self.assertEqual(
                    len(outcome.payload.data["critic_evidence_sha256"]), 64
                )

    def test_replay_only_provider_without_transcripts_raises(self):
        """A production review stage with no transcripts and --replay-only
        fails closed via TranscriptMissingError."""
        with tempfile.TemporaryDirectory() as tmp:
            provider = providers_mod.RoutedProvider(
                providers_mod.load_role_routing(),
                providers_mod.TranscriptStore(Path(tmp) / "transcripts"),
                providers_mod.CostMeter(),
                replay_only=True,
            )
            run_review = cli.make_review_runner(provider)
            with self.assertRaises(providers_mod.TranscriptMissingError):
                run_review(None, demo_fixture.demo_task())


class TestProposalFailureRoute(unittest.TestCase):
    """The attack stage's FAIL route is derived from the promoter's artifacts,
    never from a critic's `route_hint` (trust boundary 6.4; row 2: default
    SPECIFICATION). A model-authored hint once steered the whole AttackPayload
    to the POPULATION route."""

    def test_proposal_failure_route_ignores_critic_route_hint(self):
        import inspect

        from elt_taskgen.models import CouncilRole, Finding, PopulationName, RepairRoute, Severity
        from elt_taskgen.verification.attacks import PromotionOutcome

        def finding(fid, hint):
            return Finding(
                finding_id=fid, role=CouncilRole.SHORTCUT_ATTACKER, severity=Severity.MAJOR,
                summary="x", route_hint=hint,
            )

        hinted = finding("f1", RepairRoute.POPULATION)
        fatal = finding("f2", RepairRoute.FATAL)
        plain = finding("f1", None)
        self.assertIs(cli._proposal_failure_route([hinted, fatal]), RepairRoute.SPECIFICATION)
        self.assertIs(cli._proposal_failure_route([]), RepairRoute.SPECIFICATION)
        mismatch = PromotionOutcome(
            finding_id="f1", case_name="c", kind="inner_join", promoted=False,
            reason="measured matrix does not match the proposed expectation",
            measured_pass={"development": True, "primary": False}, mismatches=("primary",),
        )
        self.assertIs(cli._proposal_failure_route([hinted], [mismatch]), RepairRoute.SPECIFICATION)
        exploit = PromotionOutcome(
            finding_id="f1", case_name="c", kind="inner_join", promoted=False,
            reason="proposal would keep FULL combined reward everywhere",
            measured_pass={p.value: True for p in PopulationName},
        )
        # The MEASURED fact (a wrong implementation passes everywhere) routes
        # POPULATION with or without a hint; the hint itself moves nothing.
        self.assertIs(cli._proposal_failure_route([plain], [exploit]), RepairRoute.POPULATION)
        self.assertIs(cli._proposal_failure_route([fatal], [exploit.model_copy(update={"finding_id": "f2"})]), RepairRoute.POPULATION)
        self.assertIs(cli._proposal_failure_route([finding("f1", RepairRoute.REFERENCE)], [mismatch]), RepairRoute.SPECIFICATION)
        body = inspect.getsource(cli._proposal_failure_route).split('"""')[-1]
        self.assertNotIn("route_hint", body)

    def test_role_selects_the_repair_surface_without_model_route_authority(self):
        from elt_taskgen.models import CouncilRole, Finding, RepairRoute, Severity

        def finding(fid, role, hint=RepairRoute.FATAL):
            return Finding(
                finding_id=fid,
                role=role,
                severity=Severity.MAJOR,
                summary="customers customer_id need a clearer inner_join rule",
                detail="development exposes it; primary and c_9001 are private",
                route_hint=hint,
            )

        ambiguity = finding("z-ambiguity", CouncilRole.AMBIGUITY_CRITIC)
        feasibility = finding("z-feasibility", CouncilRole.FEASIBILITY_REVIEWER)
        population = finding("z-population", CouncilRole.POPULATION_ADVERSARY)
        self.assertIs(
            cli._proposal_failure_route([ambiguity]), RepairRoute.SPECIFICATION
        )
        self.assertIs(
            cli._proposal_failure_route([feasibility]), RepairRoute.SPECIFICATION
        )
        self.assertIs(
            cli._proposal_failure_route([population]), RepairRoute.POPULATION
        )

        # Selection is by the harness id, never list order or finding prose.
        selected = cli._selected_blocking_finding(
            [population, feasibility, ambiguity]
        )
        self.assertIs(selected, ambiguity)

    def test_missing_proposal_at_major_is_an_unresolved_claim_not_a_protocol_failure(self):
        """Batch repair D1 (reports 111/123/156): the handoff helpers agree
        that a MAJOR finding WITHOUT a proposal is an unresolved claim for the
        role's repair route — never `critic_attack_handoff_invalid` (blocked
        forever) and, for a bare major ambiguity claim, never a human hold —
        while MALFORMED executable content and a MINOR named attack without
        a proposal stay protocol failures, and FATAL ambiguity / reference
        disputes stay held."""
        from elt_taskgen.models import (
            AttackKind, CouncilRole, Finding, PopulationName, ProposedAttackCase,
            RepairRoute, Severity, TaskVariant,
        )
        from elt_taskgen.review.council import ProviderProtocolError

        task = demo_fixture.demo_task()

        def finding(fid, role, severity=Severity.MAJOR, attack=None, proposal=None, hint=None):
            return Finding(
                finding_id=fid, role=role, severity=severity,
                summary="customer_summary admits two readings of total_spend",
                detail="the prose and the schema disagree on customer_summary",
                route_hint=hint, suggested_attack=attack, proposed_case=proposal,
            )

        bare_ambiguity = finding("ambiguity_critic-00", CouncilRole.AMBIGUITY_CRITIC, hint=RepairRoute.SPECIFICATION)
        hinted_ambiguity = finding("ambiguity_critic-01", CouncilRole.AMBIGUITY_CRITIC, attack=AttackKind.NO_NULL_DEFAULT)
        bare_adversary = finding("population_adversary-00", CouncilRole.POPULATION_ADVERSARY, hint=RepairRoute.POPULATION)
        hinted_adversary = finding("population_adversary-01", CouncilRole.POPULATION_ADVERSARY, attack=AttackKind.WRONG_AGG_STAGE)
        unresolved = [bare_ambiguity, hinted_ambiguity, bare_adversary, hinted_adversary]
        # Not protocol, not held.
        self.assertEqual(cli._validated_executable_findings(task, unresolved), unresolved)
        self.assertIsNone(cli._critic_adjudication_block(unresolved))
        # Unresolved, and routed by role: the ambiguity critic's claim selects
        # SPECIFICATION; alone, the adversary's selects POPULATION.
        blocking, problems = cli._blocking_proposal_failures(unresolved, ())
        self.assertEqual([f.finding_id for f in blocking], [f.finding_id for f in unresolved])
        self.assertTrue(all("has no structured proposed_case; it is unresolved" in p for p in problems))
        self.assertIs(cli._proposal_failure_route(blocking, ()), RepairRoute.SPECIFICATION)
        self.assertIs(cli._proposal_failure_route([bare_adversary, hinted_adversary], ()), RepairRoute.POPULATION)
        # Still held: a FATAL ambiguity claim, a reference dispute, and a
        # major ambiguity claim carrying an executable alternative.
        matrix = {p: True for p in PopulationName}
        proposal = ProposedAttackCase(
            kind=AttackKind.INNER_JOIN, params={}, expected_pass=matrix,
            expected_pass_by_stage={TaskVariant.EXTRACT_LOAD: matrix, TaskVariant.TRANSFORM: matrix},
            rationale="the other reading keeps full reward on customer_summary",
        )
        # Since 2026-09-11 (batch10 runs K and L) a MAJOR ambiguity claim
        # carrying an executable alternative is not held either: the attack
        # stage withholds the alternative from promotion, so nothing freezes
        # a reading, and the claim reaches the SPECIFICATION route unresolved.
        carrying = finding("ambiguity_critic-03", CouncilRole.AMBIGUITY_CRITIC, attack=AttackKind.INNER_JOIN, proposal=proposal)
        self.assertIsNone(cli._critic_adjudication_block([carrying]))
        blocking, problems = cli._blocking_proposal_failures([carrying], ())
        self.assertEqual([f.finding_id for f in blocking], ["ambiguity_critic-03"])
        self.assertIn("withheld from promotion", problems[0])
        self.assertIs(cli._proposal_failure_route(blocking, ()), RepairRoute.SPECIFICATION)
        for held in (
            finding("ambiguity_critic-02", CouncilRole.AMBIGUITY_CRITIC, severity=Severity.FATAL),
            finding("feasibility_reviewer-00", CouncilRole.FEASIBILITY_REVIEWER, hint=RepairRoute.REFERENCE),
            finding("ambiguity_critic-04", CouncilRole.AMBIGUITY_CRITIC,
                    hint=RepairRoute.REFERENCE, proposal=proposal),
        ):
            with self.subTest(held=held.finding_id):
                outcome = cli._critic_adjudication_block([held])
                self.assertIsNotNone(outcome)
                self.assertEqual(outcome.payload.data["failure_class"], "pending_adjudication")
        # Still protocol: malformed executable content, and a MINOR named
        # attack that promised a case and attached none.
        for invalid in (
            finding("population_adversary-02", CouncilRole.POPULATION_ADVERSARY, severity=Severity.MINOR, attack=AttackKind.NO_OP),
            finding("population_adversary-03", CouncilRole.POPULATION_ADVERSARY, attack=AttackKind.WRONG_AGG_STAGE,
                    proposal=proposal.model_copy(update={"kind": AttackKind.WRONG_AGG_STAGE, "params": {"variant": "made_up"}})),
            finding("population_adversary-04", CouncilRole.POPULATION_ADVERSARY, severity=Severity.INFO, proposal=proposal),
        ):
            with self.subTest(invalid=invalid.finding_id):
                with self.assertRaises(ProviderProtocolError):
                    cli._validated_executable_findings(task, [invalid])

    def test_unresolved_major_failure_carries_the_critics_own_words(self):
        """Batch 2026-09-10: the SPECIFICATION proposer is asked to resolve an
        unresolved MAJOR finding, and its whole view of that finding is this
        failure string. Naming only the finding id told it WHICH claim to fix
        and never WHAT was wrong — 30 recorded turns, all reads, zero patches,
        aborting `insufficient_information` on 32 of 45 blocked tasks. The
        claim's own text must travel, and must still pass the value-free
        gatekeeper that guards every repair view."""
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import CouncilRole, Finding, RepairRoute, Severity
        from elt_taskgen.review import repair_proposer as rp

        task = demo_fixture.demo_task()
        finding = Finding(
            finding_id="ambiguity_critic-00", role=CouncilRole.AMBIGUITY_CRITIC,
            severity=Severity.MAJOR,
            summary="customer_summary never states the null-handling rule for total_spend",
            detail="Rule 5 aggregates total_spend but no rule says what it reports "
                   "for a customer with no completed orders.",
        )
        blocking, problems = cli._blocking_proposal_failures([finding], ())
        self.assertEqual([f.finding_id for f in blocking], ["ambiguity_critic-00"])
        joined = "; ".join(problems)
        self.assertIn("it is unresolved", joined)
        self.assertIn("the claim to resolve is:", joined)
        # EVERY blocking branch carries it, not just the no-proposal one: the
        # 2026-09-10 run blocked on "major proposal has no promotion outcome",
        # a sibling branch that named only the id.
        self.assertEqual(
            cli._claim_suffix(finding),
            " — the claim to resolve is: "
            "customer_summary never states the null-handling rule for total_spend "
            "Rule 5 aggregates total_spend but no rule says what it reports "
            "for a customer with no completed orders.",
        )
        blank = finding.model_copy(update={"summary": "", "detail": ""})
        self.assertEqual(cli._claim_suffix(blank), "")
        self.assertIn("never states the null-handling rule", joined)
        self.assertIn("no rule says what it reports", joined)
        # The enriched evidence still passes the D1 gatekeeper on the route
        # that consumes it — the critic only ever saw the public author view.
        view = rp.view_for_route(task, RepairRoute.SPECIFICATION, joined)
        self.assertIn("the claim to resolve is:", view)

    def test_a_claim_with_a_refused_value_shape_is_redacted_not_dropped(self):
        """batch10 run B 2026-09-11: three of three attack failures reached
        the proposer as "major finding has no structured proposed_case; it is
        unresolved" and nothing else, because the critic's own hypotheticals
        (`child_count=3`, `tied_count=3`, `row_count=0`) match the
        count-vector shape the D1 gatekeeper refuses, and the screen dropped
        the WHOLE claim. The proposer aborted after two turns. The number is
        withheld and the sentence travels."""
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import CouncilRole, Finding, Severity
        from elt_taskgen.review import repair_proposer as rp

        task = demo_fixture.demo_task()
        finding = Finding(
            finding_id="ambiguity_critic-00",
            role=CouncilRole.AMBIGUITY_CRITIC,
            severity=Severity.MAJOR,
            summary="customer_summary leaves order_count undefined for a childless customer",
            detail=(
                "For a customer with three orders of which one is cancelled, Reading A "
                "emits order_count=3 and Reading B emits order_count=2; a key such as "
                "('c_9001', 3) is graded differently under each."
            ),
        )
        suffix = cli._claim_suffix(finding)
        self.assertIn("the claim to resolve is:", suffix)
        self.assertIn("Reading A emits order_count=[withheld]", suffix)
        self.assertIn("Reading B emits order_count=[withheld]", suffix)
        self.assertIn("a key such as [withheld] is graded", suffix)
        self.assertNotIn("order_count=3", suffix)
        self.assertNotIn("c_9001", suffix)
        # Still admissible on the route that consumes it.
        view = rp.view_for_route(task, RepairRoute.SPECIFICATION, "blocked" + suffix)
        self.assertIn("order_count=[withheld]", view)

    def test_one_shot_seat_malformed_proposal_is_voided_never_a_stage_abort(self):
        """Rerun 2026-09-10: 20 of 50 tasks blocked at review because the
        ONE-SHOT ambiguity critic emitted a proposed_case whose variant or
        target its own prose did not name, and the handoff raised a protocol
        failure. That seat runs no compile validator, so nothing ever told it
        — the proposal is voided (words kept, claimed severity in the screen
        record) and the finding travels as an unresolved claim the repair
        route answers. A seat that DOES have the channel still raises."""
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import (
            AttackKind, CouncilRole, Finding, FindingScreenStatus, PopulationName,
            ProposedAttackCase, Severity, TaskVariant,
        )
        from elt_taskgen.review.council import ProviderProtocolError

        task = demo_fixture.demo_task()
        matrix = {p: True for p in PopulationName}
        malformed = ProposedAttackCase(
            kind=AttackKind.WRONG_AGG_STAGE, params={"variant": "made_up"},
            expected_pass=matrix,
            expected_pass_by_stage={TaskVariant.EXTRACT_LOAD: matrix, TaskVariant.TRANSFORM: matrix},
            rationale="the other reading keeps full reward on customer_summary",
        )

        def finding(fid, role):
            return Finding(
                finding_id=fid, role=role, severity=Severity.MAJOR,
                summary="customer_summary admits two readings of total_spend",
                detail="the prose and the schema disagree on customer_summary",
                suggested_attack=AttackKind.WRONG_AGG_STAGE, proposed_case=malformed,
            )

        # The two one-shot seats: voided, never raised.
        for role in (CouncilRole.AMBIGUITY_CRITIC, CouncilRole.FEASIBILITY_REVIEWER):
            with self.subTest(voided=role.value):
                self.assertFalse(cli._seat_has_compile_channel(role))
                out = cli._validated_executable_findings(task, [finding(f"{role.value}-00", role)])
                self.assertEqual(len(out), 1)
                screen = out[0].screen
                self.assertIsNotNone(screen)
                self.assertIs(screen.status, FindingScreenStatus.VOID)
                self.assertIn(cli.UNCOMPILABLE_NO_CHANNEL_SIGNAL, tuple(screen.signals))
                # The words are kept and the claim is recoverable.
                self.assertEqual(out[0].summary, finding("x", role).summary)
                self.assertIs(screen.claimed_severity, Severity.MAJOR)
                self.assertEqual(screen.withheld_proposal, malformed)
                self.assertIs(out[0].severity, Severity.INFO)
                self.assertIsNone(out[0].proposed_case)
        # A seat WITH the channel was told and did not fix it: still protocol.
        for role in (CouncilRole.POPULATION_ADVERSARY, CouncilRole.SHORTCUT_ATTACKER):
            with self.subTest(raises=role.value):
                self.assertTrue(cli._seat_has_compile_channel(role))
                with self.assertRaises(ProviderProtocolError):
                    cli._validated_executable_findings(task, [finding(f"{role.value}-00", role)])

    def test_a_major_finding_withdrawn_by_its_disposition_is_voided_not_blocking(self):
        """batch10 2026-09-11: lefty02w and lavestima blocked at attack on a
        MAJOR finding whose detail ended "this observation is therefore not a
        graded fork and I withdraw it" / "this passage does not change graded
        output"; the proposer read the retraction and abstained.

        R02: that sentence withdraws nothing, so the recorded finding stays an
        active claim and blocks. The critic withdraws it with disposition
        'withdrawn', which voids it on record without blocking."""
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import (
            CouncilRole, Finding, FindingDisposition, FindingScreenStatus, Severity,
        )

        task = demo_fixture.demo_task()

        def finding(detail, fid="ambiguity_critic-00", disposition=None):
            return Finding(
                finding_id=fid, role=CouncilRole.AMBIGUITY_CRITIC, severity=Severity.MAJOR,
                summary="customer_summary never states what a NULL customer_id row does",
                detail=detail, disposition=disposition,
            )

        detail = (
            "Rule 4 brings in orders whose customer_id matches. Reading A: a NULL "
            "customer_id never matches and is dropped. Reading B is the same under "
            "an equality join, so the graded values coincide — this observation is "
            "therefore not a graded fork and I withdraw it."
        )
        out = cli._validated_executable_findings(task, [finding(detail)])
        self.assertIsNone(out[0].screen)
        blocking, _ = cli._blocking_proposal_failures(out, ())
        self.assertEqual([f.finding_id for f in blocking], ["ambiguity_critic-00"])

        withdrawn = finding(detail, disposition=FindingDisposition.WITHDRAWN)
        out = cli._validated_executable_findings(task, [withdrawn])
        self.assertEqual(len(out), 1)
        self.assertIs(out[0].screen.status, FindingScreenStatus.VOID)
        self.assertIn(cli.WITHDRAWN_BY_DISPOSITION_SIGNAL, tuple(out[0].screen.signals))
        self.assertIs(out[0].screen.claimed_severity, Severity.MAJOR)
        self.assertIs(out[0].severity, Severity.INFO)
        blocking, problems = cli._blocking_proposal_failures(out, ())
        self.assertEqual(blocking, [])
        self.assertEqual(problems, [])

        # A retraction mid-argument that then presses on is not a withdrawal.
        pressed = finding(
            "One might say this is not a graded fork, but it is: Reading A emits "
            "0 and Reading B emits an empty value for the same customer, so the "
            "two readings differ by a graded cell."
        )
        out = cli._validated_executable_findings(task, [pressed])
        self.assertIsNone(out[0].screen)
        blocking, _ = cli._blocking_proposal_failures(out, ())
        self.assertEqual([f.finding_id for f in blocking], ["ambiguity_critic-00"])

    def test_a_no_dedup_proposal_on_a_task_without_a_dedupe_rule_is_voided(self):
        """batch10 run G 2026-09-11, dlt__personio: every table has a primary
        key and no mart plan declares a DEDUPE rule, yet the population
        adversary proposed `no_dedup`, measured it inert everywhere, and the
        handoff blocked the task. Omitting a step the contract never asks
        for is not a wrong implementation; the finding is voided on record."""
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import (
            AttackKind, CouncilRole, Finding, FindingScreenStatus, MartOpKind,
            PopulationName, ProposedAttackCase, Severity, TaskVariant,
        )

        task = demo_fixture.demo_task()
        has_dedupe = any(op.kind is MartOpKind.DEDUPE for m in task.marts for op in m.plan.ops)
        if has_dedupe:
            stripped = [
                m.model_copy(update={"plan": m.plan.model_copy(update={
                    "ops": tuple(op for op in m.plan.ops if op.kind is not MartOpKind.DEDUPE)})})
                for m in task.marts
            ]
            task = task.model_copy(update={"marts": tuple(stripped)})
        matrix = {p: True for p in PopulationName}
        proposal = ProposedAttackCase(
            kind=AttackKind.NO_DEDUP, params={},
            expected_pass=matrix,
            expected_pass_by_stage={TaskVariant.EXTRACT_LOAD: matrix, TaskVariant.TRANSFORM: matrix},
            rationale="a missing dedup step is undetectable everywhere",
        )
        finding = Finding(
            finding_id="population_adversary-00", role=CouncilRole.POPULATION_ADVERSARY,
            severity=Severity.MAJOR,
            summary="No graded population contains duplicate rows, so a missing dedup is undetectable",
            detail="Every table declares a primary key; the stress population injects no duplicates.",
            suggested_attack=AttackKind.NO_DEDUP, proposed_case=proposal,
        )
        out = cli._validated_executable_findings(task, [finding])
        self.assertEqual(len(out), 1)
        self.assertIs(out[0].screen.status, FindingScreenStatus.VOID)
        self.assertIn(cli.MUTATION_TARGETS_NO_DECLARED_RULE_SIGNAL, tuple(out[0].screen.signals))
        self.assertIs(out[0].screen.claimed_severity, Severity.MAJOR)
        blocking, problems = cli._blocking_proposal_failures(out, ())
        self.assertEqual((blocking, problems), ([], []))

    def test_a_second_hop_join_proposal_on_a_one_hop_task_is_voided(self):
        """dlt__personio, batch10 run L 2026-09-11: `inner_join@second_hop`
        on a one-hop argmax mart flipped only the extremal-row attach (a
        CTE over the group's own rows), kept FULL reward on all five
        populations and was reported as a live exploit. The compiler no
        longer flips the attach, so on a task whose marts join one source
        table the variant has no target: the finding is voided on record."""
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import (
            AttackKind, CouncilRole, Finding, FindingScreenStatus, MartOpKind,
            PopulationName, ProposedAttackCase, Severity, TaskVariant,
        )

        task = demo_fixture.demo_task()
        sources = {t.name for t in task.tables}

        def one_hop(mart):
            seen = 0
            kept = []
            for op in mart.plan.ops:
                if op.kind is MartOpKind.JOIN and any(n in sources for n in tuple(op.tables)[1:]):
                    seen += 1
                    if seen >= 2:
                        continue
                kept.append(op)
            return mart.model_copy(update={"plan": mart.plan.model_copy(update={"ops": tuple(kept)})})

        one = task.model_copy(update={"marts": tuple(one_hop(m) for m in task.marts)})
        full = {p: True for p in PopulationName}
        proposal = ProposedAttackCase(
            kind=AttackKind.INNER_JOIN, params={"variant": "second_hop"}, expected_pass=full,
            expected_pass_by_stage={TaskVariant.EXTRACT_LOAD: full, TaskVariant.TRANSFORM: full},
            rationale="no population has an orphan bridge row, so the second-hop join type is untestable",
        )
        finding = Finding(
            finding_id="population_adversary-01", role=CouncilRole.POPULATION_ADVERSARY,
            severity=Severity.MAJOR,
            summary="No graded population contains a bridge row with no parent, so an inner-side second_hop join is untestable",
            suggested_attack=AttackKind.INNER_JOIN, proposed_case=proposal,
        )
        self.assertTrue(cli._proposal_names_a_hop_the_task_never_joins(one, proposal))
        out = cli._validated_executable_findings(one, [finding])
        self.assertIs(out[0].screen.status, FindingScreenStatus.VOID)
        self.assertIn(cli.MUTATION_NAMES_NO_SECOND_HOP_SIGNAL, tuple(out[0].screen.signals))
        self.assertEqual(cli._blocking_proposal_failures(out, ()), ([], []))
        # A task that does join a second source table keeps the claim live,
        # and the default variant is never voided.
        two_hops = any(
            sum(1 for op in m.plan.ops if op.kind is MartOpKind.JOIN and any(n in sources for n in tuple(op.tables)[1:])) >= 2
            for m in task.marts
        )
        self.assertEqual(cli._proposal_names_a_hop_the_task_never_joins(task, proposal), not two_hops)
        plain = proposal.model_copy(update={"params": {}})
        self.assertFalse(cli._proposal_names_a_hop_the_task_never_joins(one, plain))

    def test_zero_is_missing_over_zero_defaults_is_a_proved_identity(self):
        """wikidbs__c60432, batch10 run M 2026-09-11: `no_null_default@
        zero_is_missing` rewrites COALESCE(x, 0) to COALESCE(NULLIF(x, 0), 0)
        — an identity — kept FULL reward everywhere and was reported as a
        live exploit; the proposer was sent to repair populations for it.
        Proved from the gold (every default it touches is the number 0) and
        accepted by the non-exploit reader; a non-zero default fails closed."""
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import (
            AttackKind, CouncilRole, Finding, PopulationName, ProposedAttackCase,
            Severity, TaskVariant,
        )
        from elt_taskgen.verification import attacks
        from elt_taskgen.verification.attacks import PromotionOutcome

        base = demo_fixture.demo_task()
        mart = base.marts[0]
        gold = 'SELECT c."customer_id", COALESCE(SUM(o."amount"), 0) AS "total", COALESCE(MAX(o."amount"), 0.0) AS "peak" FROM "customers" AS c LEFT JOIN "orders" AS o ON o."customer_id" = c."customer_id" GROUP BY c."customer_id"'
        task = base.model_copy(update={"reference": base.reference.model_copy(update={"sql_by_mart": {**dict(base.reference.sql_by_mart), mart.name: gold}})})
        full = {p: True for p in PopulationName}
        proposal = ProposedAttackCase(
            kind=AttackKind.NO_NULL_DEFAULT, params={"zero_is_missing": True}, expected_pass=full,
            expected_pass_by_stage={TaskVariant.EXTRACT_LOAD: full, TaskVariant.TRANSFORM: full},
            rationale="no population has a missing amount, so the zero default is untestable",
        )
        finding = Finding(
            finding_id="population_adversary-00", role=CouncilRole.POPULATION_ADVERSARY, severity=Severity.MAJOR,
            summary="No graded population contains a missing amount, so the null default is dead logic",
            suggested_attack=AttackKind.NO_NULL_DEFAULT, proposed_case=proposal,
        )
        ok = {p.value: True for p in PopulationName}
        outcome = PromotionOutcome(
            finding_id=finding.finding_id, case_name="proposed__population_adversary-00", kind="no_null_default", promoted=False,
            reason="proposal was confirmed to keep FULL combined reward on all five populations",
            predicted=dict(ok), measured={p.value: 1.0 for p in PopulationName}, measured_pass=dict(ok),
            predicted_by_stage={"extract_load": dict(ok), "transform": dict(ok)},
            measured_pass_by_stage={"extract_load": dict(ok), "transform": dict(ok)},
            fidelity={"passed": True, "compiler": {"passed": True, "realized": {"marts": [mart.name], "operation": "zero_is_missing", "zero_nullif_count": 2}, "requested": {"operation": "zero_is_missing"}}},
        )
        proof = cli._zero_default_equivalence_proof(task, finding, outcome, attacks)
        self.assertIsNotNone(proof)
        self.assertEqual((proof["proof"], proof["operation"], proof["marts"], proof["sites"]), (cli._SCHEMA_EQUIVALENT_ZERO_DEFAULT_PROOF, "zero_is_missing", [mart.name], 2))
        accepted = outcome.model_copy(update={"fidelity": {**outcome.fidelity, "schema_equivalence": proof}})
        self.assertEqual(cli._blocking_proposal_failures([finding], [accepted]), ([], []))
        # Without the proof the same outcome is still the live-exploit block.
        blocking, problems = cli._blocking_proposal_failures([finding], [outcome])
        self.assertEqual([f.finding_id for f in blocking], [finding.finding_id])
        # A default other than 0 is a real mutation: no proof.
        other = task.model_copy(update={"reference": task.reference.model_copy(update={"sql_by_mart": {**dict(task.reference.sql_by_mart), mart.name: gold.replace("0.0) AS", "-1) AS")}})})
        self.assertIsNone(cli._zero_default_equivalence_proof(other, finding, outcome, attacks))
        # The read side refuses a proof whose shape or matrices are off.
        self.assertFalse(cli._zero_default_identity_accepted(proposal, accepted.model_copy(update={"measured_pass": {**ok, "stress": False}})))
        self.assertFalse(cli._zero_default_identity_accepted(proposal.model_copy(update={"params": {}}), accepted))

    def test_zero_is_missing_in_variant_form_is_the_same_proved_identity(self):
        """schemapile defterp, batch20 run api-20 2026-09-14: the shortcut
        attacker filed the same rewrite as `params: {"variant":
        "zero_is_missing"}`, which the compiler records as a `kind` directive
        (`operation: kind, variant: zero_is_missing, target_marts`). The proof
        knew only the operation-key spelling, so an identity over 14 zero
        defaults (COALESCE(x, 0), COALESCE(ROUND(a / NULLIF(b, 0), 3), 0.0)
        with the gold's own divisor guard inside) was reported as a live
        exploit, the failure routed POPULATION, and the ambiguity critic's
        unresolved major claim never reached the SPECIFICATION proposer."""
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import (
            AttackKind, CouncilRole, Finding, PopulationName, ProposedAttackCase,
            RepairRoute, Severity, TaskVariant,
        )
        from elt_taskgen.verification import attacks
        from elt_taskgen.verification.attacks import PromotionOutcome

        base = demo_fixture.demo_task()
        mart = base.marts[0]
        gold = (
            'SELECT c."customer_id", COALESCE(SUM(o."amount"), 0) AS "total", '
            'COALESCE(ROUND(CAST(MAX(o."amount") AS DOUBLE) / NULLIF(SUM(o."amount"), 0), 3), 0.0) AS "peak_share" '
            'FROM "customers" AS c LEFT JOIN "orders" AS o ON o."customer_id" = c."customer_id" GROUP BY c."customer_id"'
        )
        task = base.model_copy(update={"reference": base.reference.model_copy(update={"sql_by_mart": {**dict(base.reference.sql_by_mart), mart.name: gold}})})
        none = {p: False for p in PopulationName}
        proposal = ProposedAttackCase(
            kind=AttackKind.NO_NULL_DEFAULT, params={"variant": "zero_is_missing"}, expected_pass=none,
            expected_pass_by_stage={TaskVariant.EXTRACT_LOAD: {p: True for p in PopulationName}, TaskVariant.TRANSFORM: none},
            rationale="treating 0 as missing changes the guarded share wherever an amount of 0 occurs",
        )
        finding = Finding(
            finding_id="shortcut_attacker-05", role=CouncilRole.SHORTCUT_ATTACKER, severity=Severity.MINOR,
            summary="Probe no_null_default with variant zero_is_missing on the amount defaults",
            suggested_attack=AttackKind.NO_NULL_DEFAULT, proposed_case=proposal,
        )
        ok = {p.value: True for p in PopulationName}
        realized = {"operation": "kind", "kind": "no_null_default", "variant": "zero_is_missing", "target_marts": [mart.name]}
        outcome = PromotionOutcome(
            finding_id=finding.finding_id, case_name="proposed__shortcut_attacker-05", kind="no_null_default", promoted=False,
            reason="proposal was confirmed to keep FULL combined reward on all five populations",
            predicted={p.value: False for p in PopulationName}, measured={p.value: 1.0 for p in PopulationName}, measured_pass=dict(ok),
            predicted_by_stage={"extract_load": dict(ok), "transform": {p.value: False for p in PopulationName}},
            measured_pass_by_stage={"extract_load": dict(ok), "transform": dict(ok)},
            fidelity={"passed": True, "compiler": {"passed": True, "realized": dict(realized), "requested": dict(realized)}},
        )
        self.assertTrue(cli._names_zero_is_missing(proposal))
        self.assertFalse(cli._names_zero_is_missing(proposal.model_copy(update={"params": {"variant": "zero_is_missing", "zero_is_missing": True}})))
        self.assertEqual(cli._zero_is_missing_realized_targets(realized), [mart.name])
        self.assertIsNone(cli._zero_is_missing_realized_targets({**realized, "variant": ""}))
        self.assertIsNone(cli._zero_is_missing_realized_targets({**realized, "kind": "wrong_window"}))
        self.assertIsNone(cli._zero_is_missing_realized_targets({**realized, "target_marts": []}))
        proof = cli._zero_default_equivalence_proof(task, finding, outcome, attacks)
        self.assertIsNotNone(proof)
        # Two rewritten sites: the SUM default and the ROUND(...) default. The
        # gold's own NULLIF(SUM, 0) divisor guard survives and is not a site.
        self.assertEqual(
            (proof["proof"], proof["operation"], proof["form"], proof["marts"], proof["sites"]),
            (cli._SCHEMA_EQUIVALENT_ZERO_DEFAULT_PROOF, "zero_is_missing", "variant", [mart.name], 2),
        )
        accepted = outcome.model_copy(update={"fidelity": {**outcome.fidelity, "schema_equivalence": proof}})
        self.assertTrue(cli._zero_default_identity_accepted(proposal, accepted))
        ambiguity = Finding(
            finding_id="ambiguity_critic-00", role=CouncilRole.AMBIGUITY_CRITIC, severity=Severity.MAJOR,
            summary="Rule 6 leaves the snapshot winner unstated when every row of a group lacks a date",
        )
        # With the proof the identity is voided and the unresolved ambiguity
        # claim routes SPECIFICATION; without it the identity is selected as a
        # measured live exploit and the same failure routes POPULATION.
        blocking, problems = cli._blocking_proposal_failures([ambiguity, finding], [accepted])
        self.assertEqual([f.finding_id for f in blocking], [ambiguity.finding_id])
        self.assertEqual(len(problems), 1)
        self.assertIs(cli._proposal_failure_route(blocking, [accepted]), RepairRoute.SPECIFICATION)
        blocking, _ = cli._blocking_proposal_failures([ambiguity, finding], [outcome])
        self.assertEqual([f.finding_id for f in blocking], [ambiguity.finding_id, finding.finding_id])
        self.assertIs(cli._proposal_failure_route(blocking, [outcome]), RepairRoute.POPULATION)
        # A non-zero default under the variant spelling is still a real mutation.
        other = task.model_copy(update={"reference": task.reference.model_copy(update={"sql_by_mart": {**dict(task.reference.sql_by_mart), mart.name: gold.replace("0.0) AS", "-1) AS")}})})
        self.assertIsNone(cli._zero_default_equivalence_proof(other, finding, outcome, attacks))

    def test_zero_is_missing_under_kind_custom_is_the_same_proved_identity(self):
        """The compiler admits `{"zero_is_missing": true}` under kind `custom`
        as well as `no_null_default` and materializes both through the same
        rewrite with the same operation record; the proof accepts the custom
        spelling and still refuses `variant` under `custom` (not registered)."""
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import (
            AttackKind, CouncilRole, Finding, PopulationName, ProposedAttackCase,
            Severity, TaskVariant,
        )
        from elt_taskgen.verification import attacks
        from elt_taskgen.verification.attacks import PromotionOutcome

        base = demo_fixture.demo_task()
        mart = base.marts[0]
        gold = 'SELECT c."customer_id", COALESCE(SUM(o."amount"), 0) AS "total" FROM "customers" AS c LEFT JOIN "orders" AS o ON o."customer_id" = c."customer_id" GROUP BY c."customer_id"'
        task = base.model_copy(update={"reference": base.reference.model_copy(update={"sql_by_mart": {**dict(base.reference.sql_by_mart), mart.name: gold}})})
        full = {p: True for p in PopulationName}
        proposal = ProposedAttackCase(
            kind=AttackKind.CUSTOM, params={"zero_is_missing": True}, expected_pass=full,
            expected_pass_by_stage={TaskVariant.EXTRACT_LOAD: full, TaskVariant.TRANSFORM: full},
            rationale="no population has a missing amount",
        )
        self.assertTrue(cli._names_zero_is_missing(proposal))
        self.assertFalse(cli._names_zero_is_missing(proposal.model_copy(update={"params": {"variant": "zero_is_missing"}})))
        self.assertFalse(cli._names_zero_is_missing(proposal.model_copy(update={"kind": AttackKind.WRONG_WINDOW})))
        finding = Finding(
            finding_id="population_adversary-01", role=CouncilRole.POPULATION_ADVERSARY, severity=Severity.MAJOR,
            summary="No graded population contains a missing amount", suggested_attack=AttackKind.CUSTOM, proposed_case=proposal,
        )
        ok = {p.value: True for p in PopulationName}
        outcome = PromotionOutcome(
            finding_id=finding.finding_id, case_name="proposed__population_adversary-01", kind="custom", promoted=False,
            reason="proposal was confirmed to keep FULL combined reward on all five populations",
            predicted=dict(ok), measured={p.value: 1.0 for p in PopulationName}, measured_pass=dict(ok),
            predicted_by_stage={"extract_load": dict(ok), "transform": dict(ok)},
            measured_pass_by_stage={"extract_load": dict(ok), "transform": dict(ok)},
            fidelity={"passed": True, "compiler": {"passed": True, "realized": {"marts": [mart.name], "operation": "zero_is_missing", "zero_nullif_count": 1}, "requested": {"operation": "zero_is_missing"}}},
        )
        proof = cli._zero_default_equivalence_proof(task, finding, outcome, attacks)
        self.assertIsNotNone(proof)
        self.assertEqual((proof["form"], proof["sites"]), ("operation_key", 1))
        accepted = outcome.model_copy(update={"fidelity": {**outcome.fidelity, "schema_equivalence": proof}})
        self.assertEqual(cli._blocking_proposal_failures([finding], [accepted]), ([], []))

    def test_drop_frame_over_a_self_ordered_extreme_is_a_proved_identity(self):
        """wikidbs__c20112, batch10 run Q 2026-09-11: `wrong_window@drop_frame`
        unframes MAX(f_measure) OVER (PARTITION BY k ORDER BY f_measure DESC
        ROWS ...). The first row of every partition already carries the
        maximum, so the default frame gives the same value on every row: an
        identity, measured FULL everywhere, reported as a live exploit. A
        window over another column, or a SUM, fails closed."""
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import (
            AttackKind, CouncilRole, Finding, PopulationName, ProposedAttackCase,
            Severity, TaskVariant,
        )
        from elt_taskgen.verification import attacks
        from elt_taskgen.verification.attacks import PromotionOutcome

        base = demo_fixture.demo_task()
        mart = base.marts[0]
        framed = ('SELECT c."customer_id", MAX(o."amount") OVER (PARTITION BY c."customer_id" ORDER BY o."amount" DESC '
                  'ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS "peak" FROM "customers" AS c LEFT JOIN "orders" AS o ON o."customer_id" = c."customer_id"')
        def with_gold(sql):
            return base.model_copy(update={"reference": base.reference.model_copy(update={"sql_by_mart": {**dict(base.reference.sql_by_mart), mart.name: sql}})})
        full = {p: True for p in PopulationName}
        proposal = ProposedAttackCase(
            kind=AttackKind.WRONG_WINDOW, params={"variant": "drop_frame"}, expected_pass={**full, PopulationName.STRESS: False},
            expected_pass_by_stage={TaskVariant.EXTRACT_LOAD: full, TaskVariant.TRANSFORM: {**full, PopulationName.STRESS: False}},
            rationale="the frame is never exercised",
        )
        finding = Finding(
            finding_id="population_adversary-00", role=CouncilRole.POPULATION_ADVERSARY, severity=Severity.MAJOR,
            summary="No population exercises the window frame of the top mart", suggested_attack=AttackKind.WRONG_WINDOW, proposed_case=proposal,
        )
        ok = {p.value: True for p in PopulationName}
        outcome = PromotionOutcome(
            finding_id=finding.finding_id, case_name="proposed__population_adversary-00", kind="wrong_window", promoted=False,
            reason="measured reward matrix does not match the proposed expectation on 2 population(s)",
            predicted={**ok, "stress": False}, measured={p.value: 1.0 for p in PopulationName}, measured_pass=dict(ok),
            predicted_by_stage={"extract_load": dict(ok), "transform": {**ok, "stress": False}},
            measured_pass_by_stage={"extract_load": dict(ok), "transform": dict(ok)},
            fidelity={"passed": True, "compiler": {"passed": True, "realized": {"kind": "wrong_window", "operation": "kind", "variant": "drop_frame", "target_marts": [mart.name]}}},
            mismatches=("stress: predicted FAIL, measured reward 1.0",),
        )
        proof = cli._drop_frame_equivalence_proof(with_gold(framed), finding, outcome, attacks)
        self.assertIsNotNone(proof)
        self.assertEqual((proof["proof"], proof["sites"]), (cli._SCHEMA_EQUIVALENT_DROP_FRAME_PROOF, 1))
        accepted = outcome.model_copy(update={"fidelity": {**outcome.fidelity, "schema_equivalence": proof}})
        self.assertEqual(cli._blocking_proposal_failures([finding], [accepted]), ([], []))
        blocking, _ = cli._blocking_proposal_failures([finding], [outcome])
        self.assertEqual([f.finding_id for f in blocking], [finding.finding_id])
        # Not an identity: a running SUM, or an extreme ordered by another column.
        self.assertIsNone(cli._drop_frame_equivalence_proof(with_gold(framed.replace("MAX(", "SUM(")), finding, outcome, attacks))
        self.assertIsNone(cli._drop_frame_equivalence_proof(with_gold(framed.replace('ORDER BY o."amount" DESC', 'ORDER BY o."order_id" DESC')), finding, outcome, attacks))
        self.assertIsNone(cli._drop_frame_equivalence_proof(with_gold(framed.replace(" DESC ", " ASC ")), finding, outcome, attacks))

    def test_a_population_blind_spot_killed_everywhere_is_refuted_not_unresolved(self):
        """wikidbs__c20112, batch10 run K 2026-09-11: the population
        adversary predicted FULL reward on all five populations for a
        wrong_grain mutant; the realized mutant (fidelity passed) scored 0.0
        on all five under the TRANSFORM reward. The handoff called that
        "major proposal unresolved" and sent the proposer down the
        POPULATION route, where it abstained twice: there is no population
        repair for a mutation every population already detects. The claim is
        refuted by the trusted measurement; a mutant that survives on any
        population, or one without fidelity, stays unresolved."""
        from elt_taskgen.models import (
            AttackKind, CouncilRole, Finding, PopulationName, ProposedAttackCase,
            Severity, TaskVariant,
        )
        from elt_taskgen.verification.attacks import PromotionOutcome

        full = {p: True for p in PopulationName}
        proposal = ProposedAttackCase(
            kind=AttackKind("wrong_grain"), params={}, expected_pass=full,
            expected_pass_by_stage={TaskVariant.EXTRACT_LOAD: full, TaskVariant.TRANSFORM: full},
            rationale="a wrong parent grain produces byte-identical marts everywhere",
        )
        finding = Finding(
            finding_id="population_adversary-00-469b33f8", role=CouncilRole.POPULATION_ADVERSARY,
            severity=Severity.MAJOR,
            summary="No graded population's conditions guarantee two parent rows sharing a key, so a wrong parent grain is undetectable",
            proposed_case=proposal,
        )
        none = {p.value: False for p in PopulationName}
        killed = PromotionOutcome(
            finding_id=finding.finding_id, case_name="proposed__population_adversary-00-469b33f8",
            kind="wrong_grain", promoted=False,
            reason="measured reward matrix does not match the proposed expectation on 10 population(s)",
            predicted={p.value: True for p in PopulationName},
            measured={p.value: 0.0 for p in PopulationName}, measured_pass=dict(none),
            predicted_by_stage={"extract_load": {p.value: True for p in PopulationName}, "transform": {p.value: True for p in PopulationName}},
            measured_pass_by_stage={"extract_load": {p.value: True for p in PopulationName}, "transform": dict(none)},
            fidelity={"passed": True},
            mismatches=tuple(f"{p.value}: predicted FULL, measured reward 0.0" for p in PopulationName),
        )
        self.assertTrue(cli._population_blind_spot_refuted(finding, killed))
        self.assertEqual(cli._blocking_proposal_failures([finding], [killed]), ([], []))

        # Killed on ONE hidden population is a kill (the task reward is the
        # minimum over populations): dlt__personio, run R, predicted FULL
        # everywhere for a null-row drop that primary, resampled and
        # counterfactual killed while development and stress kept 1.0.
        partial = killed.model_copy(update={
            "measured_pass": {**none, "development": True, "stress": True},
            "measured_pass_by_stage": {**killed.measured_pass_by_stage, "transform": {**none, "development": True, "stress": True}},
        })
        self.assertTrue(cli._population_blind_spot_refuted(finding, partial))
        # Killed only on the public development rows is not a hidden kill.
        survives = killed.model_copy(update={
            "measured_pass": {**{p.value: True for p in PopulationName}, "development": False},
            "measured_pass_by_stage": {**killed.measured_pass_by_stage, "transform": {**{p.value: True for p in PopulationName}, "development": False}},
        })
        self.assertFalse(cli._population_blind_spot_refuted(finding, survives))
        blocking, problems = cli._blocking_proposal_failures([finding], [survives])
        self.assertEqual([f.finding_id for f in blocking], [finding.finding_id])
        self.assertIn("major proposal unresolved", problems[0])

        # Only the TRANSFORM reward decides a transform blind spot: a combined
        # 0.0 that comes from the EXTRACT_LOAD side is not a detection.
        el_only = killed.model_copy(update={
            "measured_pass_by_stage": {"extract_load": dict(none), "transform": {p.value: True for p in PopulationName}},
        })
        self.assertFalse(cli._population_blind_spot_refuted(finding, el_only))
        unfaithful = killed.model_copy(update={"fidelity": {"passed": False}})
        self.assertFalse(cli._population_blind_spot_refuted(finding, unfaithful))
        shortcut = finding.model_copy(update={"role": CouncilRole.SHORTCUT_ATTACKER})
        self.assertFalse(cli._population_blind_spot_refuted(shortcut, killed))

    def test_attack_finding_descriptor_extracts_only_public_identifiers(self):
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import CouncilRole, Finding, Severity, canonical_json

        finding = Finding(
            finding_id="not-public-and-not-projected",
            role=CouncilRole.AMBIGUITY_CRITIC,
            severity=Severity.MAJOR,
            summary=(
                "customers customer_id and inner_join are relevant; primary, "
                "c_9001, 4711, and /Users/private are not public subjects"
            ),
            detail="development also exposes customer_summary and customers",
        )
        descriptor = cli._attack_finding_descriptor(
            demo_fixture.demo_task(), finding
        )
        self.assertEqual(descriptor.role, CouncilRole.AMBIGUITY_CRITIC)
        self.assertEqual(descriptor.severity, Severity.MAJOR)
        self.assertEqual(
            descriptor.identifiers,
            (
                "customer_id",
                "customer_summary",
                "customers",
                "development",
                "inner_join",
            ),
        )
        dumped = canonical_json(descriptor.model_dump(mode="json"))
        for private in ("primary", "c_9001", "4711", "/Users", "finding_id"):
            self.assertNotIn(private, dumped)

    def test_attack_payload_old_rows_default_to_no_blocking_descriptor(self):
        payload = cli.AttackPayload.model_validate(
            {"rewards": {}, "rewards_by_variant": {}, "cases": []}
        )
        self.assertIsNone(payload.blocking_finding)


class TestReviewTranscriptManifest(unittest.TestCase):
    class ManifestProvider:
        def begin_task_evidence(self, task_id, task_content_hash):
            self.task_id = task_id
            self.task_content_hash = task_content_hash
            self.exchange_evidence = []

        def complete(self, role, prompt):
            role_name = getattr(role, "value", str(role))
            if role_name == "shortcut_attacker":
                response = json.dumps(
                    {
                        "findings": [
                            {
                                "severity": "minor",
                                "summary": "constant output probe",
                                "detail": "emit constant output values",
                                "route_hint": "population",
                                "suggested_attack": "constants", "disposition": "active",
                                "proposed_case": {
                                    "kind": "constants",
                                    "params": "{}",
                                    "expected_pass_by_stage": {
                                        "extract_load": {
                                            name: True
                                            for name in (
                                                "development", "primary", "resampled",
                                                "counterfactual", "stress",
                                            )
                                        },
                                        "transform": {
                                            name: False
                                            for name in (
                                                "development", "primary", "resampled",
                                                "counterfactual", "stress",
                                            )
                                        },
                                    },
                                    "rationale": (
                                        "constant output must lose transform reward"
                                    ),
                                },
                            }
                        ]
                    }
                )
                count = 1
            else:
                response = '{"findings": []}'
                count = 0
            behavior = providers_mod.role_behavior_manifest(role_name)
            self.exchange_evidence.append(
                {
                    "task_id": self.task_id,
                    "task_content_hash": self.task_content_hash,
                    "role": role_name,
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
                    "attempt_count": 1,
                    "correction_count": 0,
                    "entry_schema": providers_mod.TRANSCRIPT_ENTRY_SCHEMA,
                    "finding_count": count,
                    "zero_findings": count == 0,
                    "provider": "test",
                    "model": "test",
                    "behavior_sha256": providers_mod.role_behavior_sha256(
                        role_name
                    ),
                    "tools_sha256": hashlib.sha256(
                        _canonical_json(behavior["tools"]).encode("utf-8")
                    ).hexdigest(),
                    "policy_sha256": behavior["policy_sha256"],
                    "diagnostics_version": providers_mod.DIAGNOSTICS_VERSION,
                    "replayed": True,
                }
            )
            return response

    def test_review_payload_and_private_file_bind_all_four_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(Path(tmp))
            try:
                task = demo_fixture.demo_task()
                engine.register(task)
                outcome = cli.make_review_runner(self.ManifestProvider())(
                    engine, task
                )
                self.assertEqual(outcome.verdict, VERDICT_PASS)
                self.assertEqual(len(outcome.payload.transcript_manifest), 4)
                path = (
                    engine.task_dir(task.task_id)
                    / "reports"
                    / "review_transcript_manifest.json"
                )
                record = json.loads(path.read_text())
                self.assertEqual(record["task_content_hash"], task.content_hash())
                self.assertEqual(len(record["roles"]), 4)
                self.assertEqual(
                    {entry["zero_findings"] for entry in record["roles"]},
                    {False, True},
                )
            finally:
                engine.close()




class TestRunGenerateRederives(unittest.TestCase):
    """`generate` certifies that what is on disk IS the IR's derivation.

    The old rule was "the files exist, therefore reuse them", which is not a
    check: an IR edit that moved the content hash left the rows stale and the
    stage still reported PASS (measured: 63 rows on disk where the IR derived
    99), and a hand-edited rows/*.jsonl survived every gate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.task = demo_fixture.demo_task()
        self.engine.register(self.task)
        self.pops = self.engine.task_dir(self.task.task_id) / "populations"

    def _artifact_rows(self) -> int:
        return int(
            self.engine._con.execute(
                "SELECT COUNT(*) FROM artifacts WHERE task_id=?",
                (self.task.task_id,),
            ).fetchone()[0]
        )

    def test_byte_identical_populations_are_reused_without_writes(self):
        first = cli.run_generate(self.engine, self.task)
        self.assertEqual(first.verdict, VERDICT_PASS)
        self.assertEqual(first.payload.data["reused"], "")
        before_fingerprint = repair.repair_fingerprint(self.workspace, self.task)
        before_artifacts = self._artifact_rows()

        again = cli.run_generate(self.engine, self.task)
        self.assertEqual(again.verdict, VERDICT_PASS)
        self.assertEqual(again.payload.data["built"], "")
        self.assertEqual(
            sorted(again.payload.data["reused"].split(",")),
            sorted(p.name.value for p in self.task.populations),
        )
        self.assertEqual(again.payload.data["verified"], "rederived")
        # Re-deriving identical bytes must not move the repair fingerprint (the
        # inert-repair rule reads it) or append artifact rows.
        self.assertEqual(
            repair.repair_fingerprint(self.workspace, self.task), before_fingerprint
        )
        self.assertEqual(self._artifact_rows(), before_artifacts)
        for suffix in (".rebuild-tmp", ".stale"):
            self.assertEqual(list(self.pops.glob(f"*{suffix}")), [])
        self.assertFalse((self.pops.parent / cli._REBUILD_SCRATCH_DIR).exists())

    def test_a_crashed_rebuild_cannot_move_the_repair_fingerprint(self):
        """The scratch trees live OUTSIDE the snapshot.

        A crash between `pop_dir.rename(stale)` and `rmtree(stale)` leaves the
        tree on disk until the next `generate`; under populations/ that tree
        was inside what `repair.snapshot` hashes, so a crash could perturb the
        fingerprint the inert-repair rule reads.
        """
        cli.run_generate(self.engine, self.task)
        before = repair.repair_fingerprint(self.workspace, self.task)
        crashed = (
            self.pops.parent
            / cli._REBUILD_SCRATCH_DIR
            / ("primary" + cli._REBUILD_STALE_SUFFIX)
            / "rows"
        )
        crashed.mkdir(parents=True)
        (crashed / "leftover.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
        self.assertEqual(
            repair.repair_fingerprint(self.workspace, self.task), before
        )
        # ... and the next run sweeps it away.
        cli.run_generate(self.engine, self.task)
        self.assertFalse((self.pops.parent / cli._REBUILD_SCRATCH_DIR).exists())

    def test_a_legacy_in_place_scratch_tree_is_swept_away(self):
        """Workspaces written before the scratch dir moved out of populations/."""
        cli.run_generate(self.engine, self.task)
        legacy = self.pops / ("primary" + cli._REBUILD_STALE_SUFFIX)
        legacy.mkdir()
        (legacy / "stale.txt").write_text("x", encoding="utf-8")
        cli.run_generate(self.engine, self.task)
        self.assertFalse(legacy.exists())

    def test_hand_edited_rows_are_reverted_to_the_derivation(self):
        cli.run_generate(self.engine, self.task)
        rows = sorted((self.pops / "primary" / "rows").glob("*.jsonl"))[0]
        derivation = rows.read_bytes()
        rows.write_bytes(derivation + b'{"smuggled": true}\n')

        outcome = cli.run_generate(self.engine, self.task)
        self.assertIn("primary", outcome.payload.data["built"])
        self.assertIn("differs", outcome.payload.data["drift:primary"])
        self.assertEqual(rows.read_bytes(), derivation)

    def test_stale_rendered_files_are_removed_by_the_rebuild(self):
        cli.run_generate(self.engine, self.task)
        stray = self.pops / "primary" / "rendered" / "files" / "leftover_page.csv"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("a,b\n1,2\n", encoding="utf-8")

        outcome = cli.run_generate(self.engine, self.task)
        self.assertIn("primary", outcome.payload.data["built"])
        self.assertIn("not derived from the IR", outcome.payload.data["drift:primary"])
        self.assertFalse(stray.exists())

    def test_stale_rows_are_replaced_when_the_population_spec_moves(self):
        from elt_taskgen.generation import source_data
        from elt_taskgen.models import PopulationName

        cli.run_generate(self.engine, self.task)
        # primary and resampled must share a scale (validate_population_
        # coverage), so both move together — this is the shape a POPULATION
        # repair patch produces.
        moved = self.task.model_copy(
            update={
                "populations": tuple(
                    pop.model_copy(
                        update={
                            "scale": {
                                table: count + 7 for table, count in pop.scale.items()
                            }
                        }
                    )
                    if pop.name
                    in (PopulationName.PRIMARY, PopulationName.RESAMPLED)
                    else pop
                    for pop in self.task.populations
                )
            }
        )
        self.assertNotEqual(moved.content_hash(), self.task.content_hash())
        self.engine.save_task(moved)

        outcome = cli.run_generate(self.engine, moved)
        self.assertIn("primary", outcome.payload.data["built"])
        expected = source_data.generate_rows(moved, PopulationName.PRIMARY)
        for table, rows in expected.items():
            on_disk = (self.pops / "primary" / "rows" / f"{table}.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertEqual(len(on_disk.strip().splitlines()), len(rows), table)
        self.assertEqual(
            source_data.population_drift(
                moved, PopulationName.PRIMARY, self.pops / "primary"
            ),
            (),
        )

    def test_drift_is_a_stage_failure_routed_to_the_rebuild(self):
        """The reference resume shortcut and both variant batteries refuse to
        certify populations that are no longer the derivation."""
        from elt_taskgen.models import RepairRoute

        cli.run_generate(self.engine, self.task)
        self.assertIsNone(cli._population_drift_failure(self.engine, self.task))
        rendered = sorted(
            (self.pops / "primary" / "rendered").rglob("*.csv")
        )
        rendered[0].write_text("tampered\n", encoding="utf-8")

        outcome = cli._population_drift_failure(self.engine, self.task)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertEqual(outcome.route, RepairRoute.RUNTIME)
        self.assertIn("population artifacts drift", outcome.payload.error)
        self.assertIn("primary", outcome.payload.data["drifted_populations"])
        # ... and the route's rerun set leads with the stage that repairs it.
        self.assertEqual(repair.stages_to_rerun(RepairRoute.RUNTIME)[0], "generate")


class TestExitCodes(unittest.TestCase):
    """STAGE EXIT-CODE CONTRACT: 0 passed, 1 task rejected, 2 could not
    measure/decide. Every case here used to be a traceback and exit 1 — the
    code reserved for a rejected task."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.workspace.mkdir(parents=True)

    def _main(self, argv):
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = cli.main([*argv, "--workspace", str(self.workspace)])
        return code, out.getvalue() + err.getvalue()

    def test_record_and_replay_only_exit_2_without_traceback(self):
        code, printed = self._main(
            ["generate", "--task-id", "x", "--record", "--replay-only"]
        )
        self.assertEqual(code, 2)
        self.assertIn("contradictory", printed)

    def test_calibrate_unknown_variant_exits_2(self):
        code, printed = self._main(
            ["calibrate", "--task-id", "x", "--empirical", "--variants", "bogus"]
        )
        self.assertEqual(code, 2)
        self.assertIn("unknown variant 'bogus'", printed)

    def test_unknown_task_id_exits_2(self):
        code, printed = self._main(["generate", "--task-id", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("nope", printed)
        self.assertIn("EngineError", printed)

    def test_stage_subcommand_on_a_rejected_task_exits_1(self):
        engine = Engine(self.workspace)
        task = demo_fixture.demo_task()
        engine.register(task)
        engine.record_report(
            task, StageName.GENERATE.value, VERDICT_PASS, StagePayload(detail="ok")
        )
        engine.record_report(
            task,
            StageName.GATES.value,
            "fatal",
            StagePayload(error="human audit rejected this task"),
        )
        engine.close()

        code, printed = self._main(
            ["generate", "--task-id", task.task_id, "--replay-only"]
        )
        self.assertEqual(code, 1)
        self.assertIn("final verdict: rejected", printed)
        self.assertIn("REJECTED", printed)

    def test_infrastructure_failure_exits_2_and_rejects_nothing(self):
        engine = Engine(self.workspace)
        task = demo_fixture.demo_task()
        engine.register(task)
        engine.close()

        def review(engine, task):
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(error="TranscriptMissingError: nothing recorded"),
            )

        real_build = cli.build_stage_runners

        def stub_runners(provider, **kwargs):
            runners = {
                stage: (lambda e, t, s=stage: StageOutcome(
                    VERDICT_PASS, StagePayload(detail=f"{s.value} ok")
                ))
                for stage in STAGE_ORDER
                if stage is not StageName.INTAKE
            }
            runners[StageName.REVIEW] = review
            return runners

        with mock.patch.object(cli, "build_stage_runners", stub_runners):
            code, printed = self._main(
                ["review", "--task-id", task.task_id, "--replay-only"]
            )
        self.assertEqual(code, 2)
        self.assertIn("could not measure", printed)
        self.assertIs(real_build, cli.build_stage_runners)

        engine = Engine(self.workspace)
        self.addCleanup(engine.close)
        self.assertNotEqual(
            engine.load_task(task.task_id).status.value, "rejected"
        )

    def test_blocked_stage_exits_2_with_a_message(self):
        engine = Engine(self.workspace)
        task = demo_fixture.demo_task()
        engine.register(task)
        engine.close()

        def stub_runners(provider, **kwargs):
            runners = {
                stage: (lambda e, t, s=stage: StageOutcome(
                    VERDICT_PASS, StagePayload(detail=f"{s.value} ok")
                ))
                for stage in STAGE_ORDER
                if stage is not StageName.INTAKE
            }
            runners[StageName.AUDIT] = lambda e, t: StageOutcome(
                "blocked",
                StagePayload(
                    error="1 borderline collision(s) require human sign-off",
                    data={"blocked_on": "human"},
                ),
            )
            return runners

        with mock.patch.object(cli, "build_stage_runners", stub_runners):
            code, printed = self._main(
                [
                    "release",
                    "--task-id",
                    task.task_id,
                    "--replay-only",
                    "--development-release",
                    "--allow-structural-difficulty",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED at audit", printed)
        self.assertIn("waiting on: human", printed)
        self.assertIn("nothing was rejected", printed)

    def test_a_block_further_down_the_ladder_is_not_this_stage_verdict(self):
        """A stage subcommand answers for ITS stage: an outstanding audit
        sign-off must not make `generate` report failure."""
        engine = Engine(self.workspace)
        task = demo_fixture.demo_task()
        engine.register(task)
        engine.record_report(
            task,
            StageName.AUDIT.value,
            "blocked",
            StagePayload(
                error="waiting for a signature", data={"blocked_on": "human"}
            ),
        )
        engine.close()

        def stub_runners(provider, **kwargs):
            return {
                stage: (lambda e, t, s=stage: StageOutcome(
                    VERDICT_PASS, StagePayload(detail=f"{s.value} ok")
                ))
                for stage in STAGE_ORDER
                if stage is not StageName.INTAKE
            }

        with mock.patch.object(cli, "build_stage_runners", stub_runners):
            code, printed = self._main(
                ["generate", "--task-id", task.task_id, "--replay-only"]
            )
        self.assertEqual(code, 0)
        self.assertNotIn("BLOCKED at audit", printed)


class TestProviderResolution(unittest.TestCase):
    def test_stage_commands_never_consult_the_committed_fixtures(self):
        """`elt-taskgen demo` is gone; a production replay hit must come from
        THIS workspace, or a --record re-recording is shadowed forever by a
        same-key fixture."""
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args(
                ["generate", "--task-id", "t", "--workspace", tmp, "--replay-only"]
            )
            provider = cli._resolve_provider(args, Path(tmp))
            self.assertIsNone(provider.store.fixtures_dir)
            self.assertEqual(
                provider.store.record_dir, Path(tmp) / "transcripts"
            )

    def test_live_metrology_forces_fresh_calls_and_has_no_fixture_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args(["metrology", "--workspace", tmp])
            provider = SimpleNamespace(task_id="", meter=SimpleNamespace())
            with mock.patch.object(
                cli, "_resolve_provider", return_value=provider
            ) as resolve, mock.patch.object(
                cli, "_cmd_metrology_measured", return_value=2
            ), mock.patch.object(cli, "_print_demo_spend"):
                cli._cmd_metrology_inner(args)
        kwargs = resolve.call_args.kwargs
        self.assertFalse(kwargs["replay_only"])
        self.assertTrue(kwargs["refresh"])
        self.assertIsNone(kwargs["fixtures_dir"])
        self.assertEqual(kwargs["source"], "metrology-fresh-live")

    def test_only_diagnostic_metrology_replay_reads_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args(
                ["metrology", "--workspace", tmp, "--replay-only"]
            )
            provider = SimpleNamespace(task_id="", meter=SimpleNamespace())
            with mock.patch.object(
                cli, "_resolve_provider", return_value=provider
            ) as resolve, mock.patch.object(
                cli, "_cmd_metrology_measured", return_value=2
            ), mock.patch.object(cli, "_print_demo_spend"):
                cli._cmd_metrology_inner(args)
        kwargs = resolve.call_args.kwargs
        self.assertTrue(kwargs["replay_only"])
        self.assertFalse(kwargs["refresh"])
        self.assertEqual(
            kwargs["fixtures_dir"], providers_mod.default_fixtures_dir()
        )
        self.assertEqual(kwargs["source"], "metrology-diagnostic-replay")

    def test_live_metrology_binds_its_sublimit_to_existing_parent_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metrology_workspace = root / "council"
            parent_workspace = root / "candidate-run"
            DurableBudgetLedger.initialize(
                "parent-run",
                350.0,
                parent_workspace,
                per_task_limit_usd=7.0,
            )
            args = cli.build_parser().parse_args(
                [
                    "metrology",
                    "--workspace",
                    str(metrology_workspace),
                    "--budget-per-task",
                    "60",
                    "--budget-total",
                    "60",
                    "--parent-budget-workspace",
                    str(parent_workspace),
                    "--parent-budget-run-id",
                    "parent-run",
                    "--parent-budget-total",
                    "350",
                ]
            )
            provider = SimpleNamespace(task_id="", meter=SimpleNamespace())
            with mock.patch.object(
                cli, "_resolve_provider", return_value=provider
            ) as resolve, mock.patch.object(
                cli, "_cmd_metrology_measured", return_value=2
            ), mock.patch.object(cli, "_print_demo_spend"):
                code = cli._cmd_metrology_inner(args)
        self.assertEqual(code, 2)
        resolved_args = resolve.call_args.args[0]
        self.assertEqual(
            resolved_args.global_budget_workspace,
            parent_workspace.resolve(),
        )
        self.assertEqual(resolved_args.global_budget_run_id, "parent-run")
        self.assertEqual(resolved_args.global_budget_total, 350.0)
        self.assertIsNone(resolved_args.global_budget_per_task)
        self.assertFalse(resolved_args.global_budget_enforce_task_limit)

    def test_parent_bound_metrology_requires_explicit_complete_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args(
                [
                    "metrology",
                    "--workspace",
                    tmp,
                    "--parent-budget-workspace",
                    tmp,
                ]
            )
            with mock.patch.object(cli, "_resolve_provider") as resolve:
                code = cli._cmd_metrology_inner(args)
        self.assertEqual(code, 2)
        resolve.assert_not_called()


class TestCoverageProvenanceIsWorkspaceRelative(unittest.TestCase):
    def test_contamination_reports_record_a_relative_index_dir(self):
        """All five kept drives record an absolute
        .../endtoend-workspaces/<name>/state/contamination that has not existed
        since the workspaces moved."""
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(Path(tmp) / "ws")
            self.addCleanup(engine.close)
            coverage = cli._contamination_index(engine).coverage()
            payload = cli._coverage_payload(engine, coverage)
            self.assertEqual(payload["index_dir"], "state/contamination")
            self.assertFalse(Path(payload["index_dir"]).is_absolute())


class TestReleaseSubcommands(unittest.TestCase):
    def test_verify_reports_a_missing_release_as_could_not_measure(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["verify", "--workspace", tmp])
            self.assertEqual(code, 2)
            self.assertIn("no release_manifest.json", buf.getvalue())

    def test_score_refuses_a_missing_warehouse(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(
                    [
                        "score",
                        "--workspace", tmp,
                        "--release", tmp,
                        "--unit", "demo__t__el",
                        "--duckdb", str(Path(tmp) / "absent.duckdb"),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("no DuckDB file", buf.getvalue())

    def test_semantic_score_refuses_a_missing_submission_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(
                    [
                        "semantic",
                        "score",
                        "--release", tmp,
                        "--task-id", "demo__customer_summary",
                        "--submission", str(Path(tmp) / "absent.json"),
                        "--json",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("no semantic submission file", buf.getvalue())

    def test_semantic_score_parser_defaults_to_hidden_population_set(self):
        args = cli.build_parser().parse_args(
            [
                "semantic",
                "score",
                "--release", "release",
                "--task-id", "demo__customer_summary",
                "--submission", "attempt.json",
            ]
        )
        self.assertIsNone(args.population)
        self.assertEqual(args.timeout_seconds, 60.0)
        self.assertEqual(args.memory_limit_mb, 512)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Phase 4 (roadmap Table 8 `cli.py` row): the review manifest's session
# branch, `--workers`, `--diagnostic-order-check`, the measured lines,
# `admission audit --revoked`
# ---------------------------------------------------------------------------

import contextlib as _contextlib
import os as _os
from types import SimpleNamespace as _NS

from elt_taskgen.models import CouncilRole as _CouncilRole, canonical_json as _canonical_json, sha256_hex as _sha256_hex
from elt_taskgen.review import metrology as _metrology_mod

_CRITICS = ("ambiguity_critic", "feasibility_reviewer", "population_adversary", "shortcut_attacker")


def _one_shot_row(task, role, *, count=0):
    response = '{"findings": []}'
    behavior = providers_mod.role_behavior_manifest(role)
    return {
        "task_id": task.task_id, "task_content_hash": task.content_hash(), "role": role,
        "prompt_sha256": _sha256_hex(f"{role}:prompt"), "response_sha256": _sha256_hex(response),
        "attempt_count": 1, "correction_count": 0, "entry_schema": 2,
        "finding_count": count, "zero_findings": count == 0,
        "provider": "test", "model": "test", "replayed": True,
        "behavior_sha256": providers_mod.role_behavior_sha256(role),
        "tools_sha256": _sha256_hex(
            providers_mod.canonical_json(behavior["tools"])
        ),
        "policy_sha256": behavior["policy_sha256"],
        "diagnostics_version": providers_mod.DIAGNOSTICS_VERSION,
    }


def _session_row(task, role, *, count=0, **overrides):
    """A SoT T8 SESSION row of a harness-validated seat: two model calls
    (a compile correction, then the submit), two validator runs."""
    if role in {"population_adversary", "shortcut_attacker"}:
        from elt_taskgen.review.tools import critic_validators as critic_tools

        policy = critic_tools.critic_policy(
            role, critic_tools.critic_limits(role)
        )
    else:
        policy = providers_mod.session_policy_for(role)
    row = {
        "task_id": task.task_id, "task_content_hash": task.content_hash(), "role": role,
        "prompt_sha256": _sha256_hex(f"{role}:view"), "response_sha256": _sha256_hex(f"{role}:final"),
        "attempt_count": 2, "correction_count": 1, "model_call_count": 2,
        "tool_call_count": 0, "refused_count": 0, "nudge_count": 0, "validator_run_count": 2,
        "correction_kinds": {"schema": 0, "compile": 1}, "terminal": "SUBMITTED",
        "terminal_count": 1, "limit_stop_count": 0, "live_model_call_count": 2,
        "stale_tool_result_count": 0, "replayed": False, "trajectory_sha256": _sha256_hex(f"{role}:traj"),
        "entry_schema": 3, "finding_count": count, "zero_findings": count == 0,
        "provider": "anthropic", "model": "claude-opus-5",
        "behavior_sha256": providers_mod.role_behavior_sha256(role),
        "tools_sha256": policy.tools_sha256(),
        "policy_sha256": policy.sha256(),
        "diagnostics_version": providers_mod.DIAGNOSTICS_VERSION,
        "usage": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0}, "usd": 0.01, "wall_ms": 12,
    }
    row.update(overrides)
    return row


class TestReviewManifestSessionBranch(unittest.TestCase):
    """SoT T8: `_validated_review_manifest` accepts ONE row per critic role in
    either shape and applies the count identity to a session row."""

    def setUp(self):
        self.task = demo_fixture.demo_task()

    def _manifest(self, rows, findings=()):
        provider = SimpleNamespace(exchange_evidence=list(rows))
        return cli._validated_review_manifest(provider, self.task, list(findings), required=True)

    def test_review_manifest_accepts_one_trajectory_per_role_with_count_invariants(self):
        rows = [_one_shot_row(self.task, r) for r in _CRITICS if r != "population_adversary"]
        rows.append(_session_row(self.task, "population_adversary"))
        manifest, problems = self._manifest(rows)
        self.assertEqual(problems, [])
        self.assertEqual([m["role"] for m in manifest], list(_CRITICS))
        session = next(m for m in manifest if m["role"] == "population_adversary")
        self.assertEqual(session["entry_schema"], 3)
        self.assertEqual(
            session["model_call_count"],
            session["tool_call_count"] + session["refused_count"] + session["nudge_count"]
            + session["correction_count"] + session["terminal_count"] + session["limit_stop_count"],
        )
        # Every seat may be a session row; a limit-stopped one too (SoT T4).
        rows = [_session_row(self.task, r) for r in _CRITICS]
        rows[0] = _session_row(self.task, "ambiguity_critic", terminal="LIMIT_TURNS", terminal_count=0,
                               limit_stop_count=1, model_call_count=3, attempt_count=3, correction_count=2,
                               correction_kinds={"schema": 2, "compile": 0})
        manifest, problems = self._manifest(rows)
        self.assertEqual(problems, [])
        self.assertEqual(len(manifest), 4)
        # Through the REAL provider: a harness-validated seat's session row
        # from `RoutedProvider.complete` under a task context validates.
        from tests.test_providers import FakeTransport, VALID_FINDING, anthropic_tool_response, make_routing
        from tests.test_providers_session import _agents, _doc

        with _agents(
            _doc(
                population_adversary={"enabled": True},
                # The repository default now enables both executable critics;
                # this branch intentionally exercises exactly the POP session
                # while the other three rows stay one-shot.
                shortcut_attacker={"enabled": False},
            )
        ), tempfile.TemporaryDirectory() as tmp:
            # run_council consults the seats in CRITIC_ROLES order (AMB, POP, SHC, FEA).
            transport = FakeTransport([
                anthropic_tool_response([], model="claude-sonnet-5"),
                anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5"),
                anthropic_tool_response([], model="claude-opus-5"),
                anthropic_tool_response([], model="claude-haiku-4-5"),
            ])
            provider = providers_mod.RoutedProvider(
                make_routing(), providers_mod.TranscriptStore(Path(tmp) / "transcripts"),
                providers_mod.CostMeter(budget_per_task_usd=100.0), transports={"anthropic": transport},
            )
            self.assertTrue(cli._begin_task_evidence(provider, self.task))
            from elt_taskgen.review import council

            findings = council.run_council(self.task, provider)
            manifest, problems = cli._validated_review_manifest(provider, self.task, findings, required=True)
            self.assertEqual(problems, [])
            by_role = {m["role"]: m for m in manifest}
            self.assertEqual(by_role["population_adversary"]["entry_schema"], 3)
            self.assertEqual(by_role["population_adversary"]["finding_count"], 1)
            self.assertEqual({r: by_role[r]["entry_schema"] for r in _CRITICS if r != "population_adversary"},
                             {r: 2 for r in _CRITICS if r != "population_adversary"})

    def test_review_manifest_refuses_a_trajectory_row_that_breaks_the_count_invariants(self):
        """The refusal twin: a session row whose SoT T5 identity does not
        hold, whose corrections exceed its calls, whose nudges exceed one,
        or a role with two rows, is an integrity defect; a one-shot row keeps
        today's exact `attempts-1` rule."""
        base = [_one_shot_row(self.task, r) for r in _CRITICS if r != "population_adversary"]
        for tamper, needle in (
            ({"tool_call_count": 3}, "identity broken"),
            ({"correction_count": 2}, "exceeds attempts-1"),
            ({"nudge_count": 2, "correction_count": 0, "tool_call_count": 0, "model_call_count": 3, "attempt_count": 3}, "more than one nudge"),
            ({"model_call_count": 3}, "does not equal attempt_count"),
            ({"terminal_count": 0}, "identity broken"),
        ):
            with self.subTest(tamper=tamper):
                _, problems = self._manifest(base + [_session_row(self.task, "population_adversary", **tamper)])
                self.assertTrue(any(needle in p and "population_adversary" in p for p in problems), problems)
        _, problems = self._manifest(base + [_session_row(self.task, "population_adversary")] * 2)
        self.assertIn("population_adversary: expected one exchange, found 2", problems)
        _, problems = self._manifest(base + [{**_one_shot_row(self.task, "population_adversary"), "correction_count": 1}])
        self.assertIn("population_adversary: correction_count does not equal attempts-1", problems)
        _, problems = self._manifest(base + [_session_row(self.task, "population_adversary", finding_count=2)])
        self.assertTrue(any("finding_count" in p for p in problems), problems)

    def test_review_manifest_attempt_count_alias_holds_one_release(self):
        """OQ-19: `attempt_count := model_call_count` for one release. A
        session row stating only `model_call_count` is read through the
        alias; one stating both must agree; a one-shot row still needs its
        positive `attempt_count`."""
        base = [_one_shot_row(self.task, r) for r in _CRITICS if r != "population_adversary"]
        aliased = _session_row(self.task, "population_adversary")
        del aliased["attempt_count"]
        manifest, problems = self._manifest(base + [aliased])
        self.assertEqual(problems, [])
        self.assertEqual(next(m for m in manifest if m["role"] == "population_adversary")["model_call_count"], 2)
        _, problems = self._manifest(base + [_session_row(self.task, "population_adversary", attempt_count=3)])
        self.assertTrue(any("does not equal attempt_count" in p for p in problems), problems)
        legacy = _one_shot_row(self.task, "population_adversary")
        del legacy["attempt_count"]
        _, problems = self._manifest(base + [legacy])
        self.assertIn("population_adversary: attempt_count is not positive", problems)
        self.assertEqual(providers_mod.exchange_row_problems({**_session_row(self.task, "population_adversary")}), [])


class _RowDouble:
    """A metrology provider double: answers empty findings and appends one
    SoT T8 row per (trial, seat), keyed deterministically by (role, view)."""

    def __init__(self, name="w"):
        self.name = name
        self.exchange_evidence: list[dict] = []
        self.trials: list[str] = []
        self._nonce = ""

    def begin_trial(self, ctx):
        self._nonce = ctx.trial_nonce
        self.trials.append(ctx.trial_nonce)

    def end_trial(self):
        self._nonce = ""

    def complete(self, role, prompt):
        role_name = getattr(role, "value", str(role))
        response = providers_mod.normalized_text_for(role_name, {"findings": []})
        self.exchange_evidence.append({
            "role": role_name, "prompt_sha256": _sha256_hex(prompt), "response_sha256": _sha256_hex(response),
            "attempt_count": 1, "model_call_count": 1, "correction_count": 0, "tool_call_count": 0,
            "refused_count": 0, "nudge_count": 0, "validator_run_count": 0, "terminal": "SUBMITTED",
            "live_model_call_count": 1, "stale_tool_result_count": 0, "provider": "test-live-provider",
            "model": "test-live-model", "replayed": False, "trial_nonce": self._nonce,
        })
        return response


def _synthetic_schedule(n=24):
    """(nonce, roles, view) per trial: clean trials consult all four seats,
    tampered ones a single seat."""
    schedule = []
    for index in range(n):
        roles = _CRITICS if index % 4 == 0 else (_CRITICS[index % 4],)
        schedule.append((f"{index:032x}", roles, f"view of trial {index}"))
    return schedule


def _drive(pool, schedule):
    for nonce, roles, view in schedule:
        pool.begin_trial(_NS(trial_nonce=nonce, workspace=None, limits_by_role={}, tool_policy_by_role={}))
        try:
            for role in roles:
                pool.complete(role, view)
        finally:
            pool.end_trial()


class TestMetrologyWorkers(unittest.TestCase):
    def test_trial_rows_are_taken_by_identity_not_by_a_positional_slice(self):
        """finding p4-2-5: `RoutedProviderPool.exchange_evidence` answers a
        freshly SORTED union on every read, and `run_metrology` used to slice
        it by a count taken before the trial.

        A row with no `trial_index` — a `complete` outside a trial, which the
        pool documents as supported — sorts to the END, so the positional
        slice handed the trial an unrelated trajectory and silently swallowed
        its real terminal: a limit stop, a caught POLICY_VIOLATION or a stale
        result could be replaced by a stranger's row. The effect exists only
        at `--workers > 1`, so the 1-vs-8 determinism test (which hashes the
        whole union) never saw it.
        """
        pool = providers_mod.RoutedProviderPool([_RowDouble(f"w{i}") for i in range(2)])
        # A row made OUTSIDE any trial, on worker 0.
        pool.workers[0].exchange_evidence.append({
            "role": _CRITICS[0], "prompt_sha256": _sha256_hex("pre"),
            "response_sha256": _sha256_hex("pre"), "attempt_count": 9,
            "model_call_count": 9, "terminal": "SUBMITTED",
        })
        rows_before = len(pool.exchange_evidence)
        self.assertEqual(rows_before, 1)
        nonce = "a" * 32
        pool.begin_trial(
            _NS(trial_nonce=nonce, workspace=None, limits_by_role={},
                tool_policy_by_role={}, trial_index=0)
        )
        try:
            pool.complete(_CRITICS[0], "view of trial 0")
        finally:
            pool.end_trial()
        union = pool.exchange_evidence
        # The sorted union puts the UNINDEXED row last, so the positional
        # slice would take exactly the wrong row.
        self.assertIsNone(union[-1].get("trial_index"))
        self.assertEqual(union[rows_before:][0]["model_call_count"], 9)
        # The identity selection takes the trial's own row.
        attributed: set = set()
        picked = _metrology_mod._rows_for_trial(
            pool, trial_index=0, trial_nonce=nonce,
            rows_before=rows_before, attributed=attributed,
        )
        self.assertEqual([row["trial_nonce"] for row in picked], [nonce])
        self.assertEqual([row["model_call_count"] for row in picked], [1])
        # A double whose rows carry neither key still gets the append-ordered
        # tail, so nothing that worked before stops working.
        plain = SimpleNamespace(exchange_evidence=[{"role": "a"}, {"role": "b"}])
        self.assertEqual(
            _metrology_mod._rows_for_trial(
                plain, trial_index=0, trial_nonce="", rows_before=1, attributed=set()
            ),
            [{"role": "b"}],
        )

    def test_metrology_manifest_sha_is_identical_at_workers_1_and_8(self):
        """OQ-20: one RoutedProvider per worker, the evidence sorted by
        `trial_index` before hashing — the trajectory manifest AND the
        dispatch-order digest are byte-identical at --workers 1 and 8, and
        the 8-way pool really spreads the trials over its workers."""
        schedule = _synthetic_schedule(24)
        expected = {role: sum(1 for _, roles, _ in schedule if role in roles) for role in _CRITICS}
        digests = {}
        pools = {}
        for workers in (1, 8):
            pool = providers_mod.RoutedProviderPool([_RowDouble(f"w{i}") for i in range(workers)])
            _drive(pool, schedule)
            rows = cli._evidence_in_trial_order(pool.exchange_evidence)
            self.assertEqual([r["trial_index"] for r in rows], sorted(r["trial_index"] for r in rows))
            summary = _metrology_mod.summarize_fresh_live_trajectories(rows, expected_by_role=expected)
            digests[workers] = (summary.trajectory_manifest_sha256, summary.dispatch_order_sha256)
            pools[workers] = pool
        self.assertEqual(digests[1], digests[8])
        self.assertEqual(pools[1].size, 1)
        self.assertEqual([len(w.trials) for w in pools[8].workers], [3] * 8)
        self.assertEqual(pools[8].workers[3].trials, [schedule[3][0], schedule[11][0], schedule[19][0]])
        # Every row of the 8-way pool names the trial it was made in.
        eight = pools[8].exchange_evidence
        by_trial = {}
        for row in eight:
            by_trial.setdefault(row["trial_index"], set()).add(row["trial_nonce"])
        self.assertEqual(by_trial, {i: {schedule[i][0]} for i in range(24)})
        # The pool reads like a provider (worker 0's routing, meter, ...).
        self.assertEqual(pools[8].name, "w0")
        # The CLI builds N RoutedProviders over ONE routing, store and meter.
        from tests.test_providers import make_routing

        first = providers_mod.RoutedProvider(
            make_routing(), providers_mod.TranscriptStore(None), providers_mod.CostMeter(budget_per_task_usd=1.0),
            task_id="council-metrology", refresh=True,
        )
        pool = cli._metrology_worker_pool(first, 8, providers_mod)
        self.assertEqual(pool.size, 8)
        self.assertTrue(all(w.routing is first.routing and w.meter is first.meter and w.store is first.store for w in pool.workers))
        self.assertTrue(all(w.refresh for w in pool.workers))
        self.assertIs(pool.workers[0], first)
        self.assertEqual(cli._metrology_worker_pool(first, 1, providers_mod).size, 1)
        # Rows without a trial index keep their order, after the indexed ones.
        unindexed = [{"role": "a", "prompt_sha256": "x"}, {"role": "b", "prompt_sha256": "y"}]
        ordered = cli._evidence_in_trial_order(unindexed + [{"trial_index": 0, "role": "c", "prompt_sha256": "z"}])
        self.assertEqual([r["role"] for r in ordered], ["c", "a", "b"])


def _synthetic_metrics(role, **overrides):
    fields = dict(
        role=role, tampered_count=25, detected_count=24, clean_count=10, false_alarms=0,
        recall=0.96, precision=0.9, nitpick_rate=0.0, distinct_specimens=5, replicates=5,
        recall_lb=0.85, recall_ub=1.0, nitpick_ub=0.2, per_specimen={"s1": 1.0},
        canary_trials=3, canary_hits=0, canary_hits_by_kind={"private_literal": 0, "forbidden_validator": 0, "impossible": 0},
        private_probe_count=0, policy_violation_trials=0, policy_violation_ub=0.0, block_reasons=(),
        nudge_count=1, validator_run_count=27, stuck_trials=0, limit_stopped_trials=1,
        format_retry_trials=2, compile_correction_trials=3, wasted_call_ratio=0.05,
        pass_k={"2": 0.9, "3": 0.85}, icc=0.11, n_eff=17.4,
    )
    fields.update(overrides)
    return _NS(**fields)


def _synthetic_report(seed, **metric_overrides):
    per_role = {r: _synthetic_metrics(r, **metric_overrides.get(r, {})) for r in _CRITICS}
    specimens = []
    for role in _CRITICS:
        specimens += [_NS(name=f"{role}-{i}", kind="tampered", target_role=role) for i in range(per_role[role].tampered_count)]
    specimens += [_NS(name=f"clean-{i}", kind="clean", target_role=None) for i in range(per_role[_CRITICS[0]].clean_count)]
    canaries = [_NS(name=f"{role}-canary-{i}", canary_kind="impossible", target_role=role)
                for role in _CRITICS for i in range(per_role[role].canary_trials)]
    return _NS(
        per_role=per_role, role_pass={r: not per_role[r].block_reasons for r in _CRITICS},
        admitted=all(not per_role[r].block_reasons for r in _CRITICS), seed=seed,
        tool_surface_sha256="t" * 64, harness_version=_metrology_mod.HARNESS_VERSION,
        specimens=tuple(specimens), canaries=tuple(canaries),
    )


def _stub_admission_marker(workspace, report, *, exchange_evidence, honor_override, **kwargs):
    """The admitted branch's writer, stubbed: the record path with the
    evidence row count, so a test can see what was handed to it."""
    path = _metrology_mod.marker_path(Path(workspace))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical_json({"schema": 4, "admitted": True, "rows": len(list(exchange_evidence))}))
    return path


@_contextlib.contextmanager
def _metrology_stubs(run_metrology):
    """`_cmd_metrology_measured` without the specimen pool: the fingerprint,
    the pool digests, the toolchain assertion, the report writer and
    `run_metrology` itself are stubbed; every other reading is real."""
    pins = _metrology_mod.toolchain_pins()
    specimen = _NS(name="s", kind="clean", target_role=None)
    with mock.patch.object(_metrology_mod, "installed_toolchain", lambda names=(): dict(pins)), \
         mock.patch.object(_metrology_mod, "council_routing_fingerprint", lambda routing: "f" * 64), \
         mock.patch.object(_metrology_mod, "tool_surface_sha256", lambda **kw: "t" * 64), \
         mock.patch.object(_metrology_mod, "pool_sha256", lambda **kw: "p" * 64), \
         mock.patch.object(_metrology_mod, "specimen_pool", lambda: (specimen,) * 40), \
         mock.patch.object(_metrology_mod, "canary_pool", lambda: (specimen,) * 24), \
         mock.patch.object(_metrology_mod, "write_report", lambda report, out_dir: Path(out_dir) / "metrology.json"), \
         mock.patch.object(_metrology_mod, "fingerprint_components", lambda routing, **kw: {}), \
         mock.patch.object(_metrology_mod, "write_admission_marker", _stub_admission_marker), \
         mock.patch.object(_metrology_mod, "run_metrology", run_metrology), \
         mock.patch.object(_metrology_mod, "position_dependence", lambda report: {}, create=True):
        yield


def _measured_args(workspace, **kw):
    fields = dict(workspace=workspace, agents_config=None, seed=7, replay_only=False,
                  diagnostic_order_check=False, workers=1)
    fields.update(kw)
    return _NS(**fields)


class TestMetrologyMeasuredPhase4(unittest.TestCase):
    def _provider(self):
        return _NS(routing=_NS(), replay_only=False, refresh=True, agents_config=None,
                   exchange_evidence=[], task_id="council-metrology")

    def test_metrology_measured_prints_efficiency_integrity_icc_and_n_eff(self):
        """The measured lines: per seat the integrity readings (canary flag
        and hits by kind, private probes, the policy-violation UB), the
        advisory efficiency ratios (stuck, limit-stopped, format-retry,
        compile-correction, nudges, validator runs, wasted calls), ICC,
        n_eff and pass^k; the fresh-live line names validator runs, nudges,
        corrections and policy violations."""
        report = _synthetic_report(7, population_adversary={"canary_hits": 1, "canary_hits_by_kind": {"private_literal": 1}, "block_reasons": ("canary_hit",)})
        provider = self._provider()

        def fake_run(prov, **kwargs):
            for role in _CRITICS:
                metrics = report.per_role[role]
                count = metrics.tampered_count + metrics.clean_count + metrics.canary_trials
                for index in range(count):
                    prov.exchange_evidence.append({
                        "role": role, "prompt_sha256": _sha256_hex(f"{role}{index}"), "response_sha256": _sha256_hex("r"),
                        "attempt_count": 1, "model_call_count": 1, "correction_count": 0, "tool_call_count": 0,
                        "refused_count": 0, "nudge_count": 0, "validator_run_count": 1, "terminal": "SUBMITTED",
                        "live_model_call_count": 1, "stale_tool_result_count": 0, "provider": "p", "model": "m",
                        "replayed": False, "trial_index": index,
                    })
            return report

        with tempfile.TemporaryDirectory() as tmp, _metrology_stubs(fake_run):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli._cmd_metrology_measured(_measured_args(Path(tmp)), Path(tmp), provider, _metrology_mod, providers_mod)
            text = out.getvalue()
        self.assertEqual(code, 1)  # POP blocked on the canary hit
        self.assertIn("CANARY HIT (1/3", text)
        self.assertIn("canary clean (0/3", text)
        self.assertIn("by kind: private_literal=1", text)
        self.assertIn("private probes=0 (<=0)", text)
        self.assertIn("policy violations=0 (UB 0.00 <=0.1)", text)
        self.assertIn("efficiency (advisory): stuck=0/35", text)
        self.assertIn("limit-stopped=1/35", text)
        self.assertIn("format-retry=2/35", text)
        self.assertIn("compile-correction=3/35", text)
        self.assertIn("nudges=1", text)
        self.assertIn("validator runs=27", text)
        self.assertIn("wasted-call ratio=0.05", text)
        self.assertIn("ICC=0.11", text)
        self.assertIn("n_eff=17.4", text)
        self.assertIn("pass^k=k2=0.90 k3=0.85", text)
        self.assertIn("validator runs=152", text)
        self.assertIn("policy violations=0, manifest=", text)
        self.assertIn("REVOKED: admission withdrawn at", text)
        self.assertIn("BLOCKED: council not admitted", text)
        # A RoleMetrics without the Phase 4 fields prints n/a, never raises.
        bare = _synthetic_report(7)
        for role in _CRITICS:
            for name in ("icc", "n_eff", "pass_k", "stuck_trials", "wasted_call_ratio"):
                delattr(bare.per_role[role], name)
        provider = self._provider()

        def bare_run(prov, **kwargs):
            fake_run(prov, **kwargs)
            return bare

        with tempfile.TemporaryDirectory() as tmp, _metrology_stubs(bare_run):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli._cmd_metrology_measured(_measured_args(Path(tmp)), Path(tmp), provider, _metrology_mod, providers_mod)
        self.assertEqual(code, 0)
        self.assertIn("ICC=n/a  n_eff=n/a  pass^k=n/a", out.getvalue())
        self.assertIn("stuck=n/a", out.getvalue())
        self.assertIn("ADMITTED: live council routing enabled", out.getvalue())

    def test_diagnostic_order_check_runs_canonical_order_and_exits_2(self):
        """`--diagnostic-order-check`: the seed's own trials (scored and
        canary) run in canonical sha256(specimen name) order, passed to
        `run_metrology` as `trials=` with no separate canary tuple (so no
        seeded reshuffle), and the run exits 2 without writing or revoking."""
        task = demo_fixture.demo_task()

        def specimen(name, kind="tampered", role=_CouncilRole.AMBIGUITY_CRITIC):
            return _metrology_mod.Specimen(name=name, kind=kind, target_role=role, description="",
                                           detection_terms=("x",), task=task)

        scored = tuple(_metrology_mod.Trial(specimen=specimen(n), variant=v, replicate=0, roles=(_CouncilRole.AMBIGUITY_CRITIC,))
                       for n, v in (("zeta", 1), ("alpha", 2), ("mid", 0)))
        canaries = (_metrology_mod.Trial(specimen=specimen("canary_a", kind="canary"), variant=3, replicate=0, roles=(_CouncilRole.AMBIGUITY_CRITIC,)),)
        seen = {}

        def fake_run(prov, **kwargs):
            seen.update(kwargs)
            return _synthetic_report(7)

        with tempfile.TemporaryDirectory() as tmp, _metrology_stubs(fake_run), \
             mock.patch.object(_metrology_mod, "select_trials", lambda seed: scored), \
             mock.patch.object(_metrology_mod, "select_canary_trials", lambda seed: canaries):
            provider = self._provider()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli._cmd_metrology_measured(
                    _measured_args(Path(tmp), diagnostic_order_check=True), Path(tmp), provider, _metrology_mod, providers_mod
                )
            self.assertEqual(code, 2)
            self.assertFalse(_metrology_mod.marker_path(Path(tmp)).exists())
        self.assertEqual(seen["canary_trials"], ())
        names = [t.specimen.name for t in seen["trials"]]
        expected = sorted(["zeta", "alpha", "mid", "canary_a"], key=lambda n: hashlib.sha256(n.encode()).hexdigest())
        self.assertEqual(names, expected)
        self.assertNotEqual(names, ["zeta", "alpha", "mid", "canary_a"])
        self.assertIn("DIAGNOSTIC ONLY (order check)", out.getvalue())
        self.assertIn("canonical sha256(name) order", out.getvalue())
        # The parser carries both Phase 4 flags with safe defaults.
        args = cli.build_parser().parse_args(["metrology", "--workspace", "x"])
        self.assertEqual((args.workers, args.diagnostic_order_check), (1, False))
        args = cli.build_parser().parse_args(["metrology", "--workspace", "x", "--workers", "8", "--diagnostic-order-check"])
        self.assertEqual((args.workers, args.diagnostic_order_check), (8, True))

    def test_metrology_caught_tuple_includes_harness_fault_classes(self):
        """C7: a harness fault out of the run — every `SessionFault`, a
        `DiagnosticTripwire`, a `PolicyFault` raised outside a session's own
        catch, a chain that does not verify — is could-not-measure: exit 2,
        the class named, nothing written or revoked."""
        from elt_taskgen.review import session as S
        from elt_taskgen.review import trajectory as T
        from elt_taskgen.review.tools.projection import DiagnosticTripwire

        faults = (
            S.ToolHarnessFault("compile_probe", cause_type="RuntimeError"),
            S.ToolDeadlineExceeded("compile_probe", deadline_s=20.0),
            S.SandboxFault("validator worker died", code="worker_failed"),
            DiagnosticTripwire("numbers", "standalone_number", source="compile"),
            S.ToolNotPermitted("compile_probe"),
            S.ForbiddenArgument(tool="x", detail="population_argument"),
            T.ChainError("chain link 1 does not bind the turn at that position"),
            providers_mod.RoleCapExceeded("cap"),
        )
        for fault in faults:
            with self.subTest(fault=type(fault).__name__):
                def raising(prov, **kwargs):
                    raise fault

                with tempfile.TemporaryDirectory() as tmp, _metrology_stubs(raising):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        code = cli._cmd_metrology_measured(_measured_args(Path(tmp)), Path(tmp), self._provider(), _metrology_mod, providers_mod)
                    self.assertEqual(code, 2)
                    self.assertIn(f"ERROR: {type(fault).__name__}:", out.getvalue())
                    self.assertFalse(_metrology_mod.marker_path(Path(tmp)).exists())


class TestAdmissionAudit(unittest.TestCase):
    def test_admission_audit_revoked_lists_trajectories_under_the_withdrawn_record(self):
        """`admission audit --revoked PATH` (read-only) lists every stamped
        record of the workspace's transcript store — a one-shot entry, a
        session record, a content-addressed trajectory record — whose
        `admission_record_path` is PATH or whose stamped fingerprint is the
        tombstone's withdrawn one; an unstamped record and one stamped with
        another admission are not listed; nothing is written."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "council"
            marker = _metrology_mod.marker_path(ws)
            marker.parent.mkdir(parents=True)
            marker.write_text(_canonical_json({
                "schema": _metrology_mod.ADMISSION_SCHEMA, "admitted": False, "revoked": True,
                "revoked_reason": "a metrology run BLOCKED", "revoked_reason_code": "canary_hit",
                "revoked_seed": 7, "revoked_routing_fingerprint": "f" * 64,
                "revoked_harness_version": _metrology_mod.HARNESS_VERSION,
                "revoked_tool_surface_sha256": "t" * 64, "revoked_trajectory_manifest_sha256": "m" * 64,
            }), encoding="utf-8")
            stamp = {"admission_mode": "admitted", "admission_record_path": str(marker),
                     "admission_routing_fingerprint": "f" * 64, "admission_evidence_sha256": "e" * 64, "admission_seed": "7"}
            other = {**stamp, "admission_record_path": str(Path(tmp) / "elsewhere"), "admission_routing_fingerprint": "0" * 64}
            transcripts = ws / "transcripts"
            (transcripts / "population_adversary").mkdir(parents=True)
            (transcripts / "population_adversary" / "sessions").mkdir()
            (transcripts / "trajectories" / "population_adversary").mkdir(parents=True)
            (transcripts / "population_adversary" / ("a" * 64 + ".json")).write_text(_canonical_json({
                "role": "population_adversary", "prompt_sha256": "a" * 64, "task_id": "t1", "response": "{}",
                "admission": stamp, "recorded_by": "review",
            }))
            (transcripts / "population_adversary" / ("b" * 64 + ".json")).write_text(_canonical_json({
                "role": "population_adversary", "prompt_sha256": "b" * 64, "task_id": "t2", "response": "{}",
            }))
            (transcripts / "population_adversary" / ("c" * 64 + ".json")).write_text(_canonical_json({
                "role": "population_adversary", "prompt_sha256": "c" * 64, "task_id": "t3", "response": "{}",
                "admission": other,
            }))
            (transcripts / "population_adversary" / "sessions" / ("d" * 64 + ".json")).write_text(_canonical_json({
                "role": "population_adversary", "session_key": "d" * 64, "session_sha256": "1" * 64, "task_id": "t1",
                "admission": {**stamp, "admission_record_path": "/moved/elsewhere"},  # the fingerprint still matches
            }))
            (transcripts / "trajectories" / "population_adversary" / ("e" * 64 + ".json")).write_text(_canonical_json({
                "role": "population_adversary", "trajectory_sha256": "e" * 64, "turns": [], "task_id": "council-metrology",
                "admission": stamp, "recorded_by": "metrology-fresh-live",
            }))
            before = sorted((p, p.read_bytes()) for p in ws.rglob("*") if p.is_file())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["admission", "audit", "--workspace", str(ws), "--revoked", str(marker)])
            text = out.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("REVOKED [canary_hit]", text)
            self.assertIn("3 record(s) ran under this admission (withdrawn)", text)
            self.assertIn("exchange   population_adversary     task=t1", text)
            self.assertIn("session    population_adversary     task=t1", text)
            self.assertIn("trajectory population_adversary     task=council-metrology", text)
            self.assertNotIn("task=t2", text)
            self.assertNotIn("task=t3", text)
            self.assertEqual(sorted((p, p.read_bytes()) for p in ws.rglob("*") if p.is_file()), before)
            # With no PATH the consulted record is audited (the workspace's).
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.dict(_os.environ, {_metrology_mod.ADMISSION_ENV: ""}):
                code = cli.main(["admission", "audit", "--workspace", str(ws), "--revoked"])
            self.assertEqual(code, 0)
            self.assertIn("3 record(s)", out.getvalue())
            # An absent record: nothing matches, exit 0.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["admission", "audit", "--workspace", str(ws), "--revoked", str(Path(tmp) / "nowhere")])
            self.assertEqual(code, 0)
            self.assertIn("0 record(s)", out.getvalue())

"""Human audit stage + audit CLI subcommand tests.

WHY THIS EXISTS
The audit stage is the one place a HUMAN decision enters the pipeline, so its
binding rules are regression-critical: an approval is only valid when it is
bound to the CURRENT task content hash AND covers exactly the pending
borderline contamination collisions. The historical bug this module guards
against: a sign-off recorded before a repair silently carrying over to the
repaired (semantically different) task. These tests drive the real CLI
subcommands (`audit list/approve/reject`) against a stub ledger state so they
run in milliseconds, not the full 15-stage pipeline.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen import cli, demo_fixture
from elt_taskgen.engine import (
    Engine,
    StageName,
    StagePayload,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
    variant_gate_stage,
)
from elt_taskgen.models import (
    AcceptanceReport,
    AuditApproval,
    GateResult,
    RLVR_TASK_VARIANTS,
    TaskVariant,
    canonical_json,
    variant_task_id,
)
from elt_taskgen.verification import gates as gates_mod
from elt_taskgen.verification import variant_battery

BORDERLINE_A = {
    "kind": "text",
    "against": "eltbench",
    "detail": "fingerprint text:deadbeef present in corpus 'eltbench'",
    "fatal": False,
}
BORDERLINE_B = {
    "kind": "deps",
    "against": "admitted",
    "detail": "fingerprint deps:cafef00d matches previously admitted task 'x'",
    "fatal": False,
}


class TestVariantHelpers(unittest.TestCase):
    def test_variant_ids(self):
        self.assertEqual(variant_task_id("demo__t1", TaskVariant.FULL), "demo__t1")
        self.assertEqual(
            variant_task_id("demo__t1", TaskVariant.EXTRACT_LOAD), "demo__t1__el"
        )
        self.assertEqual(
            variant_task_id("demo__t1", TaskVariant.TRANSFORM), "demo__t1__t"
        )

    def test_empty_task_id_rejected(self):
        with self.assertRaises(ValueError):
            variant_task_id("", TaskVariant.FULL)

    def test_enum_values(self):
        self.assertEqual(
            {v.value for v in TaskVariant}, {"full", "extract_load", "transform"}
        )


class TestAuditApprovalModel(unittest.TestCase):
    def _approval(self, **overrides) -> AuditApproval:
        base = dict(
            task_id="demo__t1",
            task_content_hash="0" * 64,
            approved_collision_fingerprints=("a" * 64,),
            per_axis_labels={"risk": "low"},
            reviewer="wes",
            approved_at="2026-08-08T00:00:00+00:00",
        )
        base.update(overrides)
        return AuditApproval(**base)

    def test_canonical_round_trip(self):
        approval = self._approval()
        again = AuditApproval.model_validate_json(approval.to_canonical_json())
        self.assertEqual(again, approval)
        self.assertEqual(again.content_hash(), approval.content_hash())

    def test_non_iso_approved_at_rejected(self):
        with self.assertRaises(ValueError):
            self._approval(approved_at="yesterday-ish")

    def test_no_wall_clock_inside_the_model(self):
        # approved_at is caller-supplied; two constructions are identical.
        self.assertEqual(self._approval(), self._approval())


class _AuditHarness(unittest.TestCase):
    """Stub ledger state that puts a task exactly at the audit stage's door."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.task = demo_fixture.demo_task()
        self.engine.register(self.task)

    def arm(self, task, collisions):
        """Record post-scan, shared integrity, and both unit acceptances."""
        evidence_dir = cli._evidence_dir(self.engine, task)
        (evidence_dir / "contamination_post.json").write_text(
            canonical_json(
                {
                    "task_id": task.task_id,
                    "task_content_hash": task.content_hash(),
                    "collisions": list(collisions),
                }
            ),
            encoding="utf-8",
        )
        self.engine.record_report(
            task,
            StageName.TASK_INTEGRITY.value,
            VERDICT_PASS,
            StagePayload(detail="stub gates pass for audit tests"),
        )
        for variant in RLVR_TASK_VARIANTS:
            report = AcceptanceReport.from_gates(
                task_id=variant_task_id(task.task_id, variant),
                revision=task.current_revision,
                task_content_hash=task.content_hash(),
                gates=tuple(
                    GateResult(gate=name, passed=True, details="stub unit pass")
                    for name in variant_battery.gate_roster(variant)
                ),
                # THE LIVE scorer + roster digest: a battery is evidence only
                # when it was measured under the scorer and roster now in
                # force (engine.report_is_current / release.variant_acceptance),
                # so a fixture stamped 'test' reads — correctly — as stale.
                scorer_version=gates_mod.SCORER_VERSION,
                roster_digest=gates_mod.ROSTER_DIGEST,
                roster=variant_battery.gate_roster(variant),
            )
            self.engine.record_report(
                task, variant_gate_stage(variant).value, VERDICT_PASS, report
            )

    def cli_main(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main([*argv, "--workspace", str(self.workspace)])
        return code, buf.getvalue()


class TestAuditStage(_AuditHarness):
    def test_auto_pass_only_when_provably_empty(self):
        self.arm(self.task, [])
        outcome = cli.run_audit(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_PASS)
        self.assertIn("queue empty", outcome.payload.detail)

    def test_missing_evidence_fails_closed(self):
        self.engine.record_report(
            self.task, StageName.GATES.value, VERDICT_PASS, StagePayload(detail="stub")
        )
        outcome = cli.run_audit(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertIn("no post-generation contamination evidence", outcome.payload.error)

    def test_pending_collisions_without_approval_block(self):
        """WAITING ON A HUMAN IS NOT A TASK DEFECT.

        As an ordinary FAIL this routed SPECIFICATION, spent a repair round on
        a fully certified task, re-authored byte-identical prose, hit the
        inert-repair rule and REJECTED the task permanently — inside the same
        `release` invocation that printed "run: elt-taskgen audit approve"."""
        self.arm(self.task, [BORDERLINE_A])
        outcome = cli.run_audit(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertEqual(outcome.payload.data.get("blocked_on"), "human")
        self.assertEqual(outcome.payload.data.get("pending"), "1")
        self.assertIn("require human sign-off", outcome.payload.error)
        self.assertIn("audit approve", outcome.payload.error)

    def test_defect_outranks_the_human_queue(self):
        """A task that is not certified cannot be waiting for a signature."""
        self.arm(self.task, [BORDERLINE_A])
        # Shadow the shared-integrity pass with a failure: now there is BOTH a
        # defect and a pending sign-off, and the defect must win.
        self.engine.record_report(
            self.task,
            StageName.TASK_INTEGRITY.value,
            VERDICT_FAIL,
            StagePayload(error="stub gates failure"),
        )
        outcome = cli.run_audit(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertIn("no shared task-integrity pass", outcome.payload.error)

    def test_unresolved_license_blocks_rather_than_rejecting(self):
        """run_audit's own words are "a human must resolve it"; as a FAIL the
        word "licens" keyword-routed FATAL and rejected the task outright."""
        unlicensed = self.task.model_copy(update={"license": "unspecified"})
        self.engine.save_task(unlicensed)
        self.arm(unlicensed, [])
        outcome = cli.run_audit(self.engine, unlicensed)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertEqual(outcome.payload.data.get("blocked_on"), "human")
        self.assertIn("license is unresolved", outcome.payload.error)

    def test_happy_path_via_subcommands(self):
        self.arm(self.task, [BORDERLINE_A])

        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertIn(self.task.task_id, out)
        self.assertIn("pending=1", out)
        self.assertIn("approval=none", out)

        code, out = self.cli_main(
            "audit", "approve", self.task.task_id,
            "--reviewer", "wes", "--labels", "risk=low", "license=ok",
        )
        self.assertEqual(code, 0, msg=out)

        approval_path = self.workspace / "audit" / f"{self.task.task_id}.approval.json"
        self.assertTrue(approval_path.is_file())
        approval = AuditApproval.model_validate_json(
            approval_path.read_text(encoding="utf-8")
        )
        self.assertEqual(approval.reviewer, "wes")
        self.assertEqual(approval.task_content_hash, self.task.content_hash())
        self.assertEqual(
            approval.per_axis_labels, {"risk": "low", "license": "ok"}
        )
        self.assertEqual(len(approval.approved_collision_fingerprints), 1)

        outcome = cli.run_audit(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_PASS, msg=outcome.payload)
        self.assertEqual(outcome.payload.data.get("reviewer"), "wes")
        self.assertIn("signed off by wes", outcome.payload.detail)

        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertIn("approval=current", out)

    def test_stale_pre_repair_approval_never_passes(self):
        """REGRESSION: approve, then semantically mutate the task — the old
        approval is bound to the old hash and MUST fail the stage."""
        self.arm(self.task, [BORDERLINE_A])
        code, out = self.cli_main(
            "audit", "approve", self.task.task_id, "--reviewer", "wes"
        )
        self.assertEqual(code, 0, msg=out)

        repaired = self.task.model_copy(
            update={"title": self.task.title + " (repaired)"}
        )
        self.assertNotEqual(repaired.content_hash(), self.task.content_hash())
        self.engine.save_task(repaired)
        self.arm(repaired, [BORDERLINE_A])  # fresh evidence + gates at new hash

        outcome = cli.run_audit(self.engine, repaired)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertIn("STALE", outcome.payload.error)

        # Re-approving at the new hash unblocks the stage.
        code, out = self.cli_main(
            "audit", "approve", repaired.task_id, "--reviewer", "wes"
        )
        self.assertEqual(code, 0, msg=out)
        self.assertEqual(cli.run_audit(self.engine, repaired).verdict, VERDICT_PASS)

    def test_fingerprint_coverage_must_be_exact_missing(self):
        self.arm(self.task, [BORDERLINE_A])
        code, out = self.cli_main(
            "audit", "approve", self.task.task_id, "--reviewer", "wes"
        )
        self.assertEqual(code, 0, msg=out)

        # A new borderline collision appears after the sign-off.
        self.arm(self.task, [BORDERLINE_A, BORDERLINE_B])
        outcome = cli.run_audit(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertIn("does not cover", outcome.payload.error)

    def test_fingerprint_coverage_must_be_exact_extra(self):
        self.arm(self.task, [BORDERLINE_A])
        code, out = self.cli_main(
            "audit", "approve", self.task.task_id, "--reviewer", "wes"
        )
        self.assertEqual(code, 0, msg=out)

        # The approved collision is replaced by a DIFFERENT one.
        self.arm(self.task, [BORDERLINE_B])
        outcome = cli.run_audit(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertIn("does not cover", outcome.payload.error)
        self.assertIn("not pending", outcome.payload.error)

    def test_stale_evidence_fails_closed(self):
        self.arm(self.task, [])
        evidence = cli._evidence_dir(self.engine, self.task) / "contamination_post.json"
        recorded = json.loads(evidence.read_text(encoding="utf-8"))
        recorded["task_content_hash"] = "f" * 64
        evidence.write_text(canonical_json(recorded), encoding="utf-8")
        outcome = cli.run_audit(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertIn("stale", outcome.payload.error)

    def test_reject_subcommand_is_fatal_at_current_hash(self):
        self.arm(self.task, [BORDERLINE_A])
        code, out = self.cli_main(
            "audit", "reject", self.task.task_id, "--reason", "license provenance unclear"
        )
        self.assertEqual(code, 0, msg=out)
        outcome = cli.run_audit(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_FATAL)
        self.assertIn("license provenance unclear", outcome.payload.error)

    def test_stale_rejection_does_not_bind_the_repaired_task(self):
        self.arm(self.task, [])
        code, out = self.cli_main(
            "audit", "reject", self.task.task_id, "--reason", "old identity"
        )
        self.assertEqual(code, 0, msg=out)
        repaired = self.task.model_copy(
            update={"title": self.task.title + " (repaired)"}
        )
        self.engine.save_task(repaired)
        self.arm(repaired, [])
        outcome = cli.run_audit(self.engine, repaired)
        self.assertEqual(outcome.verdict, VERDICT_PASS, msg=outcome.payload)

    def test_approve_then_reject_and_back_are_mutually_exclusive(self):
        self.arm(self.task, [BORDERLINE_A])
        self.cli_main("audit", "approve", self.task.task_id, "--reviewer", "wes")
        self.cli_main("audit", "reject", self.task.task_id, "--reason", "changed my mind")
        audit_dir = self.workspace / "audit"
        self.assertFalse((audit_dir / f"{self.task.task_id}.approval.json").is_file())
        self.assertEqual(cli.run_audit(self.engine, self.task).verdict, VERDICT_FATAL)
        self.cli_main("audit", "approve", self.task.task_id, "--reviewer", "wes")
        self.assertFalse((audit_dir / f"{self.task.task_id}.rejection.json").is_file())
        self.assertEqual(cli.run_audit(self.engine, self.task).verdict, VERDICT_PASS)

    def test_audit_list_empty_queue(self):
        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertIn("audit queue empty", out)

    def test_malformed_labels_rejected(self):
        self.arm(self.task, [BORDERLINE_A])
        code, out = self.cli_main(
            "audit", "approve", self.task.task_id,
            "--reviewer", "wes", "--labels", "not-a-pair",
        )
        # A malformed flag is a USAGE error (2), never "task rejected" (1).
        self.assertEqual(code, 2)
        self.assertIn("key=value", out)


class TestAuditListRepairAdjudications(_AuditHarness):
    """docs/plans/bounded_agents_phase1.md §2, finding 1-7: `audit list` names
    repair adjudications — the empty-queue line and the REPAIR ADJUDICATION
    block of a task whose proposer abstained at the current hash."""

    def test_audit_list_names_repair_adjudications(self):
        from elt_taskgen.review import repair_proposer as rp

        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertIn(
            "audit queue empty: no task has pending borderline collisions, "
            "dual-build adjudications or repair adjudications",
            out,
        )
        record = rp.RepairAttemptRecord(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage="review",
            route="specification",
            status=rp.STATUS_NEEDS_ADJUDICATION,
            committed=False,
            attempts=(
                rp.ProposerAttempt(index=1, route="specification", artifact="task_ir.json",
                                   accepted=False, error_type="ScopeViolation"),
            ),
            detail="the proposer abstained after one rejected patch",
        )
        rp.queue_adjudication(self.workspace, self.task, record)
        self.engine.record_report(
            self.task,
            StageName.REVIEW.value,
            VERDICT_BLOCKED,
            StagePayload(detail="repair proposer awaiting adjudication"),
        )
        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertNotIn("audit queue empty", out)
        self.assertIn(f"{self.task.task_id}  hash={self.task.content_hash()[:12]}  pending=0", out)
        self.assertIn("REPAIR ADJUDICATION (not a sign-off item", out)
        self.assertIn("status=needs_adjudication stage=review route=specification attempts=1 codes=ScopeViolation", out)
        self.assertIn("the proposer abstained after one rejected patch", out)
        # Bound to the hash: a repaired task (another hash) lists nothing.
        moved = self.task.model_copy(update={"solver_prompt": self.task.solver_prompt + " Ties break by id."})
        self.engine.save_task(moved)
        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertIn("audit queue empty", out)

    def test_later_same_hash_pass_retires_repair_adjudication(self):
        """Append-only history remains on disk, but it is not a pending queue
        item after the stage that created it succeeds at the same identity."""
        from elt_taskgen.review import repair_proposer as rp

        record = rp.RepairAttemptRecord(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage="review",
            route="specification",
            status=rp.STATUS_NEEDS_ADJUDICATION,
            committed=False,
            attempts=(),
            detail="the proposer abstained",
        )
        path = rp.queue_adjudication(self.workspace, self.task, record)
        self.engine.record_report(
            self.task,
            StageName.REVIEW.value,
            VERDICT_BLOCKED,
            StagePayload(detail="repair proposer awaiting adjudication"),
        )
        self.assertIsNotNone(cli._pending_repair_adjudication(self.engine, self.task))

        self.engine.record_report(
            self.task,
            StageName.REVIEW.value,
            VERDICT_PASS,
            StagePayload(detail="re-measured successfully"),
        )
        self.assertIsNone(cli._pending_repair_adjudication(self.engine, self.task))
        self.assertTrue(path.is_file(), "retirement must not delete history")
        self.assertIsNotNone(rp.load_repair_adjudication(self.workspace, self.task.task_id))
        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertIn("audit queue empty", out)

    def test_crash_after_queue_write_before_blocked_keeps_adjudication_visible(self):
        """The queue file is durable before Engine appends its BLOCKED row."""
        from elt_taskgen.review import repair_proposer as rp

        record = rp.RepairAttemptRecord(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage="review",
            route="specification",
            status=rp.STATUS_NEEDS_ADJUDICATION,
            committed=False,
            attempts=(),
            detail="the proposer abstained before the process crashed",
        )
        failure_report_id = self.engine.record_report(
            self.task,
            StageName.REVIEW.value,
            VERDICT_FAIL,
            StagePayload(detail="the triggering review failure"),
        )
        path = rp.queue_adjudication(
            self.workspace,
            self.task,
            record,
            source_report_id=failure_report_id,
        )

        # Simulate process death here: no BLOCKED append exists.
        pending = cli._pending_repair_adjudication(self.engine, self.task)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["source_report_id"], failure_report_id)
        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertIn("REPAIR ADJUDICATION", out)

        self.engine.record_report(
            self.task,
            StageName.REVIEW.value,
            VERDICT_PASS,
            StagePayload(detail="re-measured successfully after restart"),
        )
        self.assertIsNone(cli._pending_repair_adjudication(self.engine, self.task))
        self.assertTrue(path.is_file(), "retirement must preserve append-only evidence")

    def test_later_same_hash_fatal_retires_repair_adjudication(self):
        from elt_taskgen.review import repair_proposer as rp

        record = rp.RepairAttemptRecord(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage="review",
            route="specification",
            status=rp.STATUS_NEEDS_ADJUDICATION,
            committed=False,
            detail="the proposer abstained",
        )
        source_id = self.engine.record_report(
            self.task,
            StageName.REVIEW.value,
            VERDICT_FAIL,
            StagePayload(detail="trigger"),
        )
        rp.queue_adjudication(
            self.workspace, self.task, record, source_report_id=source_id
        )
        self.assertIsNotNone(cli._pending_repair_adjudication(self.engine, self.task))
        self.engine.record_report(
            self.task,
            StageName.REVIEW.value,
            VERDICT_FATAL,
            StagePayload(detail="terminal rejection"),
        )
        self.assertIsNone(cli._pending_repair_adjudication(self.engine, self.task))

    def test_atomic_queue_replace_preserves_previous_complete_record_on_fault(self):
        from elt_taskgen.review import repair_proposer as rp

        first = rp.RepairAttemptRecord(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage="review",
            route="specification",
            status=rp.STATUS_NEEDS_ADJUDICATION,
            committed=False,
            detail="first complete record",
        )
        path = rp.queue_adjudication(self.workspace, self.task, first)
        before = path.read_bytes()
        replacement = first.model_copy(update={"detail": "replacement record"})
        with mock.patch.object(rp.os, "replace", side_effect=OSError("crash")):
            with self.assertRaisesRegex(OSError, "crash"):
                rp.queue_adjudication(self.workspace, self.task, replacement)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(rp.load_repair_adjudication(
            self.workspace, self.task.task_id
        )["detail"], "first complete record")
        self.assertEqual(
            list(path.parent.glob(f".{path.name}.stage-*.tmp")),
            [],
            "failed staging files should be cleaned on an ordinary write fault",
        )

    def test_source_report_id_rejects_bool_and_non_positive_values(self):
        from elt_taskgen.review import repair_proposer as rp

        record = rp.RepairAttemptRecord(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage="review",
            route="specification",
            status=rp.STATUS_NEEDS_ADJUDICATION,
            committed=False,
            detail="invalid sequence binding",
        )
        for invalid in (True, False, 0, -1):
            with self.subTest(source_report_id=invalid):
                with self.assertRaisesRegex(ValueError, "must be positive"):
                    rp.queue_adjudication(
                        self.workspace,
                        self.task,
                        record,
                        source_report_id=invalid,
                    )

    def test_corrupt_present_queue_fails_closed_in_audit_list(self):
        from elt_taskgen.review import repair_proposer as rp

        path = rp.repair_adjudication_path(self.workspace, self.task.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{incomplete", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(
                ["audit", "list", "--workspace", str(self.workspace)]
            )
        self.assertEqual(code, 2)
        self.assertNotIn("audit queue empty", stdout.getvalue())
        self.assertIn("RepairAdjudicationError", stderr.getvalue())
        self.assertIn("schema validation failed", stderr.getvalue())

    def test_unreadable_present_queue_raises_typed_engine_error(self):
        from elt_taskgen.review import repair_proposer as rp

        path = rp.repair_adjudication_path(self.workspace, self.task.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        original = Path.read_text

        def fail_only_queue(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError("denied")
            return original(candidate, *args, **kwargs)

        with mock.patch.object(Path, "read_text", fail_only_queue):
            with self.assertRaises(rp.RepairAdjudicationError):
                rp.load_repair_adjudication(self.workspace, self.task.task_id)

    def test_audit_list_renders_the_rejected_proposal_matrix(self):
        """Roadmap Phase 3 item 2 / A24: a rejected council proposal bound
        to the current hash is listed (not a sign-off item) with its
        post-session `projection_matrix` — the predicted and measured-pass
        booleans per population, promoted and fidelity — and never a
        reward float or the reason sentence; a moved hash lists nothing."""
        from elt_taskgen.models import PopulationName
        from elt_taskgen.verification import attacks

        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertIn("audit queue empty", out)
        predicted = {p.value: p.value in ("development", "stress") for p in PopulationName}
        measured_pass = {p.value: p.value != "primary" for p in PopulationName}
        outcome = attacks.PromotionOutcome(
            finding_id="population_adversary-00-deadbeef",
            case_name="proposed__population_adversary-00-deadbeef",
            kind="inner_join", promoted=False,
            reason="measured reward matrix does not match the proposed expectation on 2 population(s)",
            predicted=predicted, measured={p.value: (0.0 if p.value == "primary" else 1.0) for p in PopulationName},
            measured_pass=measured_pass, fidelity={"passed": True},
            mismatches=("counterfactual: predicted LOST, measured reward 1.0",),
        )
        attacks._record_rejected_proposal(self.workspace, self.task, outcome)
        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertNotIn("audit queue empty", out)
        self.assertIn(f"{self.task.task_id}  hash={self.task.content_hash()[:12]}  pending=0", out)
        self.assertIn("REJECTED PROPOSAL (not a sign-off item", out)
        self.assertIn("case=proposed__population_adversary-00-deadbeef", out)
        self.assertIn("finding=population_adversary-00-deadbeef", out)
        self.assertIn("promoted=false  fidelity_ok=true", out)
        self.assertIn("primary=predicted:false/measured_pass:false", out)
        self.assertIn("counterfactual=predicted:false/measured_pass:true", out)
        self.assertIn("development=predicted:true/measured_pass:true", out)
        for leak in ("1.0", "0.0", "measured reward", "does not match", "LOST"):
            self.assertNotIn(leak, out)
        # Bound to the hash: a repaired task (another hash) lists nothing.
        moved = self.task.model_copy(update={"solver_prompt": self.task.solver_prompt + " Ties break by id."})
        self.engine.save_task(moved)
        code, out = self.cli_main("audit", "list")
        self.assertEqual(code, 0)
        self.assertIn("audit queue empty", out)


if __name__ == "__main__":
    unittest.main()

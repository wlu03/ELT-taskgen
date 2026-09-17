"""Exact, append-only recovery of fatals emitted by obsolete pipeline code."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from pydantic import BaseModel, ConfigDict, ValidationError

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.engine import (
    FINAL_ACCEPTED,
    FINAL_IN_PROGRESS,
    FINAL_REJECTED,
    STAGE_ORDER,
    Engine,
    EngineError,
    StageName,
    StageOutcome,
    StagePayload,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
    RecoveryCodeIdentity,
    variant_gate_stage,
)
from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    AcceptanceReport,
    GateResult,
    PopulationName,
    RepairRoute,
    TaskStatus,
    canonical_json,
    variant_task_id,
)
from elt_taskgen.offline_recovery import (
    FatalRecoveryRequest,
    apply_recovery,
    main,
    prepare_recovery,
)
from elt_taskgen.reference.adjudication import (
    IndependentDisagreementAnalysis,
    MartDisagreement,
    RowDifference,
    persist_analysis,
    persist_diagnosis,
    persist_witness_error_decision,
)
from elt_taskgen.reference.independent import (
    STATUS_NEEDS_ADJUDICATION,
    IndependentBuildResult,
    IndependentSample,
)
from elt_taskgen.verification import gates as gates_mod
from elt_taskgen.verification import variant_battery


DECLARATIVE_PROSE = (
    Path(__file__).parent / "fixtures" / "declarative_prose.txt"
).read_text(encoding="utf-8")


class _GateFailurePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    gates: list[dict]


class _AttackFailurePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: str
    error: str = ""
    blocking_finding: dict


class FatalRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.task = demo_task().model_copy(
            update={
                "task_id": "recovery_exact_task",
                "solver_prompt": DECLARATIVE_PROSE,
            }
        )

    def _engine(self, *, author=None) -> Engine:
        def passed(stage: str):
            def runner(_engine, _task):
                return StageOutcome(VERDICT_PASS, StagePayload(detail=f"{stage} ok"))

            return runner

        runners = {stage.value: passed(stage.value) for stage in STAGE_ORDER}
        if author is not None:
            runners[StageName.AUTHOR.value] = author
        engine = Engine(
            self.workspace,
            stage_runners=runners,
            max_repair_rounds=0,
        )
        self.addCleanup(engine.close)
        return engine

    @staticmethod
    def _payload_sha(row) -> str:
        return hashlib.sha256(row.payload_json.encode("utf-8")).hexdigest()

    def _request(
        self,
        cause,
        target,
        *,
        recovery_kind: str = "author_prose_fidelity_false_fatal_v1",
        observation_paths: tuple[str, ...] = (),
    ) -> FatalRecoveryRequest:
        return FatalRecoveryRequest(
            task_id=target.task_id,
            recovery_kind=recovery_kind,
            stage=target.stage,
            cause_report_id=cause.id,
            cause_payload_sha256=self._payload_sha(cause),
            target_report_id=target.id,
            target_payload_sha256=self._payload_sha(target),
            observation_paths=observation_paths,
        )

    def _direct_fatal(self, stage: str = StageName.AUTHOR.value):
        engine = self._engine()
        engine.register(self.task)
        task = engine.load_task(self.task.task_id).with_status(TaskStatus.REJECTED)
        engine.save_task(task)
        prompt_sha256 = hashlib.sha256(task.solver_prompt.encode("utf-8")).hexdigest()
        cause_id = engine.record_report(
            task,
            stage,
            VERDICT_FAIL,
            StagePayload(
                error="prose fidelity: obsolete function-call finding",
                data={
                    "gate": "prose-fidelity",
                    "prose_sha256": prompt_sha256,
                    "source": "authored_revised",
                },
            ),
        )
        report_id = engine.record_report(
            task,
            stage,
            VERDICT_FATAL,
            StagePayload(
                detail="repair budget exhausted (0 rounds); rejecting",
                data={"failed_stage": "author", "route": "specification"},
            ),
        )
        cause = engine.report_by_id(cause_id)
        target = engine.report_by_id(report_id)
        assert cause is not None and target is not None
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_REJECTED)
        return engine, task, cause, target

    def _record_accepted_units(self, engine: Engine, task) -> None:
        for variant in RLVR_TASK_VARIANTS:
            roster = variant_battery.gate_roster(variant)
            report = AcceptanceReport.from_gates(
                task_id=variant_task_id(task.task_id, variant),
                revision=task.current_revision,
                task_content_hash=task.content_hash(),
                gates=tuple(
                    GateResult(gate=name, passed=True, details="measured green")
                    for name in roster
                ),
                scorer_version=gates_mod.SCORER_VERSION,
                roster_digest=gates_mod.ROSTER_DIGEST,
                roster=roster,
            )
            engine.record_report(
                task,
                variant_gate_stage(variant).value,
                VERDICT_PASS,
                report,
            )

    def _independent_fatal(self):
        engine = self._engine()
        engine.register(self.task)
        engine.run(self.task.task_id, until=StageName.ATTACK.value)
        task = engine.load_task(self.task.task_id).with_status(TaskStatus.REJECTED)
        engine.save_task(task)

        independent_path = (
            self.workspace
            / "tasks"
            / task.task_id
            / "reports"
            / "independent_build.json"
        )
        independent_path.parent.mkdir(parents=True, exist_ok=True)
        mart = task.marts[0]
        witness_sql = "SELECT 1 AS customer_id, 0 AS value"
        build = IndependentBuildResult(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            status=STATUS_NEEDS_ADJUDICATION,
            agreement={PopulationName.DEVELOPMENT.value: 1.0},
            samples=(
                IndependentSample(
                    sample_index=0,
                    prompt_sha256="1" * 64,
                    sql_by_mart={mart.name: witness_sql},
                    rewards={PopulationName.DEVELOPMENT.value: 1.0},
                    dev_pass=True,
                ),
            ),
            detail="fixture disagreement",
        )
        independent_path.write_text(
            canonical_json(build.model_dump(mode="json")),
            encoding="utf-8",
        )
        gold_path = (
            self.workspace / "tasks" / task.task_id / "answer_key" / "manifest.json"
        )
        gold_path.parent.mkdir(parents=True, exist_ok=True)
        gold_path.write_text(
            canonical_json(
                {
                    "task_id": task.task_id,
                    "task_content_hash": task.content_hash(),
                    "files": {},
                }
            ),
            encoding="utf-8",
        )
        analysis = IndependentDisagreementAnalysis(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            independent_build_sha256=hashlib.sha256(
                independent_path.read_bytes()
            ).hexdigest(),
            gold_manifest_sha256=hashlib.sha256(gold_path.read_bytes()).hexdigest(),
            witness_sql_sha256={
                mart.name: hashlib.sha256(witness_sql.encode("utf-8")).hexdigest()
            },
            analysis_status="differences_confirmed",
            marts=(
                MartDisagreement(
                    population=PopulationName.DEVELOPMENT,
                    mart=mart.name,
                    comparator_match=False,
                    expected_row_count=1,
                    actual_row_count=1,
                    differences=(
                        RowDifference(
                            kind="value_mismatch",
                            grain_key={mart.key_columns[0]: "1"},
                            expected={mart.key_columns[0]: "1", "value": "2"},
                            actual={mart.key_columns[0]: "1", "value": "0"},
                        ),
                    ),
                ),
            ),
        )
        analysis_path = persist_analysis(self.workspace, analysis)
        cause_id = engine.record_report(
            task,
            StageName.GATES.value,
            VERDICT_FAIL,
            _GateFailurePayload(
                accepted=False,
                gates=[
                    {
                        "gate": "trusted-solution",
                        "passed": False,
                        "details": (
                            "recorded status is 'needs_adjudication', not "
                            "'agreed' — pending human adjudication"
                        ),
                    }
                ],
            ),
        )
        target_id = engine.record_report(
            task,
            StageName.GATES.value,
            VERDICT_FATAL,
            StagePayload(
                detail="repair budget exhausted (0 rounds); rejecting",
                data={"failed_stage": "gates", "route": "specification"},
            ),
        )
        cause = engine.report_by_id(cause_id)
        target = engine.report_by_id(target_id)
        assert cause is not None and target is not None
        paths = (
            f"tasks/{task.task_id}/reports/independent_build.json",
            f"tasks/{task.task_id}/answer_key/manifest.json",
            analysis_path.relative_to(self.workspace).as_posix(),
        )
        return engine, task, cause, target, paths

    def _adjudicated_independent_fatal(self):
        engine, task, cause, target, paths = self._independent_fatal()
        analysis_path = self.workspace / paths[-1]
        diagnosis_path = persist_diagnosis(
            self.workspace,
            analysis_path,
            determined_cause="witness",
            determination_basis="The hand-checkable counterexample identifies the witness.",
        )
        decision_path = persist_witness_error_decision(
            self.workspace,
            task,
            analysis_path,
            diagnosis_path,
            adjudicator="fixture_operator",
            decision_basis="Obtain one new blind build; do not alter gold.",
        )
        return engine, task, cause, target, paths + (
            diagnosis_path.relative_to(self.workspace).as_posix(),
            decision_path.relative_to(self.workspace).as_posix(),
        )

    def _reserved_task(self):
        old = "customers"
        new = "group"
        task = self.task
        return task.model_copy(
            update={
                "tables": tuple(
                    table.model_copy(update={"name": new})
                    if table.name == old
                    else table
                    for table in task.tables
                ),
                "backends": tuple(
                    backend.model_copy(update={"table": new})
                    if backend.table == old
                    else backend
                    for backend in task.backends
                ),
                "relationships": tuple(
                    relationship.model_copy(
                        update={
                            "child_table": (
                                new if relationship.child_table == old else relationship.child_table
                            ),
                            "parent_table": (
                                new
                                if relationship.parent_table == old
                                else relationship.parent_table
                            ),
                        }
                    )
                    for relationship in task.relationships
                ),
                "marts": tuple(
                    mart.model_copy(
                        update={
                            "plan": mart.plan.model_copy(
                                update={
                                    "ops": tuple(
                                        op.model_copy(
                                            update={
                                                "tables": tuple(
                                                    new if name == old else name
                                                    for name in op.tables
                                                )
                                            }
                                        )
                                        for op in mart.plan.ops
                                    )
                                }
                            )
                        }
                    )
                    for mart in task.marts
                ),
                "populations": tuple(
                    population.model_copy(
                        update={
                            "scale": {
                                (new if name == old else name): count
                                for name, count in population.scale.items()
                            },
                            "literal_rows": {
                                (new if name == old else name): rows
                                for name, rows in population.literal_rows.items()
                            },
                        }
                    )
                    for population in task.populations
                ),
            }
        )

    def test_prepare_is_read_only_and_apply_appends_fail_idempotently(self):
        engine, task, cause, fatal = self._direct_fatal()
        before = tuple(engine.report_history(task.task_id, fatal.stage))

        prepared = prepare_recovery(self.workspace, self._request(cause, fatal))
        self.assertFalse(prepared.ledger_appended)
        self.assertEqual(
            tuple(engine.report_history(task.task_id, fatal.stage)),
            before,
        )
        self.assertTrue((self.workspace / prepared.evidence_path).is_file())

        applied = apply_recovery(self.workspace, prepared.evidence_path)
        self.assertTrue(applied.ledger_appended)
        latest = engine.latest_report(task.task_id, fatal.stage)
        assert latest is not None
        self.assertEqual(latest.verdict, VERDICT_FAIL)
        self.assertGreater(latest.id, fatal.id)
        self.assertEqual(engine.report_by_id(fatal.id), fatal)
        self.assertEqual(engine.load_task(task.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_IN_PROGRESS)

        history_size = len(engine.report_history(task.task_id, fatal.stage))
        replay = apply_recovery(self.workspace, prepared.evidence_path)
        self.assertFalse(replay.ledger_appended)
        self.assertEqual(
            len(engine.report_history(task.task_id, fatal.stage)),
            history_size,
        )

    def test_module_cli_prepares_one_exact_request_without_applying_it(self):
        engine, task, cause, fatal = self._direct_fatal()
        request_path = self.workspace / "request.json"
        request_path.write_text(
            self._request(cause, fatal).model_dump_json(),
            encoding="utf-8",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "prepare",
                    "--workspace",
                    str(self.workspace),
                    "--request",
                    str(request_path),
                ]
            )
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ledger_appended"])
        self.assertTrue((self.workspace / result["evidence_path"]).is_file())
        self.assertEqual(
            engine.latest_report(task.task_id, fatal.stage).id,
            fatal.id,
        )

    def test_reopened_fail_requires_a_genuine_stage_pass(self):
        counter = Counter()

        def obsolete_author(_engine, task):
            counter["author"] += 1
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(
                    error="prose fidelity: obsolete function-call finding",
                    data={
                        "gate": "prose-fidelity",
                        "prose_sha256": hashlib.sha256(
                            task.solver_prompt.encode("utf-8")
                        ).hexdigest(),
                        "source": "authored_revised",
                    },
                ),
                route=RepairRoute.SPECIFICATION,
            )

        engine = self._engine(author=obsolete_author)
        engine.register(self.task)
        engine.run(self.task.task_id, until=StageName.AUTHOR.value)
        fatal = engine.latest_report(self.task.task_id, StageName.AUTHOR.value)
        assert fatal is not None
        self.assertEqual(fatal.verdict, VERDICT_FATAL)
        cause = next(
            row
            for row in engine.report_history(self.task.task_id, StageName.AUTHOR.value)
            if row.verdict == VERDICT_FAIL
        )
        prepared = prepare_recovery(self.workspace, self._request(cause, fatal))
        apply_recovery(self.workspace, prepared.evidence_path)

        def fixed_author(_engine, _task):
            counter["author"] += 1
            return StageOutcome(VERDICT_PASS, StagePayload(detail="measured green"))

        resumed = self._engine(author=fixed_author)
        resumed.run(self.task.task_id, until=StageName.AUTHOR.value)
        latest = resumed.latest_report(self.task.task_id, StageName.AUTHOR.value)
        assert latest is not None
        self.assertEqual(latest.verdict, VERDICT_PASS)
        self.assertEqual(counter["author"], 2)
        self.assertEqual(resumed.load_task(self.task.task_id).status, TaskStatus.AUTHORED)

    def test_incomplete_author_draft_is_reopened_only_for_regeneration(self):
        engine = self._engine()
        incomplete = self.task.model_copy(
            update={
                "task_id": "recovery_incomplete_author_task",
                "solver_prompt": "This draft names customers but omits the mart rules.",
            }
        )
        engine.register(incomplete)
        task = engine.load_task(incomplete.task_id)
        prompt_sha256 = hashlib.sha256(
            task.solver_prompt.encode("utf-8")
        ).hexdigest()
        cause_id = engine.record_report(
            task,
            StageName.AUTHOR.value,
            VERDICT_FAIL,
            StagePayload(
                error="prose fidelity: required TaskIR semantics are missing",
                data={
                    "gate": "prose-fidelity",
                    "prose_sha256": prompt_sha256,
                    "source": "authored_revised",
                },
            ),
        )
        target_id = engine.record_report(
            task,
            StageName.AUTHOR.value,
            VERDICT_FATAL,
            StagePayload(
                detail="repair budget exhausted (0 rounds); rejecting",
                data={"failed_stage": "author", "route": "specification"},
            ),
        )
        cause = engine.report_by_id(cause_id)
        target = engine.report_by_id(target_id)
        assert cause is not None and target is not None
        prepared = prepare_recovery(
            self.workspace,
            self._request(
                cause,
                target,
                recovery_kind="author_instruction_regeneration_v1",
            ),
        )
        evidence = json.loads(
            (self.workspace / prepared.evidence_path).read_text(encoding="utf-8")
        )
        self.assertGreater(
            int(evidence["certification"]["current_finding_count"]),
            0,
        )
        applied = apply_recovery(self.workspace, prepared.evidence_path)
        self.assertTrue(applied.ledger_appended)
        latest = engine.latest_report(task.task_id, StageName.AUTHOR.value)
        assert latest is not None
        self.assertEqual(latest.verdict, VERDICT_FAIL)
        payload = json.loads(latest.payload_json)
        self.assertEqual(
            payload["data"]["failure_code"],
            "incomplete_author_prose_requires_regeneration",
        )
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_IN_PROGRESS)

    def test_old_acceptance_cannot_substitute_for_post_recovery_measurement(self):
        engine = self._engine()
        engine.register(self.task)
        task = engine.load_task(self.task.task_id)
        engine.record_report(
            task,
            StageName.AUTHOR.value,
            VERDICT_PASS,
            StagePayload(detail="old measured author pass"),
        )
        self._record_accepted_units(engine, task)
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_ACCEPTED)

        prompt_sha256 = hashlib.sha256(task.solver_prompt.encode("utf-8")).hexdigest()
        cause_id = engine.record_report(
            task,
            StageName.AUTHOR.value,
            VERDICT_FAIL,
            StagePayload(
                error="prose fidelity: obsolete function-call finding",
                data={
                    "gate": "prose-fidelity",
                    "prose_sha256": prompt_sha256,
                    "source": "authored_revised",
                },
            ),
        )
        fatal_id = engine.record_report(
            task,
            StageName.AUTHOR.value,
            VERDICT_FATAL,
            StagePayload(
                detail="repair budget exhausted (0 rounds); rejecting",
                data={"failed_stage": "author", "route": "specification"},
            ),
        )
        cause = engine.report_by_id(cause_id)
        fatal = engine.report_by_id(fatal_id)
        assert cause is not None and fatal is not None
        prepared = prepare_recovery(self.workspace, self._request(cause, fatal))
        apply_recovery(self.workspace, prepared.evidence_path)
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_IN_PROGRESS)

        engine.record_report(
            task,
            StageName.AUTHOR.value,
            VERDICT_PASS,
            StagePayload(detail="new genuine author measurement"),
        )
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_ACCEPTED)

    def test_blocked_recovery_is_guarded_pending_adjudication(self):
        counter = Counter()
        engine, task, cause, fatal, observation_paths = self._independent_fatal()
        prepared = prepare_recovery(
            self.workspace,
            self._request(
                cause,
                fatal,
                recovery_kind="independent_gold_disagreement_pending_v1",
                observation_paths=observation_paths,
            ),
        )
        apply_recovery(self.workspace, prepared.evidence_path)
        latest = engine.latest_report(self.task.task_id, fatal.stage)
        assert latest is not None
        self.assertEqual(latest.verdict, VERDICT_BLOCKED)
        payload = json.loads(latest.payload_json)
        self.assertEqual(payload["data"]["failure_class"], "pending_adjudication")
        self.assertEqual(payload["data"]["blocked_on"], "human")
        self.assertEqual(payload["data"]["retry_guard"], "explicit")

        def gates_should_not_run(_engine, _task):
            counter["gates"] += 1
            return StageOutcome(VERDICT_PASS, StagePayload(detail="not reached"))

        resumed = self._engine()
        resumed.set_stage_runner(StageName.GATES, gates_should_not_run)
        resumed.run(task.task_id, until=StageName.GATES.value)
        self.assertEqual(counter["gates"], 0)  # unchanged hold was not polled
        self.assertEqual(resumed.final_verdict(task.task_id), FINAL_IN_PROGRESS)

    def test_bound_witness_error_decision_reopens_only_gates_as_fail(self):
        engine, task, cause, fatal, observation_paths = (
            self._adjudicated_independent_fatal()
        )
        prepared = prepare_recovery(
            self.workspace,
            self._request(
                cause,
                fatal,
                recovery_kind="independent_witness_error_adjudicated_v1",
                observation_paths=observation_paths,
            ),
        )
        self.assertEqual(prepared.disposition, VERDICT_FAIL)
        apply_recovery(self.workspace, prepared.evidence_path)
        latest = engine.latest_report(task.task_id, fatal.stage)
        assert latest is not None
        self.assertEqual(latest.verdict, VERDICT_FAIL)
        payload = json.loads(latest.payload_json)
        self.assertEqual(
            payload["data"]["failure_code"],
            "independent_witness_error_adjudicated",
        )
        self.assertEqual(
            payload["data"]["recovery_prerequisite"],
            "rerun_gates_for_one_fresh_blind_independent_build",
        )
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_IN_PROGRESS)

    def test_adjudicated_recovery_refuses_a_tampered_decision(self):
        _engine, _task, cause, fatal, observation_paths = (
            self._adjudicated_independent_fatal()
        )
        decision_path = self.workspace / observation_paths[-1]
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        decision["action"] = "invent_an_attack"
        decision_path.write_text(canonical_json(decision), encoding="utf-8")
        with self.assertRaisesRegex(
            EngineError,
            "filename is not content-addressed|decision is malformed",
        ):
            prepare_recovery(
                self.workspace,
                self._request(
                    cause,
                    fatal,
                    recovery_kind="independent_witness_error_adjudicated_v1",
                    observation_paths=observation_paths,
                ),
            )

    def test_recovery_ignores_only_exact_target_fatal(self):
        engine, task, cause, author_fatal = self._direct_fatal()
        other_id = engine.record_report(
            task,
            StageName.REVIEW.value,
            VERDICT_FATAL,
            StagePayload(error="a separate real fatal"),
        )
        other = engine.report_by_id(other_id)
        assert other is not None
        prepared = prepare_recovery(
            self.workspace,
            self._request(cause, author_fatal),
        )
        apply_recovery(self.workspace, prepared.evidence_path)
        self.assertTrue(engine.fatal_report_is_recovered(task, author_fatal))
        self.assertFalse(engine.fatal_report_is_recovered(task, other))
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_REJECTED)

    def test_tampered_observation_invalidates_recovery(self):
        engine, task, cause, fatal, observation_paths = self._independent_fatal()
        prepared = prepare_recovery(
            self.workspace,
            self._request(
                cause,
                fatal,
                recovery_kind="independent_gold_disagreement_pending_v1",
                observation_paths=observation_paths,
            ),
        )
        apply_recovery(self.workspace, prepared.evidence_path)
        observation = self.workspace / observation_paths[-1]
        observation.write_text('{"decision":"changed"}\n', encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "observation changed"):
            engine.final_verdict(task.task_id)

    def test_fixed_code_change_invalidates_recovery(self):
        engine, task, cause, fatal = self._direct_fatal()
        prepared = prepare_recovery(self.workspace, self._request(cause, fatal))
        apply_recovery(self.workspace, prepared.evidence_path)
        original = __import__(
            "elt_taskgen.engine", fromlist=["recovery_code_identity"]
        ).recovery_code_identity

        def changed(module: str):
            identity = original(module)
            if module == "elt_taskgen.review.prose_fidelity":
                return RecoveryCodeIdentity(module=module, sha256="f" * 64)
            return identity

        with mock.patch("elt_taskgen.engine.recovery_code_identity", changed):
            with self.assertRaisesRegex(EngineError, "fixed code identity changed"):
                engine.final_verdict(task.task_id)

    def test_wrong_payload_and_pass_disposition_are_refused(self):
        _engine, _task, cause, fatal = self._direct_fatal()
        invalid = self._request(cause, fatal).model_copy(
            update={"target_payload_sha256": "0" * 64}
        )
        with self.assertRaisesRegex(EngineError, "exact current fatal"):
            prepare_recovery(self.workspace, invalid)
        payload = self._request(cause, fatal).model_dump(mode="json")
        payload["recovery_kind"] = "caller_selected_arbitrary_bypass"
        arbitrary = FatalRecoveryRequest.model_validate(payload)
        with self.assertRaisesRegex(EngineError, "closed analyzer registry"):
            prepare_recovery(self.workspace, arbitrary)
        payload["unknown_authority"] = "pass"
        with self.assertRaises(ValidationError):
            FatalRecoveryRequest.model_validate(payload)

    def test_closed_analyzer_refuses_an_arbitrary_exact_fatal(self):
        engine = self._engine()
        engine.register(self.task)
        task = engine.load_task(self.task.task_id)
        cause_id = engine.record_report(
            task,
            StageName.AUTHOR.value,
            VERDICT_FAIL,
            StagePayload(error="caller says this was a pipeline bug"),
        )
        target_id = engine.record_report(
            task,
            StageName.AUTHOR.value,
            VERDICT_FATAL,
            StagePayload(
                detail="repair budget exhausted (0 rounds); rejecting",
                data={"failed_stage": "author", "route": "specification"},
            ),
        )
        cause = engine.report_by_id(cause_id)
        target = engine.report_by_id(target_id)
        assert cause is not None and target is not None
        request = self._request(cause, target)
        with self.assertRaisesRegex(EngineError, "persisted prose-fidelity"):
            prepare_recovery(self.workspace, request)

    def test_closed_handoff_analyzer_reopens_only_saved_protocol_shape(self):
        engine = self._engine()
        engine.register(self.task)
        task = engine.load_task(self.task.task_id)
        cause_id = engine.record_report(
            task,
            StageName.ATTACK.value,
            VERDICT_FAIL,
            _AttackFailurePayload(
                detail=(
                    "critic-to-mutation handoff BLOCKED: ambiguity_critic-00: "
                    "major finding has no structured proposed_case; it is unresolved"
                ),
                blocking_finding={
                    "identifiers": ["customer_id"],
                    "role": "ambiguity_critic",
                    "severity": "major",
                },
            ),
        )
        target_id = engine.record_report(
            task,
            StageName.ATTACK.value,
            VERDICT_FATAL,
            StagePayload(
                detail="repair budget exhausted (0 rounds); rejecting",
                data={"failed_stage": "attack", "route": "specification"},
            ),
        )
        cause = engine.report_by_id(cause_id)
        target = engine.report_by_id(target_id)
        assert cause is not None and target is not None
        request = self._request(
            cause,
            target,
            recovery_kind="critic_attack_handoff_misclassified_fatal_v1",
        )
        prepared = prepare_recovery(self.workspace, request)
        apply_recovery(self.workspace, prepared.evidence_path)
        self.assertEqual(
            engine.latest_report(task.task_id, StageName.ATTACK.value).verdict,
            VERDICT_FAIL,
        )
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_IN_PROGRESS)

    def test_reserved_identifier_direct_fatal_uses_closed_filter_analyzer(self):
        engine = self._engine()
        task = self._reserved_task()
        engine.register(task)
        task = engine.load_task(task.task_id)
        fatal_id = engine.record_report(
            task,
            StageName.CONTAMINATION_PRE.value,
            VERDICT_FATAL,
            StagePayload(
                error=(
                    "intake-format-blacklist filter: blacklisted candidate "
                    "shape: reserved-word-relation-name"
                ),
                data={
                    "action": "reject",
                    "filter": "intake-format-blacklist",
                    "findings": "1",
                },
            ),
        )
        fatal = engine.report_by_id(fatal_id)
        assert fatal is not None
        prepared = prepare_recovery(
            self.workspace,
            self._request(
                fatal,
                fatal,
                recovery_kind="reserved_identifier_filter_false_fatal_v1",
            ),
        )
        apply_recovery(self.workspace, prepared.evidence_path)
        latest = engine.latest_report(task.task_id, StageName.CONTAMINATION_PRE.value)
        assert latest is not None
        self.assertEqual(latest.verdict, VERDICT_FAIL)
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_IN_PROGRESS)


if __name__ == "__main__":
    unittest.main()

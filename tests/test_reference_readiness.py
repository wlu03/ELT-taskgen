from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.generation.mart_plan import column_kinds_from_plan
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    GateResult,
    MartColumnKind,
    MartOp,
    MartOpKind,
    PopulationName,
    TableSpec,
)
from elt_taskgen.verification import gates
from elt_taskgen.verification.reference_readiness import (
    CHALLENGING_REFERENCE_READINESS_ROSTER,
    REFERENCE_READINESS_EVIDENCE_REL,
    REFERENCE_READINESS_ROSTER,
    _challenging_mart_contract_gate,
    _effective_lineage_gate,
    ensure_reference_readiness_evidence,
    run_reference_readiness,
)


def _pass(name: str) -> GateResult:
    return GateResult(gate=name, passed=True, details="measured")


def _task_with_dead_table(*, source_declared: bool):
    task = demo_task()
    audit = TableSpec(
        name="audit_log",
        description="A source used only by extract/load.",
        primary_key=("event_id",),
        columns=(
            ColumnSpec(
                name="event_id",
                type=ColumnType.BIGINT,
                nullable=False,
                description="Audit event identifier.",
            ),
        ),
    )
    marts = task.marts
    if source_declared:
        mart = marts[0]
        source = MartOp(
            kind=MartOpKind.SOURCE,
            description="Read source table audit_log for extract/load.",
            tables=("audit_log",),
        )
        marts = (
            mart.model_copy(
                update={
                    "plan": mart.plan.model_copy(
                        update={"ops": (source,) + mart.plan.ops}
                    )
                }
            ),
        ) + marts[1:]
    return task.model_copy(
        update={
            "tables": task.tables + (audit,),
            "backends": task.backends
            + (BackendAssignment(table="audit_log", backend=Backend.FILES),),
            "marts": marts,
        }
    )


def _stage1_gold(task):
    return SimpleNamespace(
        task_id=task.task_id,
        stage1={
            population.value: {table.name: 1 for table in task.tables}
            for population in PopulationName
        }
    )


class ReferenceReadinessTests(unittest.TestCase):
    @staticmethod
    def _classified_task():
        task = demo_task()
        marts = []
        for mart in task.marts:
            implied = column_kinds_from_plan(mart.plan)
            columns = tuple(
                column.model_copy(
                    update={
                        "kind": implied.get(
                            column.name, MartColumnKind.PASSTHROUGH
                        )
                    }
                )
                for column in mart.columns
            )
            marts.append(mart.model_copy(update={"columns": columns}))
        return task.model_copy(update={"marts": tuple(marts)})

    def test_effective_lineage_allows_positively_graded_el_only_table(self) -> None:
        task = _task_with_dead_table(source_declared=True)

        gate = _effective_lineage_gate(task, _stage1_gold(task))

        self.assertTrue(gate.passed, gate.details)
        self.assertEqual(gate.evidence["extract_load_only_tables"], "audit_log")
        self.assertEqual(gate.evidence["unpoliced_tables"], "")
        self.assertIn("1 additional SOURCE-declared table", gate.details)

    def test_effective_lineage_rejects_undeclared_dead_table(self) -> None:
        task = _task_with_dead_table(source_declared=False)

        gate = _effective_lineage_gate(task, _stage1_gold(task))

        self.assertFalse(gate.passed)
        self.assertEqual(gate.evidence["unpoliced_tables"], "audit_log")
        self.assertIn("not declared by any SOURCE operation: audit_log", gate.details)

    def test_effective_lineage_rejects_ungraded_declared_dead_table(self) -> None:
        task = _task_with_dead_table(source_declared=True)
        gold = _stage1_gold(task)
        del gold.stage1[PopulationName.PRIMARY.value]["audit_log"]

        gate = _effective_lineage_gate(task, gold)

        self.assertFalse(gate.passed)
        self.assertEqual(gate.evidence["unpoliced_tables"], "audit_log")
        self.assertIn("not present with valid counts", gate.details)

    def test_effective_lineage_rejects_stage1_from_another_task(self) -> None:
        task = _task_with_dead_table(source_declared=True)
        gold = _stage1_gold(task)
        gold.task_id = "different_task"

        gate = _effective_lineage_gate(task, gold)

        self.assertFalse(gate.passed)
        self.assertEqual(gate.evidence["stage1_gold_task_bound"], "false")
        self.assertEqual(gate.evidence["unpoliced_tables"], "audit_log")

    def test_effective_lineage_rejects_zero_signal_declared_dead_table(self) -> None:
        task = _task_with_dead_table(source_declared=True)
        gold = _stage1_gold(task)
        for population in gates.GRADED_POPULATIONS:
            gold.stage1[population.value]["audit_log"] = 0

        gate = _effective_lineage_gate(task, gold)

        self.assertFalse(gate.passed)
        self.assertEqual(gate.evidence["unpoliced_tables"], "audit_log")
        self.assertIn("positive in at least one graded population", gate.details)

    def test_readiness_passes_gold_to_effective_lineage_gate(self) -> None:
        task = demo_task()
        gold = object()

        def guarded(name, fn):
            return fn() if name == "effective-lineage" else _pass(name)

        with patch(
            "elt_taskgen.verification.reference_readiness.gates._guarded",
            side_effect=guarded,
        ), patch(
            "elt_taskgen.verification.reference_readiness._effective_lineage_gate",
            return_value=_pass("effective-lineage"),
        ) as lineage_gate:
            report = run_reference_readiness(
                task, Path("unused"), gold, record=False
            )

        self.assertTrue(report.accepted)
        lineage_gate.assert_called_once_with(task, gold)

    def test_roster_is_fail_closed_and_recorded(self) -> None:
        task = demo_task().with_revision(route=None, reason="intake")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            patches = (
                patch(
                    "elt_taskgen.verification.gates._gate_determinism",
                    return_value=_pass("determinism"),
                ),
                patch(
                    "elt_taskgen.verification.gates._gate_data_sensitivity",
                    return_value=_pass("data-sensitivity"),
                ),
                patch(
                    "elt_taskgen.verification.gates._gate_t_info_content",
                    return_value=_pass("info-content"),
                ),
                patch(
                    "elt_taskgen.verification.gates._gate_populations_load",
                    return_value=_pass("populations-load"),
                ),
                patch(
                    "elt_taskgen.verification.gates._gate_referential_integrity",
                    return_value=_pass("referential-integrity"),
                ),
                patch(
                    "elt_taskgen.verification.gates._gate_declared_scale_reconciliation",
                    return_value=_pass("declared-scale-reconciliation"),
                ),
                patch(
                    "elt_taskgen.verification.gates._gate_mart_key_unique",
                    return_value=_pass("mart-key-unique"),
                ),
                patch(
                    "elt_taskgen.verification.gates._gate_transform_surface",
                    return_value=_pass("transform-surface"),
                ),
                patch(
                    "elt_taskgen.verification.reference_readiness._effective_lineage_gate",
                    return_value=_pass("effective-lineage"),
                ),
            )
            for context in patches:
                context.start()
            try:
                report = run_reference_readiness(task, workspace, object())
            finally:
                for context in reversed(patches):
                    context.stop()

            self.assertTrue(report.accepted)
            self.assertEqual(report.roster, REFERENCE_READINESS_ROSTER)
            self.assertEqual(
                tuple(gate.gate for gate in report.gates),
                REFERENCE_READINESS_ROSTER,
            )
            path = workspace / "tasks" / task.task_id / REFERENCE_READINESS_EVIDENCE_REL
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["accepted"])
            self.assertEqual(payload["roster"], list(REFERENCE_READINESS_ROSTER))

    def test_one_failed_measurement_rejects_readiness(self) -> None:
        task = demo_task().with_revision(route=None, reason="intake")
        with tempfile.TemporaryDirectory() as tmp, patch(
            "elt_taskgen.verification.gates._gate_determinism",
            return_value=GateResult(
                gate="determinism", passed=False, details="missing evidence"
            ),
        ), patch(
            "elt_taskgen.verification.gates._gate_data_sensitivity",
            return_value=_pass("data-sensitivity"),
        ), patch(
            "elt_taskgen.verification.gates._gate_t_info_content",
            return_value=_pass("info-content"),
        ), patch(
            "elt_taskgen.verification.gates._gate_populations_load",
            return_value=_pass("populations-load"),
        ), patch(
            "elt_taskgen.verification.gates._gate_referential_integrity",
            return_value=_pass("referential-integrity"),
        ), patch(
            "elt_taskgen.verification.gates._gate_declared_scale_reconciliation",
            return_value=_pass("declared-scale-reconciliation"),
        ), patch(
            "elt_taskgen.verification.gates._gate_mart_key_unique",
            return_value=_pass("mart-key-unique"),
        ), patch(
            "elt_taskgen.verification.gates._gate_transform_surface",
            return_value=_pass("transform-surface"),
        ), patch(
            "elt_taskgen.verification.reference_readiness._effective_lineage_gate",
            return_value=_pass("effective-lineage"),
        ):
            report = run_reference_readiness(task, Path(tmp), object(), record=False)
        self.assertFalse(report.accepted)

    def test_perturbation_probe_is_created_only_when_required(self) -> None:
        task = demo_task()
        gold = object()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "elt_taskgen.verification.reference_readiness.gates.needs_perturbation_probe",
            return_value=True,
        ), patch(
            "elt_taskgen.verification.reference_readiness.perturbation.record_perturbation_probe",
            return_value=Path(tmp) / "probe.json",
        ) as record:
            paths = ensure_reference_readiness_evidence(task, Path(tmp), gold)
            self.assertEqual(paths, (Path(tmp) / "probe.json",))
            record.assert_called_once_with(task, Path(tmp), gold)
        # the "only" half of the name: no probe when the gate does not need one
        with tempfile.TemporaryDirectory() as tmp, patch(
            "elt_taskgen.verification.reference_readiness.gates.needs_perturbation_probe",
            return_value=False,
        ), patch(
            "elt_taskgen.verification.reference_readiness.perturbation.record_perturbation_probe",
        ) as record:
            paths = ensure_reference_readiness_evidence(task, Path(tmp), gold)
            self.assertEqual(paths, ())
            record.assert_not_called()

    def test_challenging_mart_contract_rejects_unclassified_columns(self) -> None:
        gate = _challenging_mart_contract_gate(demo_task())
        self.assertFalse(gate.passed)
        self.assertIn("unclassified target columns", gate.details)

    def test_challenging_mart_contract_rejects_each_shallow_mart(self) -> None:
        task = self._classified_task()
        mart = task.marts[0]
        shallow = mart.model_copy(update={"columns": mart.columns[:2]})
        task = task.model_copy(update={"marts": (shallow,)})
        gate = _challenging_mart_contract_gate(task)
        self.assertFalse(gate.passed)
        self.assertIn("below the challenging minimum of 6", gate.details)

    def test_challenging_readiness_stays_offline_before_full_acceptance(self) -> None:
        task = self._classified_task().with_revision(route=None, reason="intake")
        measured: list[str] = []

        def guarded(name, _fn):
            measured.append(name)
            return _pass(name)

        with tempfile.TemporaryDirectory() as tmp, patch(
            "elt_taskgen.verification.reference_readiness.gates._guarded",
            side_effect=guarded,
        ):
            report = run_reference_readiness(
                task, Path(tmp), object(), record=False, challenging=True
            )
        self.assertTrue(report.accepted)
        self.assertEqual(report.roster, CHALLENGING_REFERENCE_READINESS_ROSTER)
        self.assertEqual(
            tuple(gate.gate for gate in report.gates),
            CHALLENGING_REFERENCE_READINESS_ROSTER,
        )
        self.assertNotIn("dual-build-agreement", measured)
        self.assertEqual(measured, list(CHALLENGING_REFERENCE_READINESS_ROSTER))

    def test_independent_reconstruction_requires_exact_nonempty_mart_sql(self) -> None:
        task = self._classified_task()
        rewards = {population.name.value: 1.0 for population in task.populations}
        record = {
            "role": gates.DUAL_BUILD_ROLE,
            "status": gates.DUAL_BUILD_STATUS_AGREED,
            "agreement": dict(rewards),
            "samples": [{"rewards": dict(rewards)}],
        }
        problems = gates._transform_reconstruction_problems(task, record)
        self.assertTrue(any("no 'sql_by_mart' reconstruction" in p for p in problems))

        record["samples"][0]["sql_by_mart"] = {
            task.marts[0].name: "SELECT 1"
        }
        extra = task.marts[0].model_copy(update={"name": "another_mart"})
        two_mart_task = task.model_copy(update={"marts": task.marts + (extra,)})
        problems = gates._transform_reconstruction_problems(two_mart_task, record)
        self.assertTrue(any("mart roster differs" in p for p in problems))

        record["samples"][0]["sql_by_mart"] = {
            mart.name: "SELECT 1" for mart in task.marts
        }
        self.assertEqual(gates._transform_reconstruction_problems(task, record), [])


if __name__ == "__main__":
    unittest.main()

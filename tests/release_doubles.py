"""Ledger/selection doubles shared by release-freezing tests and fixtures.

Moved VERBATIM out of tests/test_variants.py so the semantic-gate fixture
maker (tests/make_semantic_gate_fixture.py) can freeze a real release through
exactly the doubles the release tests already trust — zero behavior change.
"""

from __future__ import annotations

import json
from pathlib import Path

from elt_taskgen.engine import variant_gate_stage
from elt_taskgen.export import eltbench
from elt_taskgen.models import TaskVariant, variant_task_id
from elt_taskgen.verification.gates import (
    GATE_NAMES,
    VARIANT_GATE_NAMES as variant_gate_names_map,
)


def _census_evidence(workspace: Path, task) -> dict[str, str]:
    """The `warehouses-load` evidence a real T battery records.

    Release reconciles every shipped .duckdb against the CERTIFIED census —
    the evidence file bound to the content hash AND this ledger evidence — so
    a double that recorded no census would be asserting a freeze-time
    self-attestation, the exact hole the reconciliation closes.
    """
    path = workspace / "tasks" / task.task_id / eltbench.WAREHOUSE_CENSUS_EVIDENCE_REL
    if not path.is_file():
        return {}
    record = json.loads(path.read_text(encoding="utf-8"))
    evidence: dict[str, str] = {}
    for pop, entry in (record.get("populations") or {}).items():
        digest = str(entry.get("census_digest") or "")
        evidence[f"census:{pop}"] = digest[:16]
        evidence[f"census_digest:{pop}"] = digest
    return evidence


class FakeReport:
    def __init__(
        self,
        verdict: str,
        content_hash: str,
        gates=None,
        task_id=None,
        evidence_by_gate=None,
        scorer_version=None,
        roster_digest=None,
    ):
        from elt_taskgen.verification import gates as gates_mod

        self.verdict = verdict
        self.content_hash = content_hash
        names = GATE_NAMES if gates is None else tuple(gates)
        evidence_by_gate = evidence_by_gate or {}
        # Release verifies GATE COVERAGE, not just the verdict string (a
        # battery missing shortcut-probes / dual-build-agreement is not an
        # acceptance), and it verifies WHICH INSTRUMENT measured it (scorer
        # version + roster digest): evidence from another roster is refused
        # with "re-run", never accepted. So the double records all of it.
        payload = {
            "gates": [
                {
                    "gate": g,
                    "passed": True,
                    **(
                        {"evidence": evidence_by_gate[g]}
                        if g in evidence_by_gate
                        else {}
                    ),
                }
                for g in names
            ],
            "accepted": True,
            "scorer_version": (
                gates_mod.SCORER_VERSION if scorer_version is None else scorer_version
            ),
            "roster_digest": (
                gates_mod.ROSTER_DIGEST if roster_digest is None else roster_digest
            ),
            "roster": list(names),
        }
        if task_id is not None:
            payload["task_id"] = task_id
        self.payload_json = json.dumps(payload)


class FakeEngine:
    """Ledger double serving batteries plus the final release audit boundary.

    Release now refuses to ship a variant without a PASSING, roster-complete
    battery on its own ledger stage at the current parent hash (acceptance
    rule R3), and refuses to freeze without a current final audit and accepted
    task verdict. A double that knew only about ``gates`` would therefore be
    asserting two older, fictional release boundaries.
    """

    def __init__(self, workspace: Path, task, report: FakeReport, variants=None):
        self.workspace = workspace
        self._task = task
        self._report = report
        self._audit = FakeReport(report.verdict, report.content_hash, gates=())
        census = _census_evidence(Path(workspace), task)
        # Release now re-reads the canonical-reachability record the
        # transform battery judged (training.canonical.assert_canonical_ready).
        from tests.canonical_doubles import write_canonical_reachability

        if (Path(workspace) / "tasks" / task.task_id / "answer_key").is_dir():
            write_canonical_reachability(Path(workspace), task)
        self._variants = (
            variants
            if variants is not None
            else {
                variant: FakeReport(
                    report.verdict,
                    report.content_hash,
                    gates=variant_gate_names_map[variant],
                    task_id=variant_task_id(task.task_id, variant),
                    evidence_by_gate=(
                        {"warehouses-load": census}
                        if variant is TaskVariant.TRANSFORM
                        else {}
                    ),
                )
                for variant in (TaskVariant.EXTRACT_LOAD, TaskVariant.TRANSFORM)
            }
        )

    def load_task(self, task_id: str):
        assert task_id == self._task.task_id
        return self._task

    def latest_report(self, task_id: str, stage: str):
        if stage == "gates":
            return self._report
        if stage == "audit":
            return self._audit
        for variant, row in self._variants.items():
            if variant_gate_stage(variant).value == stage:
                return row
        raise AssertionError(f"unexpected stage {stage!r}")

    def final_verdict(self, task_id: str) -> str:
        assert task_id == self._task.task_id
        return "accepted"


class FakeSelection:
    def __init__(self, train=(), val=(), variants=None):
        self.train = tuple(train)
        self.val = tuple(val)
        self.variants = dict(variants or {})

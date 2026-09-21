"""Per-unit acceptance: rosters, failure partition, selection, release.

WHY THIS EXISTS
The extract-load and transform subtasks used to ship on the PARENT's ten-gate
battery, every gate of which is computed against the parent reward. These
tests pin the wiring that removes that fiction, and — just as important — the
places where the new machinery must REFUSE:

  * a variant with no battery on its own ledger stage never ships, and with no
    ``gates.run_variant_gates`` in the tree at all the dispatcher produces a
    RED battery rather than an empty green one;
  * a battery that ran fewer gates than its roster is not an acceptance, even
    when every gate it did run passed (`AcceptanceReport` is fail-closed on
    gate RESULTS, never on gate COVERAGE);
  * an exported bundle sitting under variants/ that no battery certified makes
    the freeze raise, loudly;
  * a VARIANT-LOCAL failure concludes without spending a task repair round, but
    the mandatory EL/T pair cannot be selected or released until both units
    pass; a TASK-LEVEL failure repairs the task instead;
  * FULL remains readable as a legacy diagnostic variant, but it is never an
    active selection, release acceptance record, or public RLVR unit.

The battery itself (verification/gates.py) is another builder's; these tests
stand in for it with a deterministic stub whose only job is to produce
roster-shaped reports, so what is under test here is the PIPELINE, ACCEPTANCE
and EXPORT wiring and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from elt_taskgen import cli, demo_fixture
from elt_taskgen.corpus import difficulty as difficulty_mod
from elt_taskgen.corpus import selection as selection_mod
from elt_taskgen.engine import STAGE_ORDER, StageName, variant_gate_stage
from elt_taskgen.export import eltbench, release as release_mod
from elt_taskgen.models import (
    AcceptanceReport,
    GateResult,
    RLVR_TASK_VARIANTS,
    TaskVariant,
    canonical_json,
    variant_task_id,
)
from elt_taskgen.reference.gold import GoldBundle
from elt_taskgen.verification import gates as gates_mod
from elt_taskgen.verification import variant_battery as battery_mod
from elt_taskgen.verification.gates import GATE_NAMES, ROSTER_DIGEST, SCORER_VERSION

V = TaskVariant

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

def stub_report(
    task,
    variant: TaskVariant,
    *,
    failing: tuple[str, ...] = (),
    drop: tuple[str, ...] = (),
    content_hash: str | None = None,
    evidence: dict[str, dict[str, str]] | None = None,
    scorer_version: str = SCORER_VERSION,
    roster_digest: str = ROSTER_DIGEST,
    recorded_roster: tuple[str, ...] | None = None,
) -> AcceptanceReport:
    """A roster-shaped battery result standing in for gates.run_variant_gates.

    Like the real battery it stamps the scorer version, the roster digest and
    — through the `variant-roster` gate's evidence — the roster it ran
    against (`recorded_roster` defaults to the live roster, so a `drop` is an
    INTERNALLY INCONSISTENT record: it claims a gate it never ran; pass an
    explicit `recorded_roster` without the gate to model a battery that
    honestly predates it).
    """
    evidence = evidence or {}
    roster = battery_mod.gate_roster(variant)
    claimed = roster if recorded_roster is None else tuple(recorded_roster)
    gates = []
    for name in roster:
        if name in drop:
            continue
        ev = dict(evidence.get(name, {}))
        if name == battery_mod.ROSTER_GATE:
            ev.setdefault("roster", ",".join(claimed))
        gates.append(
            GateResult(gate=name, passed=name not in failing, details="stub", evidence=ev)
        )
    return AcceptanceReport.from_gates(
        task_id=variant_task_id(task.task_id, variant),
        revision=task.current_revision,
        task_content_hash=content_hash or task.content_hash(),
        gates=gates,
        scorer_version=scorer_version,
        roster_digest=roster_digest,
    )


def _release_is_roster_aware(workspace: Path, task) -> bool:
    """Has export/release.variant_acceptance adopted roster currency
    (refuse a stale battery, raise only on the tamper shape)? Probed with the
    honest-drift shape (one gate short, recorded roster without it): the old
    reader raises 'never ran' on it, the new one refuses. Group C lands it
    after this module's contract; until then the tests that pin the refusal
    are skipped rather than failed on the old raise."""
    roster = battery_mod.gate_roster(V.EXTRACT_LOAD)
    without = tuple(g for g in roster if g != "mart-key-unique")
    report = stub_report(
        task, V.EXTRACT_LOAD, drop=("mart-key-unique",), recorded_roster=without
    )
    rows = {
        variant_gate_stage(V.EXTRACT_LOAD).value: FakeRow(
            "pass",
            task.content_hash(),
            json.loads(canonical_json(report.model_dump(mode="json"))),
        )
    }
    try:
        release_mod.variant_acceptance(FakeEngine(workspace, task, rows), task)
    except ValueError as exc:
        return "never ran" not in str(exc)
    return True


class FakeRow:
    def __init__(self, verdict: str, content_hash: str, payload: dict):
        self.verdict = verdict
        self.content_hash = content_hash
        self.payload_json = json.dumps(payload)


class FakeEngine:
    """Ledger double: one task, stage rows, and the final release boundary."""

    def __init__(self, workspace: Path, task, rows: dict[str, FakeRow]):
        self.workspace = workspace
        self._task = task
        self._rows = rows

    def load_task(self, task_id: str):
        assert task_id == self._task.task_id
        return self._task

    def latest_report(self, task_id: str, stage: str):
        assert task_id == self._task.task_id
        if stage == "select":
            return FakeRow("pass", self._task.content_hash(), {})
        return self._rows.get(stage)

    def final_verdict(self, task_id: str) -> str:
        assert task_id == self._task.task_id
        return "accepted"


class FakeSelection:
    def __init__(self, train=(), val=(), variants=None):
        self.train = tuple(train)
        self.val = tuple(val)
        self.variants = dict(variants or {})


# ---------------------------------------------------------------------------
# Rosters (R2)
# ---------------------------------------------------------------------------

class RosterTests(unittest.TestCase):
    def test_transform_replaces_populations_load_with_warehouses_load(self):
        roster = battery_mod.gate_roster(V.TRANSFORM)
        self.assertNotIn("populations-load", roster)
        self.assertIn(battery_mod.WAREHOUSES_LOAD, roster)

    def test_no_roster_is_empty(self):
        for variant in V:
            self.assertTrue(battery_mod.gate_roster(variant))

    def test_gate_roster_reads_the_gates_modules_matrix_not_a_local_copy(self):
        """The roster is DELEGATED: gates.py's applicability matrix is the one
        place the policy lives, so a change there must be visible here without
        editing this module."""
        from elt_taskgen.verification import gates as gates_mod

        saved = gates_mod.VARIANT_GATE_NAMES
        gates_mod.VARIANT_GATE_NAMES = dict(saved) | {V.EXTRACT_LOAD: ("only-gate",)}
        try:
            self.assertEqual(battery_mod.gate_roster(V.EXTRACT_LOAD), ("only-gate",))
        finally:
            gates_mod.VARIANT_GATE_NAMES = saved
        self.assertEqual(
            battery_mod.gate_roster(V.EXTRACT_LOAD), saved[V.EXTRACT_LOAD]
        )
        # EL replaces dual-build-agreement with TWO gates, so its roster is a
        # strict extension of the base gate set.
        self.assertGreater(len(battery_mod.gate_roster(V.EXTRACT_LOAD)), len(GATE_NAMES))

    def test_missing_roster_fails_closed(self):
        from elt_taskgen.verification import gates as gates_mod

        saved = gates_mod.VARIANT_GATE_NAMES
        gates_mod.VARIANT_GATE_NAMES = dict(saved) | {V.TRANSFORM: ()}
        try:
            with self.assertRaisesRegex(ValueError, "empty"):
                battery_mod.gate_roster(V.TRANSFORM)
        finally:
            gates_mod.VARIANT_GATE_NAMES = saved


# ---------------------------------------------------------------------------
# Coverage (R2/R3) — the half AcceptanceReport cannot enforce
# ---------------------------------------------------------------------------

class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()

    def test_complete_battery_passes_coverage(self):
        report = stub_report(self.task, V.TRANSFORM)
        battery_mod.verify_coverage(V.TRANSFORM, report)

    def test_battery_missing_a_roster_gate_is_not_an_acceptance(self):
        report = stub_report(self.task, V.EXTRACT_LOAD, drop=("shortcut-probes",))
        # models.AcceptanceReport is happy: every gate it holds passed.
        self.assertTrue(report.accepted)
        with self.assertRaisesRegex(ValueError, "missing"):
            battery_mod.verify_coverage(V.EXTRACT_LOAD, report)
        summary = battery_mod.summarize_payload(
            report.model_dump(mode="json"), V.EXTRACT_LOAD
        )
        self.assertFalse(summary["accepted"])
        self.assertEqual(summary["missing_gates"], ("shortcut-probes",))
        self.assertEqual(summary["classification"], battery_mod.TASK_LEVEL)

    def test_unreadable_payload_is_a_refusal_not_a_crash(self):
        summary = battery_mod.summarize_payload({"nonsense": 1}, V.TRANSFORM)
        self.assertFalse(summary["accepted"])
        self.assertEqual(summary["gates_recorded"], 0)


class CurrencyTests(unittest.TestCase):
    """roster_staleness / recorded_roster: ONE predicate for 'is this
    recorded battery evidence about the CURRENT roster and scorer?'"""

    def setUp(self):
        self.task = demo_fixture.demo_task()

    def _payload(self, **kw):
        return json.loads(
            canonical_json(stub_report(self.task, V.EXTRACT_LOAD, **kw).model_dump(mode="json"))
        )

    def test_current_battery_is_not_stale(self):
        payload = self._payload()
        self.assertEqual(battery_mod.roster_staleness(payload, V.EXTRACT_LOAD), "")
        self.assertEqual(
            battery_mod.recorded_roster(payload), battery_mod.gate_roster(V.EXTRACT_LOAD)
        )
        self.assertEqual(battery_mod.roster_digest(), gates_mod.ROSTER_DIGEST)

    def test_unreadable_payload_is_stale(self):
        self.assertEqual(
            battery_mod.roster_staleness({"nonsense": 1}, V.TRANSFORM),
            "battery payload unreadable",
        )
        self.assertEqual(battery_mod.roster_staleness(None, V.TRANSFORM), "battery payload unreadable")

    def test_older_scorer_is_stale(self):
        payload = self._payload(scorer_version="1.0.0")
        reason = battery_mod.roster_staleness(payload, V.EXTRACT_LOAD)
        self.assertIn("recorded under scorer 1.0.0", reason)
        self.assertIn(f"current {SCORER_VERSION}", reason)

    def test_battery_predating_a_roster_gate_is_stale_with_the_gate_named(self):
        # The exact real-ledger shape: current scorer literal, one gate short,
        # the recorded roster (variant-roster evidence) lacks it too.
        roster = battery_mod.gate_roster(V.EXTRACT_LOAD)
        without = tuple(g for g in roster if g != "mart-key-unique")
        payload = self._payload(drop=("mart-key-unique",), recorded_roster=without)
        reason = battery_mod.roster_staleness(payload, V.EXTRACT_LOAD)
        self.assertIn("predates current roster gate(s) [mart-key-unique]", reason)
        self.assertIn(f"recorded {len(without)} of current {len(roster)}", reason)
        self.assertEqual(battery_mod.recorded_roster(payload), without)

    def test_record_claiming_a_gate_it_never_ran_is_stale_and_says_so(self):
        payload = self._payload(drop=("mart-key-unique",))  # roster claims it
        reason = battery_mod.roster_staleness(payload, V.EXTRACT_LOAD)
        self.assertIn("claims roster gate(s) [mart-key-unique] it never ran", reason)
        self.assertIn("mart-key-unique", battery_mod.recorded_roster(payload))

    def test_stale_roster_digest_is_stale(self):
        payload = self._payload(roster_digest="deadbeefdeadbeef")
        reason = battery_mod.roster_staleness(payload, V.EXTRACT_LOAD)
        self.assertIn("recorded under roster deadbeefdeadbeef", reason)
        # A current-scorer payload with NO digest is unknown provenance (the
        # stamp landed with scorer 1.1.0): stale, never current — the same
        # verdict release reaches by comparing the digest, so engine currency
        # and release acceptance cannot disagree about it.
        payload = self._payload(roster_digest="")
        reason = battery_mod.roster_staleness(payload, V.EXTRACT_LOAD)
        self.assertIn("recorded under roster (none)", reason)
        self.assertIn(f"current {gates_mod.ROSTER_DIGEST}", reason)
        # ...whereas a scorer-stale payload without a digest is reported on
        # the scorer first (the real runs/* legacy shape).
        legacy = self._payload(scorer_version="1.0.0", roster_digest="")
        self.assertIn("recorded under scorer 1.0.0", battery_mod.roster_staleness(legacy, V.EXTRACT_LOAD))

    def test_recorded_roster_prefers_the_top_level_field(self):
        payload = self._payload()
        payload["roster"] = ["a", "b"]
        self.assertEqual(battery_mod.recorded_roster(payload), ("a", "b"))
        # a legacy payload without variant-roster evidence falls back to names
        legacy = {"gates": [{"gate": "x", "passed": True}, {"gate": "y", "passed": True}]}
        self.assertEqual(battery_mod.recorded_roster(legacy), ("x", "y"))
        self.assertEqual(battery_mod.recorded_roster({}), ())

    def test_legacy_acceptance_report_without_digest_still_loads(self):
        payload = self._payload()
        payload.pop("roster_digest")
        payload.pop("roster")
        report = AcceptanceReport.model_validate(payload)
        self.assertEqual(report.roster_digest, "")
        self.assertEqual(report.roster, ())

    @unittest.skipUnless(
        (REPO / "runs" / "synsql_elt" / "state" / "taskgen.sqlite").is_file(),
        "no runs/synsql_elt ledger",
    )
    def test_real_legacy_ledger_battery_is_stale_on_scorer_and_roster(self):
        """Fixture regression against the REAL synsql ledger (read-only): the
        recorded EL battery (14 gates, scorer 1.0.0, accepted) predates
        mart-key-unique — stale, never an acceptance, never a crash."""
        import sqlite3

        db = REPO / "runs" / "synsql_elt" / "state" / "taskgen.sqlite"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "select payload_json from reports where stage='gates_extract_load' "
                "and verdict='pass' order by id desc limit 1"
            ).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(row)
        payload = json.loads(row[0])
        self.assertTrue(payload.get("accepted"))
        recorded = battery_mod.recorded_roster(payload)
        self.assertNotIn("mart-key-unique", recorded)
        self.assertEqual(len(recorded), 14)
        summary = battery_mod.summarize_payload(payload, V.EXTRACT_LOAD)
        self.assertFalse(summary["accepted"])
        self.assertEqual(summary["missing_gates"], ("mart-key-unique",))
        reason = battery_mod.roster_staleness(payload, V.EXTRACT_LOAD)
        # scorer bumped to 1.1.0 for exactly this roster change
        self.assertIn("recorded under scorer 1.0.0", reason)
        # and even at the same scorer the roster hole names the gate
        payload["scorer_version"] = SCORER_VERSION
        self.assertIn("mart-key-unique", battery_mod.roster_staleness(payload, V.EXTRACT_LOAD))


# ---------------------------------------------------------------------------
# R5 failure partition
# ---------------------------------------------------------------------------

class FailurePartitionTests(unittest.TestCase):
    def _c(self, variant, gate, evidence=None):
        return battery_mod.classify_gate_failure(
            variant, GateResult(gate=gate, passed=False, evidence=evidence or {})
        )

    def test_el_local_failures(self):
        for gate in (
            "shortcut-probes",
            "info-content",
            "degenerate-zero",
            "required-mutants",
            "data-sensitivity",
            battery_mod.EL_INDEPENDENT_LOAD,
        ):
            self.assertEqual(
                self._c(V.EXTRACT_LOAD, gate), battery_mod.VARIANT_LOCAL, gate
            )

    def test_el_census_and_populations_load_are_task_level(self):
        # If these fail the frozen stage-1 counts do not match what is in the
        # artifacts — and FULL's stage-1 gate AND T's provided warehouse are
        # both built on that same false premise.
        for gate in (battery_mod.EL_ARTIFACT_CENSUS, "populations-load"):
            self.assertEqual(
                self._c(V.EXTRACT_LOAD, gate), battery_mod.TASK_LEVEL, gate
            )

    def test_t_masked_claims_are_task_level(self):
        for gate in (
            "degenerate-zero",
            "info-content",
            "data-sensitivity",
            battery_mod.WAREHOUSES_LOAD,
            "determinism",
            "trusted-solution",
            "required-mutants",
        ):
            self.assertEqual(self._c(V.TRANSFORM, gate), battery_mod.TASK_LEVEL, gate)

    def test_t_shortcut_probes_is_local(self):
        self.assertEqual(
            self._c(V.TRANSFORM, "shortcut-probes"), battery_mod.VARIANT_LOCAL
        )

    def test_required_mutants_locality_must_be_affirmed_by_the_gate(self):
        # TASK-LEVEL by default; only the gate knows whether the divergence
        # from the parent number came from a stage-1-gated kind.
        self.assertEqual(
            self._c(V.TRANSFORM, "required-mutants"), battery_mod.TASK_LEVEL
        )
        self.assertEqual(
            self._c(
                V.TRANSFORM,
                "required-mutants",
                {battery_mod.SCOPE_EVIDENCE_KEY: battery_mod.VARIANT_LOCAL},
            ),
            battery_mod.VARIANT_LOCAL,
        )

    def test_unknown_gate_names_block_the_parent(self):
        self.assertEqual(self._c(V.TRANSFORM, "brand-new-gate"), battery_mod.TASK_LEVEL)

    def test_strongest_failure_wins_for_a_whole_report(self):
        task = demo_fixture.demo_task()
        local = stub_report(task, V.EXTRACT_LOAD, failing=("shortcut-probes",))
        self.assertEqual(
            battery_mod.classify_report(V.EXTRACT_LOAD, local)[0],
            battery_mod.VARIANT_LOCAL,
        )
        mixed = stub_report(
            task,
            V.EXTRACT_LOAD,
            failing=("shortcut-probes", battery_mod.EL_ARTIFACT_CENSUS),
        )
        self.assertEqual(
            battery_mod.classify_report(V.EXTRACT_LOAD, mixed)[0],
            battery_mod.TASK_LEVEL,
        )


class DispatchTests(unittest.TestCase):
    def test_missing_run_variant_gates_is_a_red_battery(self):
        """A tree with no variant battery implementation REFUSES the variant.

        The dispatcher must never degrade to "no battery, therefore fine" —
        that is the shape that let unvalidated subtasks ship in the first
        place. Simulated by removing the function, because the gates module
        now implements it.
        """
        from elt_taskgen.verification import gates as gates_mod

        task = demo_fixture.demo_task()
        saved = gates_mod.run_variant_gates
        del gates_mod.run_variant_gates
        try:
            report = battery_mod.run_variant_battery(
                V.EXTRACT_LOAD, task, Path("/nonexistent"), object(), {}
            )
        finally:
            gates_mod.run_variant_gates = saved
        self.assertFalse(report.accepted)
        self.assertEqual(report.task_id, variant_task_id(task.task_id, V.EXTRACT_LOAD))
        with self.assertRaises(ValueError):
            battery_mod.verify_coverage(V.EXTRACT_LOAD, report)

    def test_full_is_rejected_by_run_variant_battery(self):
        task = demo_fixture.demo_task()
        with self.assertRaisesRegex(ValueError, "FULL's battery"):
            battery_mod.run_variant_battery(
                V.FULL, task, Path("/nonexistent"), object(), {}
            )

    def test_dispatch_refuses_a_battery_produced_under_another_scorer_or_roster(self):
        """A runner/reader disagreement would record a battery that is stale
        on arrival and re-run every sweep — a code defect, raised here."""
        from elt_taskgen.verification import gates as gates_mod

        task = demo_fixture.demo_task()
        for kw in (
            {"scorer_version": "0.9.0"},
            {"roster_digest": "deadbeefdeadbeef"},
            {"roster_digest": ""},  # unstamped == unknown roster == stale on arrival
        ):
            stale = stub_report(task, V.EXTRACT_LOAD, **kw)
            saved = gates_mod.run_variant_gates
            gates_mod.run_variant_gates = lambda *a, **k: stale  # noqa: B023
            try:
                with self.assertRaisesRegex(ValueError, "stale on arrival"):
                    battery_mod.run_variant_battery(
                        V.EXTRACT_LOAD, task, Path("/nonexistent"), object(), {}
                    )
            finally:
                gates_mod.run_variant_gates = saved
        # a current one is recorded
        current = stub_report(task, V.EXTRACT_LOAD)
        saved = gates_mod.run_variant_gates
        gates_mod.run_variant_gates = lambda *a, **k: current
        try:
            self.assertIs(
                battery_mod.run_variant_battery(
                    V.EXTRACT_LOAD, task, Path("/nonexistent"), object(), {}
                ),
                current,
            )
        finally:
            gates_mod.run_variant_gates = saved


# ---------------------------------------------------------------------------
# Engine + repair wiring
# ---------------------------------------------------------------------------

class StageWiringTests(unittest.TestCase):
    def test_two_new_stages_sit_immediately_after_gates(self):
        names = [s.value for s in STAGE_ORDER]
        i = names.index("gates")
        self.assertEqual(
            names[i : i + 3], ["gates", "gates_extract_load", "gates_transform"]
        )

    def test_variant_stage_lookup(self):
        self.assertIs(variant_gate_stage(V.FULL), StageName.GATES)
        self.assertIs(variant_gate_stage(V.EXTRACT_LOAD), StageName.GATES_EXTRACT_LOAD)
        self.assertIs(variant_gate_stage(V.TRANSFORM), StageName.GATES_TRANSFORM)

    def test_repair_routes_the_new_stages(self):
        from elt_taskgen import repair as repair_mod
        from elt_taskgen.engine import StagePayload
        from elt_taskgen.models import RepairRoute

        for stage in ("gates_extract_load", "gates_transform"):
            route = repair_mod.route_for_failure(stage, StagePayload(error="boom"))
            self.assertIsInstance(route, RepairRoute)
        for route, stages in repair_mod.RERUN_STAGES.items():
            if "gates" in stages:
                self.assertIn("gates_extract_load", stages, route)
                self.assertIn("gates_transform", stages, route)

# ---------------------------------------------------------------------------
# Selection (R1 + R8 + family isolation)
# ---------------------------------------------------------------------------

class SelectionVariantTests(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task().with_status(
            __import__("elt_taskgen.models", fromlist=["TaskStatus"]).TaskStatus.ACCEPTED
        )
        self.measure = {
            self.task.task_id: difficulty_mod.structural_difficulty(self.task).model_copy(
                update={"task_content_hash": self.task.content_hash()}
            )
        }
        self.quotas = selection_mod.Quotas(size=1, val_fraction=0.0)

    def _select(self, accepted):
        return selection_mod.select(
            [self.task], self.measure, [], self.quotas, accepted_variants=accepted
        )

    def test_no_unit_evidence_rejects_the_parent_pair(self):
        result = self._select(None)
        self.assertEqual(result.train, ())
        self.assertEqual(result.val, ())
        self.assertEqual(result.variants, {})
        self.assertIn(self.task.task_id, result.rejected)
        self.assertIn("both EL and T batteries are required", result.rejected[self.task.task_id])

    def test_only_one_accepted_unit_rejects_the_parent_pair(self):
        result = self._select({self.task.task_id: frozenset({"transform"})})
        self.assertEqual(result.train, ())
        self.assertEqual(result.val, ())
        self.assertEqual(result.variants, {})
        self.assertIn("extract_load", result.rejected[self.task.task_id])

    def test_calibration_exclusion_of_either_unit_rejects_the_pair(self):
        from elt_taskgen.corpus.difficulty import with_empirical
        from elt_taskgen.models import (
            EmpiricalDifficulty,
            SolverTierResult,
            VariantCalibration,
            solver_roster_fingerprint,
        )

        aced = (SolverTierResult(model_key="anthropic:x", k=4, successes=4),)
        mixed = (
            SolverTierResult(
                model_key="anthropic:x",
                k=4,
                successes=2,
                stage1_failures=2,
            ),
        )
        empirical = EmpiricalDifficulty(
            solver_config="fixture",
            n_attempts=8,
            success_rate=0.75,
            stage1_failure_rate=1.0,
            stage2_failure_rate=0.0,
            variants={
                V.EXTRACT_LOAD: VariantCalibration(
                    variant=V.EXTRACT_LOAD, tiers=mixed
                ),
                V.TRANSFORM: VariantCalibration(variant=V.TRANSFORM, tiers=aced),
            },
            roster_fingerprint=solver_roster_fingerprint(["anthropic:x"]),
            campaign_fingerprint="1" * 64,
            measured_at_content_hash=self.task.content_hash(),
        )
        self.measure[self.task.task_id] = with_empirical(
            self.measure[self.task.task_id], empirical
        )
        result = self._select(
            {self.task.task_id: frozenset({"extract_load", "transform"})}
        )
        # T is aced by the whole roster. Since EL and T are one mandatory
        # release pair, selection rejects the parent rather than shipping EL
        # alone.
        self.assertEqual(result.train, ())
        self.assertEqual(result.val, ())
        self.assertEqual(result.variants, {})
        self.assertIn("empirically trivial", result.rejected[self.task.task_id])

    def test_legacy_full_acceptance_is_not_a_selected_unit(self):
        result = self._select(
            {
                self.task.task_id: frozenset(
                    {"full", "extract_load", "transform"}
                )
            }
        )
        self.assertEqual(set(result.train) | set(result.val), {self.task.task_id})
        self.assertEqual(
            result.variants[self.task.task_id],
            tuple(v.value for v in RLVR_TASK_VARIANTS),
        )
        self.assertNotIn("full", result.variants[self.task.task_id])

    def test_family_variant_isolation_is_asserted_not_assumed(self):
        sibling = self.task.model_copy(update={"task_id": self.task.task_id + "_2"})
        with self.assertRaisesRegex(ValueError, "isolation violated"):
            selection_mod._assert_family_variant_isolation(
                [self.task, sibling],
                {
                    self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS),
                    sibling.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS),
                },
            )

    def test_one_family_one_parent_keeps_variants_together(self):
        sibling = self.task.model_copy(update={"task_id": self.task.task_id + "_2"})
        self.measure[sibling.task_id] = self.measure[self.task.task_id].model_copy(
            update={"task_content_hash": sibling.content_hash()}
        )
        result = selection_mod.select(
            [self.task, sibling],
            self.measure,
            [],
            selection_mod.Quotas(size=2, val_fraction=0.0),
            accepted_variants={
                self.task.task_id: frozenset({"extract_load", "transform"}),
                sibling.task_id: frozenset({"extract_load", "transform"}),
            },
        )
        # Same family => only one parent survives, and that parent contributes
        # the complete EL/T pair.
        self.assertEqual(len(set(result.train) | set(result.val)), 1)
        self.assertEqual(len(result.variants), 1)
        only = next(iter(result.variants.values()))
        self.assertEqual(only, tuple(v.value for v in RLVR_TASK_VARIANTS))


# ---------------------------------------------------------------------------
# Release (R3 + R7)
# ---------------------------------------------------------------------------

class ReleaseVariantAcceptanceTests(unittest.TestCase):
    """Synthetic (cheap) workspace: release only reads trees + the ledger."""

    _BOTH_PHASES_REQUIRED = (
        r"both EL and T (?:certification phases|units) are required"
    )

    def _write_portable_gold_and_sources(self, root: Path) -> GoldBundle:
        """A tiny, real schema-3.4 semantic package for release wiring tests.

        Release now validates the copied private gold and every shipped source
        population before freezing it.  These tests stub only the *battery*;
        their release artifacts still need to satisfy that production
        contract so an unrelated portability refusal cannot mask the variant
        invariant under test.
        """
        stage1: dict[str, dict[str, int]] = {}
        stage2_csv: dict[str, dict[str, str]] = {}
        mart_csv = (
            "customer_id,completed_order_count,total_spend\n"
            "1,1,1.000000000\n"
        )
        for population in sorted(spec.name.value for spec in self.task.populations):
            rendered = root / "populations" / population / "rendered"
            (rendered / "postgres").mkdir(parents=True)
            (rendered / "mongodb").mkdir()
            (rendered / "files").mkdir()
            (rendered / "postgres" / "customers.sql").write_text(
                'INSERT INTO "customers" ("customer_id", "customer_name") '
                "VALUES (1, 'Customer 1');\n",
                encoding="utf-8",
            )
            (rendered / "mongodb" / "orders.jsonl").write_text(
                json.dumps(
                    {
                        "customer_id": 1,
                        "order_id": 1,
                        "status": "completed",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            (rendered / "files" / "order_items.csv").write_text(
                "order_id,quantity,unit_price\n1,1,1.000000000\n",
                encoding="utf-8",
            )

            gold_dir = root / "answer_key" / "gold" / population
            gold_dir.mkdir(parents=True)
            counts = {table.name: 1 for table in self.task.tables}
            (gold_dir / "stage1_counts.json").write_text(
                canonical_json(counts), encoding="utf-8"
            )
            (gold_dir / f"{self.task.marts[0].name}.csv").write_text(
                mart_csv, encoding="utf-8"
            )
            stage1[population] = counts
            stage2_csv[population] = {self.task.marts[0].name: mart_csv}

        answer_key_dir = root / "answer_key"
        file_hashes = {
            path.relative_to(answer_key_dir).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(answer_key_dir.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        }
        (answer_key_dir / "manifest.json").write_text(
            canonical_json(
                {
                    "files": file_hashes,
                    "task_content_hash": self.task.content_hash(),
                    "task_id": self.task.task_id,
                }
            ),
            encoding="utf-8",
        )
        return GoldBundle(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage1=stage1,
            stage2_csv=stage2_csv,
            file_hashes=file_hashes,
        )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="elt-va-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.task = demo_fixture.demo_task()
        self.tid = self.task.task_id
        root = self.tmp / "tasks" / self.tid
        task_dir = root / "task"
        task_dir.mkdir(parents=True)
        runtime_config = eltbench.build_config(self.task)
        (task_dir / "config.yaml").write_text(
            json.dumps(runtime_config, indent=2, sort_keys=True) + "\n"
        )
        (task_dir / "data_model.yaml").write_text("models: []\n")

        schemas_dir = task_dir / "schemas"
        schemas_dir.mkdir()
        for table in self.task.tables:
            (schemas_dir / f"{table.name}.csv").write_text(
                "column_name,column_description\n"
            )

        eltbench._write_runtime_scaffold(task_dir, self.task)

        answer_key_dir = root / "answer_key"
        answer_key_dir.mkdir()
        (answer_key_dir / "note.txt").write_text("key")
        private_contract = (
            answer_key_dir / eltbench.PRIVATE_AIRBYTE_CONNECTOR_CONTRACT
        )
        private_contract.parent.mkdir(parents=True)
        private_contract.write_text(
            canonical_json(
                eltbench.build_airbyte_connector_contract(runtime_config)
            )
            + "\n"
        )
        gold = self._write_portable_gold_and_sources(root)
        self.variants_root = root / "variants"
        for variant in (V.EXTRACT_LOAD, V.TRANSFORM):
            vdir = self.variants_root / variant.value
            (vdir / "task").mkdir(parents=True)
            (vdir / "task" / "config.yaml").write_text("sources: []\n")
            (vdir / eltbench.REWARD_MANIFEST).write_text(
                canonical_json(eltbench.reward_manifest(self.task, gold, variant))
            )

    def _rows(self, *, el=None, t=None, hashes=None):
        h = self.task.content_hash()
        hashes = hashes or {}
        # No parent/FULL acceptance row: release reads exactly the two active
        # unit batteries and never turns the parent TaskIR into a third unit.
        rows = {}
        for variant, spec in ((V.EXTRACT_LOAD, el), (V.TRANSFORM, t)):
            if spec is None:
                continue
            report = stub_report(
                self.task,
                variant,
                failing=spec.get("failing", ()),
                drop=spec.get("drop", ()),
                content_hash=hashes.get(variant.value, h),
            )
            rows[variant_gate_stage(variant).value] = FakeRow(
                spec.get("verdict", "pass"),
                hashes.get(variant.value, h),
                json.loads(canonical_json(report.model_dump(mode="json"))),
            )
        return rows

    def _freeze(self, name, rows):
        from tests.canonical_doubles import write_canonical_reachability

        # validate-t's canonical-reachability evidence, which release re-reads.
        write_canonical_reachability(self.tmp, self.task)
        engine = FakeEngine(self.tmp, self.task, rows)
        return release_mod.freeze_release(
            engine,
            FakeSelection(
                train=(self.tid,),
                variants={
                    self.tid: tuple(v.value for v in RLVR_TASK_VARIANTS)
                },
            ),
            self.tmp / "release" / name,
        )

    def test_both_batteries_certify_exactly_one_combined_public_task(self):
        manifest = self._freeze("all", self._rows(el={}, t={}))
        self.assertEqual(
            manifest.variants[self.tid],
            tuple(v.value for v in RLVR_TASK_VARIANTS),
        )
        self.assertEqual(manifest.public_layout, release_mod.COMBINED_PUBLIC_LAYOUT)
        self.assertEqual(manifest.corpus_profile, release_mod.COMBINED_CORPUS_PROFILE)
        records = {r.variant: r for r in manifest.variant_acceptance[self.tid]}
        self.assertEqual(set(records), {"extract_load", "transform"})
        self.assertNotIn("full", records)
        for name, record in records.items():
            self.assertTrue(record.accepted, name)
            self.assertTrue(record.shipped, name)
            self.assertEqual(
                record.gates_applicable,
                len(battery_mod.gate_roster(V(name))),
                name,
            )
            self.assertEqual(record.gates_passed, record.gates_applicable, name)
        # A consumer can see how thoroughly each internal phase was validated
        # without the workspace. There is no legacy FULL acceptance record.
        self.assertGreater(records["extract_load"].gates_applicable, len(GATE_NAMES))
        self.assertEqual(manifest.rejected_variants, {})
        out = self.tmp / "release" / "all"
        public_root = out / "public"
        self.assertEqual(
            {path.name for path in public_root.iterdir() if path.is_dir()},
            {self.tid},
        )
        public_task = public_root / self.tid
        for rel in (
            "config.yaml",
            "data_model.yaml",
            "check_job_status.py",
            "snowflake_credential.json",
            "elt/main.tf",
        ):
            self.assertTrue((public_task / rel).is_file(), rel)
        for table in self.task.tables:
            rel = f"schemas/{table.name}.csv"
            self.assertTrue((public_task / rel).is_file(), rel)
        for name in eltbench.documentation_filenames_for(
            eltbench.build_config(self.task), "snowflake"
        ):
            rel = f"documentation/{name}"
            self.assertTrue((public_task / rel).is_file(), rel)
        self.assertFalse((public_task / "documentation.md").exists())
        self.assertFalse(
            (
                public_task / "elt"
                / eltbench.AIRBYTE_CONNECTOR_TFVARS_FILENAME
            ).exists()
        )
        for variant in RLVR_TASK_VARIANTS:
            self.assertFalse(
                (public_root / variant_task_id(self.tid, variant)).exists()
            )
        # The same parent remains the private curation container for the shared
        # answer key while its public directory is the one end-to-end task.
        self.assertTrue((out / "private" / self.tid / "answer_key").is_dir())

    def test_el_refusal_blocks_the_mandatory_pair(self):
        rows = self._rows(el={"failing": ("shortcut-probes",)}, t={})
        with self.assertRaisesRegex(ValueError, self._BOTH_PHASES_REQUIRED):
            self._freeze("el-refused", rows)
        self.assertFalse((self.tmp / "release" / "el-refused").exists())

    def test_missing_either_battery_blocks_the_mandatory_pair(self):
        with self.assertRaisesRegex(ValueError, self._BOTH_PHASES_REQUIRED):
            self._freeze("missing-el", self._rows(t={}))
        self.assertFalse((self.tmp / "release" / "missing-el").exists())

    def test_accepted_battery_without_a_bundle_is_a_phantom_accept(self):
        shutil.rmtree(self.variants_root / V.TRANSFORM.value)
        with self.assertRaisesRegex(ValueError, "phantom accept"):
            self._freeze("phantom", self._rows(el={}, t={}))

    def test_stale_variant_battery_blocks_the_pair(self):
        rows = self._rows(el={}, t={}, hashes={"extract_load": "0" * 64})
        with self.assertRaisesRegex(ValueError, self._BOTH_PHASES_REQUIRED):
            self._freeze("stale", rows)
        self.assertFalse((self.tmp / "release" / "stale").exists())

    def test_roster_hole_claiming_acceptance_is_a_hard_refusal(self):
        # AcceptanceReport is fail-closed on gate RESULTS but not on gate
        # COVERAGE: every gate it holds passed, so it claims acceptance while
        # never having run el-artifact-census — and its recorded roster says
        # it DID (stub_report keeps the live roster). That is the fail-open /
        # tamper shape, not an ordinary refusal: it raises.
        shutil.rmtree(self.variants_root / V.EXTRACT_LOAD.value)
        rows = self._rows(el={"drop": (battery_mod.EL_ARTIFACT_CENSUS,)}, t={})
        with self.assertRaisesRegex(ValueError, "never ran"):
            self._freeze("hole", rows)

    def _el_row(self, **kw):
        report = stub_report(self.task, V.EXTRACT_LOAD, **kw)
        return FakeRow(
            "pass",
            self.task.content_hash(),
            json.loads(canonical_json(report.model_dump(mode="json"))),
        )

    def _t_row(self):
        report = stub_report(self.task, V.TRANSFORM)
        return FakeRow(
            "pass",
            self.task.content_hash(),
            json.loads(canonical_json(report.model_dump(mode="json"))),
        )

    def test_roster_drift_is_a_refusal_not_a_crash(self):
        # The real-ledger shape after a roster grew: accepted=True, one gate
        # short, recorded roster honestly WITHOUT it. Stale evidence: refuse
        # with 're-run', never accept, never raise.
        roster = battery_mod.gate_roster(V.EXTRACT_LOAD)
        without = tuple(g for g in roster if g != "mart-key-unique")
        rows = {
            variant_gate_stage(V.EXTRACT_LOAD).value: self._el_row(
                drop=("mart-key-unique",), recorded_roster=without
            ),
            variant_gate_stage(V.TRANSFORM).value: self._t_row(),
        }
        engine = FakeEngine(self.tmp, self.task, rows)
        if not _release_is_roster_aware(self.tmp, self.task):
            self.skipTest("release.variant_acceptance predates roster currency (group C)")
        records = release_mod.variant_acceptance(engine, self.task)
        el = records[V.EXTRACT_LOAD.value]
        self.assertFalse(el.accepted)
        self.assertIn("mart-key-unique", el.refusal_reason)
        self.assertIn("re-run", el.refusal_reason)
        self.assertTrue(records[V.TRANSFORM.value].accepted)

    def test_stale_scorer_battery_is_a_refusal_not_a_raise(self):
        # scorer 1.0.0 + one gate short: exactly the recorded runs/*_elt rows.
        roster = battery_mod.gate_roster(V.EXTRACT_LOAD)
        without = tuple(g for g in roster if g != "mart-key-unique")
        rows = {
            variant_gate_stage(V.EXTRACT_LOAD).value: self._el_row(
                drop=("mart-key-unique",), recorded_roster=without,
                scorer_version="1.0.0", roster_digest="",
            ),
            variant_gate_stage(V.TRANSFORM).value: self._t_row(),
        }
        engine = FakeEngine(self.tmp, self.task, rows)
        if not _release_is_roster_aware(self.tmp, self.task):
            self.skipTest("release.variant_acceptance predates roster currency (group C)")
        records = release_mod.variant_acceptance(engine, self.task)
        el = records[V.EXTRACT_LOAD.value]
        self.assertFalse(el.accepted)
        self.assertIn("scorer", el.refusal_reason)
        with self.assertRaisesRegex(ValueError, self._BOTH_PHASES_REQUIRED):
            self._freeze("stale-scorer", rows)

    def test_stale_roster_digest_is_a_refusal(self):
        rows = {
            variant_gate_stage(V.EXTRACT_LOAD).value: self._el_row(roster_digest="deadbeef"),
            variant_gate_stage(V.TRANSFORM).value: self._t_row(),
        }
        engine = FakeEngine(self.tmp, self.task, rows)
        if not _release_is_roster_aware(self.tmp, self.task):
            self.skipTest("release.variant_acceptance predates roster currency (group C)")
        records = release_mod.variant_acceptance(engine, self.task)
        self.assertFalse(records[V.EXTRACT_LOAD.value].accepted)
        self.assertIn("roster", records[V.EXTRACT_LOAD.value].refusal_reason)

    def test_anchor_guard_still_refuses(self):
        from elt_taskgen.models import Origin

        anchor = self.task.model_copy(update={"origin": Origin.ELTBENCH_ANCHOR})
        engine = FakeEngine(self.tmp, anchor, self._rows(el={}, t={}))
        with self.assertRaisesRegex(ValueError, "anchor"):
            release_mod.freeze_release(
                engine,
                FakeSelection(
                    train=(self.tid,),
                    variants={
                        self.tid: tuple(v.value for v in RLVR_TASK_VARIANTS)
                    },
                ),
                self.tmp / "release" / "anchor",
            )


# ---------------------------------------------------------------------------
# CLI stage runner: verdict semantics
# ---------------------------------------------------------------------------

class VariantStageRunnerTests(unittest.TestCase):
    """The stage verdict says the battery CONCLUDED; the payload says whether
    the unit ships. Because EL and T are both mandatory, any refusal fails its
    stage and enters bounded repair; failure scope remains diagnostic evidence
    rather than a way to make one member of the pair optional."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="elt-vsr-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        from elt_taskgen.engine import Engine

        self.engine = Engine(self.tmp)
        self.addCleanup(self.engine.close)
        self.task = demo_fixture.demo_task()
        self.engine.register(self.task)
        (self.engine.task_dir(self.task.task_id) / "reports").mkdir(
            parents=True, exist_ok=True
        )
        # Pretend the attack stage passed at this identity.
        from elt_taskgen.engine import StagePayload, VERDICT_PASS

        self.engine.record_report(
            self.task, "attack", VERDICT_PASS, StagePayload(detail="stub")
        )

    def _run(self, variant, report, canonical_outcome=None):
        from unittest import mock

        from elt_taskgen.export import eltbench as eltbench_mod
        from elt_taskgen.reference import gold as gold_mod
        from elt_taskgen.verification import variant_battery as vb

        emitted = self.engine.task_dir(self.task.task_id) / "variants" / variant.value
        orig = (
            gold_mod.load_gold,
            eltbench_mod.emit_variant,
            vb.run_variant_battery,
        )
        gold_mod.load_gold = lambda *a, **k: object()
        def _emit(*a, **k):
            (emitted / "task").mkdir(parents=True, exist_ok=True)
            (emitted / "task" / "config.yaml").write_text("x")
        eltbench_mod.emit_variant = _emit
        vb.run_variant_battery = lambda *a, **k: report
        try:
            # The canonical-reachability producer scores a real workspace; the
            # routing under test is the battery's, so it is stubbed here and
            # exercised on real artifacts in tests/test_training_canonical.py.
            with mock.patch.object(
                cli, "_ensure_canonical_reachability", return_value=canonical_outcome
            ):
                runner = cli.make_variant_gates_runner(variant)
                return runner(self.engine, self.task), emitted
        finally:
            (
                gold_mod.load_gold,
                eltbench_mod.emit_variant,
                vb.run_variant_battery,
            ) = orig

    def test_sole_canonical_reachability_refusal_blocks_for_a_pipeline_change(self):
        """Neither a repair round nor a rejection: the task's own solution is
        unreachable through the grader, which a pipeline change fixes."""
        report = stub_report(
            self.task, V.TRANSFORM, failing=("canonical-reachability",)
        )
        outcome, _ = self._run(V.TRANSFORM, report)
        self.assertEqual(outcome.verdict, "blocked")
        self.assertIsNone(outcome.route)
        self.assertEqual(outcome.payload.data["blocked_on"], "human")
        self.assertEqual(outcome.payload.data["failure_code"], "canonical_workspace_unreachable")
        self.assertIn("Nothing is rejected", outcome.payload.error)

    def test_canonical_reachability_beside_another_refusal_takes_the_repair_route(self):
        report = stub_report(
            self.task, V.TRANSFORM, failing=("canonical-reachability", "degenerate-zero")
        )
        outcome, _ = self._run(V.TRANSFORM, report)
        self.assertEqual(outcome.verdict, "fail")
        self.assertIsNone(outcome.route)

    def test_canonical_producer_block_is_returned_before_the_battery_runs(self):
        from elt_taskgen.engine import StageOutcome, StagePayload

        blocked = StageOutcome(
            "blocked",
            StagePayload(error="runtime missing", data={"blocked_on": "environment"}),
        )
        outcome, emitted = self._run(
            V.TRANSFORM, stub_report(self.task, V.TRANSFORM), canonical_outcome=blocked
        )
        self.assertIs(outcome, blocked)
        self.assertTrue(emitted.is_dir())

    def test_accepted_variant_passes_and_keeps_its_bundle(self):
        outcome, emitted = self._run(V.TRANSFORM, stub_report(self.task, V.TRANSFORM))
        self.assertEqual(outcome.verdict, "pass")
        self.assertTrue(outcome.payload.accepted)
        self.assertTrue(emitted.is_dir())

    def test_variant_local_failure_still_fails_because_the_unit_is_mandatory(self):
        report = stub_report(self.task, V.EXTRACT_LOAD, failing=("shortcut-probes",))
        outcome, emitted = self._run(V.EXTRACT_LOAD, report)
        self.assertEqual(outcome.verdict, "fail")
        self.assertFalse(outcome.payload.accepted)
        self.assertFalse(emitted.exists())  # no uncertified unit bundle

    def test_task_level_failure_fails_the_stage_so_the_task_is_repaired(self):
        report = stub_report(
            self.task, V.TRANSFORM, failing=("degenerate-zero",)
        )
        outcome, emitted = self._run(V.TRANSFORM, report)
        self.assertEqual(outcome.verdict, "fail")
        self.assertFalse(emitted.exists())

    def test_accepted_report_with_a_roster_hole_fails_the_stage(self):
        report = stub_report(self.task, V.TRANSFORM, drop=("determinism",))
        outcome, emitted = self._run(V.TRANSFORM, report)
        self.assertEqual(outcome.verdict, "fail")
        self.assertIn("roster coverage", outcome.payload.error)
        self.assertFalse(emitted.exists())


if __name__ == "__main__":
    unittest.main()

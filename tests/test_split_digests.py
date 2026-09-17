"""Tests for the SPLIT determinism digests (stage-1 vs stage-2 components).

WHY THIS EXISTS
The joint run digest mixes stage-1 counts and mart CSVs into one sha256, so a
per-variant gate that cites it is asserting something about the OTHER variant's
outputs — evidence laundering even when the run itself is clean. reference/
runner.py therefore records ``stage1_digest`` and ``stage2_digest`` alongside
the joint digest, and verification/gates.py `_gate_variant_determinism` reads
exactly those keys. These tests pin the three properties the gate depends on:

  1. PRESENCE + SPELLING — the recorded evidence carries both component keys,
     hex-sha256 shaped, under the exact names gates.py looks up.
  2. LOCATION INDEPENDENCE — a clean rebuild in a different workspace path
     reproduces both components byte for byte (no absolute path, no clock, no
     dict-iteration order in the hashed payload).
  3. ORTHOGONALITY — perturbing a mart output moves stage2_digest and CANNOT
     move stage1_digest; perturbing a source row count moves stage1_digest.
     Without this, a component digest would not be a witness about its own
     stage, and the split would buy nothing.

Plus the fail-closed rule: a component that DIVERGED across rebuild runs is
never published as a citable digest, and the failure names which one moved.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_reference import render_counterfactual
except ModuleNotFoundError:  # package-style invocation
    from tests.test_reference import render_counterfactual

from elt_taskgen.demo_fixture import DEMO_TASK_ID, MART_NAME, demo_task
from elt_taskgen.models import PopulationName
from elt_taskgen.reference import runner as runner_mod
from elt_taskgen.reference.gold import freeze_gold, gold_digests, load_gold
from elt_taskgen.reference.runner import (
    DETERMINISM_JOINT_DIGEST_KEY,
    DETERMINISM_STAGE1_DIGEST_KEY,
    DETERMINISM_STAGE2_DIGEST_KEY,
    determinism_evidence,
    run_digests,
    run_reference,
    stage1_digest_from_counts,
    stage2_digest_from_csv,
)
from elt_taskgen.verification import gates as gates_mod

P = PopulationName
POP = P.COUNTERFACTUAL


def _is_hex64(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _fresh_workspace(stack: tempfile.TemporaryDirectory) -> Path:
    workspace = Path(stack.name)
    render_counterfactual(workspace)
    return workspace


class SplitDigestTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.workspace = Path(cls._tmp.name)
        render_counterfactual(cls.workspace)
        cls.task = demo_task()

    def digests(self, workspace: Path | None = None):
        result = run_reference(self.task, POP, workspace or self.workspace)
        return run_digests(self.task, result)


class TestEvidenceContract(SplitDigestTestBase):
    """(1) The keys the gate reads exist, are digest-shaped, and are separate."""

    def test_determinism_evidence_carries_both_component_digests(self) -> None:
        gate = determinism_evidence(self.task, POP, self.workspace, runs=3)
        self.assertTrue(gate.passed, gate.details)
        for key in (
            DETERMINISM_STAGE1_DIGEST_KEY,
            DETERMINISM_STAGE2_DIGEST_KEY,
            DETERMINISM_JOINT_DIGEST_KEY,
        ):
            self.assertIn(key, gate.evidence)
            self.assertTrue(_is_hex64(gate.evidence[key]), key)
        # Three distinct surfaces => three distinct digests.
        self.assertEqual(
            len(
                {
                    gate.evidence[DETERMINISM_STAGE1_DIGEST_KEY],
                    gate.evidence[DETERMINISM_STAGE2_DIGEST_KEY],
                    gate.evidence[DETERMINISM_JOINT_DIGEST_KEY],
                }
            ),
            3,
        )
        # The joint digest is preserved, not replaced: the FULL variant keeps
        # citing exactly what it always cited.
        for i in range(3):
            self.assertEqual(
                gate.evidence[f"run_{i}_sha256"],
                gate.evidence[DETERMINISM_JOINT_DIGEST_KEY],
            )

    def test_key_spelling_matches_the_gate_that_reads_it(self) -> None:
        # runner declares the keys (gates imports runner, not the reverse);
        # this pins the two spellings together so they cannot drift apart.
        self.assertEqual(
            DETERMINISM_STAGE1_DIGEST_KEY, gates_mod.DETERMINISM_STAGE1_DIGEST_KEY
        )
        self.assertEqual(
            DETERMINISM_STAGE2_DIGEST_KEY, gates_mod.DETERMINISM_STAGE2_DIGEST_KEY
        )


class TestLocationIndependence(SplitDigestTestBase):
    """(2) Different workspace path, same component digests."""

    def test_rebuild_in_a_different_workspace_reproduces_both_components(self) -> None:
        here = self.digests()
        with tempfile.TemporaryDirectory(prefix="elsewhere-") as other:
            other_workspace = Path(other)
            render_counterfactual(other_workspace)
            self.assertNotEqual(other_workspace, self.workspace)
            there = self.digests(other_workspace)
        self.assertEqual(here.stage1, there.stage1)
        self.assertEqual(here.stage2, there.stage2)
        self.assertEqual(here.joint, there.joint)

    def test_component_digests_ignore_dict_insertion_order(self) -> None:
        counts = {"customers": 3, "orders": 2, "order_items": 4}
        reversed_counts = dict(reversed(list(counts.items())))
        self.assertEqual(
            stage1_digest_from_counts(POP.value, counts),
            stage1_digest_from_counts(POP.value, reversed_counts),
        )
        marts = {"a_mart": "x\n1\n", "b_mart": "y\n2\n"}
        self.assertEqual(
            stage2_digest_from_csv(POP.value, marts),
            stage2_digest_from_csv(POP.value, dict(reversed(list(marts.items())))),
        )


class TestOrthogonality(SplitDigestTestBase):
    """(3) Each component moves for its own stage only."""

    def test_mart_perturbation_moves_stage2_and_not_stage1(self) -> None:
        base = self.digests()
        result = run_reference(self.task, POP, self.workspace)
        rows = [dict(r) for r in result.mart_rows[MART_NAME]]
        self.assertTrue(rows, "counterfactual mart must have rows to perturb")
        rows[0]["total_spend"] = float(rows[0]["total_spend"] or 0) + 1.0
        perturbed = run_digests(
            self.task, result.model_copy(update={"mart_rows": {MART_NAME: rows}})
        )
        self.assertEqual(perturbed.stage1, base.stage1)  # untouched surface
        self.assertNotEqual(perturbed.stage2, base.stage2)
        self.assertNotEqual(perturbed.joint, base.joint)

    def test_count_perturbation_moves_stage1_and_not_stage2(self) -> None:
        base = self.digests()
        result = run_reference(self.task, POP, self.workspace)
        counts = dict(result.stage1_counts)
        counts["customers"] = counts["customers"] + 1
        perturbed = run_digests(
            self.task, result.model_copy(update={"stage1_counts": counts})
        )
        self.assertNotEqual(perturbed.stage1, base.stage1)
        self.assertEqual(perturbed.stage2, base.stage2)  # untouched surface
        self.assertNotEqual(perturbed.joint, base.joint)

    def test_end_to_end_source_value_perturbation_moves_only_stage2(self) -> None:
        """A real re-render: same row counts, different mart values.

        The rendered CSV AND the frozen rows move together: the runner
        cross-checks loaded CONTENTS against ``rows/<table>.jsonl`` (a rendered
        artifact that disagrees with the generator's truth is refused, never
        gold), so a value perturbation is a re-generation, not a CSV edit.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            render_counterfactual(workspace)
            before = self.digests(workspace)
            pop_dir = (
                workspace / "tasks" / DEMO_TASK_ID / "populations" / POP.value
            )
            csv_path = pop_dir / "rendered" / "files" / "order_items.csv"
            lines = csv_path.read_text(encoding="utf-8").splitlines()
            header, first = lines[0], lines[1].split(",")
            new_price = float(first[-1]) + 7.5
            first[-1] = str(new_price)  # price only; row count fixed
            csv_path.write_text(
                "\n".join([header, ",".join(first)] + lines[2:]) + "\n",
                encoding="utf-8",
            )
            rows_path = pop_dir / "rows" / "order_items.jsonl"
            frozen = rows_path.read_text(encoding="utf-8").splitlines()
            first_row = json.loads(frozen[0])
            first_row["unit_price"] = new_price
            frozen[0] = json.dumps(first_row, sort_keys=True)
            rows_path.write_text("\n".join(frozen) + "\n", encoding="utf-8")
            after = self.digests(workspace)
        self.assertEqual(after.stage1, before.stage1)
        self.assertNotEqual(after.stage2, before.stage2)

    def test_end_to_end_source_row_perturbation_moves_stage1(self) -> None:
        """A real re-render: one more source row => the EL witness must move."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            render_counterfactual(workspace)
            before = self.digests(workspace)
            pop_dir = (
                workspace / "tasks" / DEMO_TASK_ID / "populations" / POP.value
            )
            sql_path = pop_dir / "rendered" / "postgres" / "customers.sql"
            text = sql_path.read_text(encoding="utf-8").replace(
                "COMMIT;",
                "INSERT INTO customers (customer_id, customer_name) "
                "VALUES (13, 'C13');\nCOMMIT;",
            )
            sql_path.write_text(text, encoding="utf-8")
            rows_path = pop_dir / "rows" / "customers.jsonl"
            with rows_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps({"customer_id": 13, "customer_name": "C13"},
                               sort_keys=True)
                    + "\n"
                )
            after = self.digests(workspace)
        self.assertNotEqual(after.stage1, before.stage1)


class TestDivergenceReporting(SplitDigestTestBase):
    """A non-deterministic run must say WHICH component moved, and publish none
    of the moving ones."""

    def test_unstable_stage2_names_the_component_and_withholds_its_digest(self) -> None:
        clean = run_reference(self.task, POP, self.workspace)
        drifted_rows = [dict(r) for r in clean.mart_rows[MART_NAME]]
        drifted_rows[0]["total_spend"] = float(drifted_rows[0]["total_spend"] or 0) + 2
        drifted = clean.model_copy(update={"mart_rows": {MART_NAME: drifted_rows}})
        with mock.patch.object(
            runner_mod, "run_reference", side_effect=[clean, drifted, clean]
        ):
            gate = determinism_evidence(self.task, POP, self.workspace, runs=3)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.evidence.get("diverged_components"), "stage2")
        self.assertIn("stage2", gate.details)
        # stage-1 held still, so it stays citable; stage-2 did not, so it is
        # NOT published as a digest a per-variant gate could cite.
        self.assertIn(DETERMINISM_STAGE1_DIGEST_KEY, gate.evidence)
        self.assertNotIn(DETERMINISM_STAGE2_DIGEST_KEY, gate.evidence)
        self.assertNotIn(DETERMINISM_JOINT_DIGEST_KEY, gate.evidence)

    def test_unstable_stage1_names_the_component(self) -> None:
        clean = run_reference(self.task, POP, self.workspace)
        counts = dict(clean.stage1_counts)
        counts["orders"] = counts["orders"] + 1
        drifted = clean.model_copy(update={"stage1_counts": counts})
        with mock.patch.object(
            runner_mod, "run_reference", side_effect=[clean, drifted]
        ):
            gate = determinism_evidence(self.task, POP, self.workspace, runs=2)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.evidence.get("diverged_components"), "stage1")
        self.assertNotIn(DETERMINISM_STAGE1_DIGEST_KEY, gate.evidence)
        self.assertIn(DETERMINISM_STAGE2_DIGEST_KEY, gate.evidence)


class TestGoldSideAccessor(SplitDigestTestBase):
    """The frozen bundle must reproduce the recorded components exactly."""

    def test_gold_digests_match_the_live_run_components(self) -> None:
        result = run_reference(self.task, POP, self.workspace)
        live = run_digests(self.task, result)
        with tempfile.TemporaryDirectory() as tmp:
            akd = Path(tmp) / "answer_key"
            akd.mkdir(parents=True)
            task = self.task.model_copy(
                update={
                    "populations": tuple(
                        p for p in self.task.populations if p.name is POP
                    )
                }
            )
            freeze_gold(task, {POP: result}, akd)
            frozen = load_gold(akd)
        from_gold = gold_digests(frozen, POP)
        self.assertEqual(from_gold.stage1, live.stage1)
        self.assertEqual(from_gold.stage2, live.stage2)
        self.assertEqual(from_gold.joint, live.joint)

    def test_unknown_population_fails_closed(self) -> None:
        result = run_reference(self.task, POP, self.workspace)
        with tempfile.TemporaryDirectory() as tmp:
            akd = Path(tmp) / "answer_key"
            akd.mkdir(parents=True)
            task = self.task.model_copy(
                update={
                    "populations": tuple(
                        p for p in self.task.populations if p.name is POP
                    )
                }
            )
            freeze_gold(task, {POP: result}, akd)
            frozen = load_gold(akd)
        with self.assertRaises(KeyError):
            gold_digests(frozen, P.STRESS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

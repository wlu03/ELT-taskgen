"""Tests for the data-sensitivity gate under `population_policy: 'provided-rows'`.

WHY THIS EXISTS
docs/runs/wikidbs.md §8.2 measured the intrinsic second blocker: with a licensed
real snapshot there is no generator to re-seed, so `resampled` is a
deterministic REARRANGEMENT of `primary` and their gold is byte-identical by
construction (`568d7bd77e9ad571`). The old gate read that identity as "the
reward is not a function of the input" and could never go green.

These tests pin the replacement contract, and in particular that it is NOT a
relaxation:

  * a pair that really moved values/rows is still asserted to MOVE the gold
    (the pre-existing red case still goes red);
  * a pair that is a pure rearrangement is asserted to leave the gold
    INVARIANT — a permutation that moves the gold is order-dependence, which
    the old gate could not even express;
  * the classification is read off the MATERIALIZED ROWS, so it cannot be
    bought with prose;
  * a rearrangement-only pair does NOT get to discharge the value-sensitivity
    obligation: the gate then REQUIRES the recorded scratch-layer bijection
    probe, and is red without it, with a stale one, with one whose pinned-layer
    digest moved, and with one that did not pass. The gate is never skipped;
  * the probe itself is a real, structure-preserving value bijection, run
    against a real built workspace through the ONE reference path, and it
    proves the pinned artifacts were never written.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen.models import task_from_json
from elt_taskgen.reference.gold import load_gold
from elt_taskgen.verification import gates, perturbation

REPO = Path(__file__).resolve().parents[1]


def _built_workspace() -> tuple[Path, str] | None:
    """The first real drive under runs/ that carries a frozen answer key.

    Formerly the demo workspace, until the demo was removed. These
    tests need a BUILT workspace (task IR + gold + materialized population
    rows), which only a real drive has; any of the five released pools serves,
    so the first one found is used rather than pinning a pool.
    """
    # A drive carrying `dbt_builds/` is deprioritized: it is an order of
    # magnitude larger to copy per test, and holds relative symlinks into a
    # vendored package tree that need not resolve here.
    candidates = sorted(
        (REPO / "runs").glob("*"), key=lambda p: ((p / "dbt_builds").is_dir(), p.name)
    )
    for ws in candidates:
        if not (ws / "tasks").is_dir():
            continue
        for task_dir in sorted((ws / "tasks").glob("*")):
            if (task_dir / "task_ir.json").is_file() and (task_dir / "answer_key").is_dir():
                return ws, task_dir.name
    return None


_BUILT = _built_workspace()
BUILT_WORKSPACE = _BUILT[0] if _BUILT else REPO / "runs" / "<none>"
BUILT_TASK_ID = _BUILT[1] if _BUILT else "<none>"


def workspace_available() -> bool:
    return _BUILT is not None


@unittest.skipUnless(workspace_available(), "no built workspace under runs/")
class BuiltWorkspaceTestCase(unittest.TestCase):
    """Every test gets its own copy: nothing here may mutate the real tree."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.task = task_from_json(
            (BUILT_WORKSPACE / "tasks" / BUILT_TASK_ID / "task_ir.json").read_text(
                encoding="utf-8"
            )
        )
        cls.gold = load_gold(
            BUILT_WORKSPACE / "tasks" / BUILT_TASK_ID / "answer_key"
        )

    def fresh(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = Path(tmp.name) / "workspace"
        shutil.copytree(BUILT_WORKSPACE, dest, symlinks=True)
        return dest

    def rows_path(self, ws: Path, pop: str, table: str) -> Path:
        return (
            ws / "tasks" / BUILT_TASK_ID / "populations" / pop / "rows"
            / f"{table}.jsonl"
        )

    def make_rearrangement(self, ws: Path) -> None:
        """Turn `resampled` into a pure permutation of `primary`.

        This is what `adapters/wikidbs.py` `_stable_shuffle` produces under
        'provided-rows': the same rows, a deterministically different order.
        """
        for table in self.task.tables:
            lines = [
                ln
                for ln in self.rows_path(ws, "primary", table.name)
                .read_text(encoding="utf-8")
                .splitlines()
                if ln.strip()
            ]
            self.rows_path(ws, "resampled", table.name).write_text(
                "\n".join(reversed(lines)) + "\n", encoding="utf-8"
            )

    def gold_with_resampled_equal_to_primary(self):
        stage2 = {k: dict(v) for k, v in self.gold.stage2_csv.items()}
        stage2["resampled"] = dict(stage2["primary"])
        return self.gold.model_copy(update={"stage2_csv": stage2})

    def write_probe(self, ws: Path, **overrides) -> Path:
        record = {
            "kind": perturbation.PROBE_KIND,
            "task_id": BUILT_TASK_ID,
            "task_content_hash": self.task.content_hash(),
            "population": "primary",
            "passed": True,
            "details": "probe fixture",
            "evidence": {f"{self.task.marts[0].name}:gold-vs-probe": "differs"},
            "perturbation": {"perturbed_cells": 37880},
            "pinned_layer_sha256_before": "a" * 64,
            "pinned_layer_sha256_after": "a" * 64,
        }
        record.update(overrides)
        path = (
            ws / "tasks" / BUILT_TASK_ID / gates.PERTURBATION_PROBE_EVIDENCE_REL
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        return path

    def gate(self, ws: Path, gold=None):
        return gates._gate_data_sensitivity(self.task, ws, gold or self.gold)


# ---------------------------------------------------------------------------
# 1. Pair classification is read off the materialized rows
# ---------------------------------------------------------------------------

class TestPairClassification(BuiltWorkspaceTestCase):
    def test_generated_pool_is_unchanged_and_green(self) -> None:
        result = self.gate(self.fresh())
        self.assertTrue(result.passed, result.details)
        self.assertEqual(
            result.evidence["perturbation:primary-vs-resampled"], "values/rows moved"
        )
        self.assertEqual(
            result.evidence["perturbation:primary-vs-counterfactual"],
            "values/rows moved",
        )

    def test_identical_resampled_gold_is_still_red_when_values_moved(self) -> None:
        # The pre-existing red case (tests/test_gates_eval.py) must not have
        # been softened: the rows really differ, so identical gold is a defect.
        result = self.gate(self.fresh(), self.gold_with_resampled_equal_to_primary())
        self.assertFalse(result.passed)
        self.assertIn("identical", result.details)

    def test_rearrangement_is_classified_from_rows_not_prose(self) -> None:
        ws = self.fresh()
        # Prose alone must buy nothing.
        self.assertFalse(
            gates._pair_is_rearrangement(self.task, ws, "primary", "resampled")
        )
        self.make_rearrangement(ws)
        self.assertTrue(
            gates._pair_is_rearrangement(self.task, ws, "primary", "resampled")
        )
        # The public name (the export side stamps population_relations
        # through it) agrees with the private classifier and is a STRICT bool
        # (CROSS-GROUP CONTRACT `-> bool`).
        self.assertIs(gates.pair_is_rearrangement(self.task, ws, "primary", "resampled"), True)
        self.assertEqual(gates.POPULATION_RELATION_REARRANGEMENT, "rearrangement_of:primary")
        # The counterfactual really removes rows: never a rearrangement.
        self.assertFalse(
            gates._pair_is_rearrangement(self.task, ws, "primary", "counterfactual")
        )
        self.assertIs(gates.pair_is_rearrangement(self.task, ws, "primary", "counterfactual"), False)
        # An unreadable rows artifact: the private classifier answers None
        # (the gate fails closed on it) while the public form asserts NO
        # relation — a manifest can never claim a rearrangement it could not
        # read the evidence for.
        self.rows_path(ws, "resampled", self.task.tables[0].name).unlink()
        self.assertIsNone(gates._pair_is_rearrangement(self.task, ws, "primary", "resampled"))
        self.assertIs(gates.pair_is_rearrangement(self.task, ws, "primary", "resampled"), False)

    def test_missing_rows_artifact_fails_closed(self) -> None:
        ws = self.fresh()
        self.rows_path(ws, "resampled", self.task.tables[0].name).unlink()
        result = self.gate(ws)
        self.assertFalse(result.passed)
        self.assertIn("cannot classify", result.details)

    def test_rearrangement_that_moves_the_gold_is_order_dependence(self) -> None:
        # A check the OLD gate could not express: identical row multisets whose
        # gold differs means the transform depends on physical row order.
        ws = self.fresh()
        self.make_rearrangement(ws)
        result = self.gate(ws)  # frozen resampled gold still differs from primary
        self.assertFalse(result.passed)
        self.assertIn("order-dependent", result.details)


# ---------------------------------------------------------------------------
# 2. A rearrangement-only pair must hand the obligation to the probe
# ---------------------------------------------------------------------------

class TestProbeObligation(BuiltWorkspaceTestCase):
    def setUp(self) -> None:
        self.ws = self.fresh()
        self.make_rearrangement(self.ws)
        self.gold_eq = self.gold_with_resampled_equal_to_primary()

    def test_red_without_a_recorded_probe(self) -> None:
        result = self.gate(self.ws, self.gold_eq)
        self.assertFalse(result.passed)
        self.assertIn("no value-perturbation probe recorded", result.details)
        self.assertIn("never skipped", result.details)

    def test_green_with_a_passing_probe(self) -> None:
        self.write_probe(self.ws)
        result = self.gate(self.ws, self.gold_eq)
        self.assertTrue(result.passed, result.details)
        self.assertIn("scratch-layer value bijection", result.details)
        self.assertEqual(result.evidence["probe_passed"], "true")

    def test_red_on_a_stale_probe(self) -> None:
        self.write_probe(self.ws, task_content_hash="0" * 64)
        result = self.gate(self.ws, self.gold_eq)
        self.assertFalse(result.passed)
        self.assertIn("STALE", result.details)

    def test_red_when_the_probe_moved_the_pinned_layer(self) -> None:
        self.write_probe(self.ws, pinned_layer_sha256_after="b" * 64)
        result = self.gate(self.ws, self.gold_eq)
        self.assertFalse(result.passed)
        self.assertIn("MOVED the pinned population layer", result.details)

    def test_red_when_the_probe_did_not_pass(self) -> None:
        self.write_probe(
            self.ws, passed=False, details="the reward reads no source value"
        )
        result = self.gate(self.ws, self.gold_eq)
        self.assertFalse(result.passed)
        self.assertIn("did not pass", result.details)

    def test_red_on_a_probe_of_the_wrong_kind(self) -> None:
        self.write_probe(self.ws, kind="something-else")
        result = self.gate(self.ws, self.gold_eq)
        self.assertFalse(result.passed)
        self.assertIn("not 'value-bijection'", result.details)

    def test_no_failure_detail_routes_the_task_to_FATAL(self) -> None:
        # repair.route_for_failure reads "contamination"/"licens"/"plagiar" out
        # of the payload as a REJECTION. A data-sensitivity failure is a
        # repairable defect, so none of those words may appear in any detail
        # this gate emits — easy to write by accident when the subject is a
        # licensed snapshot.
        from elt_taskgen import repair

        details = []
        for overrides in (
            {},
            {"passed": False, "details": "the reward reads no source value"},
            {"pinned_layer_sha256_after": "b" * 64},
            {"task_content_hash": "0" * 64},
            {"kind": "something-else"},
        ):
            if overrides:
                self.write_probe(self.ws, **overrides)
            else:
                path = (
                    self.ws / "tasks" / BUILT_TASK_ID
                    / gates.PERTURBATION_PROBE_EVIDENCE_REL
                )
                path.unlink(missing_ok=True)
            result = self.gate(self.ws, self.gold_eq)
            self.assertFalse(result.passed)
            details.append(result.details)
        for text in details:
            for word in repair._FATAL_KEYWORDS:
                self.assertNotIn(word, text.lower(), f"{word!r} in: {text}")

    def test_red_when_the_probe_is_for_another_task(self) -> None:
        self.write_probe(self.ws, task_id="someone_elses_task")
        result = self.gate(self.ws, self.gold_eq)
        self.assertFalse(result.passed)
        self.assertIn("someone_elses_task", result.details)


# ---------------------------------------------------------------------------
# 3. The bijection is structure preserving (this is the whole argument)
# ---------------------------------------------------------------------------

class TestValueBijection(BuiltWorkspaceTestCase):
    def load_primary(self, ws: Path) -> dict[str, list[dict]]:
        out = {}
        for table in self.task.tables:
            out[table.name] = [
                json.loads(ln)
                for ln in self.rows_path(ws, "primary", table.name)
                .read_text(encoding="utf-8")
                .splitlines()
                if ln.strip()
            ]
        return out

    def test_preserves_shape_nullness_and_distinctness(self) -> None:
        rows = self.load_primary(self.fresh())
        out, report = perturbation.perturb_population(self.task, rows)
        self.assertGreater(report["perturbed_cells"], 0)
        for table in self.task.tables:
            before, after = rows[table.name], out[table.name]
            self.assertEqual(len(before), len(after), table.name)
            for col in (c.name for c in table.columns):
                self.assertEqual(
                    [r.get(col) is None for r in before],
                    [r.get(col) is None for r in after],
                    f"{table.name}.{col}: null pattern moved",
                )
                self.assertEqual(
                    len({r.get(col) for r in before if r.get(col) is not None}),
                    len({r.get(col) for r in after if r.get(col) is not None}),
                    f"{table.name}.{col}: distinct count moved (not a bijection)",
                )

    def test_preserves_referential_integrity_exactly(self) -> None:
        rows = self.load_primary(self.fresh())
        out, _ = perturbation.perturb_population(self.task, rows)
        for rel in self.task.relationships:
            parents = {
                tuple(r.get(c) for c in rel.parent_columns)
                for r in out[rel.parent_table]
            }
            for child in out[rel.child_table]:
                key = tuple(child.get(c) for c in rel.child_columns)
                if all(v is None for v in key):
                    continue
                if not rel.required and key not in {
                    tuple(r.get(c) for c in rel.parent_columns)
                    for r in rows[rel.parent_table]
                }:
                    continue  # a dangling key stays dangling: also preserved
                self.assertIn(
                    key, parents,
                    f"{rel.child_table} -> {rel.parent_table}: link broken",
                )

    def test_enum_columns_rotate_inside_their_domain(self) -> None:
        # Relabeling an enum would empty every predicate that filters on it,
        # and an emptied mart is a FALSE green ("differs"). Rotation keeps the
        # domain and still moves every value.
        rows = self.load_primary(self.fresh())
        out, _ = perturbation.perturb_population(self.task, rows)
        for table in self.task.tables:
            for col in table.columns:
                if not col.enum_values:
                    continue
                seen = {r.get(col.name) for r in out[table.name]}
                self.assertTrue(
                    seen <= set(col.enum_values) | {None},
                    f"{table.name}.{col.name} escaped its enum domain: {seen}",
                )

    def test_types_with_no_safe_bijection_are_left_alone(self) -> None:
        from elt_taskgen.models import ColumnType

        for ctype in (ColumnType.BOOLEAN, ColumnType.JSON):
            self.assertEqual(
                perturbation._class_value_map(ctype, None, [True, False]), {}, ctype
            )
        # A one-value enum cannot rotate into anything but itself.
        self.assertEqual(
            perturbation._class_value_map(ColumnType.TEXT, ("only",), ["only"]), {}
        )

    def test_bijections_are_injective_and_type_preserving(self) -> None:
        from elt_taskgen.models import ColumnType

        cases = {
            ColumnType.INTEGER: [1, 2, 3, -7],
            ColumnType.DECIMAL: [1.5, 2.25, 3.0],
            ColumnType.DATE: ["2024-01-05", "2024-01-06"],
            ColumnType.TEXT: ["a", "b", "c"],
        }
        for ctype, values in cases.items():
            vmap = perturbation._class_value_map(ctype, None, values)
            self.assertEqual(len(vmap), len(values), ctype)
            self.assertEqual(len(set(vmap.values())), len(values), f"{ctype}: collision")
            for src, dst in vmap.items():
                self.assertNotEqual(src, dst, ctype)
                self.assertIs(type(src), type(dst), ctype)

    def test_link_classes_join_child_and_parent_columns(self) -> None:
        classes = perturbation.link_classes(self.task)
        for rel in self.task.relationships:
            for child_col, parent_col in zip(rel.child_columns, rel.parent_columns):
                self.assertEqual(
                    classes[(rel.child_table, child_col)],
                    classes[(rel.parent_table, parent_col)],
                    "joined columns must share one value map",
                )


# ---------------------------------------------------------------------------
# 4. The probe end to end, against the real workspace
# ---------------------------------------------------------------------------

class TestPerturbationProbe(BuiltWorkspaceTestCase):
    def test_probe_passes_and_never_writes_the_pinned_layer(self) -> None:
        ws = self.fresh()
        before = perturbation.pinned_layer_digest(ws, BUILT_TASK_ID)
        record = perturbation.run_perturbation_probe(self.task, ws, self.gold)
        self.assertTrue(record["passed"], record["details"])
        self.assertEqual(record["kind"], perturbation.PROBE_KIND)
        self.assertGreater(record["perturbation"]["perturbed_cells"], 0)
        self.assertEqual(record["pinned_layer_sha256_before"], before)
        self.assertEqual(record["pinned_layer_sha256_after"], before)
        # Level-1 integrity, independently re-measured after the call.
        self.assertEqual(perturbation.pinned_layer_digest(ws, BUILT_TASK_ID), before)
        # Keyed by the task's own mart, not a fixture name — this suite binds to
        # whichever real drive runs/ offers.
        self.assertNotIn(
            "identical",
            record["evidence"][f"{self.task.marts[0].name}:gold-vs-probe"],
        )

    def test_probe_is_deterministic(self) -> None:
        ws = self.fresh()
        a = perturbation.run_perturbation_probe(self.task, ws, self.gold)
        b = perturbation.run_perturbation_probe(self.task, ws, self.gold)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_probe_red_when_the_gold_does_not_move(self) -> None:
        # THE failure mode the gate exists to catch: the bijection preserved
        # every structural property and moved every value, and the reference
        # produced the SAME output anyway — so it reads no source value.
        ws = self.fresh()
        from elt_taskgen.reference import runner as runner_mod

        real = runner_mod.run_reference

        def frozen_output(task, population, workspace):
            # Run against the PINNED population instead of the scratch one:
            # a reference that ignores its input behaves exactly like this.
            return real(task, population, ws)

        with mock.patch.object(runner_mod, "run_reference", frozen_output):
            record = perturbation.run_perturbation_probe(self.task, ws, self.gold)
        self.assertFalse(record["passed"])
        self.assertIn("reads no source value", record["details"])

    def test_probe_red_when_the_gold_moves_only_within_reward_tolerance(self) -> None:
        # Byte-different is not moved: a probe output that stays inside
        # compare_mart's rtol-1e-2 tolerance still pays a memorized frozen
        # gold full credit, so the probe must call it STUCK.
        ws = self.fresh()
        from elt_taskgen.reference import runner as runner_mod

        real = runner_mod.run_reference

        def within_tolerance(task, population, workspace):
            result = real(task, population, ws)  # the PINNED (frozen) rows
            moved = {}
            for mart_name, rows in result.mart_rows.items():
                out = []
                for row in rows:
                    new_row = {}
                    for k, v in row.items():
                        if isinstance(v, bool) or v is None:
                            new_row[k] = v
                        elif isinstance(v, (int, float)):
                            new_row[k] = float(v) * 1.005
                        else:
                            new_row[k] = v
                    out.append(new_row)
                moved[mart_name] = out
            return result.model_copy(update={"mart_rows": moved})

        with mock.patch.object(runner_mod, "run_reference", within_tolerance):
            record = perturbation.run_perturbation_probe(self.task, ws, self.gold)
        self.assertFalse(record["passed"], record["details"])
        self.assertIn("tolerance", record["details"])
        comparisons = [
            value
            for key, value in record["evidence"].items()
            if key.endswith(":gold-vs-probe")
        ]
        # A mart with no numeric result cell is unchanged by this deliberately
        # numeric-only stub.  At least one mart must move bytewise while staying
        # inside the reward tolerance; the rest may be exactly identical.
        self.assertTrue(
            any("reward-equivalent" in value for value in comparisons), comparisons
        )
        self.assertTrue(
            all(
                "reward-equivalent" in value or value.startswith("identical")
                for value in comparisons
            ),
            comparisons,
        )

    def test_probe_evidence_says_reward_distinct_when_it_really_moved(self) -> None:
        ws = self.fresh()
        record = perturbation.run_perturbation_probe(self.task, ws, self.gold)
        self.assertTrue(record["passed"], record["details"])
        for key, value in record["evidence"].items():
            if key.endswith(":gold-vs-probe"):
                self.assertIn("reward-distinct", value)

    def test_record_writes_canonical_evidence_where_the_gate_reads_it(self) -> None:
        ws = self.fresh()
        path = perturbation.record_perturbation_probe(self.task, ws, self.gold)
        self.assertEqual(
            path,
            ws / "tasks" / BUILT_TASK_ID / gates.PERTURBATION_PROBE_EVIDENCE_REL,
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(record["passed"])
        # The two constants must never drift apart.
        self.assertEqual(
            gates.PERTURBATION_PROBE_EVIDENCE_REL,
            perturbation.PERTURBATION_PROBE_EVIDENCE_REL,
        )
        self.assertEqual(gates.PERTURBATION_PROBE_KIND, perturbation.PROBE_KIND)

    def test_full_battery_green_on_a_rearranged_pool_with_a_real_probe(self) -> None:
        # The end-to-end claim: a 'provided-rows'-shaped task (resampled is a
        # pure reshuffle, so its gold is identical to primary's) passes
        # data-sensitivity ONLY because a real probe discharged the obligation.
        ws = self.fresh()
        self.make_rearrangement(ws)
        gold_eq = self.gold_with_resampled_equal_to_primary()
        self.assertFalse(self.gate(ws, gold_eq).passed)  # no probe yet
        perturbation.record_perturbation_probe(self.task, ws, gold_eq)
        result = self.gate(ws, gold_eq)
        self.assertTrue(result.passed, result.details)
        self.assertIn("primary-vs-resampled", result.details)


class TestSqlLiteralsAreHeld(unittest.TestCase):
    """A text value the reference SQL names is held at its own value.

    batch50 2026-09-19: the wikidbs cohort marts filter on a real status value
    (film_color IN ('black-and-white')) in a column with no declared enum, the
    rank relabeling renamed that value, the cohort mart went empty, and the
    probe reported INCONCLUSIVE on wikidbs__c40096 and __c80113. Needs no
    built workspace: the demo task's SQL names 'completed'.
    """

    def setUp(self) -> None:
        from elt_taskgen import demo_fixture

        self.task = demo_fixture.demo_task()
        # The same task with orders.status as plain text, as a real pool ships it.
        self.plain = self.task.model_copy(
            update={
                "tables": tuple(
                    t.model_copy(
                        update={
                            "columns": tuple(
                                c.model_copy(update={"enum_values": None})
                                if (t.name, c.name) == ("orders", "status")
                                else c
                                for c in t.columns
                            )
                        }
                    )
                    for t in self.task.tables
                )
            }
        )
        self.rows = {
            "customers": [
                {"customer_id": 1, "customer_name": "Ann"},
                {"customer_id": 2, "customer_name": "Bob"},
            ],
            "orders": [
                {"order_id": 10, "customer_id": 1, "status": "completed"},
                {"order_id": 11, "customer_id": 2, "status": "cancelled"},
            ],
            "order_items": [{"order_id": 10, "quantity": 2, "unit_price": 3.5}],
        }

    def test_the_reference_literals_are_collected(self) -> None:
        self.assertEqual(
            perturbation._sql_string_literals(self.task), frozenset({"completed"})
        )

    def test_a_named_value_is_held_and_every_other_value_moves(self) -> None:
        out, report = perturbation.perturb_population(self.plain, self.rows)
        statuses = [row["status"] for row in out["orders"]]
        self.assertEqual(statuses[0], "completed")
        self.assertNotEqual(statuses[1], "cancelled")
        self.assertNotEqual(out["customers"][0]["customer_name"], "Ann")
        self.assertEqual(report["sql_literal_classes"], ["orders.status:1"])

    def test_a_declared_enum_still_rotates(self) -> None:
        out, report = perturbation.perturb_population(self.task, self.rows)
        self.assertEqual([row["status"] for row in out["orders"]], ["cancelled", "completed"])
        self.assertEqual(report["sql_literal_classes"], [])

    def test_a_held_value_keeps_the_text_map_injective(self) -> None:
        from elt_taskgen.models import ColumnType

        vmap = perturbation._class_value_map(
            ColumnType.TEXT, None, ["black-and-white", "color", "sepia"],
            frozenset({"black-and-white"}),
        )
        self.assertNotIn("black-and-white", vmap)
        self.assertEqual(set(vmap), {"color", "sepia"})
        self.assertEqual(len(set(vmap.values())), 2)
        self.assertNotIn("black-and-white", vmap.values())


if __name__ == "__main__":
    unittest.main()

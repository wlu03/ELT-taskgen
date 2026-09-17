"""Tests for the pre-council filters, the batch path, and advisory triage.

WHY THIS EXISTS
Three fail-closed contracts meet here, and each one is the kind that fails
SILENTLY if nobody tests it:

  * FILTERS (verification/filters.py) run before any provider call and must
    catch the things that are cheap to catch: a counterfactual/stress
    population that perturbs nothing observable, a near-clone of an already
    admitted task, an answer-enumerable candidate shape. Every run leaves a
    ledger-visible artifact, pass or fail, and a tripped filter routes or
    rejects — it is never dropped.
  * BATCH (review/providers.BatchQueue) is an optimization that must be
    invisible: batched answers land in the SAME TranscriptStore under the same
    (role, prompt) key, and anything the Batch API cannot serve falls back to
    the ordinary per-call path. Without keys the queue refuses with the
    standard remedy message; in replay-only mode it never submits anything.
  * TRIAGE (cli.cmd_triage + the audit_triage role) writes ADVISORY labels
    into audit queue entries and may flag for a human. It must be structurally
    incapable of approving: no acceptance vocabulary in its schema, no code
    path to an AuditApproval, and the audit stage unmoved by its output.

Everything here runs offline: filters are pure, the provider layer is doubled
at the TRANSPORT level, and the triage command is driven by a protocol-valid
stub provider (no mock provider is reintroduced).
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import inspect
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
    VERDICT_PASS,
    variant_gate_stage,
)
from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    AcceptanceReport,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    GateResult,
    JoinType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    PopulationName,
    PopulationSpec,
    RepairRoute,
    TableSpec,
    TaskIR,
    canonical_json,
    derive_seed,
    sha256_hex,
    variant_task_id,
)
from elt_taskgen.review import providers as P
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.verification import filters as F
from elt_taskgen.verification import variant_battery


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FakeGold:
    """The recorded reference-runner outputs, in the shape the filter reads."""

    def __init__(self, stage1, stage2_csv):
        self.stage1 = stage1
        self.stage2_csv = stage2_csv


MART = demo_fixture.MART_NAME

_PRIMARY_CSV = "customer_id,completed_order_count,total_spend\n1,2,30.0\n"
_OTHER_CSV = "customer_id,completed_order_count,total_spend\n7,0,0.0\n"


def gold_with(counterfactual_csv=_OTHER_CSV, stress_csv=_OTHER_CSV,
              counterfactual_counts=None, stress_counts=None):
    base_counts = {"customers": 10, "orders": 20, "order_items": 30}
    stage1 = {
        "development": {"customers": 2, "orders": 4, "order_items": 8},
        "primary": dict(base_counts),
        "resampled": dict(base_counts),
        "counterfactual": dict(counterfactual_counts or base_counts),
        "stress": dict(stress_counts or base_counts),
    }
    stage2 = {
        "development": {MART: _OTHER_CSV},
        "primary": {MART: _PRIMARY_CSV},
        "resampled": {MART: _PRIMARY_CSV},
        "counterfactual": {MART: counterfactual_csv},
        "stress": {MART: stress_csv},
    }
    return FakeGold(stage1, stage2)


def tiny_task(task_id="tiny__single_table", *, tables=1, joins=False, ops=2):
    """A minimal candidate used for blacklist / dedup shape checks."""
    columns = (
        ColumnSpec(name="thing_id", type=ColumnType.INTEGER, description="Id."),
        ColumnSpec(name="amount", type=ColumnType.DECIMAL, description="Amount."),
    )
    table_specs = tuple(
        TableSpec(
            name=f"things{i or ''}",
            description="Things.",
            columns=columns,
            primary_key=("thing_id",),
        )
        for i in range(tables)
    )
    plan_ops = [
        MartOp(
            kind=MartOpKind.AGGREGATE,
            description="Sum amount per thing.",
            tables=(table_specs[0].name,),
            columns=("thing_id", "amount"),
        )
    ]
    if joins:
        plan_ops.append(
            MartOp(
                kind=MartOpKind.JOIN,
                description="Join the two thing tables.",
                tables=tuple(t.name for t in table_specs),
                columns=("thing_id",),
                join_type=JoinType.LEFT,
                predicate="things.thing_id = things1.thing_id",
            )
        )
    while len(plan_ops) < ops:
        plan_ops.append(
            MartOp(
                kind=MartOpKind.TIE_BREAK,
                description="Deterministic order by thing_id.",
                columns=("thing_id",),
            )
        )
    mart = MartSpec(
        name="thing_summary",
        description="Per-thing summary.",
        grain="One row per thing.",
        key_columns=("thing_id",),
        columns=(
            MartColumn(name="thing_id", type=ColumnType.INTEGER, description="Id."),
            MartColumn(name="total", type=ColumnType.DECIMAL, description="Total."),
        ),
        plan=MartPlan(mart="thing_summary", ops=tuple(plan_ops[:max(ops, 1)])),
    )
    return TaskIR(
        task_id=task_id,
        family_id=task_id,
        cluster_id=task_id,
        origin=Origin.DEMO,
        license="CC0-1.0",
        title="Tiny",
        tables=table_specs,
        backends=tuple(
            BackendAssignment(table=spec.name, backend=Backend.POSTGRES)
            for spec in table_specs
        ),
        marts=(mart,),
        populations=(
            PopulationSpec(
                name=PopulationName.PRIMARY,
                seed=derive_seed(task_id, "primary"),
                scale={table_specs[0].name: 10},
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 1. Execution-effect filter
# ---------------------------------------------------------------------------

class ExecutionEffectFilterTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()
        self.config = F.load_filter_config()

    def test_no_op_counterfactual_is_caught_and_routed_population(self):
        """A counterfactual whose outputs equal primary's perturbs NOTHING."""
        gold = gold_with(counterfactual_csv=_PRIMARY_CSV)
        report = F.execution_effect_filter(self.task, gold, self.config)
        self.assertFalse(report.passed)
        self.assertEqual(report.action, F.FilterAction.ROUTE)
        self.assertEqual(report.route, RepairRoute.POPULATION)
        subjects = {f.subject for f in report.findings}
        self.assertEqual(subjects, {"counterfactual"})
        finding = report.findings[0]
        self.assertEqual(finding.route, RepairRoute.POPULATION)
        self.assertIn("changes no observable output", finding.summary)

    def test_stage1_only_difference_is_enough(self):
        """Identical marts but different row counts still moves an output."""
        gold = gold_with(
            counterfactual_csv=_PRIMARY_CSV,
            counterfactual_counts={"customers": 3, "orders": 2, "order_items": 4},
        )
        report = F.execution_effect_filter(self.task, gold, self.config)
        self.assertTrue(report.passed, msg=report.detail)
        self.assertEqual(
            report.evidence["counterfactual:changed_outputs"], "stage1"
        )

    def test_distinct_populations_pass(self):
        report = F.execution_effect_filter(self.task, gold_with(), self.config)
        self.assertTrue(report.passed, msg=report.detail)
        self.assertEqual(report.findings, ())
        self.assertIsNone(report.action)

    def test_missing_baseline_fails_closed(self):
        gold = gold_with()
        gold.stage1.pop("primary")
        gold.stage2_csv.pop("primary")
        report = F.execution_effect_filter(self.task, gold, self.config)
        self.assertFalse(report.passed)
        self.assertEqual(report.findings[0].subject, "primary")

    def test_declared_population_without_recorded_outputs_fails_closed(self):
        gold = gold_with()
        gold.stage1.pop("stress")
        gold.stage2_csv.pop("stress")
        report = F.execution_effect_filter(self.task, gold, self.config)
        self.assertFalse(report.passed)
        self.assertIn(
            "no recorded reference outputs", report.findings[0].summary
        )

    def test_gold_bound_to_another_identity_fails_closed(self):
        gold = gold_with()
        gold.task_content_hash = "b" * 64
        report = F.execution_effect_filter(self.task, gold, self.config)
        self.assertFalse(report.passed)
        self.assertIn("gold", {f.subject for f in report.findings})

    def test_both_populations_reported_not_just_the_first(self):
        gold = gold_with(counterfactual_csv=_PRIMARY_CSV, stress_csv=_PRIMARY_CSV)
        report = F.execution_effect_filter(self.task, gold, self.config)
        self.assertEqual(
            {f.subject for f in report.findings}, {"counterfactual", "stress"}
        )


# ---------------------------------------------------------------------------
# 2. Near-duplicate intake dedup
# ---------------------------------------------------------------------------

class NearDuplicateFilterTest(unittest.TestCase):
    def setUp(self):
        self.task = demo_fixture.demo_task()
        self.config = F.load_filter_config()

    def test_clone_under_a_new_id_is_caught(self):
        clone = self.task.model_copy(
            update={
                "task_id": "cloned__customer_summary",
                "family_id": "cloned__customer_summary",
                "cluster_id": "cloned__customer_summary",
            }
        )
        report = F.near_duplicate_filter(self.task, [clone], self.config)
        self.assertFalse(report.passed)
        self.assertEqual(report.action, F.FilterAction.REJECT)
        self.assertEqual(report.findings[0].subject, "cloned__customer_summary")
        self.assertEqual(report.evidence["max_similarity"], "1.0000")

    def test_a_genuinely_different_task_is_admitted(self):
        report = F.near_duplicate_filter(self.task, [tiny_task()], self.config)
        self.assertTrue(report.passed, msg=report.detail)
        self.assertLess(float(report.evidence["max_similarity"]), 0.85)

    def test_empty_admitted_set_admits(self):
        report = F.near_duplicate_filter(self.task, [], self.config)
        self.assertTrue(report.passed)
        self.assertEqual(report.evidence["compared_against"], "0")

    def test_the_candidate_never_duplicates_itself(self):
        report = F.near_duplicate_filter(self.task, [self.task], self.config)
        self.assertTrue(report.passed)

    def test_shared_schema_with_a_different_plan_is_not_a_duplicate(self):
        """`combine: min` is the conservative choice — reusing a schema while
        computing something else must not be rejected as a clone."""
        other_mart = MartSpec(
            name="order_backlog",
            description="Backlog of unfulfilled orders by status.",
            grain="One row per order status.",
            key_columns=("status",),
            columns=(
                MartColumn(name="status", type=ColumnType.TEXT,
                           description="Order status value."),
                MartColumn(name="backlog_orders", type=ColumnType.INTEGER,
                           description="Count of orders in that status."),
            ),
            plan=MartPlan(
                mart="order_backlog",
                ops=(
                    MartOp(
                        kind=MartOpKind.AGGREGATE,
                        description="Count orders grouped by status value.",
                        tables=("orders",),
                        columns=("status", "order_id"),
                    ),
                    MartOp(
                        kind=MartOpKind.TIE_BREAK,
                        description="Deterministic order: sort by status.",
                        columns=("status",),
                    ),
                ),
            ),
        )
        sibling = self.task.model_copy(
            update={
                "task_id": "sibling__order_backlog",
                "family_id": "sibling__order_backlog",
                "cluster_id": "sibling__order_backlog",
                "marts": (other_mart,),
                "reference": None,
                "attack_cases": (),
            }
        )
        report = F.near_duplicate_filter(self.task, [sibling], self.config)
        self.assertTrue(report.passed, msg=report.detail)

    def test_jaccard_edges(self):
        self.assertEqual(F.jaccard([], []), 1.0)
        self.assertEqual(F.jaccard(["a"], []), 0.0)
        self.assertEqual(F.jaccard(["a", "b"], ["a"]), 0.5)


# ---------------------------------------------------------------------------
# 3. Intake format blacklist
# ---------------------------------------------------------------------------

class FormatBlacklistFilterTest(unittest.TestCase):
    def setUp(self):
        self.config = F.load_filter_config()

    def test_single_table_no_join_is_blacklisted(self):
        report = F.format_blacklist_filter(tiny_task(), self.config)
        self.assertFalse(report.passed)
        self.assertEqual(report.action, F.FilterAction.REJECT)
        tripped = {f.subject for f in report.findings}
        self.assertIn("single-table-no-join", tripped)
        self.assertIn("too-few-source-tables", tripped)

    def test_the_demo_candidate_is_clean(self):
        report = F.format_blacklist_filter(demo_fixture.demo_task(), self.config)
        self.assertTrue(report.passed, msg=report.detail)
        self.assertEqual(
            set(report.evidence[f"rule:{r.name}"] for r in
                self.config.format_blacklist.rules),
            {"clean"},
        )

    def test_duplicate_mart_contract_rejected(self):
        """Two marts differing only by name are one computation graded twice
        (reddit_ads url_report was a byte-clone of ad_report): REJECT, naming
        both. A mart with a different plan is not a clone."""
        base = tiny_task(task_id="two__tables", tables=2, joins=True, ops=3)
        first = base.marts[0]
        clone = first.model_copy(
            update={
                "name": "thing_summary_again",
                "description": "Per-thing summary, worded differently.",
                "plan": first.plan.model_copy(
                    update={"mart": "thing_summary_again", "notes": "other prose"}
                ),
            }
        )
        different = first.model_copy(
            update={
                "name": "thing_summary_short",
                "plan": first.plan.model_copy(
                    update={"mart": "thing_summary_short", "ops": first.plan.ops[:2]}
                ),
            }
        )
        task = base.model_copy(update={"marts": (first, clone, different)})
        report = F.format_blacklist_filter(task, self.config)
        self.assertFalse(report.passed)
        tripped = {f.subject: f.detail for f in report.findings}
        self.assertIn("duplicate-mart-contract", tripped)
        self.assertIn("thing_summary", tripped["duplicate-mart-contract"])
        self.assertIn("thing_summary_again", tripped["duplicate-mart-contract"])
        self.assertNotIn("thing_summary_short", tripped["duplicate-mart-contract"])
        # Without the clone the rule is clean (prose differences never trip it).
        clean = base.model_copy(update={"marts": (first, different)})
        self.assertEqual(
            F.format_blacklist_filter(clean, self.config).evidence[
                "rule:duplicate-mart-contract"
            ],
            "clean",
        )

    def test_mart_contract_signature_factors_whole_identifiers_only(self):
        """The mart's name is factored out of its ops on identifier
        boundaries: a mart `orders` whose ops read `orders_lines` must not
        collide with a mart `orders_v2` whose ops read `orders_v2_lines` (a
        bare-substring replace turns both into `<mart>_lines`, a false clone).
        A genuine clone — same ops, only the mart's own name embedded — still
        collides."""

        def mart(name: str, table: str) -> MartSpec:
            return MartSpec(
                name=name,
                description="Per-thing summary.",
                grain="One row per thing.",
                key_columns=("thing_id",),
                columns=(
                    MartColumn(name="thing_id", type=ColumnType.INTEGER, description="Id."),
                    MartColumn(name="total", type=ColumnType.DECIMAL, description="Total."),
                ),
                plan=MartPlan(
                    mart=name,
                    ops=(
                        MartOp(
                            kind=MartOpKind.AGGREGATE,
                            description=f"Sum amount per thing for {name}.",
                            tables=(table,),
                            columns=("thing_id", "amount"),
                        ),
                    ),
                ),
            )

        self.assertNotEqual(
            F.mart_contract_signature(mart("orders", "orders_lines")),
            F.mart_contract_signature(mart("orders_v2", "orders_v2_lines")),
        )
        # Whole-identifier occurrences of the mart's own name ARE factored.
        self.assertEqual(
            F.mart_contract_signature(mart("orders", "things")),
            F.mart_contract_signature(mart("orders_v2", "things")),
        )
        self.assertNotIn("orders", F.mart_contract_signature(mart("orders", "things")))

    def test_reserved_relation_name_is_admitted_by_default(self):
        """Keyword relation names are quoted downstream, not blacklisted."""
        base = tiny_task(task_id="two__tables", tables=2, joins=True, ops=3)

        def renamed(old: str, new: str) -> TaskIR:
            tables = tuple(
                t.model_copy(update={"name": new}) if t.name == old else t
                for t in base.tables
            )
            backends = tuple(
                b.model_copy(update={"table": new}) if b.table == old else b
                for b in base.backends
            )
            marts = tuple(
                m.model_copy(
                    update={
                        "plan": m.plan.model_copy(
                            update={
                                "ops": tuple(
                                    op.model_copy(
                                        update={
                                            "tables": tuple(
                                                new if x == old else x for x in op.tables
                                            )
                                        }
                                    )
                                    for op in m.plan.ops
                                )
                            }
                        )
                    }
                )
                for m in base.marts
            )
            pops = tuple(
                pop.model_copy(
                    update={"scale": {(new if k == old else k): v for k, v in pop.scale.items()}}
                )
                for pop in base.populations
            )
            return base.model_copy(
                update={"tables": tables, "backends": backends, "marts": marts, "populations": pops}
            )

        for keyword in ("group", "order", "left"):
            reports = F.run_intake_filters(renamed("things1", keyword), (), self.config)
            self.assertTrue(all(report.passed for report in reports), keyword)
            self.assertNotIn(
                "rule:reserved-word-relation-name",
                reports[0].evidence,
            )

        # The diagnostic remains available to an explicit custom policy, but
        # its naming restriction is not part of the shipped intake contract.
        diagnostic_rule = F.FormatBlacklistRule(
            name="reserved-word-relation-name",
            kind="reserved_identifiers",
            params={},
        )
        custom = self.config.model_copy(
            update={
                "format_blacklist": self.config.format_blacklist.model_copy(
                    update={"rules": (diagnostic_rule,)}
                )
            }
        )
        report = F.format_blacklist_filter(renamed("things1", "group"), custom)
        self.assertFalse(report.passed)
        self.assertEqual(report.findings[0].subject, "reserved-word-relation-name")

        mart_named = base.model_copy(
            update={
                "marts": tuple(
                    m.model_copy(
                        update={
                            "name": "group",
                            "plan": m.plan.model_copy(update={"mart": "group"}),
                        }
                    )
                    for m in base.marts
                )
            }
        )
        reports = F.run_intake_filters(mart_named, (), self.config)
        self.assertTrue(all(report.passed for report in reports))

    def test_reserved_word_list_agrees_with_the_executor(self):
        """The optional diagnostic vocabulary tracks the pinned executor."""
        import duckdb

        refused = F.duckdb_reserved_words()
        con = duckdb.connect()
        try:
            keywords = [
                str(r[0]) for r in con.execute(
                    "SELECT keyword_name FROM duckdb_keywords()"
                ).fetchall()
            ]
            failing = []
            for kw in keywords:
                try:
                    con.execute(f'CREATE OR REPLACE TABLE "{kw}" ("c" INTEGER)')
                    con.execute(f'SELECT {kw}."c" AS "c" FROM "{kw}" AS {kw}')
                except duckdb.Error:
                    failing.append(kw.lower())
        finally:
            con.close()
        self.assertTrue(failing)  # the sweep saw the executor refuse something
        self.assertIn("order", failing)
        self.assertIn("left", failing)
        self.assertEqual(sorted(set(failing) - refused), [])
        # Sanity: the refused set is category-driven, not a hand list.
        self.assertEqual(F.RESERVED_KEYWORD_CATEGORIES, ("reserved", "type_function"))

    def test_trivial_plan_rule(self):
        task = tiny_task(task_id="two__tables", tables=2, joins=True, ops=1)
        config = F.FilterConfig(
            format_blacklist=F.FormatBlacklistConfig(
                rules=(
                    F.FormatBlacklistRule(
                        name="trivial-mart-plan",
                        kind="min_plan_ops",
                        params={"min_ops": 3},
                    ),
                ),
            )
        )
        report = F.format_blacklist_filter(task, config)
        self.assertFalse(report.passed)
        self.assertIn("fewer than 3 plan ops", report.findings[0].detail)


# ---------------------------------------------------------------------------
# Config + artifact contracts
# ---------------------------------------------------------------------------

class FilterConfigTest(unittest.TestCase):
    def test_repo_config_loads_and_names_its_source(self):
        config = F.load_filter_config()
        self.assertTrue(config.execution_effect.enabled)
        self.assertIn("filters.yaml", config.source)

    def test_explicit_missing_path_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            F.load_filter_config(Path("/nonexistent/filters.yaml"))

    def test_unknown_rule_kind_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "filters.yaml"
            path.write_text(
                "format_blacklist:\n"
                "  rules:\n"
                "    - {name: mystery, kind: vibes, params: {}}\n"
            )
            with self.assertRaises(ValueError):
                F.load_filter_config(path)

    def test_unknown_combine_fails_closed(self):
        with self.assertRaises(ValueError):
            F.NearDuplicateConfig(combine="cosine")

    def test_repo_config_and_embedded_defaults_do_not_drift(self):
        """config/filters.yaml is the source of truth; the embedded copy keeps
        the module usable without a checkout — they must agree."""
        from_file = F.load_filter_config().model_dump(exclude={"source"})
        embedded = F.FilterConfig(**F.DEFAULT_FILTER_CONFIG_DOC).model_dump(
            exclude={"source"}
        )
        self.assertEqual(from_file, embedded)

    def test_default_rules_include_clone_guard_but_allow_reserved_words(self):
        by_name = {r.name: r.kind for r in F.load_filter_config().format_blacklist.rules}
        self.assertEqual(by_name.get("duplicate-mart-contract"), "distinct_mart_plans")
        self.assertNotIn("reserved-word-relation-name", by_name)


class FilterReportTest(unittest.TestCase):
    def _passing(self, **overrides):
        base = dict(
            filter="x",
            task_id="t",
            task_content_hash="a" * 64,
            passed=True,
        )
        base.update(overrides)
        return F.FilterReport(**base)

    def test_a_passing_report_cannot_carry_findings(self):
        finding = F.FilterFinding(filter="x", subject="s", summary="s")
        with self.assertRaises(ValueError):
            self._passing(findings=(finding,))

    def test_a_failing_report_needs_findings_and_an_action(self):
        with self.assertRaises(ValueError):
            F.FilterReport(
                filter="x", task_id="t", task_content_hash="a" * 64, passed=False
            )

    def test_a_routed_failure_must_name_its_route(self):
        finding = F.FilterFinding(filter="x", subject="s", summary="s")
        with self.assertRaises(ValueError):
            F.FilterReport(
                filter="x",
                task_id="t",
                task_content_hash="a" * 64,
                passed=False,
                action=F.FilterAction.ROUTE,
                findings=(finding,),
            )

    def test_written_artifact_is_deterministic(self):
        report = self._passing(detail="fine")
        with tempfile.TemporaryDirectory() as tmp:
            first_path, first_sha = F.write_filter_report(report, Path(tmp))
            second_path, second_sha = F.write_filter_report(report, Path(tmp))
            self.assertEqual(first_path, second_path)
            self.assertEqual(first_sha, second_sha)
            self.assertEqual(first_sha, sha256_hex(first_path.read_text()))
            self.assertEqual(first_path.parent.name, F.FILTERS_DIRNAME)


# ---------------------------------------------------------------------------
# CLI wiring: filters run before any provider call and are ledger-visible
# ---------------------------------------------------------------------------

def _write_fake_gold(answer_key_dir: Path, task: TaskIR, per_pop_csv: dict) -> None:
    """Hand-built frozen gold (no execution) that load_gold accepts."""
    gold_dir = answer_key_dir / "gold"
    files: dict[str, str] = {}
    for population, csv_text in sorted(per_pop_csv.items()):
        pop_dir = gold_dir / population
        pop_dir.mkdir(parents=True, exist_ok=True)
        counts = canonical_json({t.name: 3 for t in task.tables})
        (pop_dir / "stage1_counts.json").write_text(counts, encoding="utf-8")
        files[f"gold/{population}/stage1_counts.json"] = sha256_hex(counts)
        (pop_dir / f"{MART}.csv").write_text(csv_text, encoding="utf-8")
        files[f"gold/{population}/{MART}.csv"] = sha256_hex(csv_text)
    (answer_key_dir / "manifest.json").write_text(
        canonical_json(
            {
                "task_id": task.task_id,
                "task_content_hash": task.content_hash(),
                "files": files,
            }
        ),
        encoding="utf-8",
    )


class FilterWiringTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.engine.close)

    def _artifact_rows(self, task_id):
        cur = self.engine._con.execute(
            "SELECT rel_path FROM artifacts WHERE task_id=?", (task_id,)
        )
        return [row[0] for row in cur.fetchall()]

    def test_intake_filters_admit_the_demo_and_record_both_artifacts(self):
        task = demo_fixture.demo_task()
        self.engine.register(task)
        self.assertIsNone(cli.run_intake_filters(self.engine, task))
        recorded = self._artifact_rows(task.task_id)
        for name in (F.FORMAT_BLACKLIST_FILTER, F.NEAR_DUPLICATE_FILTER):
            rel = f"tasks/{task.task_id}/reports/filters/{name}.json"
            self.assertIn(rel, recorded)
            self.assertTrue((self.workspace / rel).is_file())

    def test_clone_of_an_admitted_task_is_rejected_at_intake(self):
        admitted = demo_fixture.demo_task()
        self.engine.register(admitted)
        # Admission is read from the ledger: both RLVR units need a complete
        # passing battery at the current hash.  The shared gates report alone
        # is task-integrity evidence, not an acceptance verdict.
        for variant in RLVR_TASK_VARIANTS:
            report = AcceptanceReport.from_gates(
                task_id=variant_task_id(admitted.task_id, variant),
                revision=admitted.current_revision,
                task_content_hash=admitted.content_hash(),
                gates=tuple(
                    GateResult(gate=name, passed=True, details="test fixture pass")
                    for name in variant_battery.gate_roster(variant)
                ),
                scorer_version="test",
            )
            self.engine.record_report(
                admitted,
                variant_gate_stage(variant).value,
                VERDICT_PASS,
                report,
            )
        clone = admitted.model_copy(
            update={
                "task_id": "clone__customer_summary",
                "family_id": "clone__customer_summary",
                "cluster_id": "clone__customer_summary",
            }
        )
        self.engine.register(clone)
        outcome = cli.run_intake_filters(self.engine, clone)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.verdict, cli.VERDICT_FATAL)
        self.assertIn("near-duplicate", outcome.payload.error)
        # ... and the artifact that proves it is recorded, not just printed.
        rel = outcome.payload.data["artifact"]
        report = json.loads((self.workspace / rel).read_text())
        self.assertFalse(report["passed"])
        self.assertEqual(report["findings"][0]["subject"], admitted.task_id)

    def test_blacklisted_shape_is_rejected_at_intake(self):
        task = tiny_task()
        self.engine.register(task)
        outcome = cli.run_intake_filters(self.engine, task)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.verdict, cli.VERDICT_FATAL)
        self.assertIn("blacklisted candidate shape", outcome.payload.error)

    def test_intake_wrapper_blocks_before_the_wrapped_stage_runs(self):
        task = tiny_task()
        self.engine.register(task)
        calls: list[str] = []

        def inner(engine, task):  # pragma: no cover - must never run
            calls.append(task.task_id)
            raise AssertionError("wrapped stage ran despite a blocking filter")

        wrapped = cli._with_intake_filters(inner)
        outcome = wrapped(self.engine, task)
        self.assertEqual(outcome.verdict, cli.VERDICT_FATAL)
        self.assertEqual(calls, [])

    def test_execution_effect_filter_blocks_the_reference_stage(self):
        task = demo_fixture.demo_task()
        self.engine.register(task)
        akd = self.engine.task_dir(task.task_id) / "answer_key"
        _write_fake_gold(
            akd,
            task,
            {
                "development": _OTHER_CSV,
                "primary": _PRIMARY_CSV,
                "resampled": _PRIMARY_CSV,
                "counterfactual": _PRIMARY_CSV,   # perturbs nothing
                "stress": _OTHER_CSV,
            },
        )
        inner_calls: list[str] = []

        def inner(engine, task):
            inner_calls.append(task.task_id)
            return cli.StageOutcome(VERDICT_PASS, StagePayload(detail="gold frozen"))

        wrapped = cli._with_execution_effect_filter(inner)
        outcome = wrapped(self.engine, task)
        self.assertEqual(inner_calls, [task.task_id])   # the stage DID run
        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertEqual(outcome.route, RepairRoute.POPULATION)
        self.assertIn("counterfactual", outcome.payload.error)
        rel = f"tasks/{task.task_id}/reports/filters/{F.EXECUTION_EFFECT_FILTER}.json"
        self.assertIn(rel, self._artifact_rows(task.task_id))

    def test_execution_effect_filter_passes_distinct_populations(self):
        task = demo_fixture.demo_task()
        self.engine.register(task)
        akd = self.engine.task_dir(task.task_id) / "answer_key"
        _write_fake_gold(
            akd,
            task,
            {
                "development": _OTHER_CSV,
                "primary": _PRIMARY_CSV,
                "resampled": _PRIMARY_CSV,
                "counterfactual": _OTHER_CSV,
                "stress": "customer_id,completed_order_count,total_spend\n9,5,1.0\n",
            },
        )
        self.assertIsNone(cli.run_execution_effect_filter(self.engine, task))

# ---------------------------------------------------------------------------
# Batch path
# ---------------------------------------------------------------------------

VALID_FINDING = {
    "severity": "major",
    "summary": "constants shortcut must lose reward somewhere",
    "detail": "compile a constants mutant",
    "route_hint": None,
    "suggested_attack": "constants",
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
        "rationale": "a constant-output shortcut should lose transform reward",
    },
}


def anthropic_tool_message(findings=None, *, model="claude-sonnet-5",
                           input_tokens=100, output_tokens=20,
                           tool_name=P.FINDINGS_TOOL_NAME):
    return {
        "model": model,
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": tool_name,
                "input": {"findings": findings if findings is not None else [VALID_FINDING]},
            }
        ],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


class FakeTransport:
    """Interactive (per-call) transport double."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, payload):
        self.calls.append((url, dict(headers), payload))
        if not self.responses:
            raise AssertionError("unexpected interactive HTTP call")
        return self.responses.pop(0)


class FakeBatchTransport:
    """Batch API double: POST submit, GET poll, GET results (JSONL-equivalent)."""

    def __init__(self, results=(), *, polls_before_end=1, fail=None):
        self.results = list(results)
        self.polls_before_end = polls_before_end
        self.fail = fail
        self.calls = []
        self._polls = 0

    def __call__(self, method, url, headers, payload=None):
        self.calls.append((method, url, dict(headers), payload))
        if self.fail is not None:
            raise self.fail
        if method == "POST":
            return {"id": "msgbatch_1", "processing_status": "in_progress"}
        if url.endswith("/results"):
            return {"results": self.results}
        self._polls += 1
        status = (
            P.BATCH_STATUS_ENDED
            if self._polls >= self.polls_before_end
            else "in_progress"
        )
        return {
            "id": "msgbatch_1",
            "processing_status": status,
            "results_url": f"{url}/results",
        }


def batch_routing(*, api_key="sk-test", oss_base="http://oss:8000/v1",
                  oss_model="test-oss-model"):
    roles = {
        "ambiguity_critic": P.RoleRoute(
            "ambiguity_critic", "anthropic", "claude-sonnet-5", 1024, "medium"
        ),
        "shortcut_attacker": P.RoleRoute(
            "shortcut_attacker", "anthropic", "claude-opus-5", 1024, "high"
        ),
        "audit_triage": P.RoleRoute(
            "audit_triage", "anthropic", "claude-sonnet-5", 1024, "medium"
        ),
        "independent_implementer": P.RoleRoute(
            "independent_implementer", "openai_compat", oss_model, 2048, None
        ),
    }
    return P.RoleRouting(
        roles=roles,
        provider_config={
            "anthropic": {"api_key": api_key},
            # An openai_compat route MUST be priced: rates_for fails closed on
            # an unpriced model rather than metering $0 (a paid OpenRouter
            # route once metered as free — docs/runs/demo.md §8.5).
            "openai_compat": {
                "base_url": oss_base, "api_key": "", "model": oss_model,
                "usd_per_mtok_input": 0.70, "usd_per_mtok_output": 3.50,
            },
        },
        source="(test routing)",
    )


class BatchQueueTest(unittest.TestCase):
    """Exercise the one-shot batch optimization under its explicit rollback.

    The shipped shortcut attacker and independent implementer now run bounded
    tool-using sessions, which are deliberately never batchable.  These queue
    mechanics predate that default and still matter for an operator who sets
    either role's ``session.enabled`` to false, so every test in this class
    applies exactly that documented rollback instead of silently assuming the
    shipping profile is one-shot.
    """

    def setUp(self):
        P.clear_behavior_caches()
        shipped = json.loads(json.dumps(P._agents_doc()))
        for role in ("shortcut_attacker", "independent_implementer"):
            self.assertTrue(shipped["roles"][role]["session"]["enabled"], role)
            self.assertTrue(P.role_is_agentic(role), role)
            shipped["roles"][role]["session"]["enabled"] = False
        self._rollback = mock.patch.object(P, "_agents_doc", lambda: shipped)
        self._rollback.start()
        P.clear_behavior_caches()
        for role in ("shortcut_attacker", "independent_implementer"):
            self.assertFalse(P.role_is_agentic(role), role)

    def tearDown(self):
        self._rollback.stop()
        P.clear_behavior_caches()

    def _provider(self, tmp, *, api_key="sk-test", replay_only=False,
                  responses=(), fixtures=None):
        transport = FakeTransport(responses)
        provider = P.RoutedProvider(
            batch_routing(api_key=api_key),
            P.TranscriptStore(Path(tmp) / "transcripts", fixtures_dir=fixtures),
            P.CostMeter(budget_per_task_usd=100.0),
            replay_only=replay_only,
            task_id="task-1",
            transports={"anthropic": transport, "openai_compat": transport},
        )
        return provider, transport

    def test_groups_pending_calls_by_routed_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(tmp)
            queue = P.BatchQueue(provider, batch_transport=FakeBatchTransport())
            queue.add("ambiguity_critic", "view A")
            queue.add("shortcut_attacker", "view B")
            queue.add("independent_implementer", "build it")
            groups = queue.groups()
            self.assertEqual(set(groups), {"anthropic", "openai_compat"})
            self.assertEqual(len(groups["anthropic"]), 2)
            self.assertTrue(queue.batchable("anthropic", 2))
            # openai_compat ignores batching entirely (serial).
            self.assertFalse(queue.batchable("openai_compat", 5))

    def test_default_transport_is_the_real_batch_api(self):
        """Unset means live batching; an explicit None disables it."""
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(tmp)
            self.assertIs(
                P.BatchQueue(provider)._batch_transport, P.batch_transport_default
            )
            self.assertIsNone(
                P.BatchQueue(provider, batch_transport=None)._batch_transport
            )

    def test_identical_calls_share_one_custom_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(tmp)
            queue = P.BatchQueue(provider, batch_transport=None)
            first = queue.add("ambiguity_critic", "same view")
            second = queue.add("ambiguity_critic", "same view")
            self.assertEqual(first, second)
            self.assertEqual(len(queue), 1)

    def test_batch_submission_collects_into_the_same_transcript_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, interactive = self._provider(tmp)
            queue = P.BatchQueue(
                provider,
                batch_transport=None,   # replaced below with the wire double
                sleep=lambda _s: None,
            )
            cid_a = queue.add("ambiguity_critic", "view A")
            cid_b = queue.add("shortcut_attacker", "view B")
            batch = FakeBatchTransport(
                results=[
                    {"custom_id": cid_a,
                     "result": {"type": "succeeded",
                                "message": anthropic_tool_message()}},
                    {"custom_id": cid_b,
                     "result": {"type": "succeeded",
                                "message": anthropic_tool_message(
                                    model="claude-opus-5")}},
                ],
                polls_before_end=2,
            )
            queue._batch_transport = batch
            result = queue.run()

            self.assertEqual(set(result.batched), {cid_a, cid_b})
            self.assertEqual(result.serial, ())
            self.assertEqual(interactive.calls, [])   # zero interactive HTTP
            # ONE submission for both calls, then polls, then results.
            self.assertEqual(
                sum(1 for c in batch.calls if c[0] == "POST"), 1
            )
            submitted = batch.calls[0][3]["requests"]
            self.assertEqual(len(submitted), 2)
            self.assertEqual(
                {r["custom_id"] for r in submitted}, {cid_a, cid_b}
            )
            self.assertEqual(
                submitted[0]["params"]["tool_choice"],
                {"type": "tool", "name": P.FINDINGS_TOOL_NAME},
            )
            # Answers land in the SAME store, under the interactive key.
            store = provider.store
            entry = store.lookup("ambiguity_critic", P.transcript_key("ambiguity_critic", "view A"))
            self.assertIsNotNone(entry)
            self.assertEqual(entry["response"], result.text_for(cid_a))
            self.assertEqual(entry["batch_id"], "msgbatch_1")
            # ... so a later interactive call is served with zero HTTP.
            self.assertEqual(
                provider.complete("ambiguity_critic", "view A"),
                result.text_for(cid_a),
            )
            self.assertGreater(provider.meter.total_usd, 0.0)

    def test_batch_unavailable_falls_back_to_per_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, interactive = self._provider(
                tmp,
                responses=[anthropic_tool_message(), anthropic_tool_message()],
            )
            queue = P.BatchQueue(
                provider,
                batch_transport=FakeBatchTransport(
                    fail=RuntimeError("batch API HTTP 503")
                ),
                sleep=lambda _s: None,
            )
            cid_a = queue.add("ambiguity_critic", "view A")
            cid_b = queue.add("shortcut_attacker", "view B")
            result = queue.run()
            self.assertEqual(set(result.serial), {cid_a, cid_b})
            self.assertEqual(result.batched, ())
            self.assertEqual(len(interactive.calls), 2)
            self.assertTrue(
                any("batch unavailable" in r for r in result.fallback_reasons)
            )
            self.assertTrue(
                provider.store.lookup("shortcut_attacker", P.transcript_key("shortcut_attacker", "view B"))
            )

    def test_one_unusable_result_falls_back_for_that_call_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, interactive = self._provider(
                tmp, responses=[anthropic_tool_message()]
            )
            queue = P.BatchQueue(provider, batch_transport=None,
                                 sleep=lambda _s: None)
            cid_a = queue.add("ambiguity_critic", "view A")
            cid_b = queue.add("shortcut_attacker", "view B")
            queue._batch_transport = FakeBatchTransport(
                results=[
                    {"custom_id": cid_a,
                     "result": {"type": "succeeded",
                                "message": anthropic_tool_message()}},
                    {"custom_id": cid_b,
                     "result": {"type": "errored",
                                "error": {"type": "overloaded_error"}}},
                ]
            )
            result = queue.run()
            self.assertEqual(result.batched, (cid_a,))
            self.assertEqual(result.serial, (cid_b,))
            self.assertEqual(len(interactive.calls), 1)
            self.assertTrue(
                any("unusable" in r for r in result.fallback_reasons)
            )

    def test_openai_compat_group_is_never_batched(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, interactive = self._provider(
                tmp,
                responses=[
                    {
                        "model": "test-oss-model",
                        "choices": [{"message": {"content": "built it"}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    }
                ],
            )
            batch = FakeBatchTransport()
            queue = P.BatchQueue(provider, batch_transport=batch,
                                 sleep=lambda _s: None, min_batch_size=1)
            cid = queue.add("independent_implementer", "build the marts")
            result = queue.run()
            self.assertEqual(result.serial, (cid,))
            self.assertEqual(batch.calls, [])
            self.assertTrue(interactive.calls[0][0].endswith("/chat/completions"))

    def test_lone_anthropic_call_runs_serially(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, interactive = self._provider(
                tmp, responses=[anthropic_tool_message()]
            )
            batch = FakeBatchTransport()
            queue = P.BatchQueue(provider, batch_transport=batch,
                                 sleep=lambda _s: None)
            cid = queue.add("ambiguity_critic", "view A")
            result = queue.run()
            self.assertEqual(result.serial, (cid,))
            self.assertEqual(batch.calls, [])
            self.assertEqual(len(interactive.calls), 1)

    def test_recorded_transcripts_are_served_with_zero_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, interactive = self._provider(tmp)
            sha = P.transcript_key("ambiguity_critic", "view A")
            # A transcript is served only for the (provider, model) that
            # produced it, so the seed names the route `batch_routing` binds
            # ambiguity_critic to.
            provider.store.record(
                "ambiguity_critic", sha,
                {
                    "prompt_sha256": sha,
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "response": '{"findings": []}',
                },
            )
            batch = FakeBatchTransport()
            queue = P.BatchQueue(provider, batch_transport=batch,
                                 sleep=lambda _s: None, min_batch_size=1)
            cid = queue.add("ambiguity_critic", "view A")
            result = queue.run()
            self.assertEqual(result.memoized, (cid,))
            self.assertEqual(batch.calls, [])
            self.assertEqual(interactive.calls, [])
            self.assertEqual(result.text_for(cid), '{"findings": []}')

    def test_memoized_entry_from_other_route_is_not_served(self):
        """A transcript recorded under another model is a MISS in live mode
        and fails closed in replay-only mode — never silently replayed."""
        entry = {
            "prompt_sha256": P.transcript_key("ambiguity_critic", "view A"),
            "provider": "anthropic",
            "model": "other-model",
            "response": '{"findings": []}',
        }
        with tempfile.TemporaryDirectory() as tmp:
            provider, interactive = self._provider(
                tmp, responses=[anthropic_tool_message()]
            )
            provider.store.record("ambiguity_critic", entry["prompt_sha256"], entry)
            queue = P.BatchQueue(provider, batch_transport=None,
                                 sleep=lambda _s: None, min_batch_size=1)
            cid = queue.add("ambiguity_critic", "view A")
            result = queue.run()
            self.assertNotIn(cid, result.memoized)
            self.assertEqual(len(interactive.calls), 1, "a miss re-records live")
        with tempfile.TemporaryDirectory() as tmp:
            provider, _interactive = self._provider(tmp, replay_only=True)
            provider.store.record("ambiguity_critic", entry["prompt_sha256"], entry)
            queue = P.BatchQueue(provider, batch_transport=FakeBatchTransport(),
                                 sleep=lambda _s: None, min_batch_size=1)
            queue.add("ambiguity_critic", "view A")
            with self.assertRaises(P.TranscriptMissingError):
                queue.run()

    def test_replay_only_never_submits_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, interactive = self._provider(tmp, replay_only=True)
            batch = FakeBatchTransport()
            queue = P.BatchQueue(provider, batch_transport=batch)
            queue.add("ambiguity_critic", "view A")
            queue.add("shortcut_attacker", "view B")
            with self.assertRaises(P.TranscriptMissingError) as ctx:
                queue.run()
            self.assertEqual(batch.calls, [])
            self.assertEqual(interactive.calls, [])
            self.assertIn("record-transcripts", str(ctx.exception))

    def test_without_keys_the_queue_refuses_with_the_standard_remedy(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(tmp, api_key="")
            queue = P.BatchQueue(
                provider, batch_transport=FakeBatchTransport(), sleep=lambda _s: None
            )
            queue.add("ambiguity_critic", "view A")
            queue.add("shortcut_attacker", "view B")
            with self.assertRaises(P.MissingCredentialsError) as ctx:
                queue.run()
            message = str(ctx.exception)
            self.assertIn("ANTHROPIC_API_KEY", message)
            self.assertIn("record-transcripts", message)

    def test_poll_timeout_falls_back_rather_than_hanging(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, interactive = self._provider(
                tmp, responses=[anthropic_tool_message(), anthropic_tool_message()]
            )
            queue = P.BatchQueue(
                provider,
                batch_transport=FakeBatchTransport(polls_before_end=10_000),
                sleep=lambda _s: None,
                poll_seconds=1.0,
                max_wait_seconds=3.0,
            )
            queue.add("ambiguity_critic", "view A")
            queue.add("shortcut_attacker", "view B")
            result = queue.run()
            self.assertEqual(len(result.serial), 2)
            self.assertTrue(
                any("did not end within" in r for r in result.fallback_reasons)
            )
            self.assertEqual(len(interactive.calls), 2)

    def test_every_queued_call_is_answered_or_the_drain_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(tmp, responses=[anthropic_tool_message()])
            # batch_transport=None disables batching: the per-call path serves it.
            queue = P.BatchQueue(
                provider, batch_transport=None, sleep=lambda _s: None,
                min_batch_size=1,
            )
            cid = queue.add("ambiguity_critic", "view A")
            result = queue.run()
            self.assertIn(cid, result.responses)
            with self.assertRaises(KeyError):
                result.text_for("never-queued")
            self.assertEqual(len(queue), 0)   # the queue is drained


# ---------------------------------------------------------------------------
# audit_triage: schema, prompt, and the no-approval contract
# ---------------------------------------------------------------------------

VALID_TRIAGE = {
    "labels": {axis: "needs_review" for axis in P.AUDIT_TRIAGE_AXES},
    "flag_for_human": True,
    "rationale": "the pending collision names a real ELT-Bench family",
}


class TriageProvider:
    """Protocol-valid stub: returns the NORMALIZED triage response text."""

    def __init__(self, payload=None):
        self.payload = payload or VALID_TRIAGE
        self.prompts: list[tuple[str, str]] = []

    def complete(self, role, prompt):
        role_name = getattr(role, "value", str(role))
        self.prompts.append((role_name, prompt))
        return canonical_json({"role": role_name, **self.payload})


class AuditTriageRoleTest(unittest.TestCase):
    def test_label_vocabulary_contains_no_acceptance_word(self):
        for label in P.AUDIT_TRIAGE_LABELS:
            for banned in ("approve", "accept", "sign", "clear", "release"):
                self.assertNotIn(banned, label)

    def test_schema_has_no_approval_field(self):
        schema = P.audit_triage_tool_schema()
        text = json.dumps(schema)
        self.assertEqual(
            set(schema["properties"]), {"labels", "flag_for_human", "rationale"}
        )
        self.assertEqual(
            set(schema["properties"]["labels"]["properties"]),
            set(P.AUDIT_TRIAGE_AXES),
        )
        for banned in ('"approved"', '"accept"', '"approve"'):
            self.assertNotIn(banned, text)

    def test_backend_forces_the_triage_tool_and_prompt(self):
        transport = FakeTransport([
            anthropic_tool_message(tool_name=P.AUDIT_TRIAGE_TOOL_NAME)
            for _ in range(1 + P.SCHEMA_RETRIES)
        ])
        # The findings payload is wrong for this role, so it must NOT validate:
        # the wire is held to the triage schema.
        backend = P.AnthropicBackend("sk-test", transport=transport)
        with self.assertRaises(ProviderProtocolError):
            backend.complete(
                role_name=P.AUDIT_TRIAGE_ROLE, model="claude-sonnet-5",
                prompt="QUEUE ENTRY", max_tokens=1024, effort=None,
            )
        payload = transport.calls[0][2]
        self.assertEqual(
            payload["tool_choice"],
            {"type": "tool", "name": P.AUDIT_TRIAGE_TOOL_NAME},
        )
        self.assertEqual(
            payload["tools"][0]["input_schema"], P.audit_triage_tool_schema()
        )
        self.assertEqual(payload["system"], P.AUDIT_TRIAGE_SYSTEM)
        self.assertEqual(
            payload["messages"], [{"role": "user", "content": "QUEUE ENTRY"}]
        )

    def test_valid_triage_call_normalizes(self):
        message = {
            "model": "claude-sonnet-5",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": P.AUDIT_TRIAGE_TOOL_NAME,
                    "input": VALID_TRIAGE,
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }
        backend = P.AnthropicBackend("sk-test", transport=FakeTransport([message]))
        result = backend.complete(
            role_name=P.AUDIT_TRIAGE_ROLE, model="claude-sonnet-5",
            prompt="QUEUE ENTRY", max_tokens=1024, effort=None,
        )
        advice = P.parse_triage_response(result.text)
        self.assertTrue(advice.flag_for_human)
        self.assertEqual(set(advice.labels), set(P.AUDIT_TRIAGE_AXES))

    def test_parse_rejects_junk_and_unknown_labels(self):
        with self.assertRaises(ProviderProtocolError):
            P.parse_triage_response("not json")
        with self.assertRaises(ProviderProtocolError):
            P.parse_triage_response(json.dumps({"labels": {}, "flag_for_human": True}))
        bad = dict(VALID_TRIAGE)
        bad["labels"] = {**VALID_TRIAGE["labels"], "contamination": "approved"}
        with self.assertRaises(ProviderProtocolError):
            P.parse_triage_response(json.dumps(bad))

    def test_role_is_routed_at_the_sonnet_tier(self):
        routing = P.load_role_routing()
        route = routing.for_role(P.AUDIT_TRIAGE_ROLE)
        self.assertEqual(route.provider, "anthropic")
        self.assertEqual(route.model, "claude-sonnet-5")


class TriageCommandTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.task = demo_fixture.demo_task()
        self.engine.register(self.task)
        # One borderline (non-fatal) collision recorded at the current hash:
        # exactly what puts a task in the human audit queue.
        self.collision = {
            "kind": "schema",
            "against": "eltbench",
            "detail": "table name overlap with an anchor schema",
            "fatal": False,
        }
        (self.engine.task_dir(self.task.task_id) / "reports").mkdir(
            parents=True, exist_ok=True
        )
        (
            self.engine.task_dir(self.task.task_id)
            / "reports"
            / "contamination_post.json"
        ).write_text(
            canonical_json(
                {
                    "task_id": self.task.task_id,
                    "task_content_hash": self.task.content_hash(),
                    "collisions": [self.collision],
                }
            ),
            encoding="utf-8",
        )
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.engine.close)

    def _args(self, **overrides):
        base = dict(workspace=self.workspace, task_id=None)
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, provider):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.cmd_triage(self._args(), provider=provider)
        return code, buf.getvalue()

    def test_triage_writes_advisory_labels_into_the_queue_entry(self):
        provider = TriageProvider()
        code, output = self._run(provider)
        self.assertEqual(code, 0, msg=output)
        record = json.loads(
            (self.workspace / "audit" / f"{self.task.task_id}.triage.json").read_text()
        )
        self.assertTrue(record["advisory"])
        self.assertFalse(record["approves"])
        self.assertEqual(set(record["per_axis_labels"]), set(P.AUDIT_TRIAGE_AXES))
        self.assertTrue(record["flag_for_human"])
        self.assertEqual(record["task_content_hash"], self.task.content_hash())
        self.assertEqual(
            record["pending_collision_fingerprints"],
            [cli._collision_fingerprint(self.collision)],
        )
        self.assertIn("never approves", record["note"])
        self.assertIn("ADVISORY", output)

    def test_the_view_carries_the_bundle_gate_evidence_and_findings(self):
        self.engine.record_report(
            self.task, StageName.GATES.value, "fail",
            StagePayload(detail="two gates red"),
        )
        provider = TriageProvider()
        self._run(provider)
        role, view = provider.prompts[0]
        self.assertEqual(role, P.AUDIT_TRIAGE_ROLE)
        self.assertIn(self.task.task_id, view)
        self.assertIn(demo_fixture.MART_NAME, view)
        self.assertIn("GATE EVIDENCE", view)
        self.assertIn("COUNCIL FINDINGS", view)
        self.assertIn("PENDING BORDERLINE CONTAMINATION COLLISIONS", view)
        self.assertIn(self.collision["detail"], view)
        # Deterministic: the same queue state yields the same prompt.
        second = TriageProvider()
        self._run(second)
        self.assertEqual(view, second.prompts[0][1])

    def test_triage_never_writes_an_approval(self):
        self._run(TriageProvider(
            payload={
                "labels": {axis: "clean" for axis in P.AUDIT_TRIAGE_AXES},
                "flag_for_human": False,
                "rationale": "everything looks fine to me",
            }
        ))
        self.assertFalse(cli._approval_path(self.engine, self.task.task_id).is_file())
        self.assertFalse(cli._rejection_path(self.engine, self.task.task_id).is_file())
        approvals = list((self.workspace / "audit").glob("*.approval.json"))
        self.assertEqual(approvals, [])

    def test_the_audit_stage_is_unmoved_by_the_cleanest_triage(self):
        """Even 'clean' on every axis leaves the sign-off outstanding."""
        self._run(TriageProvider(
            payload={
                "labels": {axis: "clean" for axis in P.AUDIT_TRIAGE_AXES},
                "flag_for_human": False,
                "rationale": "no objections",
            }
        ))
        outcome = cli.run_audit(self.engine, self.task)
        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertIn("human sign-off", outcome.payload.error)

    def test_no_code_path_from_triage_to_an_approval(self):
        """Structural: nothing on the triage path can construct an
        AuditApproval or touch the approval file."""
        referenced: set[str] = set()
        for fn in (
            cli.cmd_triage,
            cli._triage_view,
            cli._triage_path,
            cli._audit_queue,
            cli._load_triage,
        ):
            tree = ast.parse(inspect.getsource(fn))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    referenced.add(node.id)
                elif isinstance(node, ast.Attribute):
                    referenced.add(node.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    referenced.add(node.value)
        # Identifiers and string literals only — a comment cannot satisfy this.
        for banned in ("AuditApproval", "_approval_path", "approval.json"):
            self.assertNotIn(banned, referenced)

    # -- the projection layer behind the view (roadmap Phase 0.B) --------------

    def _record_battery(self, gates):
        report = AcceptanceReport.from_gates(
            task_id=self.task.task_id,
            revision=1,
            task_content_hash=self.task.content_hash(),
            gates=gates,
            scorer_version="test",
        )
        self.engine.record_report(self.task, StageName.GATES.value, "fail", report)

    def test_triage_view_uses_projected_gate_rows(self):
        self._record_battery((
            GateResult(gate="determinism", passed=True, details="3/3 identical",
                       evidence={"runs": "3"}),
            GateResult(
                gate="required-mutants", passed=False,
                details="required mutant matrix not reproduced: inner_join: LEAK — "
                        "must lose reward on primary, got 1.0",
                evidence={"inner_join": "development=1.000000,primary=1.000000"},
            ),
            GateResult(gate="info-content", passed=False,
                       details="gate crashed (fail closed): BinderError('no column x')"),
        ))
        provider = TriageProvider()
        code, _ = self._run(provider)
        self.assertEqual(code, 0)
        view = provider.prompts[0][1]
        self.assertIn("  [PASS] determinism: ok", view)
        self.assertIn("  [FAIL] required-mutants: failed", view)
        self.assertIn("  [FAIL] info-content: gate_crashed", view)
        for withheld in ("got 1.0", "on primary", "3/3", "BinderError",
                         "development=1.000000", "3/3 identical", "no column"):
            self.assertNotIn(withheld, view, withheld)
        # The rows are exactly what project_gate_battery says, in battery order.
        from elt_taskgen.review.tools import projection

        payload = json.loads(
            self.engine.latest_report(self.task.task_id, StageName.GATES.value).payload_json
        )
        expected = [
            f"  [{'PASS' if r['passed'] else 'FAIL'}] {r['gate']}: {r['code']}"
            for r in projection.project_gate_battery(payload)
        ]
        self.assertEqual(
            [line for line in view.splitlines() if line.startswith("  [")], expected
        )

    def test_triage_protocol_error_exits_2_not_1(self):
        bad = dict(VALID_TRIAGE)
        bad["labels"] = {**VALID_TRIAGE["labels"], "contamination": "approved"}
        code, output = self._run(TriageProvider(payload=bad))
        self.assertEqual(code, 2)
        self.assertIn("could not measure", output)
        self.assertNotIn("final verdict", output)
        self.assertFalse(
            (self.workspace / "audit" / f"{self.task.task_id}.triage.json").is_file()
        )

    def test_triage_tripwire_exits_2_as_harness_fault(self):
        # A gate NAME that is a value is a producer leak: the view is never
        # rendered, nothing is sent, and the command could not measure (2).
        self._record_battery((
            GateResult(gate="primary=1.0", passed=False, details="leak"),
        ))
        provider = TriageProvider()
        code, output = self._run(provider)
        self.assertEqual(code, 2)
        self.assertIn("could not measure", output)
        self.assertIn("tripwire", output)
        self.assertEqual(provider.prompts, [])

    #: What a leaky battery carries: gold counts, key tuples, rewards, paths.
    _SENTINELS = (
        "4711", "9130", "27390",                       # frozen stage-1 counts
        "('c_9001', 3)", "('o_77', 'c_9001')",         # key tuples from hidden rows
        "0.250000", "primary=0.25", "got 1.0", "agreement 0.75",  # rewards
        "/Users/nobody/runs/ws", "answer_key/gold", ".duckdb", "oracle/",  # paths
        "BinderError",                                 # executor text
    )

    def test_triage_sentinel_property_zero_private_sentinels(self):
        """A synthetic ledger seeded with gold counts, key tuples, rewards and
        paths yields ZERO sentinels in `_triage_view` and in every serialized
        projection (the property the seven approval-vocabulary assertions
        above never covered)."""
        from elt_taskgen.review.tools import projection

        s = self._SENTINELS
        self._record_battery((
            GateResult(gate="data-sensitivity", passed=False,
                       details=f"stage-1 vector primary: customers={s[0]},orders={s[1]},order_items={s[2]}",
                       evidence={"primary": f"customers={s[0]},orders={s[1]}"}),
            GateResult(gate="referential-integrity", passed=False,
                       details=f"primary:orders->customers: key {s[3]} has no parent row; row 7 key {s[4]}",
                       evidence={"examples": s[3]}),
            GateResult(gate="required-mutants", passed=False,
                       details=f"inner_join: LEAK — must lose reward on primary, {s[7]}",
                       evidence={"inner_join": f"development=1.000000,{s[6]}0000,stress={s[5]}"}),
            GateResult(gate="dual-build-agreement", passed=False,
                       details=f"{s[8]} on resampled", evidence={"agreement:primary": "0.750000"}),
            GateResult(gate="populations-load", passed=False,
                       details=f"gate crashed (fail closed): {s[13]}('cannot open {s[9]}/tasks/t/{s[10]}/primary{s[11]} via {s[12]}')"),
            GateResult(gate="determinism", passed=True, details="3/3 identical"),
        ))
        adjudication = {
            "task_id": self.task.task_id,
            "task_content_hash": self.task.content_hash(),
            "kind": "dual_build",
            "status": "needs_adjudication",
            "agreement": {"primary": 0.0, "development": 1.0},
            "detail": f"primary: expected {s[0]} rows, got {s[1]}; {s[13]} at {s[9]}",
        }
        view = cli._triage_view(self.engine, self.task, {}, adjudication)
        for sentinel in s:
            self.assertNotIn(sentinel, view, sentinel)
        self.assertNotIn("expected", view.split("DUAL-BUILD ADJUDICATION:")[1])
        self.assertIn("  code: mismatch", view)
        payload = json.loads(
            self.engine.latest_report(self.task.task_id, StageName.GATES.value).payload_json
        )
        for row in projection.project_gate_battery(payload):
            diag = projection.Diagnostic(
                source="gate", ok=row["passed"], code=row["code"], subject=row["gate"]
            )
            text = projection.serialize_for_transport(diag, task=self.task, package=None)
            projection.assert_value_free(text.encode("utf-8"), task=self.task, route=None)
            for sentinel in s:
                self.assertNotIn(sentinel, text, sentinel)
        # The raw battery DID carry every sentinel — the projection removed them.
        raw = canonical_json(payload)
        for sentinel in s:
            self.assertIn(sentinel, raw, sentinel)

    def test_triage_sentinel_property_holds_on_demo_and_runs_drives(self):
        """The same property over recorded batteries: the committed demo task's
        own battery and every `runs/<pool>_elt` drive (never `runs/**/live/`).
        Every raw `details`/`evidence` string that carries a digit or a slash
        is a sentinel that must be absent from the projected rows."""
        from elt_taskgen.review.tools import projection

        checked_batteries = 0
        sources: list[tuple[TaskIR, dict]] = []
        # 1. The demo task, run through the real gate battery is expensive; use
        #    the shape the ledger stores for it instead.
        demo_payload = {"gates": [
            {"gate": name, "passed": False, "details": f"{name} measured 4711 rows at /Users/x/answer_key",
             "evidence": {"v": "primary=0.25"}}
            for name in ("determinism", "required-mutants", "data-sensitivity")
        ]}
        sources.append((self.task, demo_payload))
        # 2. The five recorded drives, read-only, JSON copies only (no ledger open).
        runs = Path(cli.__file__).resolve().parents[2] / "runs"
        for pool in ("dbt_elt", "dlt_elt", "schemapile_elt", "synsql_elt", "wikidbs_elt"):
            drive = runs / pool / "tasks"
            if not drive.is_dir():
                continue
            for task_dir in sorted(drive.iterdir()):
                ir = task_dir / "task_ir.json"
                if not ir.is_file():
                    continue
                task = TaskIR.model_validate_json(ir.read_text(encoding="utf-8"))
                for report in sorted((task_dir / "reports").glob("*gates*.json")):
                    if "live" in report.parts:
                        continue
                    doc = json.loads(report.read_text(encoding="utf-8"))
                    payload = doc.get("payload") if isinstance(doc, dict) else None
                    if isinstance(payload, dict) and isinstance(payload.get("gates"), list):
                        sources.append((task, payload))
        for task, payload in sources:
            sentinels: set[str] = set()
            for gate in payload["gates"]:
                for value in (gate.get("details", ""), *dict(gate.get("evidence") or {}).values()):
                    value = str(value)
                    # Three characters or more: a bare "1" or "3" is a shape, not
                    # a value, and would match the recorded diagnostics_version.
                    if len(value) >= 3 and (any(ch.isdigit() for ch in value) or "/" in value):
                        sentinels.add(value)
            lines = cli._projected_gate_lines(task, payload)
            self.assertEqual(len(lines), len(payload["gates"]))
            projected = "\n".join(lines)
            for sentinel in sentinels:
                self.assertNotIn(sentinel, projected, (task.task_id, sentinel[:40]))
            for row in projection.project_gate_battery(payload):
                diag = projection.Diagnostic(
                    source="gate", ok=row["passed"], code=row["code"], subject=row["gate"]
                )
                text = projection.serialize_for_transport(diag, task=task, package=None)
                projection.assert_value_free(text.encode("utf-8"), task=task, route=None)
                for sentinel in sentinels:
                    self.assertNotIn(sentinel, text)
            checked_batteries += 1
        self.assertGreaterEqual(checked_batteries, 1)

    def test_missing_transcript_refuses_cleanly(self):
        provider = P.RoutedProvider(
            P.load_role_routing(),
            P.TranscriptStore(self.workspace / "transcripts"),
            P.CostMeter(),
            replay_only=True,
        )
        code, output = self._run(provider)
        self.assertEqual(code, 1)
        self.assertIn("record-transcripts", output)
        self.assertFalse(
            (self.workspace / "audit" / f"{self.task.task_id}.triage.json").is_file()
        )

    def test_empty_queue_is_a_clean_no_op(self):
        (
            self.engine.task_dir(self.task.task_id)
            / "reports"
            / "contamination_post.json"
        ).unlink()
        provider = TriageProvider()
        code, output = self._run(provider)
        self.assertEqual(code, 0)
        self.assertIn("audit queue empty", output)
        self.assertEqual(provider.prompts, [])

    def test_audit_list_shows_the_advisory_labels(self):
        self._run(TriageProvider())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_audit_list(self._args())
        output = buf.getvalue()
        self.assertIn("TRIAGE (advisory, NOT an approval)", output)
        self.assertIn("contamination=needs_review", output)


if __name__ == "__main__":
    unittest.main()

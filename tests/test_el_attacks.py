"""The EXTRACT-LOAD attack surface: mutants of the RENDERED ARTIFACTS.

WHY THIS FILE EXISTS
Every other attack test measures a mutation of the TRANSFORM SQL, so the whole
suite could be green while the extract-load subtask had no wrong
implementation to distinguish it from a right one. These tests assert the
other half:

  * each load mutation, applied to a real rendered artifact, changes how many
    records the TRUSTED loader reads (per backend format, one test each);
  * end to end, the mutant drops `evaluate_variant(EXTRACT_LOAD)` — strict
    binary over the frozen count vector — to 0.0, and the recorded evidence
    NAMES the table whose count broke;
  * every surface the task does not offer FAILS CLOSED at resolution time
    (one backend => no skip_backend; equal counts => no file swap; one page =>
    no truncation; one count vector => no stale snapshot);
  * a load mutant that keeps full reward everywhere RAISES rather than
    quietly passing;
  * the catalogue never contains a value-corrupting mutation, because
    `compare_stage1` grades counts only and such a mutant would score 1.0.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import duckdb

from elt_taskgen import demo_fixture
from elt_taskgen.generation import mart_plan
from elt_taskgen.generation import populations as pops
from elt_taskgen.generation import source_data
from elt_taskgen.models import (
    AttackCase,
    AttackKind,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    PopulationName,
    RLVR_TASK_VARIANTS,
    TableSpec,
    TaskVariant,
)
from elt_taskgen.reference import solution as ref_solution
from elt_taskgen.reference.gold import GoldBundle
from elt_taskgen.reference.runner import mart_rows_to_csv, sort_mart_rows
from elt_taskgen.verification import attacks, upstream_eval

TINY_SCALE = {"customers": 6, "orders": 12, "order_items": 24}


# ---------------------------------------------------------------------------
# A tiny, fully rendered workspace built from the demo fixture
# ---------------------------------------------------------------------------

def _tiny_task(backends: tuple[BackendAssignment, ...] | None = None):
    """The demo task at six customers, so a whole five-population workspace
    can be rendered inside a unit test."""
    task = demo_fixture.demo_task()
    specs = []
    for spec in task.populations:
        if spec.name is PopulationName.COUNTERFACTUAL:
            specs.append(spec)
        else:
            specs.append(spec.model_copy(update={"scale": dict(TINY_SCALE)}))
    update: dict = {"populations": tuple(specs)}
    if backends is not None:
        update["backends"] = backends
    return task.model_copy(update=update)


def _materialize(task, workspace: Path) -> GoldBundle:
    """Rows + rendered artifacts for all five populations, then frozen gold.

    Gold is produced by the TRUSTED path (load the rendered artifacts with
    `reference.solution.load_sources_duckdb`, run the reference SQL), which is
    what makes a later mutant's disagreement with it meaningful.
    """
    mart = task.marts[0]
    stage1: dict[str, dict[str, int]] = {}
    stage2: dict[str, dict[str, str]] = {}
    for pop in PopulationName:
        rows = source_data.generate_rows(task, pop)
        base = workspace / "tasks" / task.task_id / "populations" / pop.value
        source_data.write_rows(rows, base / "rows")
        source_data.render_population(task, pop, rows, base / "rendered")
        con = duckdb.connect(":memory:")
        try:
            loaded = ref_solution.load_sources_duckdb(task, base / "rendered", con)
            mart_rows = attacks._fetch_rows(con, demo_fixture.REFERENCE_SQL)
        finally:
            con.close()
        stage1[pop.value] = dict(loaded.counts)
        stage2[pop.value] = {
            mart.name: mart_rows_to_csv(sort_mart_rows(mart_rows, mart), mart)
        }
    return GoldBundle(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        stage1=stage1,
        stage2_csv=stage2,
        file_hashes={},
    )


def _shape():
    """A minimal star, so the whole catalogue (not just the EL half) can be
    derived the way an adapter derives it."""
    return mart_plan.build_star(
        mart="captain_summary",
        parent="captains",
        parent_keys=("captain_id",),
        key_columns=("captain_id",),
        measures=(mart_plan.Measure(column="n", expr="COUNT(*)"),),
    ).shape


def _case(name: str, directive: str, required: bool = False) -> AttackCase:
    return AttackCase(
        name=name,
        kind=AttackKind.CUSTOM,
        description=f"EL probe: {directive}",
        mutation=attacks.LOAD_DIRECTIVE_PREFIX + directive,
        expected_pass={} if required is False else {PopulationName.PRIMARY: False},
        required=required,
    )


# ---------------------------------------------------------------------------
# Per-format artifact surgery: the loader must READ a different number of rows
# ---------------------------------------------------------------------------

class ArtifactSurgeryTest(unittest.TestCase):
    """One test per rendered format: the op changes the RECORD COUNT the
    trusted reader returns, and nothing else about the file stops parsing."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="el_artifact_"))
        self.table = TableSpec(
            name="widgets",
            columns=(
                ColumnSpec(name="widget_id", type=ColumnType.INTEGER, nullable=False),
                ColumnSpec(name="label", type=ColumnType.TEXT, nullable=True),
            ),
            primary_key=("widget_id",),
        )
        self.rows = [
            {"widget_id": i, "label": None if i == 3 else f"w{i}"} for i in range(1, 8)
        ]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_jsonl_duplicate_drop_null_and_empty(self):
        path = source_data.render_mongodb(self.table, self.rows, self.dir)
        attacks._op_jsonl_file(path, attacks.OP_DUPLICATE)
        self.assertEqual(len(ref_solution._read_jsonl(path)), 14)
        path = source_data.render_mongodb(self.table, self.rows, self.dir)
        attacks._op_jsonl_file(path, attacks.OP_DROP_NULL_ROWS)
        self.assertEqual(len(ref_solution._read_jsonl(path)), 6)  # the NULL row
        path = source_data.render_mongodb(self.table, self.rows, self.dir)
        attacks._op_jsonl_file(path, attacks.OP_EMPTY)
        self.assertEqual(ref_solution._read_jsonl(path), [])

    def test_csv_header_as_row_lands_exactly_one_extra_record(self):
        path = source_data.render_files(self.table, self.rows, self.dir)
        attacks._op_csv_file(path, attacks.OP_HEADER_AS_ROW)
        read = ref_solution._read_csv(path)
        self.assertEqual(len(read), 8)
        self.assertEqual(read[0]["widget_id"], "widget_id")

    def test_csv_duplicate_and_drop_null(self):
        path = source_data.render_files(self.table, self.rows, self.dir)
        attacks._op_csv_file(path, attacks.OP_DUPLICATE)
        self.assertEqual(len(ref_solution._read_csv(path)), 14)
        path = source_data.render_files(self.table, self.rows, self.dir)
        attacks._op_csv_file(path, attacks.OP_DROP_NULL_ROWS)
        self.assertEqual(len(ref_solution._read_csv(path)), 6)

    def test_rest_truncation_keeps_only_the_first_page(self):
        path = source_data.render_rest(self.table, self.rows, self.dir, page_size=3)
        self.assertEqual(len(ref_solution._read_rest(path)), 7)
        attacks._op_rest_dir(path, attacks.OP_TRUNCATE_FIRST_UNIT)
        self.assertEqual(len(ref_solution._read_rest(path)), 3)
        index = json.loads((path / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["pages"], ["page_0001.json"])
        # The later pages are still ON DISK: this models the CLIENT that never
        # followed the cursor, not a server that lost data.
        self.assertTrue((path / "page_0002.json").is_file())

    def test_rest_duplicate_and_drop_null(self):
        path = source_data.render_rest(self.table, self.rows, self.dir, page_size=3)
        attacks._op_rest_dir(path, attacks.OP_DUPLICATE)
        self.assertEqual(len(ref_solution._read_rest(path)), 14)
        path = source_data.render_rest(self.table, self.rows, self.dir, page_size=3)
        attacks._op_rest_dir(path, attacks.OP_DROP_NULL_ROWS)
        self.assertEqual(len(ref_solution._read_rest(path)), 6)

    def test_s3_truncation_keeps_only_the_first_part(self):
        path = source_data.render_s3(self.table, self.rows, self.dir)
        # One part at the shipped chunk size; write a second by hand so the
        # multi-part case is exercised rather than assumed.
        (path / "part-00001.jsonl").write_text(
            (path / "part-00000.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.assertEqual(len(ref_solution._read_s3(path)), 14)
        attacks._op_s3_dir(path, attacks.OP_TRUNCATE_FIRST_UNIT)
        self.assertEqual(len(ref_solution._read_s3(path)), 7)

    def test_postgres_statement_surgery(self):
        path = source_data.render_postgres(self.table, self.rows, self.dir)
        con = duckdb.connect(":memory:")
        try:
            ref_solution.create_table(con, self.table)
            attacks._op_postgres_sql(path, attacks.OP_DUPLICATE)
            ref_solution._load_postgres_sql(con, self.table, path)
            (count,) = con.execute('SELECT COUNT(*) FROM "widgets"').fetchone()
            self.assertEqual(count, 14)
        finally:
            con.close()
        path = source_data.render_postgres(self.table, self.rows, self.dir)
        attacks._op_postgres_sql(path, attacks.OP_DROP_NULL_ROWS)
        con = duckdb.connect(":memory:")
        try:
            ref_solution.create_table(con, self.table)
            ref_solution._load_postgres_sql(con, self.table, path)
            (count,) = con.execute('SELECT COUNT(*) FROM "widgets"').fetchone()
            self.assertEqual(count, 6)
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Resolution: every missing surface fails closed
# ---------------------------------------------------------------------------

class LoadMutationResolutionTest(unittest.TestCase):
    def setUp(self):
        self.task = _tiny_task()
        self.gold = GoldBundle(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            stage1={
                p.value: {"customers": 6, "orders": 12, "order_items": 24}
                for p in PopulationName
            },
            stage2_csv={},
            file_hashes={},
        )

    def test_unknown_load_mutation_raises(self):
        with self.assertRaises(ValueError):
            attacks.split_load_directive("truncate_teh_table")

    def test_single_backend_task_has_no_skip_backend(self):
        one = self.task.model_copy(
            update={
                "backends": tuple(
                    BackendAssignment(table=b.table, backend=Backend.FILES)
                    for b in self.task.backends
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "more than one source backend"):
            attacks.resolve_load_mutation(one, "skip_backend", "", self.gold)

    def test_partial_backend_needs_no_surface_at_all(self):
        plan = attacks.resolve_load_mutation(self.task, "partial_backend", "", self.gold)
        self.assertEqual(len(plan.omit_tables), 1)
        self.assertFalse(plan.ops)

    def test_wrong_source_file_refuses_an_invisible_swap(self):
        """Swapping two EQUAL-count tables scores 1.0 under compare_stage1 —
        a leak, not a kill — so the mutation must refuse to compile."""
        same = self.task.model_copy(
            update={
                "backends": tuple(
                    BackendAssignment(table=b.table, backend=Backend.FILES)
                    for b in self.task.backends
                )
            }
        )
        flat = self.gold.model_copy(
            update={
                "stage1": {
                    p.value: {"customers": 6, "orders": 6, "order_items": 6}
                    for p in PopulationName
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "differ in row count"):
            attacks.resolve_load_mutation(same, "wrong_source_file", "", flat)

    def test_truncate_refuses_a_table_that_fits_in_one_unit(self):
        with self.assertRaisesRegex(ValueError, "SPANS more than one unit"):
            attacks.resolve_load_mutation(self.task, "truncate_table", "", self.gold)

    def test_truncate_picks_a_table_that_spans_units(self):
        big = self.gold.model_copy(
            update={
                "stage1": {
                    p.value: {"customers": 4000, "orders": 12, "order_items": 24}
                    for p in PopulationName
                }
            }
        )
        plan = attacks.resolve_load_mutation(self.task, "truncate_table", "", big)
        self.assertEqual(
            plan.ops, {"customers": attacks.OP_TRUNCATE_FIRST_UNIT}
        )

    def test_stale_snapshot_refuses_one_constant_count_vector(self):
        with self.assertRaisesRegex(ValueError, "SAME stage-1 count vector"):
            attacks.resolve_load_mutation(self.task, "stale_snapshot", "", self.gold)

    def test_header_as_row_needs_a_files_backed_table(self):
        no_files = self.task.model_copy(
            update={
                "backends": tuple(
                    BackendAssignment(table=b.table, backend=Backend.MONGODB)
                    for b in self.task.backends
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "no FILES-backed table"):
            attacks.resolve_load_mutation(no_files, "header_as_row", "", self.gold)

    def test_header_as_row_refuses_a_typed_table(self):
        """A typed FILES table is NO surface: the header text dies in coercion,
        which is a crash-kill the per-variant gate refuses as evidence — so
        the resolver must not compile it as a weaker kill either."""
        # the demo's only FILES table (order_items) is typed
        with self.assertRaises(attacks.InapplicableLoadMutationError) as ctx:
            attacks.resolve_load_mutation(self.task, "header_as_row", "", self.gold)
        self.assertIn("no all-TEXT FILES table", str(ctx.exception))
        self.assertIn("coercion crash is not a count kill", str(ctx.exception))
        # naming the typed table explicitly is refused too
        with self.assertRaises(attacks.InapplicableLoadMutationError):
            attacks.resolve_load_mutation(
                self.task, "header_as_row", "order_items", self.gold
            )
        # an all-TEXT FILES table resolves, and the kill is a COUNT kill
        text_only = self.task.model_copy(
            update={
                "tables": tuple(
                    t.model_copy(
                        update={
                            "columns": tuple(
                                c.model_copy(update={"type": ColumnType.TEXT})
                                for c in t.columns
                            )
                        }
                    )
                    if t.name == "order_items"
                    else t
                    for t in self.task.tables
                )
            }
        )
        plan = attacks.resolve_load_mutation(text_only, "header_as_row", "", self.gold)
        self.assertEqual(plan.ops, {"order_items": attacks.OP_HEADER_AS_ROW})
        self.assertIn("N+1 — a COUNT kill", plan.detail)
        self.assertNotIn("coercion", plan.detail)
        # ...also when named via the arg
        plan = attacks.resolve_load_mutation(
            text_only, "header_as_row", "order_items", self.gold
        )
        self.assertEqual(plan.ops, {"order_items": attacks.OP_HEADER_AS_ROW})


# ---------------------------------------------------------------------------
# End to end: the mutant loses the EXTRACT-LOAD reward, and the record says why
# ---------------------------------------------------------------------------

class ExtractLoadKillTest(unittest.TestCase):
    """The claim this whole track exists to make: a wrong LOAD scores 0.0."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="el_kill_"))
        cls.workspace = cls.tmp / "workspace"
        cls.task = _tiny_task()
        cls.gold = _materialize(cls.task, cls.workspace)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _record(self, name: str) -> dict:
        path = (
            self.workspace / "tasks" / self.task.task_id / "attacks" / name
            / "rewards.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_trusted_load_itself_scores_full_extract_load_reward(self):
        """The control. Without it a 0.0 proves nothing about the mutation."""
        for pop in PopulationName:
            result = upstream_eval.evaluate_variant(
                TaskVariant.EXTRACT_LOAD,
                self.task,
                self.gold,
                pop,
                dict(self.gold.stage1[pop.value]),
                {},
            )
            self.assertEqual(result.reward, 1.0, pop.value)

    def test_partial_backend_kills_on_every_population_by_absence(self):
        case = _case("partial_backend", "partial_backend")
        attacks.run_attack(self.task, case, self.gold, self.workspace)
        record = self._record("partial_backend")
        el = record["rewards_by_variant"][TaskVariant.EXTRACT_LOAD.value]
        self.assertEqual(set(el.values()), {0.0})
        omitted = record["load_mutation"]["omit_tables"][0]
        for pop in PopulationName:
            self.assertEqual(
                record["stage1_breaks"][pop.value][omitted], "table not found"
            )

    def test_duplicate_on_load_lands_exactly_twice_the_rows(self):
        case = _case("duplicate_on_load", "duplicate_on_load")
        attacks.run_attack(self.task, case, self.gold, self.workspace)
        record = self._record("duplicate_on_load")
        el = record["rewards_by_variant"][TaskVariant.EXTRACT_LOAD.value]
        self.assertEqual(set(el.values()), {0.0})
        breaks = record["stage1_breaks"][PopulationName.PRIMARY.value]
        for table, count in self.gold.stage1[PopulationName.PRIMARY.value].items():
            self.assertEqual(breaks[table], f"expected {count} rows, got {2 * count}")

    def test_skip_backend_empties_one_backend_through_the_real_readers(self):
        case = _case("skip_backend", "skip_backend")
        attacks.run_attack(self.task, case, self.gold, self.workspace)
        record = self._record("skip_backend")
        el = record["rewards_by_variant"][TaskVariant.EXTRACT_LOAD.value]
        self.assertEqual(set(el.values()), {0.0})
        skipped = self.task.backends[0].table
        got = record["stage1_breaks"][PopulationName.PRIMARY.value][skipped]
        self.assertIn("got 0", got)

    def test_stale_snapshot_grades_one_population_against_another(self):
        case = _case("stale_snapshot", "stale_snapshot")
        attacks.run_attack(self.task, case, self.gold, self.workspace)
        record = self._record("stale_snapshot")
        el = record["rewards_by_variant"][TaskVariant.EXTRACT_LOAD.value]
        self.assertEqual(el[PopulationName.PRIMARY.value], 0.0)
        # ...and the primary/resampled pair is NEVER the one used: the
        # coverage validator forces them to share a scale, so the memorization
        # population carries zero extract-load signal.
        sources = record["load_mutation"]["source_population"]
        self.assertNotEqual(sources["primary"], "resampled")
        self.assertNotEqual(sources["resampled"], "primary")

    def test_wrong_source_file_swaps_two_same_backend_artifacts(self):
        """The demo task has one table per backend, so the swap has no surface
        — and refusing is the correct behaviour, not a skipped test. A
        REQUIRED case re-raises (a required mutant with no surface is a bug in
        the case); an informational probe records the reason on disk and
        contributes NO rewards entry instead of crashing the battery."""
        with self.assertRaisesRegex(ValueError, "differ in row count"):
            attacks.run_attack(
                self.task,
                _case("swap", "wrong_source_file", required=True),
                self.gold,
                self.workspace,
            )
        probe = _case("swap_probe", "wrong_source_file")
        self.assertEqual(
            attacks.run_attack(self.task, probe, self.gold, self.workspace), {}
        )
        record = json.loads(
            (
                self.workspace / "tasks" / self.task.task_id
                / "attacks" / "swap_probe" / "inapplicable.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIn("differ in row count", record["inapplicable"])
        self.assertEqual(record["task_content_hash"], self.task.content_hash())

    def test_null_row_drop_kills_only_where_the_population_declares_nulls(self):
        """The first EL mutant with a NON-uniform expectation — which is what
        makes it evidence about population STRUCTURE and not about counting.
        Primary declares 'optional nullable foreign keys contain NULLs';
        development declares 'no NULL foreign keys'."""
        case = _case("null_row_drop", "null_row_drop")
        attacks.run_attack(self.task, case, self.gold, self.workspace)
        el = self._record("null_row_drop")["rewards_by_variant"][
            TaskVariant.EXTRACT_LOAD.value
        ]
        self.assertEqual(el[PopulationName.PRIMARY.value], 0.0)
        self.assertEqual(el[PopulationName.DEVELOPMENT.value], 1.0)

    def test_fabricate_hint_dies_wherever_the_realized_counts_diverge(self):
        """THE mutant the realized-count divergence exists to kill.

        At a declared scale at or above REALIZED_DIVERGENCE_MIN_SCALE the
        generator is REQUIRED to land 2-7% off the hint, so submitting the
        documentation's numbers loses on every generated population, and the
        recorded stage-1 breaks name each table with expected-vs-fabricated
        counts. Nothing was loaded: no load error, no artifact opened."""
        scaled = demo_fixture.demo_task().model_copy(
            update={
                "populations": tuple(
                    spec
                    if spec.name is PopulationName.COUNTERFACTUAL
                    else spec.model_copy(
                        update={
                            "scale": {
                                "customers": 60, "orders": 120, "order_items": 240
                            }
                        }
                    )
                    for spec in demo_fixture.demo_task().populations
                )
            }
        )
        ws = self.tmp / "fab_hint_ws"
        gold = _materialize(scaled, ws)
        case = _case("fabricate_counts", "fabricate_counts")
        attacks.run_attack(scaled, case, gold, ws)
        record = json.loads(
            (ws / "tasks" / scaled.task_id / "attacks" / case.name / "rewards.json")
            .read_text(encoding="utf-8")
        )
        el = record["rewards_by_variant"][TaskVariant.EXTRACT_LOAD.value]
        self.assertEqual({pop: 0.0 for pop in el}, el)
        self.assertEqual(
            record["load_mutation"]["fabricated_counts"]["primary"],
            {"customers": 60, "orders": 120, "order_items": 240},
        )
        # the kill is by COUNT, not by a crashed load
        self.assertEqual(record["errors"], {})
        self.assertIn("customers", record["stage1_breaks"]["primary"])

    def test_fabricate_hint_leaks_below_the_divergence_floor(self):
        """Below REALIZED_DIVERGENCE_MIN_SCALE the hint IS realized exactly, so
        the hint mutant keeps full EL reward there — measured and recorded,
        which is precisely why graded populations must sit at or above the
        floor (gates exclude only the solver-visible development population)."""
        case = _case("fabricate_hint_tiny", "fabricate_counts")
        attacks.run_attack(self.task, case, self.gold, self.workspace)
        el = self._record("fabricate_hint_tiny")["rewards_by_variant"][
            TaskVariant.EXTRACT_LOAD.value
        ]
        self.assertEqual(el[PopulationName.PRIMARY.value], 1.0)  # 6/12/24 exact
        self.assertEqual(el[PopulationName.COUNTERFACTUAL.value], 0.0)

    def test_fabricate_primary_echo_leaks_on_the_memorization_pair(self):
        """The primary echo keeps full EL reward on primary (it IS the answer
        there) and on resampled (shared scale => identical realized vector) —
        the recorded proof that the memorization pair carries ZERO
        extract-load signal — and dies where the data differs."""
        case = _case("fabricate_echo", "fabricate_counts:primary")
        attacks.run_attack(self.task, case, self.gold, self.workspace)
        record = self._record("fabricate_echo")
        el = record["rewards_by_variant"][TaskVariant.EXTRACT_LOAD.value]
        self.assertEqual(el[PopulationName.PRIMARY.value], 1.0)
        self.assertEqual(el[PopulationName.RESAMPLED.value], 1.0)
        self.assertEqual(el[PopulationName.COUNTERFACTUAL.value], 0.0)
        self.assertEqual(el[PopulationName.STRESS.value], 0.0)
        self.assertEqual(
            record["load_mutation"]["fabricated_counts"]["stress"],
            self.gold.stage1[PopulationName.PRIMARY.value],
        )

    def test_fabricate_refuses_where_nothing_can_be_fabricated(self):
        """No scale hint anywhere => nothing documented to submit; a single
        graded population => echoing primary is the correct answer. Both are
        no-surface refusals, same rule as the other load mutations."""
        bare = self.task.model_copy(
            update={
                "populations": tuple(
                    spec.model_copy(update={"scale": {}})
                    for spec in self.task.populations
                )
            }
        )
        with self.assertRaisesRegex(
            attacks.InapplicableLoadMutationError, "no population declares"
        ):
            attacks.resolve_load_mutation(bare, "fabricate_counts", "", self.gold)
        primary_only = self.task.model_copy(
            update={
                "populations": tuple(
                    spec
                    for spec in self.task.populations
                    if spec.name is PopulationName.PRIMARY
                )
            }
        )
        with self.assertRaisesRegex(
            attacks.InapplicableLoadMutationError, "ONLY graded population"
        ):
            attacks.resolve_load_mutation(
                primary_only, "fabricate_counts", "primary", self.gold
            )

    def test_fabricate_hint_raises_loudly_if_the_divergence_regresses(self):
        """If the frozen counts ever equal the hints again (a divergence
        regression), the hint mutant is INERT and must raise — the loud
        failure mode, never a quiet 1.0."""
        regressed = self.gold.model_copy(
            update={
                "stage1": {
                    pop.value: {
                        t.name: dict(TINY_SCALE)[t.name] for t in self.task.tables
                    }
                    if pop is not PopulationName.COUNTERFACTUAL
                    else dict(self.gold.stage1[pop.value])
                    for pop in PopulationName
                }
            }
        )
        # make the counterfactual ALSO equal the fallback hint, so no
        # population kills and the mutant is fully inert
        regressed = regressed.model_copy(
            update={
                "stage1": {
                    **regressed.stage1,
                    PopulationName.COUNTERFACTUAL.value: dict(TINY_SCALE),
                }
            }
        )
        ws = self.tmp / "fab_inert_ws"
        (ws / "tasks" / self.task.task_id).mkdir(parents=True, exist_ok=True)
        with self.assertRaises(attacks.InertLoadMutationError):
            attacks.run_attack(
                self.task,
                _case("fabricate_inert", "fabricate_counts"),
                regressed,
                ws,
            )

    def test_an_inert_load_mutation_raises_instead_of_passing(self):
        """A mutant that keeps FULL extract-load reward on every population is
        indistinguishable from the trusted load and must never 'pass'.

        Forced deterministically by scoring `duplicate_on_load` against a gold
        whose counts ARE the doubled ones — the rule under test is the
        fail-closed check, not the data."""
        doubled = self.gold.model_copy(
            update={
                "stage1": {
                    pop: {table: 2 * n for table, n in counts.items()}
                    for pop, counts in self.gold.stage1.items()
                }
            }
        )
        with self.assertRaises(attacks.InertLoadMutationError):
            attacks.run_attack(
                self.task,
                _case("inert_probe", "duplicate_on_load"),
                doubled,
                self.workspace,
            )

    def test_internal_composite_is_recorded_alongside_two_unit_rewards(self):
        """`rewards` keeps its historical shape: no recorded artifact moves,
        and the per-variant numbers come from the SAME execution."""
        case = _case("partial_backend", "partial_backend")
        parent = attacks.run_attack(self.task, case, self.gold, self.workspace)
        record = self._record("partial_backend")
        self.assertEqual(
            record["rewards"], {p.value: parent[p] for p in parent}
        )
        self.assertEqual(
            set(record["rewards_by_variant"]),
            {v.value for v in RLVR_TASK_VARIANTS},
        )

    def test_a_load_mutant_is_deterministic_and_carries_no_wall_clock(self):
        case = _case("stale_snapshot", "stale_snapshot")
        first = attacks.run_attack(self.task, case, self.gold, self.workspace)
        record_a = self._record("stale_snapshot")
        second = attacks.run_attack(self.task, case, self.gold, self.workspace)
        record_b = self._record("stale_snapshot")
        self.assertEqual(first, second)
        self.assertEqual(record_a, record_b)
        self.assertNotIn("time", json.dumps(record_a).lower())

    def test_a_load_mutant_leaves_the_workspace_artifacts_untouched(self):
        """The mutation runs on a throwaway clone. If it edited the real
        rendered tree, every later stage would be scoring mutated data."""
        rendered = (
            self.workspace / "tasks" / self.task.task_id / "populations"
            / PopulationName.PRIMARY.value / "rendered"
        )
        before = {
            p.relative_to(rendered).as_posix(): p.read_bytes()
            for p in sorted(rendered.rglob("*"))
            if p.is_file()
        }
        attacks.run_attack(
            self.task,
            _case("duplicate_on_load", "duplicate_on_load"),
            self.gold,
            self.workspace,
        )
        after = {
            p.relative_to(rendered).as_posix(): p.read_bytes()
            for p in sorted(rendered.rglob("*"))
            if p.is_file()
        }
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# Declaration: what a task may honestly claim
# ---------------------------------------------------------------------------

class ElCatalogueTest(unittest.TestCase):
    def _named(self, cases) -> dict[str, AttackCase]:
        return {c.name: c for c in cases}

    def test_every_task_declares_the_two_structural_mutants_as_required(self):
        cases = self._named(pops.el_attack_cases())
        self.assertTrue(cases["partial_backend"].required)
        self.assertTrue(cases["duplicate_on_load"].required)
        self.assertEqual(
            set(cases["partial_backend"].expected_pass.values()), {False}
        )
        self.assertEqual(len(cases["partial_backend"].expected_pass), 5)

    def test_a_single_backend_task_still_has_an_el_attack(self):
        """This is the point of `partial_backend`: `skip_extraction` needs two
        backends, so without it a one-backend task has NO load-side mutant."""
        one = self._named(pops.derive_attack_cases((_shape(),), backends=1))
        self.assertNotIn("skip_extraction", one)
        self.assertIn("partial_backend", one)

    def test_skip_extraction_is_required_and_load_side_with_two_backends(self):
        """Promoted from an informational probe to a required EL mutant, and
        re-pointed at the RENDERED artifacts: the historical form set a skip
        list that forced the rows/*.jsonl fallback, so the probe named after
        extraction never exercised extraction."""
        many = self._named(pops.derive_attack_cases((_shape(),), backends=3))
        skip = many["skip_extraction"]
        self.assertTrue(skip.required)
        self.assertEqual(
            skip.mutation, attacks.LOAD_DIRECTIVE_PREFIX + "skip_backend"
        )
        self.assertEqual(set(skip.expected_pass.values()), {False})

    def test_backend_specific_probes_need_the_backend(self):
        files_only = pops.el_attack_cases(
            backend_assignments=(
                BackendAssignment(table="a", backend=Backend.FILES),
                BackendAssignment(table="b", backend=Backend.FILES),
            )
        )
        names = {c.name for c in files_only}
        self.assertIn("header_as_row", names)
        self.assertIn("wrong_source_file", names)  # two tables share a backend
        self.assertNotIn("truncate_table", names)  # a CSV is one unit

        rest = pops.el_attack_cases(
            backend_assignments=(
                BackendAssignment(table="a", backend=Backend.REST),
            )
        )
        names = {c.name for c in rest}
        self.assertIn("truncate_table", names)
        self.assertNotIn("header_as_row", names)
        self.assertNotIn("wrong_source_file", names)  # nothing shares a backend

    def test_header_as_row_is_required_only_where_a_text_only_csv_exists(self):
        """The catalogue must not declare a required kill whose only mechanism
        is a coercion crash (the gate refuses crash-kills as evidence)."""
        text_only = TableSpec(
            name="notes",
            columns=(
                ColumnSpec(name="note_id", type=ColumnType.TEXT, nullable=False),
                ColumnSpec(name="body", type=ColumnType.TEXT, nullable=True),
            ),
            primary_key=("note_id",),
        )
        typed = TableSpec(
            name="ticks",
            columns=(
                ColumnSpec(name="tick_id", type=ColumnType.INTEGER, nullable=False),
                ColumnSpec(name="label", type=ColumnType.TEXT, nullable=True),
            ),
            primary_key=("tick_id",),
        )
        files = lambda *names: tuple(  # noqa: E731
            BackendAssignment(table=n, backend=Backend.FILES) for n in names
        )
        with_text = self._named(
            pops.el_attack_cases((text_only,), backend_assignments=files("notes"))
        )["header_as_row"]
        self.assertTrue(with_text.required)
        self.assertTrue(with_text.expected_pass)
        self.assertEqual(set(with_text.expected_pass.values()), {False})
        self.assertEqual(len(with_text.expected_pass), len(PopulationName))
        for tables, assignments in (
            ((typed,), files("ticks")),
            ((), files("ticks")),
        ):
            case = self._named(
                pops.el_attack_cases(tables, backend_assignments=assignments)
            )["header_as_row"]
            self.assertFalse(case.required, tables)
            self.assertEqual(case.expected_pass, {}, tables)
            self.assertIn("informational", case.description, tables)
        # the demo itself: its only CSV is typed, so header_as_row is a probe
        demo = self._named(demo_fixture.demo_task().attack_cases)["header_as_row"]
        self.assertFalse(demo.required)
        self.assertEqual(demo.expected_pass, {})

    def test_null_row_drop_is_declared_only_where_a_column_is_nullable(self):
        not_null = (
            TableSpec(
                name="t",
                columns=(
                    ColumnSpec(name="id", type=ColumnType.INTEGER, nullable=False),
                ),
                primary_key=("id",),
            ),
        )
        self.assertNotIn(
            "null_row_drop", {c.name for c in pops.el_attack_cases(not_null)}
        )

    def test_fabricate_pair_is_declared_from_the_population_specs(self):
        """Applicability is decided from the SPEC, not asserted: the hint case
        is required exactly where the declared scales sit at or above the
        divergence floor (there the generator MUST miss the hint by 2-7%), the
        echo case exactly where the counterfactual's literal counts sit
        outside the primary hint's whole divergence band. A caller passing no
        populations declares neither — less, honestly."""
        task = demo_fixture.demo_task()
        cases = self._named(
            pops.el_attack_cases(
                task.tables,
                backend_assignments=task.backends,
                backends=3,
                populations=task.populations,
            )
        )
        hint = cases["fabricate_counts"]
        self.assertIs(hint.kind, AttackKind.FABRICATE_COUNTS)
        self.assertTrue(hint.required)
        self.assertEqual(
            hint.expected_pass,
            {
                PopulationName.PRIMARY: False,
                PopulationName.RESAMPLED: False,
                PopulationName.STRESS: False,
            },
        )
        echo = cases["fabricate_counts_primary_echo"]
        self.assertIs(echo.kind, AttackKind.FABRICATE_COUNTS)
        self.assertTrue(echo.required)
        self.assertEqual(
            echo.expected_pass, {PopulationName.COUNTERFACTUAL: False}
        )
        self.assertEqual(
            echo.mutation, attacks.LOAD_DIRECTIVE_PREFIX + "fabricate_counts:primary"
        )
        # neither PRIMARY nor RESAMPLED may carry a declared True: the echo
        # leaks there under the EL reward but scores 0 under the PARENT
        # reward (empty warehouse), and expected_pass is asserted under both.
        self.assertNotIn(PopulationName.PRIMARY, echo.expected_pass)
        without = {c.name for c in pops.el_attack_cases(task.tables, backends=3)}
        self.assertNotIn("fabricate_counts", without)
        self.assertNotIn("fabricate_counts_primary_echo", without)
        # below the divergence floor the hint is realized EXACTLY, so the
        # required claim would be a lie — the case degrades to a probe.
        tiny = self._named(
            pops.el_attack_cases(
                _tiny_task().tables,
                backend_assignments=_tiny_task().backends,
                backends=3,
                populations=_tiny_task().populations,
            )
        )
        self.assertFalse(tiny["fabricate_counts"].required)
        self.assertEqual(tiny["fabricate_counts"].expected_pass, {})

    def test_every_el_case_compiles_to_a_load_directive(self):
        cases = pops.el_attack_cases(
            _tiny_task().tables,
            backend_assignments=_tiny_task().backends,
            backends=3,
        )
        self.assertTrue(cases)
        for case in cases:
            self.assertTrue(
                case.mutation.startswith(attacks.LOAD_DIRECTIVE_PREFIX), case.name
            )
            name, _arg = attacks.split_load_directive(
                case.mutation[len(attacks.LOAD_DIRECTIVE_PREFIX):]
            )
            self.assertIn(name, attacks.LOAD_MUTATIONS)

    def test_el_cases_do_not_mutate_the_transform_sql(self):
        """An EL mutant ships the TRUSTED transform verbatim: if it changed the
        SQL too, a kill would not be evidence about the load."""
        task = _tiny_task()
        case = _case("partial_backend", "partial_backend")
        sql = attacks.materialize_mutation(task, case, None)
        self.assertEqual(sql, {m.name: task.reference.sql_by_mart[m.name] for m in task.marts})

    def test_the_catalogue_has_no_value_corrupting_mutation(self):
        """compare_stage1 grades ROW COUNTS ONLY, so a mutant that lands the
        right number of rows with wrong content scores 1.0. The vocabulary
        must not contain one — a declared mutant that cannot be killed is the
        fiction this catalogue exists to remove."""
        forbidden = {
            "wrong_column_mapping",
            "truncate_strings",
            "wrong_types",
            "column_swap",
            "wrong_row_order",
            "value_corruption",
        }
        self.assertEqual(attacks.LOAD_MUTATIONS & forbidden, frozenset())

    def test_load_cases_are_not_asserted_against_the_counterfactual_prose(self):
        """A load mutant is discriminated by the COUNT VECTOR, never by a
        constructed row, so `validate_population_coverage` must not demand the
        counterfactual's prose 'target' it."""
        task = _tiny_task()
        stripped = task.model_copy(
            update={
                "attack_cases": tuple(pops.el_attack_cases(task.tables, backends=3)),
                "populations": tuple(
                    p.model_copy(update={"literal_rows": {}, "conditions": ("none",)})
                    if p.name is PopulationName.COUNTERFACTUAL
                    else p
                    for p in task.populations
                ),
            }
        )
        problems = [
            p
            for p in pops.validate_population_coverage(stripped)
            if "attack kind" in p
        ]
        self.assertEqual(problems, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

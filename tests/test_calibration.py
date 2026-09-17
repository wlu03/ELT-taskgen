"""Tests for corpus/calibration.py — the SolverCalibrator (Round 3, step 7).

WHY THIS EXISTS
Difficulty claims are worthless unless something measured them. These tests
prove, with REAL execution over rendered populations and REAL frozen gold (no
network, no API keys — a deterministic fixture solver returns canned
submissions that are EXECUTED and scored, never trusted):

  * the pinned roster comes from config/agents.yaml and its fingerprint
    (Phase 0.F) folds the instrument version, the harness version and every
    tier's k, effort, max_tokens and endpoint beside the model keys actually
    run (the optional openai_compat tier changes the roster identity when
    present), so a cached record misses when any of them moves,
  * the solver view is the PUBLIC bundle of ONE variant (leak tripwire armed)
    and submissions are schema-enforced (fail closed),
  * each variant is calibrated SEPARATELY and the resulting
    VariantCalibration / EmpiricalDifficulty satisfy the model validators —
    extract_load never attributes a stage-2 failure, transform never
    attributes a stage-1 failure, the roster fingerprint matches the recorded
    tiers, and the evidence binds to the measured content hash,
  * a second run at the same (content hash, variant, roster) is a CACHE HIT
    with zero further solver calls,
  * c == 0 on every tier routes the task back to feasibility re-review as a
    Finding artifact, and c == k on EVERY tier flags it trivial — the ONE
    definition selection excludes on (Phase 0.F),
  * corpus/selection.py consumes those as BAND FILTERING: an empirically
    impossible task leaves the eligible pool while combined_score() stays
    structural,
  * without credentials/transcripts calibration is a VISIBLE SKIP with a
    reason — never fabricated numbers,
  * the solver view publishes EXACTLY the source-schema facts the shipped
    bundle publishes (the exporter's markdown block, both directions) and the
    EL/FULL prompt carries the shipped sample SOURCE TREE listing (paths only;
    transform refuses one),
  * a HARNESS failure (missing rendered population, missing gold, broken
    trusted loader, populations drifted from frozen gold) is the same visible
    skip — decided by preflight before any solver call, never a measured zero,
    never 'empirically impossible', never cached,
  * untrusted solver SQL runs on a sandboxed DuckDB connection (no file reads,
    no host writes, configuration locked) while the reference SQL still scores
    1.0,
  * `cached_calibration` reports what is already measured at this identity
    with zero provider calls and zero writes, so measured infeasibility is
    sticky at a content hash.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen import demo_fixture
from elt_taskgen.corpus import calibration as cal
from elt_taskgen.corpus import selection as selection_mod
from elt_taskgen.corpus.difficulty import structural_difficulty, with_empirical
from elt_taskgen.demo_fixture import MART_NAME, REFERENCE_SQL
from elt_taskgen.generation import source_data
from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    EmpiricalDifficulty,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    PopulationName,
    SolverTierResult,
    TableSpec,
    TaskIR,
    TaskStatus,
    TaskVariant,
    VariantCalibration,
    sha256_hex,
    solver_roster_fingerprint,
)
from elt_taskgen.reference import gold as gold_mod
from elt_taskgen.reference import runner as runner_mod
from elt_taskgen.reference import solution as solution_mod
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.verification import upstream_eval

P = PopulationName
V = TaskVariant

#: Reader format each demo backend is correctly read with.
FORMAT_BY_BACKEND: dict[Backend, str] = {
    Backend.POSTGRES: "postgres_sql",
    Backend.MONGODB: "jsonl",
    Backend.FILES: "csv",
    Backend.REST: "rest_pages",
    Backend.S3: "s3_jsonl",
}


# ---------------------------------------------------------------------------
# Shared expensive fixture: one generated + rendered demo workspace with gold
# ---------------------------------------------------------------------------

def build_workspace(task: TaskIR, workspace: Path):
    tdir = workspace / "tasks" / task.task_id
    for pop_spec in sorted(task.populations, key=lambda p: p.name.value):
        pop = pop_spec.name
        rows = source_data.generate_rows(task, pop)
        source_data.write_rows(rows, tdir / "populations" / pop.value / "rows")
        source_data.render_population(
            task, pop, rows, tdir / "populations" / pop.value / "rendered"
        )
    results = {pop: runner_mod.run_reference(task, pop, workspace) for pop in P}
    return gold_mod.freeze_gold(task, results, tdir / "answer_key")


#: Smaller local scales test calibration without repeating full throughput
#: coverage; the pinned demo remains untouched.
_REDUCED_SCALES: dict[PopulationName, dict[str, int]] = {
    P.PRIMARY: {"customers": 20, "orders": 60, "order_items": 180},
    P.RESAMPLED: {"customers": 20, "orders": 60, "order_items": 180},
    P.STRESS: {"customers": 8, "orders": 200, "order_items": 600},
}


def reduced_demo_task() -> TaskIR:
    task = demo_fixture.demo_task()
    populations = tuple(
        spec.model_copy(update={"scale": _REDUCED_SCALES[spec.name]})
        if spec.name in _REDUCED_SCALES
        else spec
        for spec in task.populations
    )
    return task.model_copy(update={"populations": populations})


_SHARED: dict = {}


def _shared_fixture():
    if not _SHARED:
        task = reduced_demo_task()
        tmp = tempfile.TemporaryDirectory()

        def cleanup_fixture() -> None:
            _SHARED.clear()
            tmp.cleanup()

        unittest.addModuleCleanup(cleanup_fixture)
        workspace = Path(tmp.name) / "taskgen-workspace"
        gold = build_workspace(task, workspace)
        _SHARED.update(
            {"task": task, "tmp": tmp, "workspace": workspace, "gold": gold}
        )
    return _SHARED


def correct_load_plan(task: TaskIR, rendered_root: Path) -> dict[str, dict[str, str]]:
    """The load plan a correct solver would submit (paths are population
    independent: every population renders the same layout)."""
    plan: dict[str, dict[str, str]] = {}
    for table in task.tables:
        artifact = solution_mod.find_rendered_artifact(
            task, rendered_root, table.name
        )
        backend = task.backend_for(table.name).backend
        plan[table.name] = {
            "path": str(artifact.relative_to(rendered_root)),
            "format": FORMAT_BY_BACKEND[backend],
        }
    return plan


#: A plan naming an artifact that does not exist: every population fails in the
#: LOAD phase, so every failure is attributed to stage 1.
BROKEN_LOAD_PLAN = {
    "customers": {"path": "postgres/nope.sql", "format": "postgres_sql"},
    "orders": {"path": "mongodb/nope.jsonl", "format": "jsonl"},
    "order_items": {"path": "files/nope.csv", "format": "csv"},
}

#: Valid SQL that produces the wrong result: fails in the TRANSFORM phase.
DEGENERATE_SQL = "SELECT customer_id, customer_name, 0 AS x FROM customers WHERE 1=0"

CORRECT = "correct"
DEGENERATE = "degenerate"


class FixtureSolver:
    """Deterministic canned solver (test-internal instrument, never trusted).

    Per-role policy: 'correct' returns the reference solution, 'degenerate'
    returns a submission that executes but scores 0. The variant is read off
    the prompt's declared response schema, exactly as a real solver would.
    """

    def __init__(self, policy: dict[str, str], load_plan: dict[str, dict[str, str]]):
        self.policy = dict(policy)
        self.load_plan = load_plan
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _variant_of(prompt: str) -> TaskVariant:
        wants_load = '"load_plan"' in prompt
        wants_sql = '"sql_by_mart"' in prompt
        if wants_load and wants_sql:
            return V.FULL
        if wants_load:
            return V.EXTRACT_LOAD
        return V.TRANSFORM

    def complete(self, role, prompt: str) -> str:
        role_name = str(getattr(role, "value", role))
        self.calls.append((role_name, prompt))
        variant = self._variant_of(prompt)
        mode = self.policy.get(role_name, CORRECT)
        payload: dict = {}
        if variant in (V.FULL, V.EXTRACT_LOAD):
            payload["load_plan"] = (
                self.load_plan if mode == CORRECT else BROKEN_LOAD_PLAN
            )
        if variant in (V.FULL, V.TRANSFORM):
            payload["sql_by_mart"] = {
                MART_NAME: REFERENCE_SQL if mode == CORRECT else DEGENERATE_SQL
            }
        return json.dumps(payload)


def tier(model_key: str, k: int, rank: int) -> cal.SolverTier:
    return cal.SolverTier(
        model_key=model_key,
        provider="anthropic",
        model=model_key.split(":", 1)[-1],
        k=k,
        rank=rank,
    )


#: Two-tier test roster: 'weak' is rank 0 (ordering only — the trivial flag is
#: read off EVERY tier) and the tiers carry DIFFERENT k, like the shipped 8/8/4
#: roster. Small k keeps the suite fast — every attempt really executes five
#: populations.
WEAK = tier("anthropic:weak-tier", k=2, rank=0)
STRONG = tier("anthropic:strong-tier", k=1, rank=1)
ROSTER = (WEAK, STRONG)
#: Attempts per variant for ROSTER (2 + 1).
ATTEMPTS_PER_VARIANT = WEAK.k + STRONG.k


class CalibrationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shared = _shared_fixture()
        cls.task = shared["task"]
        cls.base_workspace = shared["workspace"]
        cls.gold = shared["gold"]
        cls.plan = correct_load_plan(
            cls.task,
            runner_mod.rendered_dir(cls.base_workspace, cls.task.task_id, P.PRIMARY),
        )
        #: The shipped sample source-tree listing (development rendered tree),
        #: which the EL/FULL prompt REQUIRES (transform refuses it).
        cls.listing = cal.sources_listing(cls.base_workspace, cls.task.task_id)

    @classmethod
    def view(cls, task: TaskIR, variant: TaskVariant) -> str:
        """solver_view with the per-variant listing rule applied."""
        variant = TaskVariant(variant)
        if variant is V.TRANSFORM:
            return cal.solver_view(task, variant)
        return cal.solver_view(task, variant, sources_listing=cls.listing)

    @classmethod
    def prompt(cls, task: TaskIR, variant: TaskVariant, index: int) -> str:
        variant = TaskVariant(variant)
        if variant is V.TRANSFORM:
            return cal.attempt_prompt(task, variant, index)
        return cal.attempt_prompt(task, variant, index, sources_listing=cls.listing)

    def fresh_workspace(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = Path(tmp.name) / "taskgen-workspace"
        shutil.copytree(self.base_workspace, dest)
        return dest

    def solver(self, **policy_by_tier: str) -> FixtureSolver:
        policy = {
            t.role_name: policy_by_tier.get(name, CORRECT)
            for name, t in (("weak", WEAK), ("strong", STRONG))
        }
        return FixtureSolver(policy, self.plan)


# ---------------------------------------------------------------------------
# The pinned roster
# ---------------------------------------------------------------------------

class RosterTests(unittest.TestCase):
    def test_default_roster_from_agents_yaml(self) -> None:
        with mock.patch.dict(os.environ, {"ELT_TASKGEN_OSS_MODEL": ""}):
            roster = cal.load_calibration_roster()
        keys = [t.model_key for t in roster]
        self.assertEqual(
            keys,
            [
                "anthropic:claude-haiku-4-5",
                "anthropic:claude-sonnet-5",
                "anthropic:claude-opus-5",
            ],
        )
        self.assertEqual([t.k for t in roster], [8, 8, 4])
        # weakest first (ordering only: the trivial flag is read off every tier)
        self.assertEqual(roster[0].rank, 0)
        # Phase 0.F: the campaign fingerprint is STRICTER than the model-level
        # roster identity — it also folds instrument/harness version, k,
        # effort, max_tokens and endpoint — and it is deterministic.
        fingerprint = cal.roster_fingerprint(roster)
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(fingerprint, cal.roster_fingerprint(tuple(roster)))
        self.assertNotEqual(fingerprint, solver_roster_fingerprint(keys))
        # ... while EmpiricalDifficulty keeps carrying the model-level identity
        self.assertEqual(
            solver_roster_fingerprint(t.model_key for t in roster),
            solver_roster_fingerprint(keys),
        )

    def test_optional_oss_tier_changes_roster_identity(self) -> None:
        with mock.patch.dict(os.environ, {"ELT_TASKGEN_OSS_MODEL": ""}):
            without = cal.load_calibration_roster()
        with mock.patch.dict(
            os.environ, {"ELT_TASKGEN_OSS_MODEL": "some-oss/model-1"}
        ):
            with_oss = cal.load_calibration_roster()
        self.assertEqual(len(with_oss), len(without) + 1)
        self.assertIn("openai_compat:some-oss/model-1", [t.model_key for t in with_oss])
        self.assertNotEqual(
            cal.roster_fingerprint(with_oss), cal.roster_fingerprint(without)
        )

    def test_roster_tiers_carry_their_provider_endpoint(self) -> None:
        env = {
            "ELT_TASKGEN_OSS_MODEL": "some-oss/model-1",
            "ELT_TASKGEN_OSS_BASE_URL": "https://oss.example.test/v1/",
        }
        with mock.patch.dict(os.environ, env):
            roster = cal.load_calibration_roster()
        by_provider = {t.provider: t.endpoint for t in roster}
        # the backend default when agents.yaml sets no base_url ...
        self.assertEqual(by_provider["anthropic"], "https://api.anthropic.com")
        # ... and the configured base_url (normalised) otherwise
        self.assertEqual(by_provider["openai_compat"], "https://oss.example.test/v1")
        # the same model ids behind another gateway are a different instrument
        with mock.patch.dict(
            os.environ, {**env, "ELT_TASKGEN_OSS_BASE_URL": "https://other.example.test/v1"}
        ):
            other = cal.load_calibration_roster()
        self.assertEqual([t.model_key for t in other], [t.model_key for t in roster])
        self.assertNotEqual(cal.roster_fingerprint(other), cal.roster_fingerprint(roster))

    def test_role_names_are_filesystem_safe_and_unique(self) -> None:
        with mock.patch.dict(os.environ, {"ELT_TASKGEN_OSS_MODEL": "vendor/model:v1"}):
            roster = cal.load_calibration_roster()
        names = [t.role_name for t in roster]
        self.assertEqual(len(set(names)), len(names))
        for name in names:
            self.assertRegex(name, r"^solver__[A-Za-z0-9_.-]+$")

    def test_malformed_roster_fails_closed(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "agents.yaml"
        path.write_text(
            "calibration:\n  roster:\n"
            "    - provider: nope\n      model: m\n      k: 2\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            cal.load_calibration_roster(path)
        path.write_text(
            "calibration:\n  roster:\n"
            "    - provider: anthropic\n      model: m\n      k: 0\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            cal.load_calibration_roster(path)
        with self.assertRaises(FileNotFoundError):
            cal.load_calibration_roster(Path(tmp.name) / "missing.yaml")


# ---------------------------------------------------------------------------
# The solver view + submission schema
# ---------------------------------------------------------------------------

class ViewTests(CalibrationTestCase):
    def test_variant_views_are_public_and_variant_shaped(self) -> None:
        full = self.view(self.task, V.FULL)
        el = self.view(self.task, V.EXTRACT_LOAD)
        transform = self.view(self.task, V.TRANSFORM)

        self.assertIn('"load_plan"', full)
        self.assertIn('"sql_by_mart"', full)
        # EL ships no data model: no mart specs, no transform submission
        self.assertIn('"load_plan"', el)
        self.assertNotIn('"sql_by_mart"', el)
        self.assertNotIn(MART_NAME, el)
        # TRANSFORM is handed the warehouse: no load plan asked for
        self.assertNotIn('"load_plan"', transform)
        self.assertIn('"sql_by_mart"', transform)
        self.assertIn(MART_NAME, transform)

        for view in (full, el, transform):
            self.assertNotIn("answer_key", view.lower())
            self.assertNotIn("SELECT DISTINCT order_id, customer_id", view)

    def test_leaky_prose_trips_the_view_guard(self) -> None:
        leaky = self.task.model_copy(
            update={"solver_prompt": "here is the answer:\n" + REFERENCE_SQL}
        )
        for variant in (V.FULL, V.EXTRACT_LOAD, V.TRANSFORM):
            with self.assertRaises(ValueError):
                self.view(leaky, variant)

    def test_attempt_prompts_are_distinct_transcript_keys(self) -> None:
        p0 = self.prompt(self.task, V.FULL, 0)
        p1 = self.prompt(self.task, V.FULL, 1)
        self.assertNotEqual(p0, p1)
        self.assertTrue(p1.startswith(p0))

    # -- parity with the shipped bundle (BOTH directions) ------------------

    def test_source_section_publishes_what_the_bundle_publishes(self) -> None:
        """Every source-schema line the exporter puts in documentation.md
        (enum domains, business keys, nullability, relationships) is in the
        solver view of every variant — by construction, the same block."""
        from elt_taskgen.export import eltbench

        task = demo_fixture.demo_task()  # enum_values + business_key present
        block = [line for line in eltbench._source_schema_markdown(task) if line]
        self.assertTrue(any("one of: cancelled, completed" in ln for ln in block))
        self.assertTrue(any("business key: order_id" in ln for ln in block))
        for variant in (V.EXTRACT_LOAD, V.TRANSFORM, V.FULL):
            view = self.view(task, variant)
            for line in block:
                self.assertIn(line, view, f"{variant.value}: {line!r}")
            self.assertIn("one of: cancelled, completed", view)
            self.assertIn("business key: order_id", view)
            self.assertIn("NOT NULL", view)
            self.assertIn("--- SOURCE TABLES ---", view)

    def test_view_states_no_fact_the_bundle_omits(self) -> None:
        """Parity the other way: strip enum domains and business keys from
        the task and the view must not mention them."""
        task = demo_fixture.demo_task()
        tables = tuple(
            t.model_copy(
                update={
                    "business_key": (),
                    "columns": tuple(
                        c.model_copy(update={"enum_values": None}) for c in t.columns
                    ),
                }
            )
            for t in task.tables
        )
        stripped = task.model_copy(update={"tables": tables})
        for variant in (V.EXTRACT_LOAD, V.TRANSFORM, V.FULL):
            view = self.view(stripped, variant)
            self.assertNotIn("one of:", view)
            self.assertNotIn("business key", view)


# ---------------------------------------------------------------------------
# The shipped SOURCE TREE listing in the EL/FULL prompt
# ---------------------------------------------------------------------------

class SourceTreeListingTests(CalibrationTestCase):
    """The EL/FULL stimulus carries the one public thing the shipped bundle
    carries and a TaskIR-only prompt omits: the sample source-tree LISTING
    (paths only). Transform carries none — its bundle ships a warehouse."""

    def test_el_and_full_views_list_the_shipped_source_tree(self) -> None:
        listing = cal.sources_listing(self.base_workspace, self.task.task_id)
        self.assertEqual(listing, self.listing)
        self.assertTrue(listing)
        for variant in (V.EXTRACT_LOAD, V.FULL):
            view = cal.solver_view(self.task, variant, sources_listing=listing)
            self.assertIn("--- SOURCE TREE", view)
            for step in self.plan.values():
                path = step["path"]
                self.assertTrue(
                    path in view
                    or any(line.startswith(path + "/") for line in listing),
                    f"{variant.value}: {path!r} not derivable from the view",
                )
            for line in listing:
                self.assertIn(line, view)
        transform = cal.solver_view(self.task, V.TRANSFORM)
        self.assertNotIn("--- SOURCE TREE", transform)
        for line in self.listing:
            self.assertNotIn(line, transform)

    def test_el_view_fails_closed_without_listing(self) -> None:
        with self.assertRaises(ValueError):
            cal.solver_view(self.task, V.EXTRACT_LOAD)
        with self.assertRaises(ValueError):
            cal.solver_view(self.task, V.FULL)
        with self.assertRaises(ValueError):
            cal.solver_view(self.task, V.EXTRACT_LOAD, sources_listing=())
        with self.assertRaises(ValueError):
            cal.attempt_prompt(self.task, V.EXTRACT_LOAD, 0)
        # per-variant honesty: transform REFUSES a listing
        with self.assertRaises(ValueError):
            cal.solver_view(self.task, V.TRANSFORM, sources_listing=self.listing)
        with self.assertRaises(ValueError):
            cal.attempt_prompt(self.task, V.TRANSFORM, 0, sources_listing=self.listing)

        # a workspace without the development rendered tree cannot be
        # measured on EL: CalibrationError (harness), never a solver zero
        ws = self.fresh_workspace()
        shutil.rmtree(runner_mod.rendered_dir(ws, self.task.task_id, P.DEVELOPMENT))
        with self.assertRaises(cal.CalibrationError):
            cal.sources_listing(ws, self.task.task_id)
        with self.assertRaises(cal.CalibrationError):
            cal.calibrate_variant(
                self.task, self.gold, ws, self.solver(), ROSTER, V.EXTRACT_LOAD
            )
        self.assertFalse(cal.cache_path(ws, self.task.task_id, V.EXTRACT_LOAD).exists())

    def test_listing_matches_shipped_bundle_and_is_paths_only(self) -> None:
        from elt_taskgen.export import eltbench
        from elt_taskgen.reference import independent

        ws = self.fresh_workspace()
        task_dir = ws / "tasks" / self.task.task_id
        eltbench.emit_variant(
            self.task,
            self.gold,
            V.EXTRACT_LOAD,
            task_dir / "variants" / "extract_load",
            populations_dir=task_dir / "populations",
        )
        bundle_dir = independent.el_bundle_dir(ws, self.task.task_id)
        self.assertEqual(
            list(independent._sources_listing(bundle_dir)),
            list(cal.sources_listing(ws, self.task.task_id)),
        )
        listing = cal.sources_listing(ws, self.task.task_id)
        for entry in listing:
            self.assertNotIn("\n", entry)
            self.assertNotIn("{", entry)
            self.assertFalse(entry.startswith("/"))
            self.assertNotIn("..", entry)
        # rendered_listing is the shared primitive: same bytes for the bundle
        self.assertEqual(
            cal.rendered_listing(bundle_dir / "sources"), listing
        )
        self.assertEqual(cal.rendered_listing(ws / "does-not-exist"), ())
        # the view built from it passes the leak barrier
        view = cal.solver_view(self.task, V.EXTRACT_LOAD, sources_listing=listing)
        cal._assert_view_clean(self.task, view)

    def test_calibrate_variant_hands_solvers_the_listing(self) -> None:
        ws = self.fresh_workspace()
        solver = self.solver()
        for variant in (V.EXTRACT_LOAD, V.FULL, V.TRANSFORM):
            cal.calibrate_variant(self.task, self.gold, ws, solver, ROSTER, variant)
        self.assertEqual(len(solver.calls), ATTEMPTS_PER_VARIANT * 3)
        for _role, prompt in solver.calls:
            wants_listing = '"load_plan"' in prompt
            for line in self.listing:
                if wants_listing:
                    self.assertIn(line, prompt)
                else:
                    self.assertNotIn(line, prompt)
            self.assertEqual("--- SOURCE TREE" in prompt, wants_listing)


# ---------------------------------------------------------------------------
# The solver PROMPT contract — the prompts ARE the measuring instrument
# ---------------------------------------------------------------------------

def _flat(text: str) -> str:
    """Whitespace-collapsed lowercase (the prompts are hard-wrapped)."""
    return re.sub(r"\s+", " ", text).strip().lower()


#: Solving advice that would inflate the measured pass rate above what a
#: training-time solver would achieve on the shipped bundle. Checked over the
#: FACTORY-authored text only (preamble + per-variant scope): the task
#: material itself is the author's prose and is owned by the prose gate.
_SOLVING_HINTS: tuple[str, ...] = (
    "order by",
    "tie-break",
    "tie break",
    "any_value",
    "left join",
    "inner join",
    "coalesce",
    "group by",
    "distinct",
    "make sure to",
    "remember to",
    "hint",
    "be careful",
    "watch out",
    "step by step",
    "think through",
)


class SolverPromptContractTests(CalibrationTestCase):
    """A pass rate means nothing without the stimulus that produced it: the
    per-variant prompts must match each variant's bundle and reward, carry no
    solving hints, and be byte-identical for a fixed task."""

    def _views(self) -> dict[TaskVariant, str]:
        return {v: self.view(self.task, v) for v in cal.DEFAULT_VARIANTS}

    def test_default_prompts_are_exactly_the_two_active_unit_documents(self) -> None:
        views = self._views()
        self.assertEqual(cal.DEFAULT_VARIANTS, RLVR_TASK_VARIANTS)
        self.assertEqual(tuple(views), RLVR_TASK_VARIANTS)
        self.assertEqual(len(set(views.values())), 2)
        # Each names its own scope, and only its own submission contract.
        el, tr = views[V.EXTRACT_LOAD], views[V.TRANSFORM]
        self.assertIn("Implement ONLY the Extract + Load stage", el)
        self.assertIn("Implement ONLY the transform stage", tr)
        self.assertIn('"load_plan"', el)
        self.assertNotIn('"sql_by_mart"', el)
        self.assertIn('"sql_by_mart"', tr)
        self.assertNotIn('"load_plan"', tr)
        # The EL bundle ships no data model, so the prompt shows no marts.
        self.assertNotIn(MART_NAME, el)
        self.assertIn(MART_NAME, tr)

    def test_legacy_full_prompt_remains_available_when_explicit(self) -> None:
        full = self.view(self.task, V.FULL)
        self.assertIn("Implement the WHOLE project", full)
        self.assertIn('"load_plan"', full)
        self.assertIn('"sql_by_mart"', full)
        self.assertIn(MART_NAME, full)

    def test_prompted_reward_matches_evaluate_variant(self) -> None:
        """Each prompt's scoring paragraph is checked against what
        upstream_eval.evaluate_variant actually computes — a promise the
        evaluator does not keep would measure the wrong thing."""
        views = {v: _flat(t) for v, t in self._views().items()}
        pop = P.PRIMARY
        counts = dict(self.gold.stage1[pop.value])
        wrong_counts = dict(counts)
        wrong_counts[sorted(wrong_counts)[0]] += 1

        # extract_load — STRICT BINARY on stage-1 counts, no partial credit.
        self.assertIn("strict binary", views[V.EXTRACT_LOAD])
        self.assertIn("no partial credit", views[V.EXTRACT_LOAD])
        self.assertNotIn("fraction of target marts", views[V.EXTRACT_LOAD])
        self.assertEqual(
            upstream_eval.evaluate_variant(
                V.EXTRACT_LOAD, self.task, self.gold, pop, counts, {}
            ).reward,
            1.0,
        )
        self.assertEqual(
            upstream_eval.evaluate_variant(
                V.EXTRACT_LOAD, self.task, self.gold, pop, wrong_counts, {}
            ).reward,
            0.0,
        )

        # transform — stage 1 is handed over and NOT scored; mart fraction.
        self.assertIn("not scored", views[V.TRANSFORM])
        self.assertIn("fraction of target marts", views[V.TRANSFORM])
        self.assertNotIn("strict binary", views[V.TRANSFORM])
        handed_over = upstream_eval.evaluate_variant(
            V.TRANSFORM, self.task, self.gold, pop, {}, {}
        )
        self.assertTrue(handed_over.stage1_pass)  # no counts submitted at all
        self.assertEqual(handed_over.reward, 0.0)  # marts still all wrong

        # Legacy FULL remains an explicit diagnostic, outside DEFAULT_VARIANTS.
        full_view = _flat(self.view(self.task, V.FULL))
        self.assertIn("the load gates everything", full_view)
        self.assertIn("fraction of target marts", full_view)
        gated = upstream_eval.evaluate_variant(
            V.FULL, self.task, self.gold, pop, wrong_counts, {}
        )
        self.assertFalse(gated.stage1_pass)
        self.assertEqual(gated.reward, 0.0)

    def test_prompts_carry_no_solving_hints(self) -> None:
        factory_text = _flat(
            cal._SOLVER_PREAMBLE
            + "\n"
            + "\n".join(
                "\n".join(block) for block in cal._VARIANT_SCOPE.values()
            )
        )
        for hint in _SOLVING_HINTS:
            self.assertNotIn(hint, factory_text, f"solving hint: {hint!r}")
        # ... and no answer-side vocabulary either.
        for marker in ("answer_key", "population", "gold row", "attack"):
            self.assertNotIn(marker, factory_text, f"private vocab: {marker!r}")

    def test_prompts_state_the_declarative_load_plan_contract(self) -> None:
        for variant in (V.EXTRACT_LOAD, V.FULL):
            flat = _flat(self.view(self.task, variant))
            self.assertIn("you do not write extraction code", flat)
            for reader in cal.LOAD_FORMATS:
                self.assertIn(reader, flat)
            self.assertIn("path relative to the source root", flat)

    def test_prompts_are_deterministic_for_a_fixed_task(self) -> None:
        for variant in cal.DEFAULT_VARIANTS:
            first = self.view(reduced_demo_task(), variant)
            second = self.view(reduced_demo_task(), variant)
            self.assertEqual(first, second)
            self.assertEqual(
                self.prompt(reduced_demo_task(), variant, 3),
                self.prompt(reduced_demo_task(), variant, 3),
            )
            # Invariant framing FIRST (prompt caching), salt appended LAST.
            self.assertTrue(first.startswith(cal._SOLVER_PREAMBLE))
            self.assertTrue(
                self.prompt(self.task, variant, 1).startswith(
                    self.view(self.task, variant)
                )
            )
        # The listing is derived from the real tree and sorted, so the same
        # workspace yields the same listing (and hence the same prompt bytes).
        self.assertEqual(
            cal.sources_listing(self.base_workspace, self.task.task_id),
            self.listing,
        )


class SubmissionSchemaTests(CalibrationTestCase):
    def test_valid_submissions_parse(self) -> None:
        sub = cal.parse_submission(
            self.task,
            V.FULL,
            json.dumps(
                {"load_plan": self.plan, "sql_by_mart": {MART_NAME: REFERENCE_SQL}}
            ),
        )
        self.assertEqual(set(sub.load_plan), {t.name for t in self.task.tables})
        self.assertEqual(sub.sql_by_mart[MART_NAME], REFERENCE_SQL)
        fenced = "```json\n" + json.dumps({"load_plan": self.plan}) + "\n```"
        self.assertEqual(
            set(cal.parse_submission(self.task, V.EXTRACT_LOAD, fenced).load_plan),
            {t.name for t in self.task.tables},
        )

    def test_malformed_submissions_fail_closed(self) -> None:
        bad = [
            (V.FULL, "not json"),
            (V.FULL, json.dumps({"load_plan": self.plan})),  # missing sql
            (V.EXTRACT_LOAD, json.dumps({"sql_by_mart": {MART_NAME: "SELECT 1"}})),
            (V.TRANSFORM, json.dumps({"sql_by_mart": {"other_mart": "SELECT 1"}})),
            (V.TRANSFORM, json.dumps({"sql_by_mart": {MART_NAME: ""}})),
            (
                V.EXTRACT_LOAD,
                json.dumps({"load_plan": {"customers": {"path": "a", "format": "x"}}}),
            ),
            (
                V.EXTRACT_LOAD,
                json.dumps(
                    {
                        "load_plan": {
                            name: {"path": "a.csv", "format": "csv"}
                            for name in ("customers", "orders")
                        }
                    }
                ),
            ),
        ]
        for variant, text in bad:
            with self.assertRaises(ProviderProtocolError, msg=text[:50]):
                cal.parse_submission(self.task, variant, text)

    def test_load_plan_cannot_escape_the_source_root(self) -> None:
        ws = self.fresh_workspace()
        rdir = runner_mod.rendered_dir(ws, self.task.task_id, P.PRIMARY)
        escaping = {
            name: {"path": "../../answer_key/manifest.json", "format": "csv"}
            for name in (t.name for t in self.task.tables)
        }
        submission = cal.parse_submission(
            self.task, V.EXTRACT_LOAD, json.dumps({"load_plan": escaping})
        )
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            with self.assertRaises(cal.LoadPlanError):
                cal.execute_load_plan(self.task, submission.load_plan, rdir, con)
        finally:
            con.close()

    def test_s3_jsonl_refuses_a_part_file(self) -> None:
        """The published reader contract says PREFIX DIRECTORY; a plan naming
        a FILE under-reads any table larger than one part on exactly the
        population where it grows past the boundary (measured: the reddit
        independent loader named part-00000 and lost only stress). The
        executor must refuse the file loudly on EVERY population instead."""
        ws = self.fresh_workspace()
        rdir = runner_mod.rendered_dir(ws, self.task.task_id, P.PRIMARY)
        some_file = next(
            p.relative_to(rdir).as_posix() for p in sorted(rdir.rglob("*")) if p.is_file()
        )
        plan = {k: dict(v) for k, v in self.plan.items()}
        first = sorted(plan)[0]
        plan[first] = {"path": some_file, "format": "s3_jsonl"}
        submission = cal.parse_submission(
            self.task, V.EXTRACT_LOAD, json.dumps({"load_plan": plan})
        )
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            with self.assertRaises(cal.LoadPlanError) as ctx:
                cal.execute_load_plan(self.task, submission.load_plan, rdir, con)
            self.assertIn("prefix directory", str(ctx.exception))
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Per-variant calibration over real execution
# ---------------------------------------------------------------------------

class PerVariantCalibrationTests(CalibrationTestCase):
    def test_both_default_units_measured_separately_and_model_valid(self) -> None:
        ws = self.fresh_workspace()
        solver = self.solver(weak=DEGENERATE, strong=CORRECT)
        result = cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=ROSTER
        )

        self.assertEqual(result.skipped_reason, "")
        self.assertEqual(cal.DEFAULT_VARIANTS, RLVR_TASK_VARIANTS)
        self.assertEqual(
            sorted(result.records), sorted(v.value for v in cal.DEFAULT_VARIANTS)
        )
        # one call per attempt: (k=2 + k=1) x 2 active units
        self.assertEqual(len(solver.calls), ATTEMPTS_PER_VARIANT * 2)

        for variant in cal.DEFAULT_VARIANTS:
            record = result.records[variant.value]
            vector = record.pass_rate_vector()
            self.assertEqual(vector[STRONG.model_key], 1.0)
            self.assertEqual(vector[WEAK.model_key], 0.0)
            self.assertEqual(record.task_content_hash, self.task.content_hash())
            self.assertEqual(
                record.roster_fingerprint, cal.roster_fingerprint(ROSTER)
            )
            tiers = {t.model_key: t for t in record.calibration.tiers}
            weak = tiers[WEAK.model_key]
            if variant is V.EXTRACT_LOAD:
                self.assertEqual(weak.stage1_failures, weak.k)
                self.assertEqual(weak.stage2_failures, 0)
            elif variant is V.TRANSFORM:
                self.assertEqual(weak.stage1_failures, 0)
                self.assertEqual(weak.stage2_failures, weak.k)

        empirical = result.empirical
        self.assertIsNotNone(empirical)
        self.assertEqual(
            set(empirical.variants), set(cal.DEFAULT_VARIANTS)
        )
        # The campaign fingerprint binds the records; EmpiricalDifficulty carries
        # the model-level roster identity its validator re-derives (Phase 0.F).
        self.assertEqual(result.roster_fingerprint, cal.roster_fingerprint(ROSTER))
        self.assertEqual(
            empirical.roster_fingerprint,
            solver_roster_fingerprint(t.model_key for t in ROSTER),
        )
        self.assertEqual(
            empirical.campaign_fingerprint,
            cal.roster_fingerprint(ROSTER),
        )
        self.assertEqual(
            empirical.measured_at_content_hash, self.task.content_hash()
        )
        self.assertEqual(empirical.n_attempts, ATTEMPTS_PER_VARIANT * 2)
        self.assertAlmostEqual(empirical.success_rate, STRONG.k / ATTEMPTS_PER_VARIANT)
        self.assertAlmostEqual(
            empirical.stage1_failure_rate + empirical.stage2_failure_rate, 1.0
        )

        # the seam: attach via with_empirical, structural scores untouched
        structural = structural_difficulty(self.task)
        attached = with_empirical(structural, empirical)
        self.assertEqual(attached.combined_score(), structural.combined_score())
        self.assertEqual(attached.empirical, empirical)

    def test_legacy_full_can_still_be_calibrated_when_explicit(self) -> None:
        ws = self.fresh_workspace()
        solver = self.solver(weak=DEGENERATE, strong=CORRECT)
        result = cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            solver,
            roster=ROSTER,
            variants=(V.FULL,),
        )
        self.assertEqual(tuple(result.records), (V.FULL.value,))
        self.assertEqual(len(solver.calls), ATTEMPTS_PER_VARIANT)
        self.assertEqual(set(result.empirical.variants), {V.FULL})

    def test_stale_evidence_cannot_attach(self) -> None:
        ws = self.fresh_workspace()
        result = cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            self.solver(),
            roster=ROSTER,
            variants=(V.TRANSFORM,),
        )
        other = structural_difficulty(self.task).model_copy(
            update={"task_content_hash": "f" * 64}
        )
        with self.assertRaises(ValueError):
            with_empirical(other, result.empirical)


class CacheTests(CalibrationTestCase):
    def test_second_run_is_a_cache_hit_with_no_recomputation(self) -> None:
        ws = self.fresh_workspace()
        solver = self.solver()
        first = cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=ROSTER, variants=(V.TRANSFORM,)
        )
        calls_after_first = len(solver.calls)
        self.assertEqual(calls_after_first, ATTEMPTS_PER_VARIANT)
        self.assertEqual(first.from_cache, ())
        self.assertTrue(cal.cache_path(ws, self.task.task_id, V.TRANSFORM).is_file())

        second = cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=ROSTER, variants=(V.TRANSFORM,)
        )
        self.assertEqual(len(solver.calls), calls_after_first)  # ZERO new calls
        self.assertEqual(second.from_cache, (V.TRANSFORM.value,))
        self.assertEqual(
            second.records[V.TRANSFORM.value].pass_rate_vector(),
            first.records[V.TRANSFORM.value].pass_rate_vector(),
        )
        self.assertEqual(second.empirical, first.empirical)

    def test_refresh_and_stale_identity_bypass_the_cache(self) -> None:
        ws = self.fresh_workspace()
        solver = self.solver()
        cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=ROSTER, variants=(V.TRANSFORM,)
        )
        baseline = len(solver.calls)

        cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            solver,
            roster=ROSTER,
            variants=(V.TRANSFORM,),
            refresh=True,
        )
        self.assertEqual(len(solver.calls), baseline * 2)

        # a different roster is a different identity: cache miss
        other_roster = (WEAK,)
        self.assertIsNone(
            cal.load_cached_record(
                ws, self.task, V.TRANSFORM, cal.roster_fingerprint(other_roster)
            )
        )
        # a moved content hash is a cache miss too (repair invalidates evidence)
        moved = self.task.model_copy(update={"title": "a different title"})
        self.assertNotEqual(moved.content_hash(), self.task.content_hash())
        self.assertIsNone(
            cal.load_cached_record(
                ws, moved, V.TRANSFORM, cal.roster_fingerprint(ROSTER)
            )
        )

    def test_calibration_cache_misses_when_k_effort_max_tokens_or_instrument_change(
        self,
    ) -> None:
        """Roadmap Phase 0.F: the roster fingerprint folds the instrument
        version, the harness version and every tier's k, effort, max_tokens
        and endpoint, so a record measured under any other value is a MISS —
        a cached one-shot record can never masquerade as a measurement taken
        by a different instrument."""
        from dataclasses import replace

        from elt_taskgen.review import metrology as metrology_mod

        ws = self.fresh_workspace()
        solver = self.solver()
        cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=ROSTER, variants=(V.TRANSFORM,)
        )
        baseline = len(solver.calls)
        same = cal.roster_fingerprint(ROSTER)
        self.assertIsNotNone(
            cal.load_cached_record(ws, self.task, V.TRANSFORM, same)
        )
        # tier order is not identity
        self.assertEqual(cal.roster_fingerprint((STRONG, WEAK)), same)
        # the model-level identity alone is NOT enough to hit the cache
        self.assertIsNone(
            cal.load_cached_record(
                ws,
                self.task,
                V.TRANSFORM,
                solver_roster_fingerprint(t.model_key for t in ROSTER),
            )
        )

        perturbed = {
            "k": (replace(WEAK, k=WEAK.k + 1), STRONG),
            "effort": (replace(WEAK, effort="high"), STRONG),
            "max_tokens": (replace(WEAK, max_tokens=WEAK.max_tokens * 2), STRONG),
            "endpoint": (replace(WEAK, endpoint="https://gateway.example.test"), STRONG),
            "provider": (replace(WEAK, provider="openai_compat"), STRONG),
            "model": (replace(WEAK, model="weak-tier-v2"), STRONG),
        }
        seen = {same}
        for changed, roster in perturbed.items():
            with self.subTest(changed=changed):
                fingerprint = cal.roster_fingerprint(roster)
                self.assertNotIn(fingerprint, seen)
                seen.add(fingerprint)
                self.assertIsNone(
                    cal.load_cached_record(ws, self.task, V.TRANSFORM, fingerprint)
                )
        # an edited instrument (prompt / parser / scorer) ...
        with mock.patch.object(
            cal, "INSTRUMENT_VERSION", cal.INSTRUMENT_VERSION + "-edited"
        ):
            edited = cal.roster_fingerprint(ROSTER)
        self.assertNotIn(edited, seen)
        self.assertIsNone(cal.load_cached_record(ws, self.task, V.TRANSFORM, edited))
        # ... and a different harness version are different instruments too
        with mock.patch.object(
            metrology_mod, "HARNESS_VERSION", "not-the-shipped-harness"
        ):
            other_harness = cal.roster_fingerprint(ROSTER)
        self.assertNotIn(other_harness, seen | {edited})
        self.assertIsNone(
            cal.load_cached_record(ws, self.task, V.TRANSFORM, other_harness)
        )

        # end to end: a changed k RE-MEASURES (solver calls spent, nothing
        # served from cache) and the new record binds to the new identity.
        changed_k = perturbed["k"]
        result = cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=changed_k, variants=(V.TRANSFORM,)
        )
        self.assertEqual(result.from_cache, ())
        self.assertEqual(len(solver.calls), baseline + (WEAK.k + 1) + STRONG.k)
        self.assertEqual(
            result.records[V.TRANSFORM.value].roster_fingerprint,
            cal.roster_fingerprint(changed_k),
        )
        self.assertEqual(result.roster_fingerprint, cal.roster_fingerprint(changed_k))


class CalibrationEvidenceConsistencyTests(unittest.TestCase):
    """Raw attempts are the evidence; summaries and cache hits reproduce them."""

    def setUp(self) -> None:
        self.task = demo_fixture.demo_task()
        self.tier = cal.SolverTier(
            model_key="fixture:solver",
            provider="anthropic",
            model="solver",
            k=2,
        )
        self.roster = (self.tier,)

    def attempt(
        self,
        index: int,
        *,
        success: bool,
        failed_stage: str = "",
    ) -> cal.AttemptRecord:
        rewards = (
            {population.value: 1.0 for population in cal.GRADED_POPULATIONS}
            if success
            else {cal.GRADED_POPULATIONS[0].value: 0.0}
        )
        return cal.AttemptRecord(
            model_key=self.tier.model_key,
            attempt_index=index,
            prompt_sha256=sha256_hex(f"attempt:{index}"),
            success=success,
            rewards=rewards,
            failed_stage=failed_stage,
        )

    def record(
        self, variant: TaskVariant = V.EXTRACT_LOAD
    ) -> cal.CalibrationRecord:
        failed_stage = cal.STAGE1 if variant is V.EXTRACT_LOAD else cal.STAGE2
        attempts = (
            self.attempt(0, success=True),
            self.attempt(1, success=False, failed_stage=failed_stage),
        )
        tier_result = SolverTierResult(
            model_key=self.tier.model_key,
            k=self.tier.k,
            successes=1,
            stage1_failures=1 if failed_stage == cal.STAGE1 else 0,
            stage2_failures=1 if failed_stage == cal.STAGE2 else 0,
        )
        calibration = VariantCalibration(variant=variant, tiers=(tier_result,))
        return cal.CalibrationRecord(
            task_id=self.task.task_id,
            task_content_hash=self.task.content_hash(),
            variant=variant,
            roster_fingerprint=cal.roster_fingerprint(self.roster),
            calibration=calibration,
            attempts=attempts,
            flags=(),
        )

    def test_attempt_success_must_reproduce_from_graded_rewards(self) -> None:
        full_rewards = {
            population.value: 1.0 for population in cal.GRADED_POPULATIONS
        }
        cases = (
            (True, {}, "claimed success without full rewards"),
            (False, full_rewards, "claimed failure with full rewards"),
        )
        for success, rewards, label in cases:
            with self.subTest(label=label), self.assertRaisesRegex(
                ValueError, "success does not reproduce"
            ):
                cal.AttemptRecord(
                    model_key=self.tier.model_key,
                    attempt_index=0,
                    prompt_sha256="0" * 64,
                    success=success,
                    rewards=rewards,
                )

    def test_raw_attempts_must_reproduce_tier_counts_and_histograms(self) -> None:
        record = self.record()
        tier = record.calibration.tiers[0]
        duplicate_index = record.attempts[1].model_copy(
            update={"attempt_index": 0}
        )
        reward_mismatch = record.attempts[0].model_copy(update={"success": False})
        cases = {
            "zero attempts": (
                record.model_copy(update={"attempts": ()}),
                "no raw attempts",
            ),
            "duplicate attempt index": (
                record.model_copy(
                    update={"attempts": (record.attempts[0], duplicate_index)}
                ),
                "attempt indices",
            ),
            "tier k mismatch": (
                record.model_copy(
                    update={
                        "calibration": record.calibration.model_copy(
                            update={"tiers": (tier.model_copy(update={"k": 3}),)}
                        )
                    }
                ),
                "configured roster",
            ),
            "success total mismatch": (
                record.model_copy(
                    update={
                        "calibration": record.calibration.model_copy(
                            update={
                                "tiers": (
                                    tier.model_copy(update={"successes": 0}),
                                )
                            }
                        ),
                        "flags": (cal.FLAG_IMPOSSIBLE,),
                    }
                ),
                "successes does not reproduce",
            ),
            "failure histogram mismatch": (
                record.model_copy(
                    update={
                        "calibration": record.calibration.model_copy(
                            update={
                                "tiers": (
                                    tier.model_copy(update={"stage1_failures": 0}),
                                )
                            }
                        )
                    }
                ),
                "stage1_failures does not reproduce",
            ),
            "reward success mismatch": (
                record.model_copy(
                    update={"attempts": (reward_mismatch, record.attempts[1])}
                ),
                "success does not reproduce",
            ),
        }
        for label, (forged, expected) in cases.items():
            with self.subTest(label=label):
                problem = cal.calibration_record_problem(forged, roster=self.roster)
                self.assertIsNotNone(problem)
                self.assertIn(expected, problem)

    def test_duplicate_variant_records_fail_closed(self) -> None:
        record = self.record()
        with self.assertRaisesRegex(ValueError, "duplicate calibration record"):
            cal.empirical_from_records(self.task, self.roster, (record, record))

    def test_cache_rechecks_raw_and_configured_attempt_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = self.record()
            path = cal.cache_path(workspace, self.task.task_id, record.variant)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(record.model_dump_json(), encoding="utf-8")
            self.assertIsNotNone(
                cal.load_cached_record(
                    workspace,
                    self.task,
                    record.variant,
                    record.roster_fingerprint,
                    roster=self.roster,
                )
            )

            wrong_k = cal.SolverTier(
                model_key=self.tier.model_key,
                provider=self.tier.provider,
                model=self.tier.model,
                k=3,
            )
            self.assertIsNone(
                cal.load_cached_record(
                    workspace,
                    self.task,
                    record.variant,
                    record.roster_fingerprint,
                    roster=(wrong_k,),
                )
            )

            tier = record.calibration.tiers[0]
            forged = record.model_copy(
                update={
                    "calibration": record.calibration.model_copy(
                        update={
                            "tiers": (
                                tier.model_copy(update={"stage1_failures": 0}),
                            )
                        }
                    )
                }
            )
            path.write_text(forged.model_dump_json(), encoding="utf-8")
            self.assertIsNone(
                cal.load_cached_record(
                    workspace,
                    self.task,
                    record.variant,
                    record.roster_fingerprint,
                    roster=self.roster,
                )
            )


class FlagRoutingTests(CalibrationTestCase):
    def test_zero_successes_route_to_feasibility_re_review(self) -> None:
        ws = self.fresh_workspace()
        solver = self.solver(weak=DEGENERATE, strong=DEGENERATE)
        result = cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            solver,
            roster=ROSTER,
            variants=(V.EXTRACT_LOAD,),
        )
        record = result.records[V.EXTRACT_LOAD.value]
        self.assertIn(cal.FLAG_IMPOSSIBLE, record.flags)
        self.assertEqual(result.impossible_variants, (V.EXTRACT_LOAD.value,))

        path = (
            cal.evidence_dir(ws, self.task.task_id)
            / cal.FEASIBILITY_FINDING_FILENAME
        )
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["task_content_hash"], self.task.content_hash())
        self.assertEqual(len(payload["findings"]), 1)
        finding = payload["findings"][0]
        self.assertEqual(finding["role"], "feasibility_reviewer")
        self.assertEqual(finding["route_hint"], "specification")
        self.assertIn("impossible", finding["summary"])

    def test_weakest_tier_acing_flags_trivial(self) -> None:
        ws = self.fresh_workspace()
        result = cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            self.solver(),
            roster=ROSTER,
            variants=(V.TRANSFORM,),
        )
        record = result.records[V.TRANSFORM.value]
        self.assertIn(cal.FLAG_TRIVIAL, record.flags)
        self.assertNotIn(cal.FLAG_IMPOSSIBLE, record.flags)
        self.assertEqual(result.trivial_variants, (V.TRANSFORM.value,))
        # no feasibility artifact for a solvable task
        self.assertFalse(
            (
                cal.evidence_dir(ws, self.task.task_id)
                / cal.FEASIBILITY_FINDING_FILENAME
            ).is_file()
        )


class KeyOptionalTests(CalibrationTestCase):
    def test_missing_credentials_is_a_visible_skip_not_a_number(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        class NoKeys:
            def complete(self, role, prompt):
                raise providers_mod.MissingCredentialsError(
                    "ANTHROPIC_API_KEY is not set"
                )

        ws = self.fresh_workspace()
        result = cal.calibrate_task(
            self.task, self.gold, ws, NoKeys(), roster=ROSTER
        )
        self.assertEqual(result.records, {})
        self.assertIsNone(result.empirical)
        self.assertIn("ANTHROPIC_API_KEY", result.skipped_reason)
        self.assertIn("skipped", result.skipped_reason)

    def test_replay_miss_is_a_visible_skip(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        class NoTranscripts:
            def complete(self, role, prompt):
                raise providers_mod.TranscriptMissingError("replay-only mode: no record")

        ws = self.fresh_workspace()
        result = cal.calibrate_task(
            self.task, self.gold, ws, NoTranscripts(), roster=ROSTER
        )
        self.assertIsNone(result.empirical)
        self.assertTrue(result.skipped_reason)

    def test_unparseable_response_is_an_unattributed_failure(self) -> None:
        class Garbage:
            def __init__(self):
                self.calls = 0

            def complete(self, role, prompt):
                self.calls += 1
                return "I refuse to answer in JSON."

        ws = self.fresh_workspace()
        solver = Garbage()
        result = cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            solver,
            roster=ROSTER,
            variants=(V.TRANSFORM,),
        )
        record = result.records[V.TRANSFORM.value]
        self.assertEqual(solver.calls, ATTEMPTS_PER_VARIANT)
        for tier_result in record.calibration.tiers:
            self.assertEqual(tier_result.successes, 0)
            self.assertEqual(tier_result.stage1_failures, 0)
            self.assertEqual(tier_result.stage2_failures, 0)  # unattributed
        self.assertTrue(all(a.error for a in record.attempts))


# ---------------------------------------------------------------------------
# HARNESS failure != SOLVER failure (never a measured zero, never cached)
# ---------------------------------------------------------------------------

class HarnessFailureTests(CalibrationTestCase):
    """When the INSTRUMENT cannot measure — a rendered population missing,
    frozen gold missing, the trusted loader broken, populations drifted from
    the frozen gold — the outcome is the same VISIBLE SKIP provider
    unavailability produces: no record, no cache entry, no 'empirically
    impossible' finding, and (by preflight) no solver call spent."""

    def _feasibility_file(self, ws: Path) -> Path:
        return cal.evidence_dir(ws, self.task.task_id) / cal.FEASIBILITY_FINDING_FILENAME

    def _assert_skipped(self, result, solver, ws: Path, variant: TaskVariant, *needles: str):
        for needle in ("harness", *needles):
            self.assertIn(needle, result.skipped_reason, result.skipped_reason)
        self.assertEqual(result.records, {})
        self.assertEqual(result.impossible_variants, ())
        self.assertEqual(result.trivial_variants, ())
        self.assertIsNone(result.empirical)
        self.assertEqual(len(getattr(solver, "calls", [])), 0)
        self.assertFalse(cal.cache_path(ws, self.task.task_id, variant).exists())
        self.assertFalse(self._feasibility_file(ws).is_file())

    def test_missing_rendered_population_is_a_skip_not_impossible(self) -> None:
        ws = self.fresh_workspace()
        rdir = runner_mod.rendered_dir(ws, self.task.task_id, P.STRESS)
        aside = rdir.parent / "rendered.aside"
        rdir.rename(aside)
        solver = self.solver()
        result = cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=ROSTER, variants=(V.TRANSFORM,)
        )
        self._assert_skipped(result, solver, ws, V.TRANSFORM, "stress")

        # restore -> measured normally, NOT served from a poisoned cache
        aside.rename(rdir)
        again = cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=ROSTER, variants=(V.TRANSFORM,)
        )
        self.assertEqual(again.skipped_reason, "")
        self.assertEqual(again.from_cache, ())
        self.assertEqual(len(solver.calls), ATTEMPTS_PER_VARIANT)
        vector = again.records[V.TRANSFORM.value].pass_rate_vector()
        self.assertTrue(all(rate == 1.0 for rate in vector.values()), vector)
        self.assertEqual(again.impossible_variants, ())

    def test_missing_rendered_population_extract_load_is_not_a_stage1_failure(self) -> None:
        ws = self.fresh_workspace()
        rdir = runner_mod.rendered_dir(ws, self.task.task_id, P.STRESS)
        aside = rdir.parent / "rendered.aside"
        rdir.rename(aside)
        solver = self.solver()
        result = cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=ROSTER, variants=(V.EXTRACT_LOAD,)
        )
        self._assert_skipped(result, solver, ws, V.EXTRACT_LOAD, "stress")

        aside.rename(rdir)
        again = cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=ROSTER, variants=(V.EXTRACT_LOAD,)
        )
        self.assertEqual(again.skipped_reason, "")
        record = again.records[V.EXTRACT_LOAD.value]
        for tier_result in record.calibration.tiers:
            self.assertEqual(tier_result.stage1_failures, 0)
            self.assertEqual(tier_result.successes, tier_result.k)

    def test_trusted_loader_failure_is_harness_not_solver(self) -> None:
        ws = self.fresh_workspace()
        solver = self.solver()

        def boom(task, rendered_dir, con):
            raise RuntimeError("loader exploded")

        with mock.patch.object(cal.solution_mod, "load_sources_duckdb", boom):
            result = cal.calibrate_task(
                self.task, self.gold, ws, solver, roster=ROSTER, variants=(V.TRANSFORM,)
            )
        self._assert_skipped(result, solver, ws, V.TRANSFORM, "loader exploded")

    def test_trusted_loader_failure_mid_attempt_is_still_harness(self) -> None:
        """The belt: preflight passes, then the trusted load fails inside a
        transform attempt -> CalibrationHarnessError propagates, nothing is
        recorded or cached, and it is a skip (not a measured zero)."""
        ws = self.fresh_workspace()
        solver = self.solver()
        real = cal.solution_mod.load_sources_duckdb
        calls = {"n": 0}
        preflight_loads = len(list(P))

        def flaky(task, rendered_dir, con):
            calls["n"] += 1
            if calls["n"] > preflight_loads:
                raise RuntimeError("loader exploded later")
            return real(task, rendered_dir, con)

        with mock.patch.object(cal.solution_mod, "load_sources_duckdb", flaky):
            with self.assertRaises(cal.CalibrationHarnessError):
                cal.calibrate_variant(
                    self.task, self.gold, ws, solver, ROSTER, V.TRANSFORM
                )
            self.assertEqual(len(solver.calls), 1)  # one solver spent, then stop
            self.assertFalse(
                cal.cache_path(ws, self.task.task_id, V.TRANSFORM).exists()
            )
            solver2 = self.solver()
            result = cal.calibrate_task(
                self.task, self.gold, ws, solver2, roster=ROSTER, variants=(V.TRANSFORM,)
            )
        self.assertIn("harness", result.skipped_reason)
        self.assertEqual(result.records, {})
        self.assertEqual(result.impossible_variants, ())

    def test_missing_gold_population_is_a_skip(self) -> None:
        ws = self.fresh_workspace()
        partial = self.gold.model_copy(
            update={
                "stage1": {k: v for k, v in self.gold.stage1.items() if k != P.STRESS.value},
                "stage2_csv": {
                    k: v for k, v in self.gold.stage2_csv.items() if k != P.STRESS.value
                },
            }
        )
        for variant in (V.EXTRACT_LOAD, V.TRANSFORM):
            solver = self.solver()
            result = cal.calibrate_task(
                self.task, partial, ws, solver, roster=ROSTER, variants=(variant,)
            )
            self._assert_skipped(result, solver, ws, variant, "stress", "gold")

    def test_evaluator_without_gold_is_harness_inside_score_attempt(self) -> None:
        """The belt on the evaluator side: a RewardResult carrying the
        'no frozen ... gold' evidence is a harness error, never a solver zero."""
        ws = self.fresh_workspace()
        partial = self.gold.model_copy(
            update={"stage1": {k: v for k, v in self.gold.stage1.items() if k != P.PRIMARY.value}}
        )
        submission = cal.parse_submission(
            self.task, V.EXTRACT_LOAD, json.dumps({"load_plan": self.plan})
        )
        with self.assertRaises(cal.CalibrationHarnessError):
            cal._score_attempt(self.task, partial, V.EXTRACT_LOAD, submission, ws)

    def test_stale_population_vs_frozen_gold_is_harness(self) -> None:
        ws = self.fresh_workspace()
        rdir = runner_mod.rendered_dir(ws, self.task.task_id, P.STRESS)
        artifact = rdir / "mongodb" / "orders.jsonl"
        lines = artifact.read_text(encoding="utf-8").splitlines()
        artifact.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        for variant in (V.EXTRACT_LOAD, V.TRANSFORM):
            solver = self.solver()
            result = cal.calibrate_task(
                self.task, self.gold, ws, solver, roster=ROSTER, variants=(variant,)
            )
            self._assert_skipped(result, solver, ws, variant, "stress", "frozen stage-1 gold")

    def test_earlier_measured_variants_still_assemble_on_a_later_harness_skip(self) -> None:
        ws = self.fresh_workspace()
        solver = self.solver()
        cal.calibrate_task(
            self.task, self.gold, ws, solver, roster=ROSTER, variants=(V.TRANSFORM,)
        )
        rdir = runner_mod.rendered_dir(ws, self.task.task_id, P.DEVELOPMENT)
        shutil.rmtree(rdir)
        result = cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            solver,
            roster=ROSTER,
            variants=(V.TRANSFORM, V.EXTRACT_LOAD),
        )
        self.assertEqual(result.from_cache, (V.TRANSFORM.value,))
        self.assertEqual(tuple(result.records), (V.TRANSFORM.value,))
        self.assertIn("harness", result.skipped_reason)
        self.assertIn(V.EXTRACT_LOAD.value, result.skipped_reason)
        self.assertIsNotNone(result.empirical)
        self.assertEqual(set(result.empirical.variants), {V.TRANSFORM})

    def test_solver_side_failures_still_score_zero_and_attribute(self) -> None:
        """Regression guard: the harness split must not soften SOLVER scoring
        — a broken load plan is k stage-1 failures, degenerate SQL k stage-2."""
        ws = self.fresh_workspace()
        result = cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            self.solver(weak=DEGENERATE, strong=DEGENERATE),
            roster=ROSTER,
        )
        self.assertEqual(result.skipped_reason, "")
        el = result.records[V.EXTRACT_LOAD.value]
        for t in el.calibration.tiers:
            self.assertEqual(t.successes, 0)
            self.assertEqual(t.stage1_failures, t.k)
            self.assertEqual(t.stage2_failures, 0)
        tr = result.records[V.TRANSFORM.value]
        for t in tr.calibration.tiers:
            self.assertEqual(t.successes, 0)
            self.assertEqual(t.stage1_failures, 0)
            self.assertEqual(t.stage2_failures, t.k)
        self.assertEqual(
            result.impossible_variants, (V.EXTRACT_LOAD.value, V.TRANSFORM.value)
        )
        self.assertTrue(self._feasibility_file(ws).is_file())


# ---------------------------------------------------------------------------
# Untrusted solver SQL runs SANDBOXED (no file reads, no host writes)
# ---------------------------------------------------------------------------

class SandboxTests(CalibrationTestCase):
    def _score_sql(self, ws: Path, sql: str):
        submission = cal.SolverSubmission(
            variant=V.TRANSFORM, sql_by_mart={MART_NAME: sql}
        )
        return cal._score_attempt(self.task, self.gold, V.TRANSFORM, submission, ws)

    def test_calibration_solver_sql_cannot_read_files(self) -> None:
        ws = self.fresh_workspace()
        gold_csv = ws / "tasks" / self.task.task_id / "answer_key" / "gold" / "primary" / f"{MART_NAME}.csv"
        self.assertTrue(gold_csv.is_file())
        for sql in (
            "SELECT * FROM read_csv_auto('leak.csv')",
            f"SELECT * FROM read_csv_auto('{gold_csv.as_posix()}', header=true)",
            "SELECT * FROM read_csv_auto('**/answer_key/gold/**/*.csv', header=true)",
        ):
            rewards, failed_stage, error = self._score_sql(ws, sql)
            for pop in P:
                if pop in rewards:
                    self.assertEqual(rewards[pop.value], 0.0, sql)
            self.assertTrue(any(rewards.get(p.value) == 0.0 for p in cal.GRADED_POPULATIONS))
            self.assertIn("PermissionException", error)
            self.assertEqual(failed_stage, cal.STAGE2)

    def test_calibration_solver_sql_cannot_write_files(self) -> None:
        ws = self.fresh_workspace()
        target = ws / "pwn.csv"
        cwd = Path.cwd()
        rewards, _stage, error = self._score_sql(
            ws, f"COPY (SELECT 1 AS x) TO '{target.as_posix()}'"
        )
        self.assertFalse(target.exists())
        self.assertFalse((cwd / "pwn.csv").exists())
        self.assertIn("PermissionException", error)
        self.assertTrue(all(r == 0.0 for r in rewards.values()))
        rewards, _stage, error = self._score_sql(
            ws, "COPY (SELECT 1 AS x) TO 'pwn.csv'"
        )
        self.assertFalse((cwd / "pwn.csv").exists())
        self.assertIn("PermissionException", error)

    def test_rest_index_cannot_escape_its_fixture_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "rest" / "events"
            root.mkdir(parents=True)
            outside = parent / "private.json"
            outside.write_text('{"data": [{"secret": "no"}]}', encoding="utf-8")
            (root / "index.json").write_text(
                json.dumps({"pages": ["../../private.json"]}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "escapes"):
                solution_mod._read_rest(root)

    def test_s3_prefix_cannot_follow_a_nested_object_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "s3" / "events"
            root.mkdir(parents=True)
            outside = parent / "private.jsonl"
            outside.write_text('{"secret": "no"}\n', encoding="utf-8")
            (root / "part-0001.jsonl").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "escapes"):
                solution_mod._read_s3(root)

    def test_calibration_solver_sql_cannot_unlock_the_sandbox(self) -> None:
        ws = self.fresh_workspace()
        rewards, _stage, error = self._score_sql(
            ws, "SET enable_external_access=true"
        )
        self.assertTrue(all(r == 0.0 for r in rewards.values()))
        self.assertIn("InvalidInputException", error)

    def test_calibration_reference_transform_still_scores_one(self) -> None:
        ws = self.fresh_workspace()
        rewards, failed_stage, error = self._score_sql(ws, REFERENCE_SQL)
        self.assertEqual(error, "")
        self.assertEqual(failed_stage, "")
        for pop in P:
            self.assertEqual(rewards[pop.value], 1.0, pop)


# ---------------------------------------------------------------------------
# cached_calibration: what is ALREADY measured at this identity, no calls
# ---------------------------------------------------------------------------

class CachedCalibrationTests(CalibrationTestCase):
    def _roster_config(self, keys: tuple[str, ...] = ("weak-tier",)) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "agents.yaml"
        entries = "".join(
            f"    - model_key: anthropic:{key}\n      provider: anthropic\n"
            f"      model: {key}\n      k: 2\n"
            for key in keys
        )
        path.write_text("calibration:\n  roster:\n" + entries, encoding="utf-8")
        return path

    def _tree_snapshot(self, ws: Path) -> dict[str, float]:
        return {
            str(p): p.stat().st_mtime_ns for p in sorted(ws.rglob("*")) if p.is_file()
        }

    def test_without_cache_is_empty_and_writes_nothing(self) -> None:
        ws = self.fresh_workspace()
        before = self._tree_snapshot(ws)
        result = cal.cached_calibration(
            self.task, ws, agents_config=self._roster_config()
        )
        self.assertEqual(result.records, {})
        self.assertEqual(result.from_cache, ())
        self.assertEqual(result.skipped_reason, "")
        self.assertIsNone(result.empirical)
        self.assertEqual(result.impossible_variants, ())
        self.assertEqual(result.trivial_variants, ())
        self.assertEqual(result.task_content_hash, self.task.content_hash())
        self.assertEqual(self._tree_snapshot(ws), before)

    def test_cached_impossible_evidence_is_sticky_and_costs_no_calls(self) -> None:
        ws = self.fresh_workspace()
        config = self._roster_config()
        roster = cal.load_calibration_roster(config)
        solver = FixtureSolver(
            {t.role_name: DEGENERATE for t in roster}, self.plan
        )
        measured = cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            solver,
            roster=roster,
            variants=(V.EXTRACT_LOAD,),
        )
        self.assertEqual(measured.impossible_variants, (V.EXTRACT_LOAD.value,))
        calls = len(solver.calls)
        before = self._tree_snapshot(ws)

        cached = cal.cached_calibration(self.task, ws, agents_config=config)
        self.assertEqual(len(solver.calls), calls)  # zero provider calls
        self.assertEqual(self._tree_snapshot(ws), before)  # zero writes
        self.assertEqual(cached.from_cache, (V.EXTRACT_LOAD.value,))
        self.assertEqual(tuple(cached.records), (V.EXTRACT_LOAD.value,))
        self.assertEqual(cached.impossible_variants, (V.EXTRACT_LOAD.value,))
        self.assertEqual(cached.roster_fingerprint, cal.roster_fingerprint(roster))
        self.assertEqual(cached.skipped_reason, "")
        self.assertIsNotNone(cached.empirical)
        self.assertEqual(cached.empirical, measured.empirical)
        # a trivial (all-correct) record surfaces the same way
        cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            FixtureSolver({}, self.plan),
            roster=roster,
            variants=(V.TRANSFORM,),
        )
        both = cal.cached_calibration(self.task, ws, agents_config=config)
        self.assertEqual(both.from_cache, (V.EXTRACT_LOAD.value, V.TRANSFORM.value))
        self.assertEqual(both.trivial_variants, (V.TRANSFORM.value,))
        self.assertEqual(both.impossible_variants, (V.EXTRACT_LOAD.value,))
        # variants can be narrowed
        only_t = cal.cached_calibration(
            self.task, ws, variants=(V.TRANSFORM,), agents_config=config
        )
        self.assertEqual(tuple(only_t.records), (V.TRANSFORM.value,))
        self.assertEqual(only_t.impossible_variants, ())

    def test_other_roster_or_stale_hash_is_a_miss(self) -> None:
        ws = self.fresh_workspace()
        config = self._roster_config()
        roster = cal.load_calibration_roster(config)
        cal.calibrate_task(
            self.task,
            self.gold,
            ws,
            FixtureSolver({t.role_name: DEGENERATE for t in roster}, self.plan),
            roster=roster,
            variants=(V.EXTRACT_LOAD,),
        )
        other = cal.cached_calibration(
            self.task, ws, agents_config=self._roster_config(("weak-tier", "other-tier"))
        )
        self.assertEqual(other.records, {})
        self.assertEqual(other.impossible_variants, ())
        moved = self.task.model_copy(update={"title": "a different title"})
        stale = cal.cached_calibration(moved, ws, agents_config=config)
        self.assertEqual(stale.records, {})
        self.assertEqual(stale.task_content_hash, moved.content_hash())

    def test_malformed_roster_is_a_miss_not_a_crash(self) -> None:
        ws = self.fresh_workspace()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bad = Path(tmp.name) / "agents.yaml"
        bad.write_text(
            "calibration:\n  roster:\n    - provider: nope\n      model: m\n      k: 2\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            cal.load_calibration_roster(bad)
        result = cal.cached_calibration(self.task, ws, agents_config=bad)
        self.assertEqual(result.records, {})
        self.assertEqual(result.roster_fingerprint, "")
        self.assertEqual(result.skipped_reason, "")


# ---------------------------------------------------------------------------
# Selection: band FILTERING (combined_score stays structural)
# ---------------------------------------------------------------------------

def _make_task(n: int, *, status: TaskStatus = TaskStatus.ACCEPTED) -> TaskIR:
    return TaskIR(
        task_id=f"synthetic__c{n:04d}",
        family_id=f"pool__fam{n:04d}",
        cluster_id=f"cluster{n:04d}",
        origin=Origin.SYNTHETIC,
        license="MIT",
        tables=(
            TableSpec(
                name="t",
                columns=(ColumnSpec(name="id", type=ColumnType.INTEGER),),
                primary_key=("id",),
            ),
        ),
        backends=(BackendAssignment(table="t", backend=Backend.POSTGRES),),
        marts=(
            MartSpec(
                name="m",
                grain="one row per id",
                key_columns=("id",),
                columns=(
                    MartColumn(name="id", type=ColumnType.INTEGER, description="key"),
                ),
                plan=MartPlan(
                    mart="m",
                    ops=(
                        MartOp(
                            kind=MartOpKind.SOURCE,
                            description="bring t in",
                            tables=("t",),
                        ),
                    ),
                ),
            ),
        ),
        status=status,
    )


def _empirical(pass_rates: dict[str, float], *, content_hash: str) -> EmpiricalDifficulty:
    """Matching per-unit calibrations for the active EL/T pair."""
    calibrations: dict[TaskVariant, VariantCalibration] = {}
    successes = 0
    total = 0
    stage1_failures = 0
    stage2_failures = 0
    for variant in RLVR_TASK_VARIANTS:
        tiers = tuple(
            SolverTierResult(
                model_key=key,
                k=4,
                successes=int(round(rate * 4)),
                stage1_failures=(
                    4 - int(round(rate * 4))
                    if variant is V.EXTRACT_LOAD
                    else 0
                ),
                stage2_failures=(
                    4 - int(round(rate * 4))
                    if variant is V.TRANSFORM
                    else 0
                ),
            )
            for key, rate in sorted(pass_rates.items())
        )
        calibrations[variant] = VariantCalibration(variant=variant, tiers=tiers)
        successes += sum(t.successes for t in tiers)
        total += sum(t.k for t in tiers)
        stage1_failures += sum(t.stage1_failures for t in tiers)
        stage2_failures += sum(t.stage2_failures for t in tiers)
    failed = total - successes
    return EmpiricalDifficulty(
        solver_config="fixture roster",
        n_attempts=total,
        success_rate=successes / total,
        stage1_failure_rate=stage1_failures / failed if failed else 0.0,
        stage2_failure_rate=stage2_failures / failed if failed else 0.0,
        variants=calibrations,
        roster_fingerprint=solver_roster_fingerprint(pass_rates),
        campaign_fingerprint="1" * 64,
        measured_at_content_hash=content_hash,
    )


def _accepted_units(*tasks: TaskIR) -> dict[str, frozenset[str]]:
    accepted = frozenset(variant.value for variant in RLVR_TASK_VARIANTS)
    return {task.task_id: accepted for task in tasks}


class SelectionFilteringTests(unittest.TestCase):
    def _measured(self, task: TaskIR, rates: dict[str, float] | None):
        m = structural_difficulty(task)
        if rates is None:
            return m
        return with_empirical(
            m, _empirical(rates, content_hash=task.content_hash())
        )

    def test_required_empirical_measurement_must_match_active_campaign(self) -> None:
        task = _make_task(99)
        measurement = self._measured(task, {"a": 0.25, "b": 0.5})
        stale = selection_mod.select(
            [task],
            {task.task_id: measurement},
            [],
            selection_mod.Quotas(size=1),
            accepted_variants=_accepted_units(task),
            require_empirical=True,
            expected_campaign_fingerprint="2" * 64,
        )
        self.assertEqual(stale.train, ())
        self.assertIn("stale for the active campaign", stale.rejected[task.task_id])

        current = selection_mod.select(
            [task],
            {task.task_id: measurement},
            [],
            selection_mod.Quotas(size=1),
            accepted_variants=_accepted_units(task),
            require_empirical=True,
            expected_campaign_fingerprint="1" * 64,
        )
        self.assertEqual(current.train, (task.task_id,))

        no_active_identity = selection_mod.select(
            [task],
            {task.task_id: measurement},
            [],
            selection_mod.Quotas(size=1),
            accepted_variants=_accepted_units(task),
            require_empirical=True,
        )
        self.assertIn(
            "active empirical campaign fingerprint was not supplied",
            no_active_identity.rejected[task.task_id],
        )

    def test_impossible_task_leaves_the_eligible_pool(self) -> None:
        ok = _make_task(1)
        impossible = _make_task(2)
        measurements = {
            ok.task_id: self._measured(ok, {"a": 0.5, "b": 0.75}),
            impossible.task_id: self._measured(impossible, {"a": 0.0, "b": 0.0}),
        }
        result = selection_mod.select(
            [ok, impossible],
            measurements,
            [],
            selection_mod.Quotas(size=10),
            accepted_variants=_accepted_units(ok, impossible),
        )
        self.assertEqual(set(result.train) | set(result.val), {ok.task_id})
        self.assertIn("empirically impossible", result.rejected[impossible.task_id])

    def test_trivial_task_leaves_the_eligible_pool(self) -> None:
        ok = _make_task(3)
        trivial = _make_task(4)
        measurements = {
            ok.task_id: self._measured(ok, {"a": 0.25, "b": 0.5}),
            trivial.task_id: self._measured(trivial, {"a": 1.0, "b": 1.0}),
        }
        result = selection_mod.select(
            [ok, trivial],
            measurements,
            [],
            selection_mod.Quotas(size=10),
            accepted_variants=_accepted_units(ok, trivial),
        )
        self.assertEqual(set(result.train) | set(result.val), {ok.task_id})
        self.assertIn("empirically trivial", result.rejected[trivial.task_id])

    def test_one_definition_of_trivial(self) -> None:
        """Roadmap Phase 0.F: calibration's FLAG_TRIVIAL and selection's
        'empirically trivial' exclusion are ONE predicate (every pinned tier
        aced k/k), so a variant flagged by calibration is exactly the variant
        selection excludes — never 'weakest tier aced' on one side only."""
        import inspect

        self.assertIs(cal.variant_is_trivial, selection_mod.variant_is_trivial)
        self.assertIs(cal.variant_is_impossible, selection_mod.variant_is_impossible)
        self.assertIn("variant_is_trivial(", inspect.getsource(cal._flags))
        self.assertIn(
            "variant_is_trivial(",
            inspect.getsource(selection_mod.variant_empirical_exclusions),
        )
        # no second, private re-derivation on either side
        for source in (inspect.getsource(cal._flags),
                       inspect.getsource(selection_mod.variant_empirical_exclusions)):
            self.assertNotIn("== 1.0", source)
            self.assertNotIn(".rank", source)

        task = _make_task(9)
        cases = {
            "every tier aced": ({"a": 1.0, "b": 1.0}, True),
            "weakest aced, strongest missed once": ({"a": 1.0, "b": 0.75}, False),
            "strongest aced, weakest missed": ({"a": 0.5, "b": 1.0}, False),
            "impossible": ({"a": 0.0, "b": 0.0}, False),
        }
        for label, (rates, trivial) in cases.items():
            with self.subTest(label):
                m = self._measured(task, rates)
                excluded = selection_mod.variant_empirical_exclusions(m)
                for variant, calibration in m.empirical.variants.items():
                    value = TaskVariant(variant).value
                    flagged = cal.FLAG_TRIVIAL in cal._flags(calibration)
                    self.assertEqual(flagged, trivial, label)
                    self.assertEqual(
                        selection_mod.variant_is_trivial(calibration), trivial, label
                    )
                    self.assertEqual(
                        "empirically trivial" in excluded.get(value, ""), trivial, label
                    )

    def test_legacy_weakest_tier_trivial_flag_is_retired(self) -> None:
        """DELIBERATE DEVIATION, recorded (Phase 0.F): before 0.F calibration
        flagged FLAG_TRIVIAL when the WEAKEST tier (rank 0) aced k/k, so a
        variant a stronger tier missed once was flagged trivial by
        calibration yet kept by selection. The one definition is now
        selection's ('every pinned tier aced'); the legacy predicate is
        pinned here as retired, and INSTRUMENT_VERSION forces every cached
        record to re-measure under it."""
        task = _make_task(11)
        m = self._measured(task, {"a": 1.0, "b": 0.75})  # weakest aced, strongest missed once
        for variant, calibration in m.empirical.variants.items():
            weakest = calibration.tiers[0]
            legacy_flag = weakest.successes == weakest.k          # pre-0.F predicate
            self.assertTrue(legacy_flag, variant)
            self.assertNotIn(cal.FLAG_TRIVIAL, cal._flags(calibration), variant)
            self.assertFalse(selection_mod.variant_is_trivial(calibration), variant)
        every = self._measured(task, {"a": 1.0, "b": 1.0})
        for calibration in every.empirical.variants.values():
            self.assertIn(cal.FLAG_TRIVIAL, cal._flags(calibration))
        self.assertEqual(cal.INSTRUMENT_VERSION, "1")

    def test_bands_consult_measured_rates_but_scores_stay_structural(self) -> None:
        task = _make_task(5)
        structural = structural_difficulty(task)
        structural_band = selection_mod.band_of(structural.combined_score())
        self.assertEqual(selection_mod.band_for(structural), structural_band)

        hard = self._measured(task, {"a": 0.25, "b": 0.25})
        easy = self._measured(task, {"a": 0.75, "b": 1.0})
        self.assertEqual(selection_mod.band_for(hard), "hard")
        self.assertEqual(selection_mod.band_for(easy), "easy")
        # the blend itself never moves
        self.assertEqual(hard.combined_score(), structural.combined_score())
        self.assertEqual(easy.combined_score(), structural.combined_score())
        self.assertAlmostEqual(selection_mod.empirical_pass_rate(hard), 0.25)
        self.assertIsNone(selection_mod.empirical_pass_rate(structural))

    def test_legacy_empirical_without_variants_changes_nothing(self) -> None:
        task = _make_task(6)
        legacy = with_empirical(
            structural_difficulty(task),
            EmpiricalDifficulty(
                solver_config="baseline-v1",
                n_attempts=8,
                success_rate=0.0,
                stage1_failure_rate=1.0,
                stage2_failure_rate=0.0,
            ),
        )
        self.assertIsNone(selection_mod.empirical_exclusion(legacy))
        self.assertEqual(
            selection_mod.band_for(legacy),
            selection_mod.band_of(legacy.combined_score()),
        )
        result = selection_mod.select(
            [task],
            {task.task_id: legacy},
            [],
            selection_mod.Quotas(size=3),
            accepted_variants=_accepted_units(task),
        )
        self.assertEqual(set(result.train) | set(result.val), {task.task_id})


# ---------------------------------------------------------------------------
# CLI stage 9 wiring
# ---------------------------------------------------------------------------

class CliCalibrateStageTests(CalibrationTestCase):
    """cli.run_calibrate (structural default) and make_calibrate_runner
    (--empirical): the evidence file stage 11 reads must carry the empirical
    record, and the ledger payload must show what calibration did."""

    def _engine(self):
        from elt_taskgen.engine import Engine

        engine = Engine(self.fresh_workspace())
        self.addCleanup(engine.close)
        engine.register(self.task)
        return engine

    def _one_tier_config(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "agents.yaml"
        path.write_text(
            "calibration:\n  roster:\n"
            "    - model_key: anthropic:weak-tier\n"
            "      provider: anthropic\n      model: weak-tier\n      k: 1\n",
            encoding="utf-8",
        )
        return path

    def test_structural_default_records_no_empirical(self) -> None:
        from elt_taskgen import cli

        engine = self._engine()
        outcome = cli.run_calibrate(engine, self.task)
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(outcome.payload.pass_rates, {})
        self.assertEqual(outcome.payload.roster_fingerprint, "")
        self.assertIsNone(outcome.payload.measurement.empirical)
        written = json.loads(
            (
                engine.task_dir(self.task.task_id) / "reports" / "difficulty.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIsNone(written["empirical"])

    def test_empirical_runner_fills_the_measurement(self) -> None:
        from elt_taskgen import cli

        engine = self._engine()
        config = self._one_tier_config()
        runner = cli.make_calibrate_runner(
            self.solver(),
            empirical=True,
            variants=(V.TRANSFORM,),
            agents_config=config,
        )
        outcome = runner(engine, self.task)
        self.assertEqual(outcome.verdict, "pass")
        payload = outcome.payload
        self.assertEqual(payload.skipped_reason, "")
        self.assertEqual(
            payload.pass_rates, {V.TRANSFORM.value: {"anthropic:weak-tier": 1.0}}
        )
        self.assertEqual(payload.trivial_variants, (V.TRANSFORM.value,))
        empirical = payload.measurement.empirical
        self.assertIsNotNone(empirical)
        self.assertEqual(
            empirical.measured_at_content_hash, self.task.content_hash()
        )
        written = json.loads(
            (
                engine.task_dir(self.task.task_id) / "reports" / "difficulty.json"
            ).read_text(encoding="utf-8")
        )
        # The ledger payload names the campaign (cache-binding) fingerprint; the
        # difficulty evidence carries the model-level roster identity (0.F).
        self.assertEqual(
            payload.roster_fingerprint,
            cal.roster_fingerprint(cal.load_calibration_roster(config)),
        )
        self.assertEqual(
            written["empirical"]["roster_fingerprint"],
            solver_roster_fingerprint(["anthropic:weak-tier"]),
        )
        self.assertEqual(
            written["empirical"]["campaign_fingerprint"],
            cal.roster_fingerprint(cal.load_calibration_roster(config)),
        )

    def test_impossible_variant_fails_the_stage_to_specification(self) -> None:
        from elt_taskgen import cli
        from elt_taskgen.models import RepairRoute

        engine = self._engine()
        runner = cli.make_calibrate_runner(
            self.solver(weak=DEGENERATE, strong=DEGENERATE),
            empirical=True,
            variants=(V.EXTRACT_LOAD,),
            agents_config=self._one_tier_config(),
        )
        outcome = runner(engine, self.task)
        self.assertEqual(outcome.verdict, "fail")
        self.assertIs(outcome.route, RepairRoute.SPECIFICATION)
        self.assertEqual(
            outcome.payload.impossible_variants, (V.EXTRACT_LOAD.value,)
        )
        self.assertIn("IMPOSSIBLE", outcome.payload.detail)
        self.assertTrue(
            (
                cal.evidence_dir(engine.workspace, self.task.task_id)
                / cal.FEASIBILITY_FINDING_FILENAME
            ).is_file()
        )

    def test_harness_failure_is_visible_skip_not_specification(self) -> None:
        from elt_taskgen import cli

        engine = self._engine()
        rdir = runner_mod.rendered_dir(engine.workspace, self.task.task_id, P.STRESS)
        shutil.rmtree(rdir)
        solver = self.solver()
        runner = cli.make_calibrate_runner(
            solver,
            empirical=True,
            variants=(V.TRANSFORM,),
            agents_config=self._one_tier_config(),
        )
        outcome = runner(engine, self.task)
        self.assertEqual(outcome.verdict, "pass")  # structural stands alone
        self.assertIsNone(outcome.route)
        self.assertEqual(outcome.payload.impossible_variants, ())
        self.assertIn("harness", outcome.payload.skipped_reason)
        self.assertIn("stress", outcome.payload.skipped_reason)
        self.assertIn("skipped", outcome.payload.detail)
        self.assertEqual(outcome.payload.pass_rates, {})
        self.assertIsNone(outcome.payload.measurement.empirical)
        self.assertEqual(solver.calls, [])
        self.assertFalse(
            (
                cal.evidence_dir(engine.workspace, self.task.task_id)
                / cal.FEASIBILITY_FINDING_FILENAME
            ).is_file()
        )
        self.assertFalse(
            cal.cache_path(engine.workspace, self.task.task_id, V.TRANSFORM).exists()
        )

    def test_empirical_runner_without_keys_is_a_visible_skip(self) -> None:
        from elt_taskgen import cli
        from elt_taskgen.review import providers as providers_mod

        class NoKeys:
            def complete(self, role, prompt):
                raise providers_mod.MissingCredentialsError("no ANTHROPIC_API_KEY")

        engine = self._engine()
        runner = cli.make_calibrate_runner(
            NoKeys(), empirical=True, agents_config=self._one_tier_config()
        )
        outcome = runner(engine, self.task)
        self.assertEqual(outcome.verdict, "pass")  # structural stands alone
        self.assertIn("skipped", outcome.payload.skipped_reason)
        self.assertIn("skipped", outcome.payload.detail)
        self.assertEqual(outcome.payload.pass_rates, {})
        self.assertIsNone(outcome.payload.measurement.empirical)


# ---------------------------------------------------------------------------
# The recorded prompt_sha256 is the key the PROVIDER files the exchange under
# ---------------------------------------------------------------------------

class TranscriptKeyProvenanceTests(CalibrationTestCase):
    """Phase-0 carry-over 2 (roadmap Phase 1): an attempt's `prompt_sha256`
    must point at the exchange that produced it. A `RoutedProvider` keys
    over the agents document its routing was loaded from
    (`transcript_key_for`), so `_run_attempt` asks the provider — on the
    scored path and on the malformed-response path alike — and falls back to
    the module default only for a solver double without one."""

    def _custom_agents_config(self, root: Path) -> Path:
        import yaml

        from elt_taskgen.review import providers as providers_mod

        doc = json.loads(json.dumps(providers_mod._agents_doc()))
        doc["roles"][WEAK.role_name] = {
            "provider": WEAK.provider,
            "model": WEAK.model,
            "session": {"max_wall_s": 301},
        }
        path = root / "agents.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return path

    def test_attempt_prompt_sha_is_the_default_transcript_key_for_a_bare_double(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        ws = self.fresh_workspace()
        record = cal._run_attempt(self.task, self.gold, V.TRANSFORM, ws, self.solver(), WEAK, 0)
        self.assertTrue(record.success)
        self.assertEqual(
            record.prompt_sha256,
            providers_mod.transcript_key(WEAK.role_name, self.prompt(self.task, V.TRANSFORM, 0)),
        )

    def test_attempt_prompt_sha_follows_the_provider_transcript_key(self) -> None:
        from elt_taskgen.review import providers as providers_mod

        providers_mod.clear_behavior_caches()
        self.addCleanup(providers_mod.clear_behavior_caches)
        ws = self.fresh_workspace()
        custom = self._custom_agents_config(ws)
        prompt = self.prompt(self.task, V.TRANSFORM, 0)
        custom_key = providers_mod.transcript_key(WEAK.role_name, prompt, agents_config=custom)
        self.assertNotEqual(custom_key, providers_mod.transcript_key(WEAK.role_name, prompt))

        class KeyedSolver(FixtureSolver):
            """What a RoutedProvider on a custom --agents-config computes."""

            def transcript_key_for(self, role, prompt: str) -> str:
                role_name = getattr(role, "value", str(role))
                return providers_mod.transcript_key(role_name, prompt, agents_config=custom)

        solver = KeyedSolver({WEAK.role_name: CORRECT}, self.plan)
        record = cal._run_attempt(self.task, self.gold, V.TRANSFORM, ws, solver, WEAK, 0)
        self.assertTrue(record.success)
        self.assertEqual(record.prompt_sha256, custom_key)

        class KeyedGarbage(KeyedSolver):
            def complete(self, role, prompt: str) -> str:
                self.calls.append((str(getattr(role, "value", role)), prompt))
                return "not a submission"

        garbage = KeyedGarbage({WEAK.role_name: CORRECT}, self.plan)
        record = cal._run_attempt(self.task, self.gold, V.TRANSFORM, ws, garbage, WEAK, 0)
        self.assertFalse(record.success)
        self.assertIn("ProviderProtocolError", record.error)
        self.assertEqual(record.prompt_sha256, custom_key)
        # The same key a real RoutedProvider on that document files under.
        routed = providers_mod.RoutedProvider(
            providers_mod.load_role_routing(custom),
            providers_mod.TranscriptStore(ws / "transcripts"),
            providers_mod.CostMeter(budget_per_task_usd=1.0),
        )
        self.assertEqual(cal._transcript_key(WEAK, prompt, routed), custom_key)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

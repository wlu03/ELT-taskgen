"""Unified semantic selection, provenance, and coverage contracts."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen import cli, ingest_manifest
from elt_taskgen.engine import StageName
from elt_taskgen.corpus.selection import Quotas, SelectionResult, select
from elt_taskgen.generation.config import DEFAULT_GENERATION_CONFIG, GenerationConfig
from elt_taskgen.generation.coverage import SemanticCoveragePolicy, measure_semantic_coverage
from elt_taskgen.generation.difficulty_profiles import (
    DEFAULT_GENERATION_PROFILE, DIFFICULTY_PROFILES, apply_generation_profile, difficulty_profile,
)
from elt_taskgen.generation.mart_plan import fan_out_rollup
from elt_taskgen.generation.recipe_registry import RecipeSelection, RECIPE_REGISTRY
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
from elt_taskgen.models import ColumnType, TaskStatus
from elt_taskgen.pipeline_readiness import GenerationRunSpec, _configured_inputs
from elt_taskgen.reference.solution import compile_plan_sql
from elt_taskgen.verification.attacks import materialize_mutation, _validate_realized_fidelity
from tests.test_dataset_column_names import EVIDENCE, build_task
from tests.test_corpus import accepted, measure
from tests.advanced_fixtures import advanced_source
from elt_taskgen.generation.advanced_evidence import ADVANCED_RECIPES
from elt_taskgen.generation.composite_evidence import DOCUMENT_BALANCE
from elt_taskgen.generation.scenario_evidence import SCENARIO_RECIPES
from tests.scenario_fixtures import scenario_source


def source_task():
    return build_task(fan_out_rollup(EVIDENCE, mart="studio_activity"), "proof__recipe_registry")


def recipe_source(recipe):
    from elt_taskgen.generation.graph_evidence import GRAPH_RECIPES
    from tests.graph_fixtures import graph_source
    if recipe in GRAPH_RECIPES:
        return graph_source(recipe)
    if recipe in SCENARIO_RECIPES or recipe == DOCUMENT_BALANCE:
        return scenario_source()
    return advanced_source() if recipe in ADVANCED_RECIPES else source_task()


class RecipeRegistryTests(unittest.TestCase):
    def test_one_semantic_profile_and_removed_names_are_rejected(self):
        self.assertEqual(DEFAULT_GENERATION_PROFILE.name, "semantic")
        self.assertEqual({n for n in DIFFICULTY_PROFILES if n.startswith("semantic")}, {"semantic"})
        for old in ("semantic_v1", "semantic_v2", "semantic_v3"):
            with self.subTest(old=old):
                with self.assertRaises(ValueError):
                    difficulty_profile(old)
                with self.assertRaises(ValueError):
                    GenerationRunSpec(candidate_count=1, profile="draft", difficulty_profile=old)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    cli.build_parser().parse_args(["pipeline", "--difficulty-profile", old])

    def test_every_recipe_is_selectable_idempotent_and_mutates_only_its_mart(self):
        tasks = []
        for recipe in RECIPE_REGISTRY:
            with self.subTest(recipe=recipe):
                original = recipe_source(recipe)
                policy = RecipeSelection(enabled_recipes=(recipe,))
                task = apply_generation_profile(original, recipe_selection=policy)
                self.assertEqual(task.marts[-1].plan.template_id, recipe)
                self.assertEqual(task.marts[:-1], original.marts)
                self.assertEqual(apply_generation_profile(task, recipe_selection=policy), task)
                case = task.attack_cases[len(original.attack_cases)]
                sqls = materialize_mutation(task, case, object())
                _validate_realized_fidelity(task, case, sqls, None)
                for mart in original.marts:
                    self.assertEqual(sqls[mart.name], compile_plan_sql(task, mart))
                tasks.append(task)
        coverage = SemanticCoveragePolicy(require_certified_patterns=True,
            minimum_template_tasks=tuple((r, 1) for r in RECIPE_REGISTRY))
        self.assertFalse(measure_semantic_coverage(((str(i), t) for i, t in enumerate(tasks)), policy=coverage).problems)
        self.assertTrue(measure_semantic_coverage((("one", tasks[0]),), policy=coverage).problems)

    def test_policy_is_validated_canonical_and_seeded(self):
        names = tuple(RECIPE_REGISTRY)
        forward = RecipeSelection(enabled_recipes=names)
        reverse = RecipeSelection(enabled_recipes=tuple(reversed(names)))
        self.assertEqual(forward.digest, reverse.digest)
        self.assertEqual(forward.ordered_for("family"), reverse.ordered_for("family"))
        orders = {RecipeSelection(seed=seed).ordered_for("family") for seed in range(12)}
        self.assertGreater(len(orders), 1)
        for values in ((), ("unknown",), (names[0], names[0])):
            with self.assertRaises(ValueError):
                RecipeSelection(enabled_recipes=values)
        for seed in (True, -1, "5", 1.5):
            with self.assertRaises(ValueError):
                RecipeSelection(seed=seed)
        with self.assertRaises(ValueError):
            RecipeSelection.model_validate({"unrecognized": True})
        with self.assertRaises(ValueError):
            apply_generation_profile(source_task(), "standard", recipe_selection=forward)

    def test_only_allowed_recipes_can_admit_or_be_reused(self):
        source = source_task()
        text_dates = source.model_copy(update={"tables": tuple(t.model_copy(update={"columns": tuple(
            c.model_copy(update={"type": ColumnType.TEXT}) if c.type is ColumnType.DATE else c for c in t.columns
        )}) for t in source.tables)})
        only_calendar = RecipeSelection(enabled_recipes=("calendar_streak_v1",))
        with self.assertRaisesRegex(SemanticDifficultyError, "No enabled semantic recipe admitted"):
            apply_generation_profile(text_dates, recipe_selection=only_calendar)
        both = RecipeSelection(enabled_recipes=("calendar_streak_v1", "entity_total_baseline_v1"))
        admitted = apply_generation_profile(text_dates, recipe_selection=both)
        self.assertEqual(admitted.marts[-1].plan.template_id, "entity_total_baseline_v1")
        with self.assertRaisesRegex(SemanticDifficultyError, "disabled"):
            apply_generation_profile(admitted, recipe_selection=only_calendar)

    def test_default_config_pins_all_recipes_and_reuses_run_seed(self):
        default = GenerationRunSpec(candidate_count=3, profile="draft")
        self.assertEqual(default.generation.version, "automatic-marts-v5")
        self.assertEqual(set(default.generation.recipe_ids), set(RECIPE_REGISTRY))
        self.assertEqual(default.generation, DEFAULT_GENERATION_CONFIG)
        self.assertEqual(GenerationRunSpec.model_validate_json(default.model_dump_json()), default)
        changed = GenerationRunSpec(candidate_count=3, profile="draft", seed=5)
        self.assertNotEqual(default.fingerprint(), changed.fingerprint())
        self.assertEqual(changed.generation.recipe_selection(changed.seed).seed, 5)
        for stage in (StageName.INTAKE, StageName.SELECT, StageName.RELEASE):
            self.assertNotEqual(_configured_inputs(default, stage), _configured_inputs(changed, stage))
            inputs = _configured_inputs(default, stage)
            self.assertEqual(json.loads(json.dumps(inputs)), inputs)
        with self.assertRaisesRegex(ValueError, "all registered recipes"):
            GenerationConfig(recipe_ids=("entity_total_baseline_v1",))
        for field, value in (("difficulty_profile", "semantic"),
                             ("recipe_selection", {"seed": 5}),
                             ("semantic_coverage", {"require_certified_patterns": True})):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Extra inputs"):
                GenerationRunSpec(candidate_count=3, profile="draft", **{field: value})

    def test_recorded_snapshots_preserve_run_identity_and_seeded_output(self):
        # Captured from each version before the next roster was added.
        snapshots = (
            ("automatic-marts-v3", 21, source_task,
             "16ee60260eaea754fb12b324f8bf2c2a3d15e092e1797114354d2d6c5f0aa753",
             "c21ec9e07b33c803e6617fa01b7490811c2ba72c6d833b532af39f16a38669cc",
             "c993b0835abdcaf8b4d66539b95d0ac1bacfb0ea4604842be9e841a15eb1388e"),
            ("automatic-marts-v4", 23, scenario_source,
             "b0143db6f98cece366532ece90f709dac78bd7cba6ab3e17f3d3befbfc87df2d",
             "13a972cb72ffa97bfd22b3a1fb04dbce63387df59ad90c1393988144385b0c89",
             "43e3ad5fc9b73f9ca65dbefb5795c637060af07d197d0c556f4b7174da1bbe8e"),
        )
        for version, count, source, fingerprint, policy_digest, content_hash in snapshots:
            with self.subTest(version=version):
                old = GenerationConfig(version=version)
                spec = GenerationRunSpec.model_validate_json(json.dumps({
                    "candidate_count": 3, "profile": "draft", "seed": 9,
                    "generation": old.model_dump(mode="json"),
                }))
                self.assertEqual(len(old.recipe_ids), count)
                self.assertEqual(spec.generation, old)
                self.assertEqual(spec.fingerprint(), fingerprint)
                self.assertEqual(old.digest(9), policy_digest)
                for stage in (StageName.INTAKE, StageName.SELECT, StageName.RELEASE):
                    self.assertEqual(_configured_inputs(spec, stage)["generation"], old.canonical_contract(9))
                self.assertEqual(old.apply(source(), seed=9).content_hash(), content_hash)
                with self.assertRaisesRegex(ValueError, "all registered recipes"):
                    GenerationConfig(version=version, recipe_ids=DEFAULT_GENERATION_CONFIG.recipe_ids)
                with self.assertRaisesRegex(ValueError, "all registered recipes"):
                    GenerationConfig(recipe_ids=old.recipe_ids)

    def test_cli_needs_no_generation_settings_and_rejects_removed_knobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "run.json"
            config.write_text(json.dumps({"candidate_count": 3, "profile": "draft"}))
            args = cli.build_parser().parse_args(["pipeline", "--run-config", str(config)])
            spec = cli._configured_run_spec(args)
            self.assertEqual(spec.generation, DEFAULT_GENERATION_CONFIG)
            for option, value in (("--difficulty-profile", "semantic"), ("--recipes", "calendar_streak_v1"),
                                  ("--recipe-seed", "9"), ("--semantic-coverage-policy", "policy.json")):
                with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    cli.build_parser().parse_args(["pipeline", "--run-config", str(config), option, value])

    def test_five_pool_ingestion_binds_unified_generation_config(self):
        from tests.test_five_source_ingest import FiveSourceManifestTests
        fixture = FiveSourceManifestTests()
        fixture.setUp()
        source = source_task()
        manifest = ingest_manifest.load_five_source_manifest(fixture.manifest)
        policy = DEFAULT_GENERATION_CONFIG.recipe_selection(9)
        try:
            def build(entry, paths, catalog, **options):
                identity = fixture._task(entry, paths, catalog)
                return source.model_copy(update={key: getattr(identity, key) for key in
                    ("task_id", "family_id", "cluster_id", "origin", "license")})
            with contextlib.ExitStack() as stack:
                for pool in ("dbt", "dlt", "synsql", "schemapile", "wikidbs"):
                    stack.enter_context(mock.patch.object(ingest_manifest, f"_build_{pool}_task", side_effect=build))
                prepared = ingest_manifest.prepare_five_sources(manifest, manifest_path=fixture.manifest,
                    workspace=fixture.workspace, difficulty_profile="semantic", recipe_selection=policy)
            self.assertEqual(len(prepared), 5)
            assignments = set()
            for item in prepared:
                self.assertIn(item.task.marts[-1].plan.template_id, RECIPE_REGISTRY)
                pins = item.provenance.source.selection_inputs
                self.assertEqual(pins["generation_config"].locator, "mart-generation:automatic")
                self.assertEqual(pins["generation_config"].digest, DEFAULT_GENERATION_CONFIG.digest(9))
                self.assertNotEqual(pins["generation_config"].digest, DEFAULT_GENERATION_CONFIG.digest(10))
                self.assertNotIn("generation_profile", pins)
                self.assertNotIn("semantic_recipes", pins)
                self.assertEqual(pins["recipe_assignment"].locator, "mart-generation:batch-capability-assignment")
                assignments.add(pins["recipe_assignment"].digest)
                self.assertEqual(item.provenance.task_content_hash, item.task.content_hash())
            self.assertEqual(len(assignments), 1)
            entry = manifest.ordered_entries()[0]
            for version in ("automatic-marts-v3", "automatic-marts-v4"):
                legacy = GenerationConfig(version=version)
                pin = ingest_manifest._identity(
                    entry, prepared[0].task, adapter_name=prepared[0].provenance.source.adapter_name,
                    difficulty_profile="semantic", recipe_selection=legacy.recipe_selection(9),
                    manifest=manifest,
                ).selection_inputs["generation_config"]
                self.assertEqual(pin.digest, legacy.digest(9))
            self.assertFalse(fixture.workspace.exists())
        finally:
            fixture.doCleanups()

    def test_automatic_coverage_scales_to_small_batches_and_survives_roundtrip(self):
        tasks = []
        for i, recipe in enumerate(RECIPE_REGISTRY):
            task = apply_generation_profile(recipe_source(recipe),
                                            recipe_selection=RecipeSelection(enabled_recipes=(recipe,)))
            tasks.append(task.model_copy(update={"task_id": f"proof__auto_{i}", "family_id": f"auto_family_{i}",
                                                 "cluster_id": f"auto_cluster_{i}", "status": TaskStatus.ACCEPTED}))
        for size in (1, 3, len(RECIPE_REGISTRY)):
            with self.subTest(size=size):
                selection = select(tasks, measure(tasks), [], Quotas(size=size), accepted_variants=accepted(tasks))
                policy = selection.semantic_coverage
                self.assertTrue(policy.require_certified_patterns)
                self.assertEqual(len(policy.minimum_template_tasks), size)
                selected = {t.task_id: t for t in tasks if t.task_id in (*selection.train, *selection.val)}
                self.assertEqual(len(selected), size)
                self.assertFalse(measure_semantic_coverage(selected.items(), policy=policy).problems)
                self.assertEqual(SelectionResult.model_validate_json(selection.model_dump_json()), selection)
        for version in ("automatic-marts-v3", "automatic-marts-v4"):
            legacy = GenerationConfig(version=version)
            selection = select(tasks, measure(tasks), [], Quotas(size=len(tasks)),
                               accepted_variants=accepted(tasks), generation_config=legacy)
            self.assertEqual({recipe for recipe, _ in selection.semantic_coverage.minimum_template_tasks},
                             set(legacy.recipe_ids))

    def test_automatic_coverage_ignores_ineligible_and_duplicate_family_recipes(self):
        recipes = ("entity_total_baseline_v1", "calendar_streak_v1", "aggregate_filter_rank_v1", "global_baseline_v1")
        tasks = []
        for i, recipe in enumerate(recipes):
            task = apply_generation_profile(source_task(), recipe_selection=RecipeSelection(enabled_recipes=(recipe,)))
            tasks.append(task.model_copy(update={"task_id": f"proof__available_{i}", "family_id": f"available_family_{i}",
                                                 "cluster_id": f"available_cluster_{i}", "status": TaskStatus.ACCEPTED}))
        tasks[1] = tasks[1].model_copy(update={"family_id": tasks[0].family_id})
        tasks[2] = tasks[2].model_copy(update={"status": TaskStatus.DRAFT})
        measurements = measure(tasks[:-1])  # The fourth candidate has no current measurements.
        selection = select(tasks, measurements, [], Quotas(size=1), accepted_variants=accepted(tasks))
        self.assertEqual(selection.semantic_coverage.minimum_template_tasks, ((recipes[0], 1),))
        self.assertEqual((*selection.train, *selection.val), (tasks[0].task_id,))


if __name__ == "__main__":
    unittest.main()

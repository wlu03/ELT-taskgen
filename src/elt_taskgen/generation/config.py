"""The automatic mart-generation policy used by every normal pipeline run.

This is a recorded policy snapshot, not a menu of user-selectable profiles.
Individual recipe builders remain callable by local correctness experiments.
"""
import json
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

from elt_taskgen.generation.coverage import SemanticCoveragePolicy
from elt_taskgen.generation.difficulty_profiles import DEFAULT_GENERATION_PROFILE
from elt_taskgen.generation.recipe_registry import RecipeSelection
from elt_taskgen.models import TaskIR, canonical_json, sha256_hex


# These exact rosters are part of persisted run identities and stage seals.
_V3_RECIPES = (
    "activity_lifecycle_v1", "aggregate_filter_rank_v1", "ambiguous_path_rollup_v1",
    "calendar_streak_v1", "cohort_retention_v1", "composite_key_bridge_v1",
    "document_reconciliation_v1", "entity_total_baseline_v1", "event_sessionization_v1",
    "global_baseline_v1", "heterogeneous_key_join_v1", "historical_asof_v1",
    "independent_winners_v1", "interval_coverage_v1", "ledger_reconciliation_v1",
    "multi_bridge_dedup_v1", "optional_chain_coverage_v1", "recursive_hierarchy_v1",
    "role_playing_dimension_v1", "semantic_rollup_v1", "weighted_allocation_v1",
)
_V4_RECIPES = tuple(sorted((*_V3_RECIPES, "composite_document_balance_v1", "composite_eligible_group_report_v1")))
_V5_RECIPES = tuple(sorted((*_V4_RECIPES, "discovered_group_report_v1")))
_VERSIONED_RECIPES = {
    "automatic-marts-v3": _V3_RECIPES,
    "automatic-marts-v4": _V4_RECIPES,
    "automatic-marts-v5": _V5_RECIPES,
}


class GenerationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["automatic-marts-v3", "automatic-marts-v4", "automatic-marts-v5"] = "automatic-marts-v5"
    recipe_ids: tuple[str, ...] = _V5_RECIPES
    selection: Literal["capability-matching-then-family-hash"] = "capability-matching-then-family-hash"
    coverage: Literal["available-recipes-and-certified-patterns"] = "available-recipes-and-certified-patterns"

    @model_validator(mode="before")
    @classmethod
    def _version_defaults(cls, value):
        if isinstance(value, dict) and "recipe_ids" not in value:
            version = value.get("version")
            if isinstance(version, str) and version in _VERSIONED_RECIPES:
                return {**value, "recipe_ids": _VERSIONED_RECIPES[version]}
        return value

    @field_validator("recipe_ids")
    @classmethod
    def _all_recipes(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        expected = _VERSIONED_RECIPES.get(info.data.get("version"))
        if tuple(sorted(value)) != expected:
            raise ValueError("Automatic generation enables all registered recipes for the recorded version")
        return expected

    def recipe_selection(self, seed: int = 0) -> RecipeSelection:
        # Reuse the run's existing seed. There is no second recipe seed to set.
        return RecipeSelection(enabled_recipes=self.recipe_ids, seed=seed % (2 ** 64))

    def apply(self, task: TaskIR, *, seed: int = 0) -> TaskIR:
        from elt_taskgen.generation.difficulty_profiles import apply_generation_profile
        return apply_generation_profile(task, DEFAULT_GENERATION_PROFILE,
                                        recipe_selection=self.recipe_selection(seed))

    def canonical_contract(self, seed: int = 0) -> dict:
        # Stage seals compare persisted JSON with this value. Normalize the
        # internal dataclass tuples so an unchanged run survives a round trip.
        return json.loads(canonical_json({**self.model_dump(mode="json"),
                "recipe_seed": self.recipe_selection(seed).seed,
                "shape_and_scale": DEFAULT_GENERATION_PROFILE.catalog_contract()}))

    def digest(self, seed: int = 0) -> str:
        return sha256_hex(canonical_json(self.canonical_contract(seed)))

    def coverage_policy(self, candidates: list[TaskIR], size: int) -> SemanticCoveragePolicy:
        """Cover the available recipe families without imposing impossible minima.

        The caller supplies eligible, family-independent candidates. A small
        selection targets at most one family per slot, preferring rare recipes;
        a larger one covers every available registered recipe. Missing schema
        capabilities never produce quotas for a recipe absent from the pool.
        """
        owners = Counter(recipe for task in candidates for recipe in {
            m.plan.template_id for m in task.marts if m.plan.template_id in self.recipe_ids})
        targets = sorted(owners, key=lambda recipe: (owners[recipe], recipe))[:size]
        return SemanticCoveragePolicy(require_certified_patterns=True,
                                      minimum_template_tasks=tuple((recipe, 1) for recipe in targets))


DEFAULT_GENERATION_CONFIG = GenerationConfig()


def contract_for_recipe_selection(selection: RecipeSelection) -> dict:
    """Bind ingest provenance to the selected roster, including one-recipe proof runs."""
    for version, roster in _VERSIONED_RECIPES.items():
        if selection.enabled_recipes == roster:
            return GenerationConfig(version=version).canonical_contract(selection.seed)
    return {**DEFAULT_GENERATION_CONFIG.canonical_contract(selection.seed), "recipe_ids": list(selection.enabled_recipes)}

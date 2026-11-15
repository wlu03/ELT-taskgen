"""The automatic mart-generation policy used by every normal pipeline run.

This is a recorded policy snapshot, not a menu of user-selectable profiles.
Individual recipe builders remain callable by local correctness experiments.
"""
import json
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from elt_taskgen.generation.coverage import SemanticCoveragePolicy
from elt_taskgen.generation.difficulty_profiles import DEFAULT_GENERATION_PROFILE
from elt_taskgen.generation.recipe_registry import RECIPE_REGISTRY, RecipeSelection
from elt_taskgen.models import TaskIR, canonical_json, sha256_hex


class GenerationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["automatic-marts-v3"] = "automatic-marts-v3"
    recipe_ids: tuple[str, ...] = tuple(sorted(RECIPE_REGISTRY))
    selection: Literal["capability-matching-then-family-hash"] = "capability-matching-then-family-hash"
    coverage: Literal["available-recipes-and-certified-patterns"] = "available-recipes-and-certified-patterns"

    @field_validator("recipe_ids")
    @classmethod
    def _all_recipes(cls, value):
        if tuple(sorted(value)) != tuple(sorted(RECIPE_REGISTRY)):
            raise ValueError("Automatic generation enables all registered recipes; custom recipe lists are not run settings")
        return tuple(sorted(value))

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

"""One registry and selection policy for versioned semantic mart recipes."""
from dataclasses import dataclass
from importlib import import_module

from pydantic import BaseModel, ConfigDict, Field, field_validator

from elt_taskgen.models import TaskIR, canonical_json, sha256_hex


@dataclass(frozen=True)
class SemanticRecipe:
    recipe_id: str
    description: str
    module: str
    builder: str
    mutation_builder: str
    argument: str = ""
    accepts_seed: bool = False

    def apply(self, task: TaskIR, *, seed: int = 0) -> TaskIR:
        kwargs = {self.argument: self.recipe_id} if self.argument else {}
        if self.accepts_seed:
            kwargs["seed"] = seed
        return getattr(import_module(self.module), self.builder)(task, **kwargs)

    def materialize_mutation(self, task: TaskIR, payload: dict) -> dict[str, str]:
        return getattr(import_module(self.module), self.mutation_builder)(task, payload)


_PREFIX = "elt_taskgen.generation."
RECIPE_REGISTRY = {
    recipe.recipe_id: recipe for recipe in (
        *(SemanticRecipe(name, description, _PREFIX + "graph_recipes", "apply_graph_difficulty", "materialize_graph_mutation", "recipe")
          for name, description in (
            ("role_playing_dimension_v1", "Resolve separate entity identities for multiple foreign-key roles."),
            ("ambiguous_path_rollup_v1", "Distinguish memberships along two valid relationship paths."),
            ("multi_bridge_dedup_v1", "Deduplicate membership pairs across successive bridges."),
            ("optional_chain_coverage_v1", "Keep partial activity distinct from missing intermediate entities."),
            ("heterogeneous_key_join_v1", "Safely normalize represented keys and distinguish invalid, absent, and unmatched links."),
            ("composite_key_bridge_v1", "Count memberships and duplicates using complete composite keys."),
          )),
        *(SemanticRecipe(name, description, _PREFIX + "composite_recipes", "apply_composite_difficulty", "materialize_composite_mutation", "recipe")
          for name, description in (
            ("composite_eligible_group_report_v1", "Select a winning eligible group and compare its total with eligible peers."),
            ("composite_document_balance_v1", "Reconcile independent line and payment totals and compare document balances."),
          )),
        SemanticRecipe("discovered_group_report_v1", "Compose group transformations and admit an executable report on a source binding.",
                       _PREFIX + "discovered_recipes", "apply_discovered_difficulty", "materialize_discovered_mutation",
                       "recipe", accepts_seed=True),
        SemanticRecipe("semantic_rollup_v1", "Parent thresholds, grouped totals, and a selected record.",
                       _PREFIX + "semantic_difficulty", "apply_semantic_difficulty", "materialize_semantic_mutation"),
        SemanticRecipe("global_baseline_v1", "Compare activity with the mean of all fact measures.",
                       _PREFIX + "compositions", "apply_composition_difficulty", "materialize_composition_mutation", "template"),
        SemanticRecipe("independent_winners_v1", "Independently select the highest and lowest records.",
                       _PREFIX + "compositions", "apply_composition_difficulty", "materialize_composition_mutation", "template"),
        SemanticRecipe("aggregate_filter_rank_v1", "Select the highest-total eligible group.",
                       _PREFIX + "dependent_compositions", "apply_dependent_difficulty", "materialize_recipe_mutation", "recipe"),
        SemanticRecipe("entity_total_baseline_v1", "Compare entity totals with a population mean including inactive entities.",
                       _PREFIX + "dependent_compositions", "apply_dependent_difficulty", "materialize_recipe_mutation", "recipe"),
        SemanticRecipe("calendar_streak_v1", "Count consecutive qualifying calendar-year runs.",
                       _PREFIX + "dependent_compositions", "apply_dependent_difficulty", "materialize_recipe_mutation", "recipe"),
        SemanticRecipe("historical_asof_v1", "Align each event with the history version effective at its timestamp.",
                       _PREFIX + "advanced_recipes", "apply_advanced_difficulty", "materialize_advanced_mutation", "recipe"),
        SemanticRecipe("cohort_retention_v1", "Distinct member retention against complete signup cohorts across calendar months.",
                       _PREFIX + "advanced_recipes", "apply_advanced_difficulty", "materialize_advanced_mutation", "recipe"),
        SemanticRecipe("weighted_allocation_v1", "Normalize sibling weights before allocating each parent's amount.",
                       _PREFIX + "advanced_recipes", "apply_advanced_difficulty", "materialize_advanced_mutation", "recipe"),
        *(SemanticRecipe(name, description, _PREFIX + "scenario_recipes", "apply_scenario_difficulty", "materialize_scenario_mutation", "recipe")
          for name, description in (
            ("interval_coverage_v1", "Clip and union overlapping intervals inside each parent window."),
            ("event_sessionization_v1", "Partition ordered events into inactivity bursts."),
            ("activity_lifecycle_v1", "Classify activity transitions over complete calendar months."),
            ("document_reconciliation_v1", "Reconcile headers, independent lines and payments."),
            ("ledger_reconciliation_v1", "Check debit/credit balance separately for every journal entry."),
            ("recursive_hierarchy_v1", "Count distinct transitive descendants and identify cycle membership."),
          )),
    )
}


class RecipeSelection(BaseModel):
    """Allowlist and reproducible selection; coverage is checked separately."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled_recipes: tuple[str, ...] = tuple(sorted(RECIPE_REGISTRY))
    seed: int = Field(default=0, strict=True, ge=0)

    @field_validator("enabled_recipes")
    @classmethod
    def _known_nonempty_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("enabled_recipes must contain at least one recipe")
        if len(value) != len(set(value)):
            raise ValueError("enabled_recipes must not contain duplicates")
        unknown = set(value) - RECIPE_REGISTRY.keys()
        if unknown:
            raise ValueError(f"Unknown semantic recipes: {', '.join(sorted(unknown))}")
        return tuple(sorted(value))

    def canonical_contract(self) -> dict:
        return self.model_dump(mode="json")

    @property
    def digest(self) -> str:
        return sha256_hex(canonical_json(self.canonical_contract()))

    def ordered_for(self, family_id: str) -> tuple[str, ...]:
        # Hash each recipe independently: allowlist order does not affect the
        # choice, and adding another recipe preserves the old relative order.
        return tuple(sorted(self.enabled_recipes, key=lambda recipe: (
            sha256_hex(canonical_json([self.seed, family_id, recipe])), recipe)))

    def validate_coverage(self, coverage) -> None:
        if coverage is None:
            return
        required = {name for name, _ in (*coverage.minimum_template_tasks, *coverage.minimum_template_marts)}
        disabled = (required & RECIPE_REGISTRY.keys()) - set(self.enabled_recipes)
        if disabled:
            raise ValueError(f"Coverage requires disabled semantic recipes: {', '.join(sorted(disabled))}")


def resolve_recipe_selection(profile: str, selection: RecipeSelection | dict | None) -> RecipeSelection | None:
    if profile != "semantic":
        if selection is not None:
            raise ValueError("Recipe selection requires the semantic generation profile")
        return None
    return RecipeSelection.model_validate(selection) if selection is not None else RecipeSelection()


def apply_semantic_recipes(task: TaskIR, selection: RecipeSelection | dict | None = None) -> TaskIR:
    """Admit one allowed recipe with executable witnesses, or refuse explicitly."""
    from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError

    policy = resolve_recipe_selection("semantic", selection)
    existing = [m.plan.template_id for m in task.marts if m.plan.template_id in RECIPE_REGISTRY]
    if existing:
        if len(existing) != 1:
            raise SemanticDifficultyError("Expected exactly one managed semantic recipe per task")
        if existing[0] not in policy.enabled_recipes:
            raise SemanticDifficultyError(f"Existing recipe {existing[0]} is disabled by recipe_selection")
        return RECIPE_REGISTRY[existing[0]].apply(task, seed=policy.seed)
    refusals = []
    from elt_taskgen.generation.recipe_assignment import supported_order
    for recipe_id in supported_order(task, policy):
        try:
            return RECIPE_REGISTRY[recipe_id].apply(task, seed=policy.seed)
        except SemanticDifficultyError as exc:
            refusals.append(f"{recipe_id}: {exc}")
    raise SemanticDifficultyError("No enabled semantic recipe admitted: "
                                  + (" | ".join(refusals) or "no source-supported capabilities"))

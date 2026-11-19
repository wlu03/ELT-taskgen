"""Bind composite plans to source relationships and construct their shared rows."""

from elt_taskgen.generation.recipe_evidence import AGGREGATE_RANK, RecipeEvidence, recipe_candidates
from elt_taskgen.generation.recipe_witness import recipe_witness_rows
from elt_taskgen.generation.scenario_evidence import DOCUMENT, ScenarioEvidence, scenario_candidates
from elt_taskgen.generation.scenario_witness import scenario_witness_rows
from elt_taskgen.models import TaskIR

ELIGIBLE_GROUP_REPORT = "composite_eligible_group_report_v1"
DOCUMENT_BALANCE = "composite_document_balance_v1"
COMPOSITE_RECIPES = (ELIGIBLE_GROUP_REPORT, DOCUMENT_BALANCE)


def composite_candidates(task: TaskIR, recipe: str) -> tuple[RecipeEvidence | ScenarioEvidence, ...]:
    if recipe == ELIGIBLE_GROUP_REPORT:
        return recipe_candidates(task, AGGREGATE_RANK)
    if recipe == DOCUMENT_BALANCE:
        return scenario_candidates(task, DOCUMENT, require_document_total=False)
    raise ValueError(f"Unknown composite recipe {recipe!r}")


def composite_witness_rows(task: TaskIR, evidence: RecipeEvidence | ScenarioEvidence, recipe: str):
    """Supply one counterfactual dataset for all branches of the final report."""
    if recipe == ELIGIBLE_GROUP_REPORT and isinstance(evidence, RecipeEvidence):
        # A high ineligible total tests both ranking and baseline population.
        # Tied eligible groups and parents without activity exercise other outputs.
        return recipe_witness_rows(task, evidence, AGGREGATE_RANK)
    if recipe == DOCUMENT_BALANCE and isinstance(evidence, ScenarioEvidence):
        # A document with repeated lines AND repeated payments makes an early
        # raw join inflate both sides. Empty documents affect the population mean.
        return scenario_witness_rows(task, evidence, DOCUMENT)
    raise ValueError(f"Invalid source binding for composite recipe {recipe!r}")

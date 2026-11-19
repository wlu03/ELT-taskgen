"""List source-supported graph bindings and compile one explicit selection."""

from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from elt_taskgen.generation.composite_evidence import COMPOSITE_RECIPES, composite_candidates
from elt_taskgen.generation.composite_recipes import apply_bound_composite
from elt_taskgen.generation.recipe_registry import RECIPE_REGISTRY
from elt_taskgen.models import Origin, PopulationName, TaskIR, TaskStatus


class CompositeProposal(BaseModel):
    """A versioned recipe fixes the graph. A binding names its actual inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["composite-proposal-v1"]
    task_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipe: str
    binding: dict[str, str | bool | list[str]]

    @field_validator("recipe")
    @classmethod
    def _known_recipe(cls, value: str) -> str:
        if value not in COMPOSITE_RECIPES:
            raise ValueError(f"Unknown composite recipe {value!r}")
        return value


def _require_source(task: TaskIR) -> None:
    if task.origin is Origin.ELTBENCH_ANCHOR:
        raise ValueError("ELT-Bench anchors are reserved for measurement")
    if any(m.plan.template_id in RECIPE_REGISTRY for m in task.marts):
        raise ValueError("Composition requires the adapter TaskIR before semantic recipe selection")
    if not any(p.name is PopulationName.COUNTERFACTUAL for p in task.populations):
        raise ValueError("Composition requires a source counterfactual population")


def _binding(evidence):
    # Omit fields belonging to other recipes supported by the same source adapter.
    return {
        role: list(value) if isinstance(value, tuple) else value
        for role, value in asdict(evidence).items()
        if value != "" and value != ()
    }


def list_composite_proposals(task: TaskIR) -> tuple[CompositeProposal, ...]:
    """List schema-supported choices. Executable admission occurs on apply."""
    _require_source(task)
    digest = task.content_hash()
    return tuple(
        CompositeProposal(
            schema_version="composite-proposal-v1",
            task_id=task.task_id,
            source_sha256=digest,
            recipe=recipe,
            binding=_binding(evidence),
        )
        for recipe in COMPOSITE_RECIPES
        for evidence in composite_candidates(task, recipe)
    )


def apply_composite_proposal(task: TaskIR, proposal: CompositeProposal) -> TaskIR:
    """Compile and witness exactly the requested binding against the original source."""
    _require_source(task)
    if proposal.task_id != task.task_id or proposal.source_sha256 != task.content_hash():
        raise ValueError("Proposal identifies a different task or stale source content")
    for evidence in composite_candidates(task, proposal.recipe):
        if proposal.binding == _binding(evidence):
            # A composed target must earn its own review, gold and release evidence.
            draft = task.model_copy(update={
                "status": TaskStatus.DRAFT, "revisions": (), "public_specification": None,
                "solver_prompt": "", "reference": None,
            })
            result = apply_bound_composite(draft, proposal.recipe, evidence)
            return result.with_revision(
                route=None, reason=f"Composed {proposal.recipe} from {task.task_id} at {proposal.source_sha256}"
            )
    raise ValueError(f"Source does not support the proposed binding for {proposal.recipe}")

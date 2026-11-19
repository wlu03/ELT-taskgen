"""List source-supported graphs and compile an explicit or automatic selection."""

from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_serializer, model_validator

from elt_taskgen.generation.composite_evidence import COMPOSITE_RECIPES, composite_candidates
from elt_taskgen.generation.composite_recipes import apply_bound_composite
from elt_taskgen.generation.composition_search import CompositionGraph, SEARCH_RECIPE
from elt_taskgen.generation.discovered_recipes import (
    DiscoveredEvidence, apply_bound_discovered, apply_discovered_difficulty, bound_discovered, discovered_candidates,
)
from elt_taskgen.generation.recipe_registry import RECIPE_REGISTRY
from elt_taskgen.models import Origin, PopulationName, TaskIR, TaskStatus, canonical_json, sha256_hex


class CompositeProposal(BaseModel):
    """Version 1 selects a named report. Version 2 selects a discovered graph."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["composite-proposal-v1", "composite-proposal-v2"]
    task_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipe: str
    binding: dict[str, str | bool | list[str]]
    graph: dict[str, JsonValue] | None = None
    proposal_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _check_contract(self):
        if self.schema_version == "composite-proposal-v1":
            if self.recipe not in COMPOSITE_RECIPES:
                raise ValueError(f"Unknown composite recipe {self.recipe!r}")
            if {"graph", "proposal_id"} & self.model_fields_set:
                raise ValueError("Version 1 proposals select a fixed recipe by name")
        else:
            if self.recipe != SEARCH_RECIPE or self.graph is None:
                raise ValueError("Version 2 proposals require the discovered recipe and its graph")
            parsed = CompositionGraph.from_dict(self.graph)
            if canonical_json(parsed.as_dict()) != canonical_json(self.graph):
                raise ValueError("Proposal graph must use its canonical node order")
            fields = self.model_dump(mode="json", exclude={"proposal_id"})
            if self.proposal_id != sha256_hex(canonical_json(fields)):
                raise ValueError("Proposal ID does not match its source, binding and graph")
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if self.schema_version == "composite-proposal-v1":
            # Keep the original wire format, including when exporting a mixed list.
            data.pop("graph", None)
            data.pop("proposal_id", None)
        return data


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


def _discovered_proposal(task_id: str, digest: str, candidate: DiscoveredEvidence) -> CompositeProposal:
    fields = {
        "schema_version": "composite-proposal-v2",
        "task_id": task_id,
        "source_sha256": digest,
        "recipe": SEARCH_RECIPE,
        "binding": _binding(candidate.source),
        "graph": candidate.graph.as_dict(),
    }
    return CompositeProposal(**fields, proposal_id=sha256_hex(canonical_json(fields)))


def list_composite_proposals(task: TaskIR) -> tuple[CompositeProposal, ...]:
    """List schema-supported choices. Executable admission occurs on apply."""
    _require_source(task)
    digest = task.content_hash()
    fixed = tuple(
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
    return fixed + tuple(
        _discovered_proposal(task.task_id, digest, candidate)
        for candidate in discovered_candidates(task, SEARCH_RECIPE)
    )


def _fresh_draft(task: TaskIR) -> TaskIR:
    # A composed target must earn its own review, gold and release evidence.
    return task.model_copy(update={
        "status": TaskStatus.DRAFT, "revisions": (), "public_specification": None,
        "solver_prompt": "", "reference": None,
    })


def _record_composition(result: TaskIR, proposal: CompositeProposal, *, seed: int | None = None) -> TaskIR:
    reason = f"Composed {proposal.recipe} from {proposal.task_id} at {proposal.source_sha256}"
    if proposal.proposal_id is not None:
        reason += f" using proposal {proposal.proposal_id}"
    if seed is not None:
        reason += f" (search seed {seed})"
    return result.with_revision(route=None, reason=reason)


def apply_composite_proposal(task: TaskIR, proposal: CompositeProposal) -> TaskIR:
    """Compile and witness exactly the requested binding against the original source."""
    _require_source(task)
    if proposal.task_id != task.task_id or proposal.source_sha256 != task.content_hash():
        raise ValueError("Proposal identifies a different task or stale source content")
    if proposal.schema_version == "composite-proposal-v2":
        for candidate in discovered_candidates(task, proposal.recipe):
            if proposal.binding != _binding(candidate.source) or proposal.graph != candidate.graph.as_dict():
                continue
            expected = _discovered_proposal(task.task_id, proposal.source_sha256, candidate)
            # Recheck the whole claim because callers can edit a nested dict
            # after model validation. Canonical JSON distinguishes 0 from false.
            if canonical_json(expected.model_dump(mode="json")) != canonical_json(proposal.model_dump(mode="json")):
                raise ValueError("Proposal does not match the selected graph and ID")
            return _record_composition(apply_bound_discovered(_fresh_draft(task), proposal.recipe, candidate), proposal)
    else:
        for evidence in composite_candidates(task, proposal.recipe):
            if proposal.binding == _binding(evidence):
                return _record_composition(apply_bound_composite(_fresh_draft(task), proposal.recipe, evidence), proposal)
    raise ValueError(f"Source does not support the proposed binding or graph for {proposal.recipe}")


def auto_compose(task: TaskIR, *, seed: int = 0) -> TaskIR:
    """Search source-compatible graphs and retain the first witnessed result."""
    _require_source(task)
    if type(seed) is not int or seed < 0:
        raise ValueError("Search seed must be a non-negative integer")
    result = apply_discovered_difficulty(_fresh_draft(task), seed=seed)
    evidence = bound_discovered(result, result.marts[-1])
    proposal = _discovered_proposal(task.task_id, task.content_hash(), evidence)
    return _record_composition(result, proposal, seed=seed)

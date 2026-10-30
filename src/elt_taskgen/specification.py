"""Outcome specifications shared by public exports and solver calibration.

The executable plan remains an internal authoring and verification artifact.
Only model/column definitions and business scope belong in data_model.yaml.
"""

from __future__ import annotations

from typing import Any

from elt_taskgen.models import MartOpKind, MartSpec, TaskIR, canonical_json, sha256_hex


def model_description(mart: MartSpec) -> str:
    """Describe the output population, including otherwise hidden scope rules.

    Filters and duplicate identity can change the required output without changing
    its columns. Preserve their business rules without publishing their position
    in the plan or predicate expressions. Other operations stay private; their
    output meanings live in the column descriptions.
    """
    parts = [mart.description.strip(), mart.grain.strip()]
    for op in mart.plan.ops:
        if op.kind is MartOpKind.FILTER:
            description = op.description.strip()
            # Some composers use the relation binding as a diagnostic label,
            # with the actual scope already defined by the output columns.
            # Such a label contains no requirement and exposes compiler state.
            binding = op.details.get("name", "")
            if binding and description == str(binding).replace("_", " "):
                continue
            parts.append(description)
        elif op.kind is MartOpKind.DEDUPE:
            # A record identity is a business rule, not a DISTINCT recipe.
            identity = (
                "records with the same " + ", ".join(op.columns)
                if op.columns else "identical records"
            )
            parts.append(
                f"Repeated {', '.join(op.tables)} {identity} count once."
            )
    sentences: list[str] = []
    for part in parts:
        if not part:
            continue
        if not part.endswith((".", "!", "?")):
            part += "."
        if part not in sentences:
            sentences.append(part)
    return " ".join(sentences)


def source_fingerprint(task: TaskIR) -> str:
    """Bind authored prose to all semantic targets and their source context."""
    return sha256_hex(canonical_json({
        "marts": [m.model_dump(mode="json") for m in task.marts],
        "tables": [t.model_dump(mode="json") for t in task.tables],
        "relationships": [r.model_dump(mode="json") for r in task.relationships],
    }))


def data_model(task: TaskIR) -> dict[str, Any]:
    """Export authored definitions when present, rejecting stale or renamed ones."""
    authored = task.public_specification
    if authored is None:
        return source_data_model(task)
    if authored.source_sha256 != source_fingerprint(task):
        raise ValueError("Public specification is stale; re-author it after changing task requirements")
    document = {"models": [m.model_dump(mode="json") for m in authored.models]}
    from elt_taskgen.review.model_specification import parse_document

    return parse_document(canonical_json(document), source_data_model(task))


def source_data_model(task: TaskIR) -> dict[str, Any]:
    """Return the original ELT-Bench model/column description schema.

    No operation sequence, join choices, intermediate fields, output sorting, or
    compiler types are part of this public interface. Sorting keys and typed
    columns remain in TaskIR and the private evaluation artifacts.
    """
    return {
        "models": [
            {
                "name": mart.name,
                "description": model_description(mart),
                "columns": [
                    {"name": column.name, "description": column.description}
                    for column in mart.columns
                ],
            }
            for mart in task.marts
        ]
    }

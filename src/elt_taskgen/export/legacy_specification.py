"""Read compatibility for frozen releases predating outcome-only specifications.

These renderers are used only to verify existing artifacts against their private
TaskIR. New exports must use eltbench.build_data_model/public_documentation.
"""

from __future__ import annotations

from elt_taskgen.generation.mart_plan import (
    solver_safe_mart_requirements,
    solver_safe_plan_summary,
)
from elt_taskgen.models import TaskIR


def data_model(task: TaskIR) -> dict:
    return {"models": [solver_safe_mart_requirements(task, mart) for mart in task.marts]}


def public_documentation(task: TaskIR) -> str:
    from elt_taskgen.export.eltbench import _source_schema_markdown

    lines = [f"# {task.title or task.task_id}", ""]
    if task.solver_prompt.strip():
        lines += ["## Specification", "", task.solver_prompt.strip(), ""]
    lines += [
        "## Transformation specification",
        "",
        "Build every mart below using the ordered semantic rules. The rules",
        "define required source inputs, matching behavior, filters, aggregates,",
        "null handling, and deterministic tie behavior; they do not prescribe",
        "a particular SQL implementation.",
        "",
        "Create every mart in the same database and schema the raw source",
        "tables are loaded into: the destination namespace named in",
        "`config.yaml`. A mart built in any other schema is not found and does",
        "not count, so do not route marts to a separate schema.",
        "",
    ]
    for mart in task.marts:
        lines += [
            f"### `{mart.name}`",
            "",
            f"- Grain: {mart.grain}",
            f"- Unique key: {', '.join(mart.key_columns)}",
            "- Required columns: " + ", ".join(col.name for col in mart.columns),
            "",
            "```text",
            solver_safe_plan_summary(task, mart),
            "```",
            "",
        ]
    lines += _source_schema_markdown(task)
    return "\n".join(lines) + "\n"

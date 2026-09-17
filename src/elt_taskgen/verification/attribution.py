"""Compare authored column lineage with frozen reference bindings.

Only proven source-table contradictions fail. Missing prose claims or reference bindings
are not measurable, and the repair target is prose rather than gold SQL.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from pathlib import Path

from elt_taskgen.models import TaskIR

#: Name the failure detail carries, so one grep finds every refusal.
GATE_NAME = "column-attribution"

#: Reported instead of a verdict when the inputs are not on disk.
NOT_MEASURABLE = "not measurable here"

#: `<table>."<col>" AS "<mart col>"` — where a passthrough acquires its source.
_BINDING = re.compile(r'(\w+)\."([^"]+)"\s+AS\s+"([^"]+)"', re.IGNORECASE)

#: A mart column's bullet line. The COLUMN is anchored; the ATTRIBUTION is not.
_CLAIM_COLUMN = re.compile(r"^\s*[-*]\s*`([^`]+)`")

#: Backticked tokens on a claim line; intersected with the task's own schema.
_BACKTICKED = re.compile(r"`([^`]+)`")

#: Mart heading ("## Mart: <name>"); claims are read per mart (see prose_claims).
_MART_HEADING = re.compile(r"^\s*#+\s*Mart:\s*(\S+)", re.IGNORECASE)


def reference_bindings(task: TaskIR, workspace: Path) -> dict[str, dict[str, str]]:
    """mart -> {mart column: SOURCE TABLE} as the FROZEN reference binds it.

    ONLY DECLARED SOURCE TABLES COUNT: the compiled reference re-projects
    columns through CTE step aliases, which are not origins. A column that
    resolves to nothing is left unchecked — inventing an origin it cannot
    resolve would turn a certifier into a guesser.
    """
    sources = {t.name for t in task.tables}
    root = Path(workspace) / "tasks" / task.task_id / "answer_key" / "reference"
    out: dict[str, dict[str, str]] = {}
    for mart in task.marts:
        path = root / f"{mart.name}.sql"
        if not path.is_file():
            continue
        try:
            sql = path.read_text(encoding="utf-8")
        except OSError:
            continue
        bindings: dict[str, str] = {}
        for table, _source_column, alias in _BINDING.findall(sql):
            if table in sources:
                bindings.setdefault(alias, table)
        if bindings:
            out[mart.name] = bindings
    return out


def prose_claims(prose: str, source_tables: Collection[str]) -> dict[str, dict[str, str]]:
    """Return source-table attribution claims by mart and output column.

    A line makes a claim only when it names exactly one task source table. Zero names
    make no claim; multiple names describe a relationship. Claims remain mart-local.
    """
    known = {t for t in source_tables}
    claims: dict[str, dict[str, str]] = {}
    mart = ""
    for line in (prose or "").splitlines():
        heading = _MART_HEADING.match(line)
        if heading is not None:
            mart = heading.group(1).strip("`")
            continue
        if not mart:
            continue
        column = _CLAIM_COLUMN.match(line)
        if column is None:
            continue
        named = {t for t in _BACKTICKED.findall(line) if t in known}
        if len(named) != 1:
            continue
        claims.setdefault(mart, {}).setdefault(column.group(1), named.pop())
    return claims


def check_attribution(task: TaskIR, workspace: Path) -> str | None:
    """The failure detail for prose that contradicts the reference, or None.

    None covers both "everything agrees" and "nothing measurable"; the caller
    must not tell them apart, as this check has no authority to certify.
    """
    bindings = reference_bindings(task, workspace)
    if not bindings:
        return None
    claims = prose_claims(
        task.solver_prompt or "", [t.name for t in task.tables]
    )
    if not claims:
        return None

    problems: list[str] = []
    for mart in sorted(claims):
        bound_in_mart = bindings.get(mart)
        if not bound_in_mart:
            continue  # nothing frozen for this mart: says nothing either way
        for column, claimed in sorted(claims[mart].items()):
            bound = bound_in_mart.get(column)
            if bound is None or claimed == bound:
                continue
            problems.append(
                f"{mart}.{column}: prose says it comes from {claimed!r}, the "
                f"reference reads it from {bound!r}"
            )
    if not problems:
        return None
    return (
        f"{GATE_NAME}: authored column lineage contradicts the frozen "
        "reference, so a solver that believes the prose cannot reproduce gold "
        "— " + "; ".join(problems[:4])
        + (f" (+{len(problems) - 4} more)" if len(problems) > 4 else "")
    )

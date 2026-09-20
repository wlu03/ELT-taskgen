"""Dataset context for the semantic author: a bounded look at real rows.

The author saw only schemas and plan summaries, so two tasks built from the
same library shape were described in nearly the same sentences. The
contamination firewall then queued those repeats as borderline collisions
between our own tasks, and 48 of 50 needed a human sign-off (batch50,
2026-09-19). Rows let the author write about THIS dataset.

Only the DEVELOPMENT population is read. That is the population a solver's own
sources are seeded with, so nothing here is hidden from a solver; the graded
populations, the gold and the reference SQL stay out of the author's view.
"""

from __future__ import annotations

import json
from pathlib import Path

from elt_taskgen.models import PopulationName, TaskIR

#: Context, not a data dump: a wide table must not crowd out the plan
#: summaries, so rows, cells and the whole section are bounded.
MAX_ROWS_PER_TABLE = 3
MAX_CELL_CHARS = 60
#: A 36-column WikiDBs table would otherwise spend the whole budget on one
#: table and truncate every later one. The full schema is already in the view.
MAX_COLUMNS_PER_ROW = 12
MAX_SNAPSHOT_CHARS = 8000

_TRUNCATED = "    (snapshot truncated)"


def _cell(value: object) -> str:
    """One value as short display text."""
    if value is None:
        return "NULL"
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > MAX_CELL_CHARS:
        return text[: MAX_CELL_CHARS - 3] + "..."
    return text


def _read_rows(path: Path, limit: int) -> tuple[list[dict], int]:
    """(up to `limit` leading rows, total row count) of one JSONL population file."""
    rows: list[dict] = []
    total = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except ValueError:
                # A malformed population is the generator's failure, reported by
                # the stages that read it; the author simply gets less context.
                break
            if not isinstance(row, dict):
                break
            total += 1
            if len(rows) < limit:
                rows.append(row)
    return rows, total


def development_snapshot(task: TaskIR, populations_dir: Path) -> str:
    """Row counts and a few example rows of the development population.

    Deterministic: tables in task order, rows in population order. Empty when
    the population is not materialized, and the author view then reads exactly
    as it did before.
    """
    rows_dir = Path(populations_dir) / PopulationName.DEVELOPMENT.value / "rows"
    if not rows_dir.is_dir():
        return ""
    lines: list[str] = []
    for table in task.tables:
        path = rows_dir / f"{table.name}.jsonl"
        if not path.is_file() or path.is_symlink():
            continue
        try:
            rows, total = _read_rows(path, MAX_ROWS_PER_TABLE)
        except OSError:
            continue
        lines.append(f"- {table.name}: {total} row(s) here")
        shown = table.columns[:MAX_COLUMNS_PER_ROW]
        hidden = len(table.columns) - len(shown)
        for row in rows:
            cells = ", ".join(
                f"{column.name}={_cell(row.get(column.name))}" for column in shown
            )
            if hidden > 0:
                cells += f", (+{hidden} more column(s))"
            lines.append(f"    example row: {cells}")
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > MAX_SNAPSHOT_CHARS:
        text = text[:MAX_SNAPSHOT_CHARS].rsplit("\n", 1)[0] + "\n" + _TRUNCATED
    return text

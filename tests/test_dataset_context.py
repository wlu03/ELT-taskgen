"""The author's dataset context: bounded DEVELOPMENT rows, and nothing else.

Without rows the author described two tasks of one library shape in the same
sentences, and the contamination firewall queued those repeats as borderline
collisions between our own tasks (batch50, 2026-09-19). The rows come from the
development population, the one a solver's own sources are seeded with; a
graded population's rows never reach the view.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from elt_taskgen import demo_fixture
from elt_taskgen.generation import dataset_context
from elt_taskgen.models import CouncilRole, PopulationName
from elt_taskgen.review import council


def _write_rows(root: Path, population: str, table: str, rows: list[dict]) -> None:
    directory = root / population / "rows"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{table}.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


class DevelopmentSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.task = demo_fixture.demo_task()
        self._tmp = tempfile.TemporaryDirectory()
        self.populations = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.table = self.task.tables[0]
        self.columns = [column.name for column in self.table.columns]

    def _row(self, index: int) -> dict:
        return {name: f"{name}-{index}" for name in self.columns}

    def test_no_materialized_population_reads_as_no_context(self) -> None:
        self.assertEqual(
            dataset_context.development_snapshot(self.task, self.populations), ""
        )

    def test_rows_are_bounded_and_the_count_is_the_whole_file(self) -> None:
        rows = [self._row(i) for i in range(dataset_context.MAX_ROWS_PER_TABLE + 4)]
        _write_rows(self.populations, PopulationName.DEVELOPMENT.value, self.table.name, rows)
        snapshot = dataset_context.development_snapshot(self.task, self.populations)

        examples = [line for line in snapshot.splitlines() if "example row:" in line]
        self.assertEqual(len(examples), dataset_context.MAX_ROWS_PER_TABLE)
        self.assertIn(f"- {self.table.name}: {len(rows)} row(s) here", snapshot)
        self.assertIn(f"{self.columns[0]}={self.columns[0]}-0", snapshot)
        self.assertNotIn(f"{self.columns[0]}-{len(rows) - 1}", snapshot)

    def test_a_long_value_and_a_wide_table_are_truncated(self) -> None:
        long_value = "x" * (dataset_context.MAX_CELL_CHARS + 50)
        row = {name: long_value for name in self.columns}
        _write_rows(self.populations, PopulationName.DEVELOPMENT.value, self.table.name, [row])
        snapshot = dataset_context.development_snapshot(self.task, self.populations)

        self.assertNotIn(long_value, snapshot)
        self.assertIn("x" * 10 + "...", snapshot)
        hidden = len(self.table.columns) - dataset_context.MAX_COLUMNS_PER_ROW
        if hidden > 0:
            self.assertIn(f"(+{hidden} more column(s))", snapshot)
            self.assertNotIn(self.columns[-1] + "=", snapshot)

    def test_only_the_development_population_is_read(self) -> None:
        _write_rows(
            self.populations, PopulationName.DEVELOPMENT.value, self.table.name,
            [{name: "development" for name in self.columns}],
        )
        for graded in (
            PopulationName.PRIMARY,
            PopulationName.RESAMPLED,
            PopulationName.COUNTERFACTUAL,
            PopulationName.STRESS,
        ):
            _write_rows(
                self.populations, graded.value, self.table.name,
                [{name: f"graded-{graded.value}" for name in self.columns}],
            )
        snapshot = dataset_context.development_snapshot(self.task, self.populations)

        self.assertIn("development", snapshot)
        for graded in ("primary", "resampled", "counterfactual", "stress"):
            self.assertNotIn(f"graded-{graded}", snapshot)

    def test_the_snapshot_is_deterministic(self) -> None:
        for table in self.task.tables:
            _write_rows(
                self.populations, PopulationName.DEVELOPMENT.value, table.name,
                [{c.name: f"{c.name}-{i}" for c in table.columns} for i in range(3)],
            )
        first = dataset_context.development_snapshot(self.task, self.populations)
        second = dataset_context.development_snapshot(self.task, self.populations)
        self.assertEqual(first, second)
        self.assertTrue(first)


class AuthorViewCarriesTheSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.task = demo_fixture.demo_task()
        self.snapshot = "- orders: 7 row(s) here\n    example row: id=1, status=paid"

    def test_the_author_sees_the_rows_and_the_caution(self) -> None:
        view = council.render_view(
            CouncilRole.SEMANTIC_AUTHOR, self.task, self.snapshot
        )
        self.assertIn("DEVELOPMENT DATA", view)
        self.assertIn("example row: id=1, status=paid", view)
        self.assertIn("never state one as a rule", view)

    def test_without_a_snapshot_the_author_view_is_unchanged(self) -> None:
        view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, self.task)
        self.assertNotIn("DEVELOPMENT DATA", view)
        self.assertNotIn("never state one as a rule", view)

    def test_no_critic_view_moves(self) -> None:
        """The critic views are the public bundle exactly, and the metrology
        view digest hashes them: a snapshot must not reach them."""
        for role in CouncilRole:
            if role is CouncilRole.SEMANTIC_AUTHOR:
                continue
            with self.subTest(role=role.value):
                self.assertEqual(
                    council.render_view(role, self.task),
                    council.render_view(role, self.task, self.snapshot),
                )


if __name__ == "__main__":
    unittest.main()

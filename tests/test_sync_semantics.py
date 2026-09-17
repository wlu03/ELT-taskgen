"""DuckDB sync-semantics outcome specifications for the declared scope (IR-009).

WHY THIS EXISTS
`full_refresh_append` is the only certified sync mode
(`elt_taskgen.destinations.SUPPORTED_SYNC_MODES`;
docs/WAREHOUSE_CONNECTORS.md "Sync-mode scope"), and until now nothing
executable modeled what that mode — or the deliberately out-of-scope modes —
must DO to the data. These tests run the trusted loader over REAL rendered
population artifacts from a built drive under `runs/` and pin the semantic
outcome contracts:

  * replaying one additional ``full_refresh_append`` sync doubles every
    table's rows exactly, deterministically across fresh connections;
  * overwrite is truncate-and-reload: drop + recreate + single ingest must
    restore the exact single-sync state;
  * append-dedupe restores single-sync counts only for tables whose baseline
    rows are fully distinct — the precondition any future dedupe mode must
    declare; and
  * the loader fails closed on schema drift rather than silently adapting.

SCOPE. These are semantic outcome specifications on the DuckDB plane, NOT
Airbyte or dbt connector certification evidence: proving the pinned connector
images produce these outcomes on a real warehouse stays with the pinned
real-runtime certification path. Everything under `runs/` is a frozen release
and is read STRICTLY read-only here; every DuckDB connection is in-memory.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import duckdb

from elt_taskgen.models import task_from_json
from elt_taskgen.reference.solution import (
    append_sources_duckdb,
    load_sources_duckdb,
)

REPO = Path(__file__).resolve().parents[1]


def _built_workspace() -> tuple[Path, str] | None:
    """The first real drive under runs/ with a task carrying rendered primary data.

    Mirrors tests/test_gates_perturbation.py: drives carrying ``dbt_builds/``
    are deprioritized (they are far larger and hold vendored symlinks), and the
    first qualifying task of the first qualifying drive is used rather than
    pinning a pool.
    """
    candidates = sorted(
        (REPO / "runs").glob("*"),
        key=lambda p: ((p / "dbt_builds").is_dir(), p.name),
    )
    for ws in candidates:
        tasks = ws / "tasks"
        if not tasks.is_dir():
            continue
        for task_dir in sorted(tasks.glob("*")):
            rendered = task_dir / "populations" / "primary" / "rendered"
            if (task_dir / "task_ir.json").is_file() and rendered.is_dir():
                return ws, task_dir.name
    return None


_BUILT = _built_workspace()
BUILT_WORKSPACE = _BUILT[0] if _BUILT else REPO / "runs" / "<none>"
BUILT_TASK_ID = _BUILT[1] if _BUILT else "<none>"
POPULATION = "primary"


def workspace_available() -> bool:
    return _BUILT is not None


@unittest.skipUnless(workspace_available(), "no built workspace under runs/")
class SyncSemanticsTests(unittest.TestCase):
    """Outcome specs on real rendered data; runs/ is never written."""

    @classmethod
    def setUpClass(cls) -> None:
        task_dir = BUILT_WORKSPACE / "tasks" / BUILT_TASK_ID
        cls.task = task_from_json(
            (task_dir / "task_ir.json").read_text(encoding="utf-8")
        )
        cls.rendered = task_dir / "populations" / POPULATION / "rendered"
        cls.context = (
            f"task={BUILT_TASK_ID} population={POPULATION} "
            f"workspace={BUILT_WORKSPACE}"
        )

    def load_once(self) -> tuple[duckdb.DuckDBPyConnection, dict[str, int]]:
        con = duckdb.connect(":memory:")
        self.addCleanup(con.close)
        loaded = load_sources_duckdb(self.task, self.rendered, con)
        return con, dict(loaded.counts)

    def count(self, con: duckdb.DuckDBPyConnection, table: str) -> int:
        (value,) = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(value)

    def distinct_count(self, con: duckdb.DuckDBPyConnection, table: str) -> int:
        (value,) = con.execute(
            f'SELECT COUNT(*) FROM (SELECT DISTINCT * FROM "{table}")'
        ).fetchone()
        return int(value)

    def test_full_refresh_append_replay_doubles_every_table_exactly(self) -> None:
        con, baseline = self.load_once()
        self.assertTrue(
            any(count > 0 for count in baseline.values()),
            f"baseline loaded no rows; {self.context}",
        )
        appended = append_sources_duckdb(self.task, self.rendered, con)
        for table, count in baseline.items():
            self.assertEqual(
                appended.counts[table],
                2 * count,
                f"replayed full_refresh_append must exactly double {table!r}; "
                f"{self.context}",
            )
        # Determinism: the same replay on a fresh connection lands on the
        # identical counts.
        second_con, second_baseline = self.load_once()
        self.assertEqual(
            second_baseline, baseline, f"baseline load drifted; {self.context}"
        )
        second_appended = append_sources_duckdb(
            self.task, self.rendered, second_con
        )
        self.assertEqual(
            second_appended.counts,
            appended.counts,
            f"replayed append drifted between connections; {self.context}",
        )

    def test_overwrite_spec_is_truncate_and_reload(self) -> None:
        con, baseline = self.load_once()
        append_sources_duckdb(self.task, self.rendered, con)
        # Overwrite = drop + recreate + single ingest; load_sources_duckdb IS
        # create_table + single ingest per table.
        for table in self.task.tables:
            con.execute(f'DROP TABLE "{table.name}"')
        reloaded = load_sources_duckdb(self.task, self.rendered, con)
        self.assertEqual(
            reloaded.counts,
            baseline,
            f"overwrite must restore the single-sync counts; {self.context}",
        )
        # Exact row equality on the smallest nonempty table against an
        # untouched single-load connection.
        fresh_con, _fresh_baseline = self.load_once()
        smallest = min(
            (name for name, count in baseline.items() if count > 0),
            key=lambda name: baseline[name],
        )
        query = f'SELECT * FROM "{smallest}" ORDER BY ALL'
        self.assertEqual(
            con.execute(query).fetchall(),
            fresh_con.execute(query).fetchall(),
            f"overwrite of {smallest!r} must reproduce the exact single-sync "
            f"rows; {self.context}",
        )

    def test_append_dedupe_spec_requires_distinct_baseline_rows(self) -> None:
        single_con, baseline = self.load_once()
        doubled_con, _ = self.load_once()
        append_sources_duckdb(self.task, self.rendered, doubled_con)
        for table, count in baseline.items():
            if count == 0:
                continue
            distinct_baseline = self.distinct_count(single_con, table)
            distinct_doubled = self.distinct_count(doubled_con, table)
            # DISTINCT over baseline+baseline is DISTINCT over baseline.
            self.assertEqual(
                distinct_doubled,
                distinct_baseline,
                f"dedupe over the doubled state must equal the baseline "
                f"distinct set for {table!r}; {self.context}",
            )
            if distinct_baseline == count:
                self.assertEqual(
                    distinct_doubled,
                    count,
                    f"dedupe restores single-sync state for the fully "
                    f"distinct table {table!r}; {self.context}",
                )
            else:
                self.assertLess(
                    distinct_doubled,
                    count,
                    f"dedupe UNDER-recovers a table with duplicate baseline "
                    f"rows ({table!r}) — the precondition any future "
                    f"append-dedupe mode must declare; {self.context}",
                )

    def test_schema_drift_fails_the_append_closed(self) -> None:
        con, _baseline = self.load_once()
        drifted = next(
            (table for table in self.task.tables if len(table.columns) >= 2),
            None,
        )
        self.assertIsNotNone(
            drifted, f"no multi-column table to drift; {self.context}"
        )
        dropped = drifted.columns[-1].name
        con.execute(
            f'ALTER TABLE "{drifted.name}" DROP COLUMN "{dropped}"'
        )
        with self.assertRaises(
            (duckdb.Error, ValueError),
            msg=(
                f"append after dropping {drifted.name}.{dropped} must fail "
                f"closed, not adapt; {self.context}"
            ),
        ):
            append_sources_duckdb(self.task, self.rendered, con)


if __name__ == "__main__":
    unittest.main()

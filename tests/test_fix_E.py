"""Fix-round (group E) tests that belong to files this group does not own.

  * N-mart_plan-5 (b): the argmax text tie-break is case-sensitive and the
    reference session pins it — the exposure `_pin_session` closes is shown
    on a raw connection (would live in tests/test_argmax_key_type.py).
  * N-mart_plan-5 (c): the collation qualifier sentence is declarative prose
    (would live in tests/test_declarative_prose.py).
  * G6 regression: an extremum whose winning row carries a NULL label on a
    FILES-backend bridge reports the documented '(none)', not '' — the trusted
    loader reads a CSV empty field as NULL like every served-world reader.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import duckdb

# Sibling-module import works under `discover -s tests` AND under
# `unittest tests.test_fix_E` (package-style) — both are used in this repo.
try:
    import test_plan_library as tpl
except ModuleNotFoundError:  # package-style invocation
    from tests import test_plan_library as tpl

from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.models import Backend, BackendAssignment, PopulationName
from elt_taskgen.reference import runner as runner_mod
from elt_taskgen.reference import solution as ref
from elt_taskgen.review import declarative_prose

P = PopulationName


def _argmax_task():
    built = mp.argmax_profile(tpl.EVIDENCE, mart="argmax_case_mart")
    task = tpl.build_task(built, "proof__argmax_case")
    return task, task.marts[0]


class ArgmaxTieBreakCollation(unittest.TestCase):
    """The winner of a label tie is a function of the input, not of the
    reader's collation: 'Zulu' < 'alpha' under the pinned binary order."""

    ROWS = (
        # subscription_id, account_id, plan_id, sub_status, sub_label, amount, started_at
        (1, 1, None, "paid", "Zulu", 5, "2024-01-01"),
        (2, 1, None, "paid", "alpha", 5, "2024-01-02"),
        (3, 2, None, "paid", "Van", 5, "2024-01-01"),
        (4, 2, None, "paid", "de Kleijn", 5, "2024-01-02"),
    )

    def _load(self, con: duckdb.DuckDBPyConnection, task) -> None:
        for table in task.tables:
            ref.create_table(con, table)
        con.execute("INSERT INTO accounts VALUES (1, 'one', 'active'), (2, 'two', 'trial')")
        con.executemany("INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?)", list(self.ROWS))

    def _winners(self, con: duckdb.DuckDBPyConnection, task, mart, sql) -> dict:
        return {
            r["parent_key"]: (r["top_label"], r["top_row_id"])
            for r in ref.execute_mart(con, mart, sql)
        }

    def test_argmax_tie_break_is_case_sensitive_and_session_independent(self) -> None:
        task, mart = _argmax_task()
        sql = ref.compile_plan_sql(task, mart)
        pinned = runner_mod.open_reference_connection()
        try:
            self._load(pinned, task)
            self.assertEqual(
                {1: ("Zulu", 1), 2: ("Van", 3)}, self._winners(pinned, task, mart, sql)
            )
        finally:
            pinned.close()
        # The exposure the pin closes: the SAME SQL under a case-insensitive
        # session picks the other row.
        raw = duckdb.connect(":memory:")
        try:
            raw.execute("SET default_collation = 'nocase'")
            self._load(raw, task)
            self.assertEqual(
                {1: ("alpha", 2), 2: ("de Kleijn", 4)}, self._winners(raw, task, mart, sql)
            )
            runner_mod._pin_session(raw)
            self.assertEqual(
                {1: ("Zulu", 1), 2: ("Van", 3)}, self._winners(raw, task, mart, sql)
            )
        finally:
            raw.close()
        # And the shipped prose says so, in words the author must echo.
        top_label = next(c for c in mart.columns if c.name == "top_label")
        self.assertIn("case-sensitive", top_label.description)
        self.assertIn("uppercase letter sorts before every lowercase", top_label.description)


class CollationProseIsDeclarative(unittest.TestCase):
    def test_declarative_prose_accepts_the_case_sensitivity_qualifier(self) -> None:
        task, mart = _argmax_task()
        identifiers = declarative_prose._task_identifiers(task)
        self.assertEqual([], declarative_prose.operator_problems(mp.TEXT_ORDER_PROSE, identifiers))
        for column in mart.columns:
            with self.subTest(column=column.name):
                self.assertEqual(
                    [], declarative_prose.operator_problems(column.description, identifiers)
                )
        # A prose author echoing the rule verbatim is not flagged either.
        echo = (
            "Ties in amount go to the smallest sub_label under a plain case-sensitive "
            "comparison of the stored text: every uppercase letter sorts before every "
            "lowercase one, so 'Zulu' outranks 'alpha'."
        )
        self.assertEqual([], declarative_prose.operator_problems(echo, identifiers))


class NullLabelOnFilesBridge(unittest.TestCase):
    """G6 regression: the FILES renderer writes a TEXT NULL as an empty CSV
    field; the trusted loader must read it back as NULL so the extremum's
    winner reports the documented '(none)' rather than ''."""

    def test_extremum_top_label_null_on_files_bridge(self) -> None:
        task, mart = _argmax_task()
        task = task.model_copy(
            update={
                "tables": tpl._nullable(task.tables, "subscriptions", "sub_label"),
                "backends": tuple(
                    BackendAssignment(table=t.name, backend=Backend.FILES) for t in task.tables
                ),
            }
        )
        rows = {
            "accounts": [
                {"account_id": 1, "account_name": "one", "account_status": "active"},
                {"account_id": 2, "account_name": "two", "account_status": "trial"},
            ],
            "subscriptions": [
                # Account 1: the unique winner carries NO label -> '(none)'.
                {"subscription_id": 1, "account_id": 1, "plan_id": None,
                 "sub_status": "paid", "sub_label": None, "amount": 9,
                 "started_at": "2024-01-01"},
                {"subscription_id": 2, "account_id": 1, "plan_id": None,
                 "sub_status": "paid", "sub_label": "beta", "amount": 3,
                 "started_at": "2024-01-02"},
                # Account 2: a labelled winner, and a NULL label that loses.
                {"subscription_id": 3, "account_id": 2, "plan_id": None,
                 "sub_status": "paid", "sub_label": "gamma", "amount": 7,
                 "started_at": "2024-01-01"},
                {"subscription_id": 4, "account_id": 2, "plan_id": None,
                 "sub_status": "paid", "sub_label": None, "amount": 1,
                 "started_at": "2024-01-02"},
            ],
            "plans": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            files_dir = (
                workspace / "tasks" / task.task_id / "populations" / P.DEVELOPMENT.value
                / "rendered" / "files"
            )
            files_dir.mkdir(parents=True)
            for table in task.tables:
                columns = [c.name for c in table.columns]
                with (files_dir / f"{table.name}.csv").open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(columns)
                    for row in rows[table.name]:
                        # What generation/source_data.render_files does: None -> ''.
                        writer.writerow(["" if row[c] is None else row[c] for c in columns])
            result = runner_mod.run_reference(task, P.DEVELOPMENT, workspace)
        by_key = {r["parent_key"]: r for r in result.mart_rows[mart.name]}
        self.assertEqual("(none)", by_key[1]["top_label"])
        self.assertEqual(1, by_key[1]["top_row_id"])
        self.assertEqual("gamma", by_key[2]["top_label"])
        # And the loader agrees with every agent-side reader on the count.
        con = duckdb.connect(":memory:")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "s.csv"
                path.write_text("sub_label\n\nbeta\n", encoding="utf-8")
                (n,) = con.execute(
                    f"SELECT COUNT(sub_label) FROM read_csv('{path}', header=true, "
                    "columns={'sub_label': 'VARCHAR'})"
                ).fetchone()
                loaded = ref._read_csv(path)
        finally:
            con.close()
        self.assertEqual(1, n)
        self.assertEqual(1, sum(1 for r in loaded if r["sub_label"] is not None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Regression tests for private row-level independent-build adjudication."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import duckdb

from elt_taskgen import cli, demo_fixture
from elt_taskgen.generation import source_data
from elt_taskgen.models import PopulationName, task_to_json
from elt_taskgen.reference import adjudication
from elt_taskgen.reference import gold as gold_mod
from elt_taskgen.reference import independent
from elt_taskgen.reference import runner as runner_mod


class RowDifferenceTests(unittest.TestCase):
    def test_childless_all_null_and_mixed_rows_use_real_child_identity(self) -> None:
        """Execute the exact COUNT boundaries from the cache adjudication."""

        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE caches(cache_id INTEGER PRIMARY KEY, cache_name VARCHAR)"
        )
        connection.execute(
            "CREATE TABLE cache_usage(usage_id INTEGER PRIMARY KEY, "
            "cache_id INTEGER, hits INTEGER)"
        )
        connection.executemany(
            "INSERT INTO caches VALUES (?, ?)",
            [(1, "childless"), (2, "two_null_hits"), (3, "mixed_hits")],
        )
        connection.executemany(
            "INSERT INTO cache_usage VALUES (?, ?, ?)",
            [(20, 2, None), (21, 2, None), (30, 3, None), (31, 3, 7)],
        )

        absent_correct = dict(
            connection.execute(
                "SELECT c.cache_id, COUNT(u.usage_id) "
                "FROM caches c LEFT JOIN cache_usage u "
                "ON c.cache_id = u.cache_id WHERE u.hits IS NULL "
                "GROUP BY c.cache_id ORDER BY c.cache_id"
            ).fetchall()
        )
        absent_historical = dict(
            connection.execute(
                "SELECT c.cache_id, COUNT(*) "
                "FROM caches c LEFT JOIN cache_usage u "
                "ON c.cache_id = u.cache_id WHERE u.hits IS NULL "
                "GROUP BY c.cache_id ORDER BY c.cache_id"
            ).fetchall()
        )
        all_children_correct = dict(
            connection.execute(
                "SELECT c.cache_id, COUNT(u.usage_id) "
                "FROM caches c LEFT JOIN cache_usage u "
                "ON c.cache_id = u.cache_id "
                "GROUP BY c.cache_id ORDER BY c.cache_id"
            ).fetchall()
        )
        all_children_historical = dict(
            connection.execute(
                "SELECT c.cache_id, COUNT(*) FILTER (WHERE u.hits IS NOT NULL) "
                "FROM caches c LEFT JOIN cache_usage u "
                "ON c.cache_id = u.cache_id "
                "GROUP BY c.cache_id ORDER BY c.cache_id"
            ).fetchall()
        )

        # Absent-state rows: childless=0, both-null=2, mixed=one null row.
        self.assertEqual(absent_correct, {1: 0, 2: 2, 3: 1})
        self.assertEqual(absent_historical, {1: 1, 2: 2, 3: 1})
        # Total usage records: childless=0, both-null=2, mixed=2.
        self.assertEqual(all_children_correct, {1: 0, 2: 2, 3: 2})
        self.assertEqual(all_children_historical, {1: 0, 2: 0, 3: 1})

    def test_null_measure_rows_are_real_children_but_placeholder_is_not(self) -> None:
        """The cache-management failure, reduced to four hand-checkable rows."""

        columns = (
            "parent_key",
            "child_count",
            "top_measure",
            "tie_state",
        )
        expected = [
            {
                "parent_key": "childless",
                "child_count": "0",
                "top_measure": "0",
                "tie_state": "empty",
            },
            {
                "parent_key": "two_null_hits",
                "child_count": "2",
                "top_measure": "0",
                "tie_state": "empty",
            },
        ]
        # The recorded witness used COUNT(hits): it counts neither real row
        # whose hits is NULL.  Its separate distribution query used COUNT(*):
        # that counts the synthetic LEFT JOIN placeholder as one child.
        actual = [
            {
                "parent_key": "childless",
                "child_count": "1",
                "top_measure": "0",
                "tie_state": "empty",
            },
            {
                "parent_key": "two_null_hits",
                "child_count": "0",
                "top_measure": "0",
                "tie_state": "empty",
            },
        ]

        differences, omitted = adjudication._row_differences(  # noqa: SLF001
            expected_rows=expected,
            actual_rows=actual,
            columns=columns,
            key_columns=("parent_key",),
            limit=10,
        )

        self.assertEqual(omitted, 0)
        self.assertEqual(len(differences), 2)
        self.assertEqual(
            {item.grain_key["parent_key"] for item in differences},
            {"childless", "two_null_hits"},
        )
        self.assertTrue(all(item.kind == "value_mismatch" for item in differences))
        self.assertTrue(
            all([cell.column for cell in item.cells] == ["child_count"] for item in differences)
        )

    def test_diagnostic_uses_the_production_numeric_tolerance(self) -> None:
        differences, omitted = adjudication._row_differences(  # noqa: SLF001
            expected_rows=[{"id": "1", "ratio": "1.0"}],
            actual_rows=[{"id": "1", "ratio": "1.005"}],
            columns=("id", "ratio"),
            key_columns=("id",),
            limit=10,
        )
        self.assertEqual(differences, ())
        self.assertEqual(omitted, 0)


class AnalysisPersistenceTests(unittest.TestCase):
    def test_analysis_is_pending_content_addressed_and_idempotent(self) -> None:
        analysis = adjudication.IndependentDisagreementAnalysis(
            task_id="synthetic__cache_case",
            task_content_hash="a" * 64,
            independent_build_sha256="b" * 64,
            gold_manifest_sha256="c" * 64,
            witness_sql_sha256={"mart": "d" * 64},
            analysis_status=adjudication.ANALYSIS_STATUS_DIFFERENCES,
            marts=(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = adjudication.persist_analysis(workspace, analysis)
            before = first.read_bytes()
            second = adjudication.persist_analysis(workspace, analysis)

            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), before)
            self.assertIn(analysis.digest(), first.name)
            self.assertEqual(
                analysis.adjudication_status,
                adjudication.ADJUDICATION_STATUS_PENDING,
            )

    def test_engineering_diagnosis_is_bound_pending_and_has_no_gate_effect(self):
        analysis = adjudication.IndependentDisagreementAnalysis(
            task_id="synthetic__cache_case",
            task_content_hash="a" * 64,
            independent_build_sha256="b" * 64,
            gold_manifest_sha256="c" * 64,
            witness_sql_sha256={"mart": "d" * 64},
            analysis_status=adjudication.ANALYSIS_STATUS_DIFFERENCES,
            marts=(
                adjudication.MartDisagreement(
                    population="primary",
                    mart="cache_summary",
                    comparator_match=False,
                    expected_row_count=1,
                    actual_row_count=1,
                    differences=(
                        adjudication.RowDifference(
                            kind="value_mismatch",
                            grain_key={"cache_id": 1},
                        ),
                    ),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            analysis_path = adjudication.persist_analysis(workspace, analysis)
            first = adjudication.persist_diagnosis(
                workspace,
                analysis_path,
                determined_cause="witness",
                determination_basis=(
                    "A two-row reduction contradicts the public row-count rule."
                ),
            )
            second = adjudication.persist_diagnosis(
                workspace,
                analysis_path,
                determined_cause="witness",
                determination_basis=(
                    "A two-row reduction contradicts the public row-count rule."
                ),
            )
            diagnosis = adjudication.IndependentDisagreementDiagnosis.model_validate_json(
                first.read_bytes()
            )

            self.assertEqual(first, second)
            self.assertEqual(diagnosis.analysis_sha256, analysis.digest())
            self.assertEqual(diagnosis.determined_cause, "witness")
            self.assertEqual(diagnosis.adjudication_status, "pending_adjudication")
            self.assertEqual(diagnosis.gate_effect, "none")


class EndToEndAdjudicationTests(unittest.TestCase):
    def test_cli_replays_and_diagnoses_without_rewriting_gold_or_witness(self):
        """Exercise the real local loader, SQL runner, comparator, and stores."""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            base = demo_fixture.demo_task()
            literal_rows = {
                table: tuple(dict(row) for row in rows)
                for table, rows in demo_fixture.COUNTERFACTUAL_LITERAL_ROWS.items()
            }
            populations = tuple(
                population.model_copy(
                    update={
                        "scale": {},
                        "literal_rows": literal_rows,
                        "conditions": (
                            "Hand-checkable C10/C11/C12 adjudication fixture.",
                        ),
                    }
                )
                for population in base.populations
            )
            task = base.model_copy(update={"populations": populations})
            task_root = workspace / "tasks" / task.task_id
            task_root.mkdir(parents=True)
            (task_root / "task_ir.json").write_text(
                task_to_json(task), encoding="utf-8"
            )

            for population in PopulationName:
                source_data.materialize_population(
                    task,
                    population,
                    task_root / "populations" / population.value,
                )
            results = {
                population: runner_mod.run_reference(task, population, workspace)
                for population in PopulationName
            }
            gold_mod.freeze_gold(task, results, task_root / "answer_key")

            inner_join_sql = next(
                case.mutation
                for case in task.attack_cases
                if case.name == "inner_join"
            )
            rewards = {population.value: 0.0 for population in PopulationName}
            result = independent.IndependentBuildResult(
                task_id=task.task_id,
                task_content_hash=task.content_hash(),
                status=independent.STATUS_NEEDS_ADJUDICATION,
                agreement=rewards,
                samples=(
                    independent.IndependentSample(
                        sample_index=0,
                        prompt_sha256="1" * 64,
                        sql_by_mart={demo_fixture.MART_NAME: inner_join_sql},
                        rewards=rewards,
                        dev_pass=False,
                    ),
                ),
                detail=(
                    "The independent LEFT-preservation interpretation disagrees "
                    "with the recorded witness output."
                ),
            )
            witness_path = independent.record_build_result(workspace, task, result)
            queue_path = independent.adjudication_path(workspace, task.task_id)

            def tree_bytes(root: Path) -> dict[str, bytes]:
                return {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in sorted(root.rglob("*"))
                    if path.is_file()
                }

            gold_before = tree_bytes(task_root / "answer_key")
            witness_before = witness_path.read_bytes()
            queue_before = queue_path.read_bytes()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = adjudication.main(
                    [
                        "--workspace",
                        str(workspace),
                        "--task-id",
                        task.task_id,
                        "--max-differences-per-mart",
                        "10",
                        "--determined-cause",
                        "witness",
                        "--determination-basis",
                        (
                            "The three-row example requires retaining C10 and "
                            "C12, while the INNER JOIN witness drops both."
                        ),
                        "--adjudicate-witness-error",
                        "--adjudicator",
                        "test:operator",
                    ]
                )
            self.assertEqual(exit_code, 0)
            output = json.loads(stdout.getvalue())
            analysis_path = workspace / output["report"]
            diagnosis_path = workspace / output["diagnosis"]
            decision_path = workspace / output["decision"]
            analysis = adjudication.IndependentDisagreementAnalysis.model_validate_json(
                analysis_path.read_bytes()
            )
            diagnosis = adjudication.IndependentDisagreementDiagnosis.model_validate_json(
                diagnosis_path.read_bytes()
            )
            decision = adjudication.IndependentDisagreementDecision.model_validate_json(
                decision_path.read_bytes()
            )

            self.assertEqual(analysis.analysis_status, "differences_confirmed")
            self.assertEqual(analysis.adjudication_status, "pending_adjudication")
            self.assertEqual(analysis.task_content_hash, task.content_hash())
            self.assertEqual(
                analysis.independent_build_sha256,
                hashlib.sha256(witness_before).hexdigest(),
            )
            self.assertEqual(
                analysis.gold_manifest_sha256,
                hashlib.sha256(
                    (task_root / "answer_key" / "manifest.json").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                analysis.witness_sql_sha256,
                {
                    demo_fixture.MART_NAME: hashlib.sha256(
                        inner_join_sql.encode("utf-8")
                    ).hexdigest()
                },
            )
            self.assertEqual(analysis.stage1_mismatches, {})
            self.assertEqual(len(analysis.marts), len(PopulationName))
            for mart in analysis.marts:
                self.assertFalse(mart.comparator_match)
                self.assertEqual(mart.expected_row_count, 3)
                self.assertEqual(mart.actual_row_count, 1)
                self.assertEqual(
                    {difference.kind for difference in mart.differences},
                    {"missing"},
                )
                self.assertEqual(
                    {
                        str(difference.grain_key["customer_id"])
                        for difference in mart.differences
                    },
                    {"10", "12"},
                )

            self.assertEqual(diagnosis.analysis_sha256, analysis.digest())
            self.assertEqual(diagnosis.determined_cause, "witness")
            self.assertEqual(diagnosis.adjudication_status, "pending_adjudication")
            self.assertEqual(diagnosis.gate_effect, "none")
            self.assertEqual(
                decision.action,
                adjudication.DECISION_ACTION_NEW_BLIND_BUILD,
            )
            self.assertEqual(decision.gate_effect, "authorize_fresh_build_only")
            self.assertEqual(decision.analysis_sha256, analysis.digest())
            self.assertEqual(decision.diagnosis_sha256, diagnosis.digest())
            self.assertEqual(
                adjudication.load_fresh_build_decision(workspace, task),
                decision,
            )
            self.assertEqual(output["recorded_row_differences"], 10)
            self.assertEqual(tree_bytes(task_root / "answer_key"), gold_before)
            self.assertEqual(witness_path.read_bytes(), witness_before)
            self.assertEqual(queue_path.read_bytes(), queue_before)

            # A subsequently recorded attempt preserves the disputed bytes and
            # makes the one-witness decision stale, rather than authorizing an
            # unbounded retry loop.
            replacement = result.model_copy(
                update={"detail": "a separately keyed fresh blind attempt"}
            )
            with mock.patch.object(
                independent,
                "run_independent_build",
                return_value=replacement,
            ) as run_build:
                note = cli._ensure_independent_build(  # noqa: SLF001
                    SimpleNamespace(workspace=workspace),
                    task,
                    gold_mod.load_gold(task_root / "answer_key"),
                    object(),
                )
            self.assertIn("fresh blind generation=1", note)
            self.assertEqual(run_build.call_args.kwargs["recovery_generation"], 1)
            self.assertIsNone(
                adjudication.load_fresh_build_decision(workspace, task)
            )
            archived = list(
                (workspace / "audit").glob(
                    f"{task.task_id}.independent_build_history.*.json"
                )
            )
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), witness_before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

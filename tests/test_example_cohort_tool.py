"""Focused contracts for the fixed example-cohort build/freeze tool."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import demo_fixture
from elt_taskgen.destinations import DESTINATION_CONTRACTS, Destination
from elt_taskgen.export.eltbench import database_name
from elt_taskgen.models import AcceptanceReport, GateResult, RLVR_TASK_VARIANTS


def _load_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "regenerate_example_cohort.py"
    spec = importlib.util.spec_from_file_location("_regenerate_example_cohort", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


class ExampleCohortToolTests(unittest.TestCase):
    @staticmethod
    def _fixed_catalog_rows() -> list[dict]:
        return [
            {
                "alias": alias,
                "source": alias.rsplit("_", 1)[0],
            }
            for alias in sorted(tool.EXPECTED_ALIASES)
        ]

    def test_destination_connector_snapshot_is_fully_pinned(self) -> None:
        self.assertEqual(
            {
                destination.value: contract.connector_version
                for destination, contract in DESTINATION_CONTRACTS.items()
            },
            {
                "snowflake": "4.1.2",
                "databricks": "4.0.2",
                "redshift": "4.0.7",
            },
        )

    def test_release_workspace_clone_copies_mutable_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "state").mkdir(parents=True)
            ledger = source / "state" / "taskgen.sqlite"
            ledger.write_bytes(b"accepted-ledger")
            (source / "evidence.txt").write_text("immutable", encoding="utf-8")

            clone = root / "clone"
            tool._clone_workspace_for_release(source, clone)
            (clone / "state" / "taskgen.sqlite").write_bytes(b"overlay-ledger")

            self.assertEqual(ledger.read_bytes(), b"accepted-ledger")
            self.assertEqual(
                (clone / "evidence.txt").read_text(encoding="utf-8"),
                "immutable",
            )

    def test_catalog_entry_binds_destination_and_runtime_namespace(self) -> None:
        task = demo_fixture.demo_task()
        spec = tool.ExampleSpec("dbt_01", "dbt", 1, "Demo", lambda: task)
        for destination in Destination:
            with self.subTest(destination=destination.value):
                row = tool._catalog_entry(spec, task, destination)
                contract = DESTINATION_CONTRACTS[destination]
                self.assertEqual(row["destination"], destination.value)
                self.assertEqual(
                    row["destination_connector_version"],
                    contract.connector_version,
                )
                self.assertEqual(row["runtime_namespace"], database_name(task))
                self.assertEqual(row["difficulty_profile"], "standard")
                self.assertEqual(row["data_provenance"], "generated_synthetic")
                self.assertGreater(row["primary_rows"], 0)
                self.assertGreater(row["stress_rows"], 0)

    def test_challenging_profile_is_visible_in_catalog_and_readme(self) -> None:
        task = demo_fixture.demo_task()
        task = tool.apply_data_scale_profile(
            task, tool.CHALLENGING_DIFFICULTY_PROFILE
        )
        spec = tool.ExampleSpec("dbt_01", "dbt", 1, "Demo", lambda: task)
        row = tool._catalog_entry(
            spec,
            task,
            Destination.SNOWFLAKE,
            difficulty_profile=tool.CHALLENGING_DIFFICULTY_PROFILE,
        )
        catalog = {
            "destination": "snowflake",
            "difficulty_profile": "challenging",
            "difficulty_profile_contract": (
                tool.CHALLENGING_DIFFICULTY_PROFILE.catalog_contract()
            ),
            "tasks": [row],
        }
        readme = tool._readme(catalog)

        self.assertEqual(row["difficulty_profile"], "challenging")
        self.assertGreaterEqual(row["primary_rows"], 32_768)
        self.assertIn("deterministic `challenging` profile", readme)
        self.assertIn("32,768 declared PRIMARY rows", readme)
        self.assertIn("never padded with synthetic data", readme)

    def test_catalog_constructs_exact_combined_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for index, alias in enumerate(sorted(tool.EXPECTED_ALIASES)):
                rows.append(
                    {
                        "alias": alias,
                        "canonical_id": f"task_{index}",
                        "task_content_hash": f"hash_{index}",
                        "split": "val" if index == 0 else "train",
                    }
                )
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"tasks": rows}), encoding="utf-8")

            selection, _ = tool._selection_from_catalog(catalog)

            self.assertEqual(len(selection.train), 14)
            self.assertEqual(len(selection.val), 1)
            required = tuple(variant.value for variant in RLVR_TASK_VARIANTS)
            self.assertEqual(set(selection.variants), {f"task_{i}" for i in range(15)})
            self.assertTrue(all(value == required for value in selection.variants.values()))

    def test_reference_validator_requires_exact_aliases_and_source_distribution(self) -> None:
        rows = self._fixed_catalog_rows()
        self.assertEqual(tool._validate_fixed_cohort_catalog(rows), rows)

        wrong_alias = copy.deepcopy(rows)
        wrong_alias[0]["alias"] = "dbt_99"
        with self.assertRaisesRegex(RuntimeError, "exact fixed cohort aliases"):
            tool._validate_fixed_cohort_catalog(wrong_alias)

        wrong_distribution = copy.deepcopy(rows)
        wrong_distribution[0]["source"] = "dlt"
        with self.assertRaisesRegex(RuntimeError, "three tasks from each source pool"):
            tool._validate_fixed_cohort_catalog(wrong_distribution)

    def test_reference_validator_rejects_stale_readiness_roster(self) -> None:
        task = demo_fixture.demo_task()
        gates = tuple(
            GateResult(gate=name, passed=True, details="measured")
            for name in tool.REFERENCE_READINESS_ROSTER
        )
        report = AcceptanceReport.from_gates(
            task_id=task.task_id,
            revision=task.current_revision,
            task_content_hash=task.content_hash(),
            gates=gates,
            scorer_version="test",
            roster_digest=tool.REFERENCE_READINESS_ROSTER_DIGEST,
            roster=tool.REFERENCE_READINESS_ROSTER,
        )
        payload = report.model_dump(mode="json")
        self.assertEqual(
            tool._validate_reference_readiness_payload(
                payload, task, alias="dbt_01"
            ),
            report,
        )

        stale_roster = copy.deepcopy(payload)
        stale_roster["roster"] = stale_roster["roster"][:-1]
        with self.assertRaisesRegex(RuntimeError, "gate roster is stale"):
            tool._validate_reference_readiness_payload(
                stale_roster, task, alias="dbt_01"
            )

        stale_digest = copy.deepcopy(payload)
        stale_digest["roster_digest"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "roster digest is stale"):
            tool._validate_reference_readiness_payload(
                stale_digest, task, alias="dbt_01"
            )

        stale_measurement = copy.deepcopy(payload)
        stale_measurement["gates"] = list(reversed(stale_measurement["gates"]))
        with self.assertRaisesRegex(RuntimeError, "measured gate roster is stale"):
            tool._validate_reference_readiness_payload(
                stale_measurement, task, alias="dbt_01"
            )

    def test_reference_validator_selects_challenging_roster_and_digest(self) -> None:
        task = demo_fixture.demo_task()
        gates = tuple(
            GateResult(gate=name, passed=True, details="measured")
            for name in tool.CHALLENGING_REFERENCE_READINESS_ROSTER
        )
        report = AcceptanceReport.from_gates(
            task_id=task.task_id,
            revision=task.current_revision,
            task_content_hash=task.content_hash(),
            gates=gates,
            scorer_version="test",
            roster_digest=tool.CHALLENGING_REFERENCE_READINESS_ROSTER_DIGEST,
            roster=tool.CHALLENGING_REFERENCE_READINESS_ROSTER,
        )
        payload = report.model_dump(mode="json")

        self.assertEqual(
            tool._validate_reference_readiness_payload(
                payload,
                task,
                alias="dbt_01",
                challenging=True,
            ),
            report,
        )
        with self.assertRaisesRegex(RuntimeError, "gate roster is stale"):
            tool._validate_reference_readiness_payload(
                payload,
                task,
                alias="dbt_01",
                challenging=False,
            )

    def test_challenging_policy_runs_before_reference_engine_is_created(self) -> None:
        task = demo_fixture.demo_task()
        specs = tuple(
            tool.ExampleSpec(alias, alias.split("_", 1)[0], 1, alias, lambda: task)
            for alias in sorted(tool.EXPECTED_ALIASES)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = mock.Mock()
            with (
                mock.patch.object(tool, "cmd_ingest_anchor", return_value=0),
                mock.patch.object(tool, "build_specs", return_value=specs),
                mock.patch.object(
                    tool,
                    "prune_to_effective_lineage",
                    side_effect=lambda value, **_: value,
                ),
                mock.patch.object(
                    tool, "apply_data_scale_profile", side_effect=lambda value, _: value
                ),
                mock.patch.object(
                    tool,
                    "validate_challenging_cohort",
                    side_effect=RuntimeError("policy refusal"),
                ) as policy,
                mock.patch.object(tool, "_engine", return_value=engine) as make_engine,
            ):
                with self.assertRaisesRegex(RuntimeError, "policy refusal"):
                    tool.generate(
                        root,
                        root,
                        root,
                        root / "workspace",
                        root / "output",
                        difficulty_profile=tool.CHALLENGING_DIFFICULTY_PROFILE,
                    )

        policy.assert_called_once()
        make_engine.assert_not_called()

    def test_release_mode_retargets_overlay_and_uses_official_freezer(self) -> None:
        base = demo_fixture.demo_task()
        tasks = {
            f"task_{index}": base.model_copy(update={"task_id": f"task_{index}"})
            for index in range(tool.EXPECTED_TASKS)
        }
        rows = [
            {
                "alias": alias,
                "canonical_id": task_id,
                "task_content_hash": tasks[task_id].content_hash(),
            }
            for alias, task_id in zip(sorted(tool.EXPECTED_ALIASES), tasks, strict=True)
        ]

        class FakeEngine:
            def __init__(self, workspace: Path):
                self.workspace = workspace
                self.closed = False

            def load_task(self, task_id: str):
                return tasks[task_id]

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "accepted"
            workspace.mkdir()
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"tasks": rows}), encoding="utf-8")
            output = root / "release"
            engines: list[FakeEngine] = []
            exported: list[tuple[str, str]] = []

            def fake_clone(_source: Path, destination: Path) -> None:
                destination.mkdir(parents=True)

            def fake_engine(path: Path, **_kwargs):
                engine = FakeEngine(path)
                engines.append(engine)
                return engine

            def fake_export(task, _gold, public, answer_key, *, destination):
                public.mkdir(parents=True, exist_ok=True)
                answer_key.mkdir(parents=True, exist_ok=True)
                exported.append((task.task_id, destination.value))

            def fake_freeze(engine, selection, out, **_kwargs):
                self.assertEqual(len(selection.train) + len(selection.val), 15)
                self.assertTrue(
                    engine.workspace.parent.name.startswith(
                        ".example-release-workspace-"
                    )
                )
                out.mkdir()
                return SimpleNamespace(
                    schema_version="3.2",
                    release_id="release-id",
                    tasks={task_id: task.content_hash() for task_id, task in tasks.items()},
                    destinations={task_id: "redshift" for task_id in tasks},
                )

            with (
                mock.patch.object(tool, "_clone_workspace_for_release", fake_clone),
                mock.patch.object(tool, "_engine", fake_engine),
                mock.patch.object(tool, "load_gold", return_value=object()),
                mock.patch.object(tool, "export_task", fake_export),
                mock.patch.object(tool, "freeze_release", fake_freeze),
                mock.patch.object(
                    tool,
                    "verify_release",
                    return_value=SimpleNamespace(ok=True, failures=(), files_checked=123),
                ),
            ):
                result = tool.freeze_certified_cohort(
                    workspace,
                    catalog,
                    output,
                    destination="redshift",
                )

            self.assertEqual(result["schema_version"], "3.2")
            self.assertEqual(result["destination"], "redshift")
            self.assertEqual(result["task_count"], 15)
            self.assertEqual(len(result["runtime_namespaces"]), 15)
            self.assertEqual(exported, [(task_id, "redshift") for task_id in tasks])
            self.assertTrue(engines[0].closed)


if __name__ == "__main__":
    unittest.main()

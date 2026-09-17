"""Contracts for the taskgen -> original ELT-Bench layout bridge."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from elt_taskgen import demo_fixture
from elt_taskgen.destinations import Destination, destination_contract
from elt_taskgen.export import eltbench
from elt_taskgen.export.eltbench import database_name
from elt_taskgen.export.upstream_layout import (
    UPSTREAM_INPUT_DIRECTORY,
    UpstreamLayoutError,
    discover_task_bundles,
    materialize_upstream_layout,
    project_all_destinations,
)
from elt_taskgen.models import task_to_json


def _json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_public(public: Path, runtime: str, destination: Destination) -> None:
    contract = destination_contract(destination)
    if destination is Destination.SNOWFLAKE:
        config_values = {
            "account": "",
            "database": runtime,
            "password": "",
            "role": "",
            "schema": "AIRBYTE_SCHEMA",
            "username": "",
            "warehouse": "",
        }
    elif destination is Destination.DATABRICKS:
        config_values = {
            "client_id": "",
            "database": "",
            "hostname": "",
            "http_path": "",
            "schema": runtime,
            "secret": "",
        }
    else:
        config_values = {
            "access_key_id": "",
            "database": "",
            "host": "",
            "password": "",
            "port": 5439,
            "schema": runtime,
            "s3_bucket_name": "",
            "s3_bucket_region": "",
            "secret_access_key": "",
            "username": "",
        }
    airbyte = dict(eltbench._AIRBYTE_BASE)
    airbyte[contract.definition_key] = contract.definition_id
    public.mkdir(parents=True, exist_ok=True)
    (public / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "Airbyte": {"config": airbyte},
                contract.config_section: {"config": config_values},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (public / "data_model.yaml").write_text("models: []\n", encoding="utf-8")
    (public / "schemas").mkdir()
    (public / "schemas" / "source.csv").write_text(
        "column_name,column_description\nid,identifier\n", encoding="utf-8"
    )
    eltbench._write_runtime_scaffold(public, demo_fixture.demo_task(), destination)


def _write_answer_key(private: Path, runtime: str, *, value: str = "1") -> None:
    answer = private / "answer_key"
    _json(answer / "table.json", {runtime: {"source": int(value)}})
    _json(answer / "sort_key.json", {runtime: {"mart": ["id"]}})
    (answer / "evaluation" / "sql").mkdir(parents=True)
    (answer / "evaluation" / "sql" / "mart.sql").write_text(
        f"select * from {runtime}.mart order by id;\n", encoding="utf-8"
    )
    (answer / "gt").mkdir()
    (answer / "gt" / "mart.csv").write_text(f"id\n{value}\n", encoding="utf-8")


def _write_reference_task(
    cohort: Path,
    *,
    alias: str,
    canonical_id: str,
    runtime: str,
    destination: Destination = Destination.SNOWFLAKE,
    value: str = "1",
) -> dict:
    root = cohort / "tasks" / alias
    _write_public(root / "public", runtime, destination)
    _write_answer_key(root / "private", runtime, value=value)
    _json(root / "private" / "task_ir.json", {"task_id": canonical_id})
    return {
        "alias": alias,
        "canonical_id": canonical_id,
        "runtime_namespace": runtime,
        "destination": destination.value,
    }


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class UpstreamLayoutTests(unittest.TestCase):
    def test_refuses_retired_non_upstream_public_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cohort = Path(directory) / "cohort"
            row = _write_reference_task(
                cohort,
                alias="task_01",
                canonical_id="canonical",
                runtime="runtime",
            )
            _json(cohort / "catalog.json", {"tasks": [row]})
            public = cohort / "tasks" / "task_01" / "public"

            retired_tfvars = public / "elt" / "connector_config.auto.tfvars.json"
            retired_tfvars.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(UpstreamLayoutError, "non-upstream solver files"):
                discover_task_bundles(cohort)

            retired_tfvars.unlink()
            (public / "documentation.md").write_text("retired\n", encoding="utf-8")
            with self.assertRaisesRegex(UpstreamLayoutError, "non-upstream solver files"):
                discover_task_bundles(cohort)

    def test_flattens_public_and_merges_private_evaluator_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cohort = root / "cohort"
            first = _write_reference_task(
                cohort,
                alias="dbt_01",
                canonical_id="canonical_one",
                runtime="runtime_one",
            )
            second = _write_reference_task(
                cohort,
                alias="dbt_02",
                canonical_id="canonical_two",
                runtime="runtime_two",
                value="2",
            )
            _json(cohort / "catalog.json", {"destination": "snowflake", "tasks": [second, first]})

            inputs = root / "upstream" / "inputs"
            evaluation = root / "upstream" / "evaluation"
            _json(evaluation / "table.json", {"legacy": {"source": 9}})
            _json(evaluation / "sort_key.json", {"legacy": {"mart": ["id"]}})
            (inputs / "runtime_one").mkdir(parents=True)
            (inputs / "runtime_one" / "stale.txt").write_text("stale", encoding="utf-8")
            (evaluation / "sql" / "runtime_one").mkdir(parents=True)
            (evaluation / "sql" / "runtime_one" / "stale.sql").write_text(
                "stale", encoding="utf-8"
            )
            stale_gt = evaluation / "agent_results" / "gt_snowflake" / "runtime_one"
            stale_gt.mkdir(parents=True)
            (stale_gt / "stale.csv").write_text("stale", encoding="utf-8")

            source_tf = cohort / "tasks" / "dbt_01" / "public" / "elt" / "main.tf"
            os.chmod(source_tf, 0o444)
            report = materialize_upstream_layout(
                cohort,
                inputs,
                evaluation,
                only=("dbt_01",),
                destination="snowflake",
            )

            self.assertEqual(report.tasks[0].runtime_name, "runtime_one")
            self.assertTrue((inputs / "runtime_one" / "config.yaml").is_file())
            self.assertFalse((inputs / "dbt_01").exists())
            self.assertFalse((inputs / "runtime_one" / "stale.txt").exists())
            self.assertFalse((inputs / "runtime_two").exists())
            self.assertTrue(os.stat(inputs / "runtime_one" / "elt" / "main.tf").st_mode & 0o200)

            table = json.loads((evaluation / "table.json").read_text(encoding="utf-8"))
            sort_key = json.loads((evaluation / "sort_key.json").read_text(encoding="utf-8"))
            self.assertEqual(table, {"legacy": {"source": 9}, "runtime_one": {"source": 1}})
            self.assertEqual(
                sort_key,
                {"legacy": {"mart": ["id"]}, "runtime_one": {"mart": ["id"]}},
            )
            self.assertEqual(
                (evaluation / "sql" / "runtime_one" / "mart.sql").read_text(
                    encoding="utf-8"
                ),
                "select * from runtime_one.mart order by id;\n",
            )
            self.assertFalse((evaluation / "sql" / "runtime_one" / "stale.sql").exists())
            self.assertEqual(
                (
                    evaluation
                    / "agent_results"
                    / "gt_snowflake"
                    / "runtime_one"
                    / "mart.csv"
                ).read_text(encoding="utf-8"),
                "id\n1\n",
            )
            self.assertFalse((stale_gt / "stale.csv").exists())

            first_digest = _tree_digest(root / "upstream")
            materialize_upstream_layout(cohort, inputs, evaluation, only=("canonical_one",))
            self.assertEqual(_tree_digest(root / "upstream"), first_digest)

    def test_reads_schema3_release_layout_and_selects_runtime_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            canonical = "canonical_release_task"
            runtime = "runtime_release_task"
            _write_public(release / "public" / canonical, runtime, Destination.REDSHIFT)
            _write_answer_key(release / "private" / canonical, runtime)
            _json(
                release / "private" / canonical / "semantic" / "task_ir.json",
                {"task_id": canonical},
            )
            _json(
                release / "release_manifest.json",
                {
                    "tasks": {canonical: "content-hash"},
                    "destinations": {canonical: "redshift"},
                },
            )

            bundles = discover_task_bundles(release)
            self.assertEqual(len(bundles), 1)
            self.assertEqual(bundles[0].runtime_name, runtime)
            report = materialize_upstream_layout(
                release,
                root / "inputs_redshift",
                root / "evaluation",
                only=(runtime,),
            )
            self.assertEqual(report.destination, Destination.REDSHIFT)
            self.assertTrue((root / "inputs_redshift" / runtime / "config.yaml").is_file())
            self.assertTrue(
                (
                    root
                    / "evaluation"
                    / "agent_results"
                    / "gt_redshift"
                    / runtime
                    / "mart.csv"
                ).is_file()
            )

    def test_fails_before_output_mutation_when_private_namespace_is_wrong(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cohort = root / "cohort"
            row = _write_reference_task(
                cohort,
                alias="task_01",
                canonical_id="canonical",
                runtime="runtime",
            )
            _json(cohort / "catalog.json", {"tasks": [row]})
            _json(
                cohort / "tasks" / "task_01" / "private" / "answer_key" / "table.json",
                {"wrong_runtime": {"source": 1}},
            )
            sentinel = root / "inputs" / "sentinel.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(UpstreamLayoutError, "exactly runtime namespace"):
                materialize_upstream_layout(
                    cohort,
                    root / "inputs",
                    root / "evaluation",
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse((root / "evaluation").exists())

    def test_projects_shared_taskir_and_gold_to_all_original_input_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cohort = root / "cohort"
            task = demo_fixture.demo_task()
            runtime = database_name(task)
            row = _write_reference_task(
                cohort,
                alias="demo_01",
                canonical_id=task.task_id,
                runtime=runtime,
            )
            (cohort / "tasks" / "demo_01" / "private" / "task_ir.json").write_text(
                task_to_json(task), encoding="utf-8"
            )
            _json(cohort / "catalog.json", {"destination": "snowflake", "tasks": [row]})
            gold = SimpleNamespace(
                task_id=task.task_id,
                task_content_hash=task.content_hash(),
            )
            exported: list[Destination] = []

            def fake_export(task_arg, _gold, public, answer, *, destination, **_kwargs):
                selected = Destination(destination)
                exported.append(selected)
                _write_public(public, database_name(task_arg), selected)
                _write_answer_key(answer.parent, database_name(task_arg))

            upstream = root / "upstream"
            with (
                mock.patch("elt_taskgen.reference.gold.load_gold", return_value=gold),
                mock.patch("elt_taskgen.export.eltbench.export_task", side_effect=fake_export),
            ):
                report = project_all_destinations(cohort, upstream)

            self.assertEqual(exported, list(Destination))
            self.assertEqual(
                {item.destination for item in report.destinations}, set(Destination)
            )
            for destination, directory_name in UPSTREAM_INPUT_DIRECTORY.items():
                config = yaml.safe_load(
                    (upstream / directory_name / runtime / "config.yaml").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertIn(destination.value, config)
                self.assertTrue(
                    (
                        upstream
                        / "evaluation"
                        / "agent_results"
                        / f"gt_{destination.value}"
                        / runtime
                        / "mart.csv"
                    ).is_file()
                )


if __name__ == "__main__":
    unittest.main()

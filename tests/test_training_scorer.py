"""Same-state orchestration tests for the workspace-v1 artifact scorer."""

from __future__ import annotations

import dataclasses
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import duckdb

from elt_taskgen.models import PopulationName
from elt_taskgen.training.contract import WorkspaceErrorCode
from elt_taskgen.training.dbt_runner import (
    DbtErrorCode,
    DbtRuntimeConfig,
    DbtTrustedFailure,
)
from elt_taskgen.training.package import load_workspace_package
from elt_taskgen.training.local_sync import run_local_sync, verify_raw_state
from elt_taskgen.training.scorer import _ScorerDependencies, score_workspace
from elt_taskgen.training.terraform_intent import (
    TerraformIntentErrorCode,
    TerraformIntentEvaluation,
    expected_terraform_graph,
)
from elt_taskgen.training.workspace import (
    install_workspace,
    load_sealed_workspace,
    replay_workspace,
    seal_workspace,
)

try:
    from workspace_proxy_fixture import portable_five_backend_release
except ImportError:  # running as tests.test_training_scorer
    from tests.workspace_proxy_fixture import portable_five_backend_release

try:
    from workspace_proxy_pilot import (
        PILOT_TASK_ID,
        generated_files_postgres_release,
        write_correct_candidate as write_correct_pilot_candidate,
    )
except ImportError:  # running as tests.test_training_scorer
    from tests.workspace_proxy_pilot import (
        PILOT_TASK_ID,
        generated_files_postgres_release,
        write_correct_candidate as write_correct_pilot_candidate,
    )


FIXTURE_RELEASE = (
    Path(__file__).resolve().parent / "fixtures" / "semantic_gate" / "release"
)
TASK_ID = "gate__five_backend_probe"
DBT_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "runtime-images" / "dbt-duckdb"


_CORRECT_MAIN_TF = r'''
terraform {
  required_providers {
    airbyte = { source = "airbytehq/airbyte", version = "0.6.5" }
  }
}

variable "workspace_id" {}
variable "postgres_password" {}
variable "custom_api_definition_id" {}
variable "s3_access_key_id" {}
variable "s3_secret_access_key" {}
variable "destination_host" {}
variable "destination_warehouse" {}
variable "destination_role" {}
variable "destination_username" {}
variable "destination_password" {}

provider "airbyte" {
  server_url = "http://airbyte-abctl-control-plane:80/api/public/v1/"
}

resource "airbyte_source_postgres" "pg" {
  name = "postgres"
  workspace_id = var.workspace_id
  definition_id = "decd338e-5647-4c0b-adf4-da0e75f5a750"
  configuration = {
    host = "elt-postgres"
    port = 5432
    database = "gate__five_backend_probe"
    username = "postgres"
    password = "${var.postgres_password}"
    schemas = ["public"]
  }
}

resource "airbyte_source_mongodb_v2" "mongo" {
  name = "mongodb"
  workspace_id = var.workspace_id
  definition_id = "b2e713cd-cc36-4c0a-b5bd-b47cb8a0561e"
  configuration = {
    database_config = {
      self_managed_replica_set = {
        connection_string = "mongodb://elt-mongodb:27017/?directConnection=true"
        database = "gate__five_backend_probe"
      }
    }
  }
}

resource "airbyte_source_custom" "api" {
  name = "custom_api"
  workspace_id = var.workspace_id
  definition_id = var.custom_api_definition_id
  configuration = jsonencode({})
}

resource "airbyte_source_s3" "s3" {
  name = "aws_s3"
  workspace_id = var.workspace_id
  definition_id = "69589781-7828-43c5-9f63-8925b1c1ccc2"
  configuration = {
    aws_access_key_id = "${var.s3_access_key_id}"
    aws_secret_access_key = "${var.s3_secret_access_key}"
    bucket = "gate--five-backend-probe-bucket"
    endpoint = "http://elt-localstack:4566"
    region_name = "us-west-2"
    streams = [{
      name = "metrics"
      format = { jsonl_format = {} }
      globs = ["metrics.jsonl"]
    }]
  }
}

resource "airbyte_source_file" "items" {
  name = "file_order_items"
  workspace_id = var.workspace_id
  definition_id = "778daa7c-feaf-4db6-96f3-70fd645acc77"
  configuration = {
    dataset_name = "order_items"
    format = "csv"
    provider = { https_public_web = {} }
    url = "http://elt-files:8080/gate__five_backend_probe/order_items.csv"
  }
}

resource "airbyte_destination_snowflake" "warehouse" {
  name = "snowflake"
  workspace_id = var.workspace_id
  definition_id = "424892c4-daac-4491-b35d-c6688ba547ba"
  configuration = {
    host = var.destination_host
    database = "gate__five_backend_probe"
    schema = "AIRBYTE_SCHEMA"
    warehouse = var.destination_warehouse
    role = var.destination_role
    number_data_type = "NUMBER(38,9)"
    credentials = {
      username_and_password = {
        username = var.destination_username
        password = "${var.destination_password}"
      }
    }
  }
}

resource "airbyte_connection" "customers" {
  source_id = airbyte_source_postgres.pg.source_id
  destination_id = airbyte_destination_snowflake.warehouse.destination_id
  namespace_definition = "destination"
  configurations = { streams = [{ name = "customers", sync_mode = "full_refresh_append" }] }
}
resource "airbyte_connection" "orders" {
  source_id = airbyte_source_mongodb_v2.mongo.source_id
  destination_id = airbyte_destination_snowflake.warehouse.destination_id
  namespace_definition = "destination"
  configurations = { streams = [{ name = "orders", sync_mode = "full_refresh_append" }] }
}
resource "airbyte_connection" "events" {
  source_id = airbyte_source_custom.api.source_id
  destination_id = airbyte_destination_snowflake.warehouse.destination_id
  namespace_definition = "destination"
  configurations = { streams = [{ name = "events", sync_mode = "full_refresh_append" }] }
}
resource "airbyte_connection" "metrics" {
  source_id = airbyte_source_s3.s3.source_id
  destination_id = airbyte_destination_snowflake.warehouse.destination_id
  namespace_definition = "destination"
  configurations = { streams = [{ name = "metrics", sync_mode = "full_refresh_append" }] }
}
resource "airbyte_connection" "order_items" {
  source_id = airbyte_source_file.items.source_id
  destination_id = airbyte_destination_snowflake.warehouse.destination_id
  namespace_definition = "destination"
  configurations = { streams = [{ name = "order_items", sync_mode = "full_refresh_append" }] }
}
'''


_CUSTOMER_ROLLUP = r'''
{{ config(materialized='table') }}
WITH completed_orders AS (
    SELECT DISTINCT order_id, customer_id
    FROM {{ source('raw', 'orders') }}
    WHERE status = 'completed'
),
order_totals AS (
    SELECT order_id, SUM(quantity * unit_price) AS order_total
    FROM {{ source('raw', 'order_items') }}
    GROUP BY order_id
)
SELECT
    c.customer_id AS customer_id,
    COUNT(DISTINCT co.order_id) AS completed_orders,
    COALESCE(SUM(ot.order_total), 0) AS total_spend
FROM {{ source('raw', 'customers') }} AS c
LEFT JOIN completed_orders AS co ON co.customer_id = c.customer_id
LEFT JOIN order_totals AS ot ON ot.order_id = co.order_id
GROUP BY c.customer_id
'''


_EVENT_WIDE = r'''
{{ config(materialized='table') }}
SELECT
    e.event_id AS event_id,
    e.big_count AS big_count,
    e.label AS label,
    e.occurred_at AS occurred_at,
    e.tz_stamp AS tz_stamp,
    m.value AS metric_value,
    m.big_note AS big_note
FROM {{ source('raw', 'events') }} AS e
LEFT JOIN {{ source('raw', 'metrics') }} AS m ON m.event_id = e.event_id
'''


def _write_candidate(attempt) -> None:
    (attempt.elt_dir / "dbt_project.yml").write_text(
        "name: gate_task\nversion: '1.0'\nprofile: elt_taskgen\n"
        "model-paths: ['models']\n",
        encoding="utf-8",
    )
    models = attempt.elt_dir / "models"
    models.mkdir(exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    (models / "customer_rollup.sql").write_text(
        "{{ config(materialized='table') }}\n"
        "select * from {{ source('raw', 'customers') }}\n",
        encoding="utf-8",
    )


def _write_correct_candidate(attempt) -> None:
    attempt.elt_dir.joinpath("main.tf").write_text(
        _CORRECT_MAIN_TF.strip() + "\n",
        encoding="utf-8",
    )
    attempt.elt_dir.joinpath("dbt_project.yml").write_text(
        "name: gate_task\n"
        "version: '1.0'\n"
        "config-version: 2\n"
        "profile: elt_taskgen\n"
        "model-paths: ['models']\n"
        "models:\n  gate_task:\n    +materialized: table\n",
        encoding="utf-8",
    )
    models = attempt.elt_dir / "models"
    models.mkdir(exist_ok=True)
    models.joinpath("sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    database: gate__five_backend_probe\n"
        "    schema: AIRBYTE_SCHEMA\n"
        "    tables:\n"
        "      - name: customers\n"
        "      - name: orders\n"
        "      - name: order_items\n"
        "      - name: events\n"
        "      - name: metrics\n",
        encoding="utf-8",
    )
    models.joinpath("customer_rollup.sql").write_text(
        _CUSTOMER_ROLLUP.strip() + "\n",
        encoding="utf-8",
    )
    models.joinpath("event_wide.sql").write_text(
        _EVENT_WIDE.strip() + "\n",
        encoding="utf-8",
    )


def _runtime_config(root: Path) -> DbtRuntimeConfig:
    # Orchestration tests inject the dbt executor; these paths are never read.
    return DbtRuntimeConfig(
        python=root / "unused-python",
        manifest=root / "unused-runtime.json",
    )


def _dbt_result(
    *,
    mart_reward: float = 1.0,
    raw_immutable: bool = True,
    errors=(),
):
    return SimpleNamespace(
        dbt_project=1.0,
        mart_reward=mart_reward,
        raw_immutable=raw_immutable,
        error_codes=tuple(errors),
    )


class WorkspaceScorerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_workspace_package(FIXTURE_RELEASE, TASK_ID)

    def _sealed(self, root: Path):
        attempt = install_workspace(self.package, root / "authoring")
        _write_candidate(attempt)
        return seal_workspace(attempt, self.package, root / "sealed")

    def test_executes_exact_same_state_order_and_minimum(self) -> None:
        events: list[tuple[str, str]] = []

        def replay(package, sealed, path, **kwargs):
            events.append(("replay", path.name))
            return replay_workspace(package, sealed, path, **kwargs)

        def terraform(package, path):
            events.append(("terraform", path.parent.name))
            return TerraformIntentEvaluation(reward=1.0, graph=SimpleNamespace())

        def sync(package, population, intent, database_path):
            events.append(("sync", population.value))
            database_path.parent.mkdir(parents=True)
            database_path.write_bytes(population.value.encode("ascii"))
            return SimpleNamespace(
                sync_lifecycle=True,
                upstream_stage1=True,
                strict_raw_tables=1.0,
                error_codes=(),
                database_path=database_path,
            )

        def dbt(package, population, *, attempt_dir, sync_execution, **kwargs):
            events.append(("dbt", population.value))
            self.assertTrue(sync_execution.database_path.is_relative_to(attempt_dir))
            self.assertEqual(
                sync_execution.database_path.read_text(encoding="ascii"),
                population.value,
            )
            reward = 0.5 if population is PopulationName.RESAMPLED else 1.0
            return _dbt_result(mart_reward=reward)

        deps = _ScorerDependencies(replay=replay, terraform=terraform, sync=sync, dbt=dbt)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sealed = self._sealed(root)
            result = score_workspace(
                self.package,
                sealed,
                attempts_root=root / "runs",
                runtime_config=_runtime_config(root),
                _dependencies=deps,
            )

        self.assertEqual(result.reward, 0.5)
        self.assertEqual(
            [event[0] for event in events],
            ["replay", "terraform", "sync", "dbt"] * 4,
        )
        self.assertEqual(result.populations["primary"].end_to_end_reward, 1.0)
        self.assertEqual(result.populations["resampled"].end_to_end_reward, 0.5)

    def test_bad_terraform_never_reaches_sync_or_dbt(self) -> None:
        downstream_calls = 0

        def terraform(_package, _path):
            return TerraformIntentEvaluation(
                reward=0.0,
                graph=None,
                error_codes=(TerraformIntentErrorCode.STREAM_CONTRACT,),
            )

        def forbidden(*args, **kwargs):
            nonlocal downstream_calls
            downstream_calls += 1
            raise AssertionError("downstream phase must not run")

        deps = _ScorerDependencies(
            replay=replay_workspace,
            terraform=terraform,
            sync=forbidden,
            dbt=forbidden,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = score_workspace(
                self.package,
                self._sealed(root),
                attempts_root=root / "runs",
                runtime_config=_runtime_config(root),
                _dependencies=deps,
            )
        self.assertEqual(downstream_calls, 0)
        self.assertEqual(result.reward, 0.0)
        score = result.populations["primary"]
        self.assertEqual(score.terraform_contract, 0.0)
        self.assertEqual(
            score.error_codes,
            (TerraformIntentErrorCode.STREAM_CONTRACT.value,),
        )

    def test_official_score_refuses_a_caller_selected_graded_subset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = score_workspace(
                self.package,
                self._sealed(root),
                attempts_root=root / "runs",
                runtime_config=_runtime_config(root),
                populations=(PopulationName.PRIMARY,),
            )

        self.assertIsNone(result.reward)
        self.assertTrue(result.valid_submission)
        self.assertEqual(
            result.failure.error_code,
            WorkspaceErrorCode.TASK_PACKAGE_INVALID,
        )

    def test_real_sealed_wrong_stream_selection_loses_el_and_end_to_end(self) -> None:
        """Use the real HCL compiler; no downstream executor is substituted."""

        with portable_five_backend_release() as release_dir:
            package = load_workspace_package(release_dir, TASK_ID)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempt = install_workspace(package, root / "authoring")
                _write_correct_candidate(attempt)
                main_tf = attempt.elt_dir / "main.tf"
                candidate = main_tf.read_text(encoding="utf-8")
                marker = (
                    'streams = [{ name = "events", '
                    'sync_mode = "full_refresh_append" }]'
                )
                self.assertEqual(candidate.count(marker), 1)
                main_tf.write_text(
                    candidate.replace(
                        marker,
                        'streams = [{ name = "wrong_events", '
                        'sync_mode = "full_refresh_append" }]',
                    ),
                    encoding="utf-8",
                )
                sealed = seal_workspace(attempt, package, root / "sealed")
                result = score_workspace(
                    package,
                    sealed,
                    attempts_root=root / "runs",
                    runtime_config=_runtime_config(root),
                )

        self.assertEqual(result.reward, 0.0)
        self.assertEqual(set(result.graded_populations), set(result.populations))
        for score in result.populations.values():
            self.assertEqual(score.terraform_contract, 0.0)
            self.assertFalse(score.sync_lifecycle)
            self.assertFalse(score.strict_el_pass)
            self.assertEqual(score.mart_reward, 0.0)
            self.assertEqual(score.end_to_end_reward, 0.0)
            self.assertIn(
                TerraformIntentErrorCode.STREAM_CONTRACT.value,
                score.error_codes,
            )

    @unittest.skipUnless(
        (DBT_RUNTIME_ROOT / ".venv" / "bin" / "python").is_file(),
        "provision the pinned dbt runtime with: "
        "uv sync --project runtime-images/dbt-duckdb --locked",
    )
    def test_real_count_correct_content_wrong_el_gates_correct_dbt(self) -> None:
        """Corrupt values after real selected-stream sync, preserving counts."""

        def corrupting_sync(package, population, intent, database_path):
            execution = run_local_sync(package, population, intent, database_path)
            connection = duckdb.connect(str(execution.database_path))
            try:
                connection.execute(
                    "UPDATE AIRBYTE_SCHEMA.customers "
                    "SET customer_name = 'count-correct-content-wrong' "
                    "WHERE customer_id = "
                    "(SELECT min(customer_id) FROM AIRBYTE_SCHEMA.customers)"
                )
            finally:
                connection.close()
            corrupted = verify_raw_state(package, execution)
            self.assertTrue(corrupted.upstream_stage1)
            self.assertLess(corrupted.strict_raw_tables, 1.0)
            return dataclasses.replace(
                execution,
                upstream_stage1=corrupted.upstream_stage1,
                strict_raw_tables=corrupted.strict_raw_tables,
                raw_state=corrupted,
                error_codes=corrupted.error_codes,
            )

        with portable_five_backend_release() as release_dir:
            package = load_workspace_package(release_dir, TASK_ID)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempt = install_workspace(package, root / "authoring")
                _write_correct_candidate(attempt)
                sealed = seal_workspace(attempt, package, root / "sealed")
                result = score_workspace(
                    package,
                    sealed,
                    attempts_root=root / "runs",
                    runtime_config=DbtRuntimeConfig(
                        python=DBT_RUNTIME_ROOT / ".venv" / "bin" / "python",
                        manifest=DBT_RUNTIME_ROOT / "runtime.json",
                    ),
                    _dependencies=_ScorerDependencies(sync=corrupting_sync),
                )

        self.assertEqual(result.reward, 0.0)
        for score in result.populations.values():
            self.assertTrue(score.upstream_stage1)
            self.assertLess(score.strict_raw_tables, 1.0)
            self.assertFalse(score.strict_el_pass)
            self.assertEqual(score.dbt_project, 1.0)
            self.assertEqual(score.mart_reward, 0.0)
            self.assertTrue(score.raw_immutable)
            self.assertEqual(score.end_to_end_reward, 0.0)
            self.assertIn("local_sync_raw_content_mismatch", score.error_codes)

    @unittest.skipUnless(
        (DBT_RUNTIME_ROOT / ".venv" / "bin" / "python").is_file(),
        "provision the pinned dbt runtime with: "
        "uv sync --project runtime-images/dbt-duckdb --locked",
    )
    def test_real_infinite_measure_loses_only_its_mart(self) -> None:
        """R01 through the production scorer, with no injected dependency: the
        pinned dbt runtime, the real sync and the shared comparator.

        The portable subset admits CAST('inf' AS DOUBLE) and the mart check
        compares column names, not types, so the comparator is the only guard.
        Before the non-finite guard this submission scored 1.0 on every graded
        population. Keys, schema and extraction stay correct, so the loss can
        only come from the numeric comparison, and event_wide is untouched.
        """
        infinite = _CUSTOMER_ROLLUP.replace(
            "COALESCE(SUM(ot.order_total), 0) AS total_spend",
            "CAST('inf' AS DOUBLE) AS total_spend",
        )
        self.assertNotEqual(infinite, _CUSTOMER_ROLLUP)
        with portable_five_backend_release() as release_dir:
            package = load_workspace_package(release_dir, TASK_ID)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempt = install_workspace(package, root / "authoring")
                _write_correct_candidate(attempt)
                attempt.elt_dir.joinpath("models", "customer_rollup.sql").write_text(
                    infinite.strip() + "\n", encoding="utf-8"
                )
                sealed = seal_workspace(attempt, package, root / "sealed")
                result = score_workspace(
                    package,
                    sealed,
                    attempts_root=root / "runs",
                    runtime_config=DbtRuntimeConfig(
                        python=DBT_RUNTIME_ROOT / ".venv" / "bin" / "python",
                        manifest=DBT_RUNTIME_ROOT / "runtime.json",
                    ),
                )

        self.assertEqual(result.reward, 0.5)
        graded = [score for score in result.populations.values() if score.graded]
        self.assertTrue(graded)
        for score in graded:
            with self.subTest(population=score.population):
                self.assertTrue(score.strict_el_pass)
                self.assertEqual(score.dbt_project, 1.0)
                self.assertTrue(score.raw_immutable)
                # One of two marts: customer_rollup lost credit, event_wide kept it.
                self.assertEqual(score.mart_reward, 0.5)
                self.assertEqual(score.end_to_end_reward, 0.5)
                self.assertNotIn("dbt_mart_schema_invalid", score.error_codes)
                self.assertNotIn("dbt_mart_key_invalid", score.error_codes)

    @unittest.skipUnless(
        (DBT_RUNTIME_ROOT / ".venv" / "bin" / "python").is_file(),
        "provision the pinned dbt runtime with: "
        "uv sync --project runtime-images/dbt-duckdb --locked",
    )
    def test_real_malformed_candidate_yaml_is_scored_not_unlabelled(self) -> None:
        """A02 through the production scorer, with no injected dependency.

        Each submission is otherwise the correct candidate. Before the YAML
        boundary classified these shapes, each one escaped preparation as an
        unclassified exception and scored reward=None (harness_internal).
        A real trusted failure (the runtime manifest is missing) must still
        produce no label.
        """

        def append(relative: str, text: str):
            def mutate(elt: Path) -> None:
                path = elt.joinpath(*relative.split("/"))
                existing = path.read_text(encoding="utf-8") if path.exists() else "version: 2\n"
                path.write_text(existing + text, encoding="utf-8")

            return mutate

        runtime = DbtRuntimeConfig(
            python=DBT_RUNTIME_ROOT / ".venv" / "bin" / "python",
            manifest=DBT_RUNTIME_ROOT / "runtime.json",
        )
        cases = (
            ("unhashable key in dbt_project.yml", append("dbt_project.yml", "? [a, b]\n: value\n")),
            ("cyclic alias in dbt_project.yml", append("dbt_project.yml", "vars: &loop [*loop]\n")),
            ("cyclic alias in sources.yml", append("models/sources.yml", "x-audit: &loop [*loop]\n")),
            ("unhashable key in schema.yml", append("models/schema.yml", "? [a, b]\n: value\n")),
        )
        with portable_five_backend_release() as release_dir:
            package = load_workspace_package(release_dir, TASK_ID)

            def score(mutate, runtime_config):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    attempt = install_workspace(package, root / "authoring")
                    _write_correct_candidate(attempt)
                    if mutate is not None:
                        mutate(attempt.elt_dir)
                    sealed = seal_workspace(attempt, package, root / "sealed")
                    return score_workspace(
                        package,
                        sealed,
                        attempts_root=root / "runs",
                        runtime_config=runtime_config,
                    )

            for label, mutate in cases:
                with self.subTest(label):
                    result = score(mutate, runtime)
                    self.assertIsNone(result.failure)
                    self.assertEqual(result.reward, 0.0)
                    graded = [s for s in result.populations.values() if s.graded]
                    self.assertTrue(graded)
                    for population in graded:
                        self.assertIn("dbt_project_invalid", population.error_codes)

            broken = DbtRuntimeConfig(
                python=runtime.python, manifest=runtime.manifest.parent / "missing.json"
            )
            trusted = score(None, broken)
            self.assertIsNone(trusted.reward)
            self.assertIsNotNone(trusted.failure)

    def test_strict_el_gates_a_diagnostic_dbt_pass(self) -> None:
        dbt_calls = 0

        def terraform(_package, _path):
            return TerraformIntentEvaluation(reward=1.0, graph=SimpleNamespace())

        def sync(_package, _population, _intent, database_path):
            database_path.parent.mkdir(parents=True)
            database_path.write_bytes(b"count-correct-content-wrong")
            return SimpleNamespace(
                sync_lifecycle=True,
                upstream_stage1=True,
                strict_raw_tables=0.8,
                error_codes=("local_sync_raw_content_mismatch",),
            )

        def dbt(*args, **kwargs):
            nonlocal dbt_calls
            dbt_calls += 1
            return _dbt_result(mart_reward=1.0)

        deps = _ScorerDependencies(
            replay=replay_workspace,
            terraform=terraform,
            sync=sync,
            dbt=dbt,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = score_workspace(
                self.package,
                self._sealed(root),
                attempts_root=root / "runs",
                runtime_config=_runtime_config(root),
                _dependencies=deps,
            )
        self.assertEqual(dbt_calls, 4)
        score = result.populations["primary"]
        self.assertEqual(score.dbt_project, 1.0)
        self.assertEqual(score.mart_reward, 0.0)
        self.assertEqual(score.end_to_end_reward, 0.0)

    def test_raw_mutation_forces_only_the_end_to_end_gate(self) -> None:
        def terraform(_package, _path):
            return TerraformIntentEvaluation(reward=1.0, graph=SimpleNamespace())

        def sync(_package, _population, _intent, database_path):
            database_path.parent.mkdir(parents=True)
            database_path.write_bytes(b"raw")
            return SimpleNamespace(
                sync_lifecycle=True,
                upstream_stage1=True,
                strict_raw_tables=1.0,
                error_codes=(),
            )

        deps = _ScorerDependencies(
            replay=replay_workspace,
            terraform=terraform,
            sync=sync,
            dbt=lambda *args, **kwargs: _dbt_result(
                raw_immutable=False,
                errors=(DbtErrorCode.RAW_MUTATED,),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = score_workspace(
                self.package,
                self._sealed(root),
                attempts_root=root / "runs",
                runtime_config=_runtime_config(root),
                _dependencies=deps,
            )
        score = result.populations["primary"]
        self.assertEqual(score.mart_reward, 1.0)
        self.assertFalse(score.raw_immutable)
        self.assertEqual(score.end_to_end_reward, 0.0)

    def test_missing_runtime_is_no_label_and_untrusted_seal_is_invalid(self) -> None:
        def terraform(_package, _path):
            return TerraformIntentEvaluation(reward=1.0, graph=SimpleNamespace())

        def sync(_package, _population, _intent, database_path):
            database_path.parent.mkdir(parents=True)
            database_path.write_bytes(b"raw")
            return SimpleNamespace(
                sync_lifecycle=True,
                upstream_stage1=True,
                strict_raw_tables=1.0,
                error_codes=(),
            )

        def unavailable(*args, **kwargs):
            raise DbtTrustedFailure(DbtErrorCode.RUNTIME_UNAVAILABLE)

        deps = _ScorerDependencies(
            replay=replay_workspace,
            terraform=terraform,
            sync=sync,
            dbt=unavailable,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sealed = self._sealed(root)
            unavailable_result = score_workspace(
                self.package,
                sealed,
                attempts_root=root / "runtime-runs",
                runtime_config=_runtime_config(root),
                _dependencies=deps,
            )
            inspected = load_sealed_workspace(sealed.root)
            untrusted_result = score_workspace(
                self.package,
                inspected,
                attempts_root=root / "untrusted-runs",
                runtime_config=_runtime_config(root),
                _dependencies=deps,
            )

        self.assertIsNone(unavailable_result.reward)
        self.assertEqual(
            unavailable_result.failure.error_code,
            WorkspaceErrorCode.INFRASTRUCTURE_UNAVAILABLE,
        )
        self.assertEqual(untrusted_result.reward, 0.0)
        self.assertFalse(untrusted_result.valid_submission)
        self.assertEqual(
            untrusted_result.failure.error_code,
            WorkspaceErrorCode.SUBMISSION_INVALID,
        )

    def test_replay_is_deterministic_and_concurrent_attempts_are_disjoint(self) -> None:
        observed: list[Path] = []
        lock = threading.Lock()

        def terraform(_package, _path):
            return TerraformIntentEvaluation(reward=1.0, graph=SimpleNamespace())

        def sync(_package, _population, _intent, database_path):
            with lock:
                observed.append(database_path)
            database_path.parent.mkdir(parents=True)
            database_path.write_bytes(b"raw")
            return SimpleNamespace(
                sync_lifecycle=True,
                upstream_stage1=True,
                strict_raw_tables=1.0,
                error_codes=(),
            )

        deps = _ScorerDependencies(
            replay=replay_workspace,
            terraform=terraform,
            sync=sync,
            dbt=lambda *args, **kwargs: _dbt_result(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sealed = self._sealed(root)
            results = []

            def run() -> None:
                results.append(
                    score_workspace(
                        self.package,
                        sealed,
                        attempts_root=root / "runs",
                        runtime_config=_runtime_config(root),
                        _dependencies=deps,
                    )
                )

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(results), 2)
        self.assertEqual(
            results[0].model_dump(mode="json"),
            results[1].model_dump(mode="json"),
        )
        self.assertEqual(len(observed), 8)
        self.assertEqual(len(set(observed)), 8)

    @unittest.skipUnless(
        (DBT_RUNTIME_ROOT / ".venv" / "bin" / "python").is_file(),
        "provision the pinned dbt runtime with: "
        "uv sync --project runtime-images/dbt-duckdb --locked",
    )
    def test_real_five_backend_m1_m2_m3_same_state_reward_is_one(self) -> None:
        """No mocks: real HCL compiler, trusted sync, and pinned dbt Core."""

        with portable_five_backend_release() as release_dir:
            package = load_workspace_package(release_dir, TASK_ID)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempt = install_workspace(package, root / "authoring")
                _write_correct_candidate(attempt)
                sealed = seal_workspace(attempt, package, root / "sealed")
                result = score_workspace(
                    package,
                    sealed,
                    attempts_root=root / "runs",
                    runtime_config=DbtRuntimeConfig(
                        python=DBT_RUNTIME_ROOT / ".venv" / "bin" / "python",
                        manifest=DBT_RUNTIME_ROOT / "runtime.json",
                    ),
                )

        self.assertEqual(result.reward, 1.0)
        self.assertEqual(
            set(result.graded_populations),
            {"primary", "resampled", "counterfactual", "stress"},
        )
        for score in result.populations.values():
            self.assertEqual(score.terraform_contract, 1.0)
            self.assertTrue(score.sync_lifecycle)
            self.assertTrue(score.upstream_stage1)
            self.assertEqual(score.strict_raw_tables, 1.0)
            self.assertEqual(score.dbt_project, 1.0)
            self.assertEqual(score.mart_reward, 1.0)
            self.assertTrue(score.raw_immutable)
            self.assertEqual(score.end_to_end_reward, 1.0)

    @unittest.skipUnless(
        (DBT_RUNTIME_ROOT / ".venv" / "bin" / "python").is_file(),
        "provision the pinned dbt runtime with: "
        "uv sync --project runtime-images/dbt-duckdb --locked",
    )
    def test_real_files_postgres_pilot_scores_one_on_every_graded_population(
        self,
    ) -> None:
        """Generate, freeze, seal, and score the two-backend pilot end to end."""

        with generated_files_postgres_release() as (release_dir, authored_task):
            self.assertEqual(authored_task.task_id, PILOT_TASK_ID)
            self.assertEqual(
                {assignment.backend.value for assignment in authored_task.backends},
                {"files", "postgres"},
            )
            package = load_workspace_package(release_dir, PILOT_TASK_ID)
            self.assertEqual(package.task.content_hash(), authored_task.content_hash())
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempt = install_workspace(package, root / "authoring")
                write_correct_pilot_candidate(attempt)
                sealed = seal_workspace(attempt, package, root / "sealed")
                result = score_workspace(
                    package,
                    sealed,
                    attempts_root=root / "runs",
                    runtime_config=DbtRuntimeConfig(
                        python=DBT_RUNTIME_ROOT / ".venv" / "bin" / "python",
                        manifest=DBT_RUNTIME_ROOT / "runtime.json",
                    ),
                )

        self.assertEqual(result.reward, 1.0)
        self.assertEqual(
            set(result.graded_populations),
            {"primary", "resampled", "counterfactual", "stress"},
        )
        self.assertEqual(set(result.populations), set(result.graded_populations))
        for population, score in result.populations.items():
            self.assertEqual(score.terraform_contract, 1.0, population)
            self.assertTrue(score.sync_lifecycle, population)
            self.assertTrue(score.upstream_stage1, population)
            self.assertEqual(score.strict_raw_tables, 1.0, population)
            self.assertTrue(score.strict_el_pass, population)
            self.assertEqual(score.dbt_project, 1.0, population)
            self.assertEqual(score.mart_reward, 1.0, population)
            self.assertTrue(score.raw_immutable, population)
            self.assertEqual(score.end_to_end_reward, 1.0, population)

    @unittest.skipUnless(
        (DBT_RUNTIME_ROOT / ".venv" / "bin" / "python").is_file(),
        "provision the pinned dbt runtime with: "
        "uv sync --project runtime-images/dbt-duckdb --locked",
    )
    def test_real_pilot_sealed_submission_replays_concurrently_and_deterministically(
        self,
    ) -> None:
        """Two real scores share only the seal and their trusted parent root."""

        observed_attempts: list[Path] = []
        observed_lock = threading.Lock()

        def observing_replay(package, sealed, destination, **kwargs):
            with observed_lock:
                observed_attempts.append(destination)
            return replay_workspace(package, sealed, destination, **kwargs)

        dependencies = _ScorerDependencies(replay=observing_replay)
        runtime_config = DbtRuntimeConfig(
            python=DBT_RUNTIME_ROOT / ".venv" / "bin" / "python",
            manifest=DBT_RUNTIME_ROOT / "runtime.json",
        )

        with generated_files_postgres_release() as (release_dir, authored_task):
            self.assertEqual(authored_task.task_id, PILOT_TASK_ID)
            package = load_workspace_package(release_dir, PILOT_TASK_ID)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempt = install_workspace(package, root / "authoring")
                write_correct_pilot_candidate(attempt)
                sealed = seal_workspace(attempt, package, root / "sealed")
                attempts_root = root / "runs"
                start = threading.Barrier(2)

                def score_once():
                    start.wait(timeout=10)
                    return score_workspace(
                        package,
                        sealed,
                        attempts_root=attempts_root,
                        runtime_config=runtime_config,
                        _dependencies=dependencies,
                    )

                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(score_once) for _ in range(2)]
                    results = [future.result(timeout=180) for future in futures]

                # Both scorer-owned run roots must be gone after their calls;
                # the caller-owned parent remains and contains no leaked state.
                observed_snapshot = tuple(observed_attempts)
                remaining_entries = tuple(attempts_root.iterdir())

        self.assertEqual(len(results), 2)
        expected_populations = {
            "primary",
            "resampled",
            "counterfactual",
            "stress",
        }
        canonical = results[0].model_dump(mode="json")
        self.assertEqual(results[1].model_dump(mode="json"), canonical)
        for result in results:
            self.assertTrue(result.valid_submission)
            self.assertEqual(result.reward, 1.0)
            self.assertEqual(set(result.graded_populations), expected_populations)
            self.assertEqual(set(result.populations), expected_populations)
            for population, score in result.populations.items():
                heads = (
                    score.terraform_contract,
                    score.sync_lifecycle,
                    score.upstream_stage1,
                    score.strict_raw_tables,
                    score.strict_el_pass,
                    score.dbt_project,
                    score.mart_reward,
                    score.raw_immutable,
                    score.end_to_end_reward,
                )
                self.assertEqual(
                    heads,
                    (1.0, True, True, 1.0, True, 1.0, 1.0, True, 1.0),
                    population,
                )

        self.assertEqual(len(observed_snapshot), 8)
        self.assertEqual(len(set(observed_snapshot)), 8)
        run_roots = {path.parent for path in observed_snapshot}
        self.assertEqual(len(run_roots), 2)
        for run_root in run_roots:
            self.assertEqual(
                {path.name for path in observed_snapshot if path.parent == run_root},
                {"replay-00", "replay-01", "replay-02", "replay-03"},
            )
            self.assertFalse(run_root.exists())
        self.assertEqual(remaining_entries, ())


class ProtocolProxyParityTests(unittest.TestCase):
    """docs/plans/cloud_free_elt_agent_rlvr.md, protocol-proxy checkpoint: for
    a VALID production graph the `AirbyteProtocolProxy` path of
    `score_workspace` hands the sync seam exactly what the direct seam gets
    and yields the same `PopulationWorkspaceScore` fields; only the
    evaluator-owned `.workspace-runtime/airbyte/airbyte-state.json` is new."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_workspace_package(FIXTURE_RELEASE, TASK_ID)

    def _sealed(self, root: Path):
        attempt = install_workspace(self.package, root / "authoring")
        _write_candidate(attempt)
        return seal_workspace(attempt, self.package, root / "sealed")

    def test_protocol_proxy_path_scores_a_valid_graph_like_the_direct_sync_seam(self) -> None:
        graph = expected_terraform_graph(self.package)
        sync_calls: list[tuple[str, object, Path]] = []

        def sync(package, population, graph_, database_path):
            # The proxy persists its state file BEFORE handing the sync seam
            # the job; the direct seam has none (the run root is removed
            # after scoring, so presence is observed here, at sync time).
            state = Path(database_path).parent.parent / "airbyte" / "airbyte-state.json"
            sync_calls.append((str(getattr(population, "value", population)), graph_, Path(database_path), state.is_file()))
            return SimpleNamespace(
                sync_lifecycle=True,
                upstream_stage1=True,
                strict_raw_tables=1.0,
                error_codes=(),
                database_path=Path(database_path),
                namespace=SimpleNamespace(raw_schema="AIRBYTE_SCHEMA"),
                selected_streams=graph_.selected_streams,
            )

        def dbt(package, population, *, attempt_dir, sync_execution, runtime_config, limits):
            return _dbt_result()

        def score_via(graph_object):
            deps = _ScorerDependencies(
                replay=replay_workspace,
                terraform=lambda _package, _path: TerraformIntentEvaluation(
                    reward=1.0, graph=graph_object, error_codes=()
                ),
                sync=sync,
                dbt=dbt,
            )
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                return score_workspace(
                    self.package,
                    self._sealed(root),
                    attempts_root=root / "runs",
                    runtime_config=_runtime_config(root),
                    _dependencies=deps,
                )

        start = len(sync_calls)
        proxied = score_via(graph)
        proxy_calls = sync_calls[start:]
        start = len(sync_calls)
        direct = score_via(
            SimpleNamespace(
                **{f.name: getattr(graph, f.name) for f in dataclasses.fields(graph)},
                selected_streams=graph.selected_streams,
            )
        )
        direct_calls = sync_calls[start:]

        self.assertEqual(proxied.reward, direct.reward)
        self.assertEqual(set(proxied.populations), set(direct.populations))
        for name, score in proxied.populations.items():
            self.assertEqual(score, direct.populations[name], name)
            self.assertEqual(score.error_codes, ())
            self.assertTrue(score.strict_el_pass, name)
        # The proxy validated the graph and then handed `sync` the same
        # (population, graph, database_path) the direct seam receives.
        self.assertEqual(len(proxy_calls), len(direct_calls))
        self.assertGreater(len(proxy_calls), 0)
        for (p_pop, p_graph, p_db, p_state), (d_pop, _d_graph, d_db, d_state) in zip(proxy_calls, direct_calls):
            self.assertEqual(p_pop, d_pop)
            self.assertIs(p_graph, graph)
            self.assertEqual(p_db.parts[-4:], d_db.parts[-4:])
            self.assertEqual(p_db.parts[-3:], (".workspace-runtime", "raw", "attempt.duckdb"))
            # The one difference: the evaluator-owned Airbyte state file.
            self.assertTrue(p_state, p_pop)
            self.assertFalse(d_state, d_pop)


if __name__ == "__main__":
    unittest.main()

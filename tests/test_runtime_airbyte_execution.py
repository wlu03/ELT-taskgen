from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from elt_taskgen.destinations import (
    DBT_ADAPTER_CONTRACTS,
    DBT_CORE_VERSION,
    Destination,
)
from elt_taskgen.runtime.airbyte import AirbyteClient, AirbyteError
from elt_taskgen.runtime import execution as execution_mod
from elt_taskgen.runtime.execution import (
    ExecutionError,
    Stage2PreflightError,
    connection_ids,
    rerun_stage1_syncs,
    run_stage1_submission,
    run_stage2_submission,
    stage2_preflight,
    validate_stage1_execution_receipt,
    validate_stage2_execution_receipt,
)
from elt_taskgen.runtime.process import CommandResult, DockerRunner, ProcessFailure


def _dbt_version_stdout(
    *,
    core: str = DBT_CORE_VERSION,
    adapter: str = DBT_ADAPTER_CONTRACTS[Destination.SNOWFLAKE][0],
    adapter_version: str = DBT_ADAPTER_CONTRACTS[Destination.SNOWFLAKE][1],
) -> str:
    return (
        "Core:\n"
        f"  - installed: {core}\n"
        f"  - latest:    {core} - Up to date!\n"
        "\n"
        "Plugins:\n"
        f"  - {adapter}: {adapter_version} - Up to date!\n"
    )


def _snowflake_profile_bindings() -> dict[str, str]:
    return {
        field: "{{ env_var('" + env_name + "') }}"
        for field, env_name in {
            "account": "ELT_TASKGEN_SNOWFLAKE_ACCOUNT",
            "user": "ELT_TASKGEN_SNOWFLAKE_USER",
            "password": "ELT_TASKGEN_SNOWFLAKE_PASSWORD",
            "role": "ELT_TASKGEN_SNOWFLAKE_ROLE",
            "warehouse": "ELT_TASKGEN_SNOWFLAKE_WAREHOUSE",
        }.items()
    }


class ScriptedAirbyte(AirbyteClient):
    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        # Avoid constructing HTTP authentication for this pure unit fake.
        self.responses = responses
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        key = (method, path)
        value = self.responses[key]
        if isinstance(value, list):
            return value.pop(0)
        return value


class FakeRunner:
    def __init__(
        self,
        state: dict | None = None,
        *,
        version_stdout: str | None = None,
    ) -> None:
        self.state = state
        self.version_stdout = (
            version_stdout if version_stdout is not None else _dbt_version_stdout()
        )
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []
        self.environments: list[dict[str, str] | None] = []

    def run(self, argv, *, cwd=None, env=None, stdin_path=None) -> CommandResult:
        cwd_path = Path(cwd) if cwd is not None else None
        command = tuple(str(value) for value in argv)
        self.calls.append((command, cwd_path))
        self.environments.append(dict(env) if env else None)
        if self.state is not None and "apply" in command and cwd_path is not None:
            (cwd_path / "terraform.tfstate").write_text(
                json.dumps(self.state), encoding="utf-8"
            )
        if "--version" in command:
            return CommandResult(0, self.version_stdout)
        return CommandResult(0)


class FakeSyncClient:
    def __init__(self) -> None:
        self.ids: tuple[str, ...] | None = None

    def trigger_and_wait(self, ids, *, poll_interval, timeout):
        self.ids = tuple(ids)
        return {value: "succeeded" for value in ids}

    def list_connections(self, workspace_id):
        return [
            {"connectionId": value, "schedule": {"scheduleType": "manual"}}
            for value in (self.ids or ("abc",))
        ]


class RuntimeAirbyteExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _provenance_stage1_execution(self):
        workspace = self.root / "provenance-attempt"
        elt = workspace / "elt"
        elt.mkdir(parents=True)
        (elt / "main.tf").write_text("terraform {}\n", encoding="utf-8")
        (workspace / "config.yaml").write_text("task: demo\n", encoding="utf-8")
        helper = workspace / "terraform-helper.sh"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        state = {
            "lineage": "6f949f38-cb01-43ff-b5f1-67b3bd2bc521",
            "serial": 7,
            "resources": [
                {
                    "mode": "managed",
                    "type": "airbyte_connection",
                    "name": "customers_to_warehouse",
                    "instances": [
                        {"attributes": {"connection_id": "connection-1"}}
                    ],
                },
                {
                    "mode": "managed",
                    "type": "airbyte_connection",
                    "name": "orders_to_warehouse",
                    "instances": [
                        {"attributes": {"connection_id": "connection-2"}}
                    ],
                },
            ],
        }

        class ReceiptClient(FakeSyncClient):
            def list_connections(self, workspace_id):
                return [
                    {
                        "connectionId": "connection-1",
                        "schedule": {"scheduleType": "manual"},
                    },
                    {
                        "connectionId": "connection-2",
                        "schedule": {"scheduleType": "manual"},
                    },
                ]

            def trigger_and_wait_receipt(self, ids, *, poll_interval, timeout):
                self.ids = tuple(ids)
                return SimpleNamespace(
                    statuses={
                        "connection-1": "succeeded",
                        "connection-2": "succeeded",
                    },
                    job_ids={"connection-1": 101, "connection-2": 102},
                )

        result = run_stage1_submission(
            workspace,
            ReceiptClient(),
            runner=FakeRunner(state),
            workspace_id="workspace-1",
            poll_interval=0,
        )
        return workspace, helper, result

    def test_publish_declarative_source_uses_workspace_endpoint(self) -> None:
        client = ScriptedAirbyte(
            {
                (
                    "POST",
                    "workspaces/ws/definitions/declarative_sources",
                ): {"definitionId": "definition-1"}
            }
        )
        definition = client.publish_declarative_source_definition(
            "ws", name="ELT Bench", manifest={"version": "6.48.15"}
        )
        self.assertEqual(definition, "definition-1")
        self.assertEqual(
            client.calls[0][2],
            {"name": "ELT Bench", "manifest": {"version": "6.48.15"}},
        )

    def test_publish_declarative_source_accepts_current_id_response(self) -> None:
        client = ScriptedAirbyte(
            {
                (
                    "POST",
                    "workspaces/ws/definitions/declarative_sources",
                ): {"id": "definition-current"}
            }
        )

        definition = client.publish_declarative_source_definition(
            "ws", name="ELT Bench", manifest={"version": "6.48.15"}
        )

        self.assertEqual(definition, "definition-current")

    def test_connection_job_statuses_orders_locally_not_by_api_list_order(self) -> None:
        path = "jobs?limit=100&orderBy=createdAt%7CDESC"
        client = ScriptedAirbyte(
            {
                ("GET", path): {
                    # Deliberately oldest-first for c1 and newest-first for c2:
                    # the deployed API has exhibited both despite orderBy.
                    "data": [
                        {
                            "jobId": 10,
                            "connectionId": "c1",
                            "status": "failed",
                            "createdAt": "2026-09-03T10:00:00Z",
                        },
                        {
                            "jobId": 21,
                            "connectionId": "c2",
                            "status": "succeeded",
                            "createdAt": 1_788_430_800,
                        },
                        {
                            "jobId": 20,
                            "connectionId": "c2",
                            "status": "failed",
                            "createdAt": 1_788_427_200,
                        },
                        {
                            "jobId": 11,
                            "connectionId": "c1",
                            "status": "succeeded",
                            "createdAt": "2026-09-03T11:00:00+00:00",
                        },
                    ]
                }
            }
        )

        self.assertEqual(
            client.connection_job_statuses(("c1", "c2")),
            {"c1": "succeeded", "c2": "succeeded"},
        )
        self.assertEqual(client.calls, [("GET", path, None)])

    def test_connection_job_statuses_uses_job_id_fallback_and_fails_ambiguous(self) -> None:
        path = "jobs?limit=100&orderBy=createdAt%7CDESC"
        fallback = ScriptedAirbyte(
            {
                ("GET", path): {
                    "data": [
                        {"id": 2, "connection_id": "c1", "status": "succeeded"},
                        {
                            "id": 1,
                            "connection_id": "c1",
                            "status": "failed",
                            "createdAt": "2026-09-03T10:00:00Z",
                        },
                    ]
                }
            }
        )
        self.assertEqual(
            fallback.connection_job_statuses(("c1",)), {"c1": "succeeded"}
        )

        ambiguous = ScriptedAirbyte(
            {
                ("GET", path): {
                    "data": [
                        {"jobId": 1, "connectionId": "c1", "status": "succeeded"},
                        {"jobId": 1, "connectionId": "c1", "status": "failed"},
                    ]
                }
            }
        )
        with self.assertRaisesRegex(AirbyteError, "ambiguous latest job states"):
            ambiguous.connection_job_statuses(("c1",))

    def test_trigger_wait_tracks_exact_returned_job_ids(self) -> None:
        client = ScriptedAirbyte(
            {
                ("POST", "jobs"): [
                    {"jobId": 10, "status": "running"},
                    {"jobId": 20, "status": "running"},
                ],
                ("GET", "jobs/10"): [
                    {"status": "running"},
                    {"status": "succeeded"},
                ],
                ("GET", "jobs/20"): [
                    {"status": "running"},
                    {"status": "succeeded"},
                ],
            }
        )
        ticks = iter((0.0, 0.0, 1.0, 1.0))
        result = client.trigger_and_wait_receipt(
            ("c1", "c2"),
            poll_interval=0,
            timeout=10,
            sleep=lambda _: None,
            monotonic=lambda: next(ticks),
        )
        self.assertEqual(result.statuses, {"c1": "succeeded", "c2": "succeeded"})
        self.assertEqual(result.job_ids, {"c1": 10, "c2": 20})
        get_paths = [path for method, path, _ in client.calls if method == "GET"]
        self.assertEqual(get_paths, ["jobs/10", "jobs/10", "jobs/20", "jobs/20"])
        self.assertEqual(
            [(method, path) for method, path, _ in client.calls],
            [
                ("POST", "jobs"),
                ("GET", "jobs/10"),
                ("GET", "jobs/10"),
                ("POST", "jobs"),
                ("GET", "jobs/20"),
                ("GET", "jobs/20"),
            ],
        )

    def test_trigger_wait_refuses_missing_job_id(self) -> None:
        client = ScriptedAirbyte({("POST", "jobs"): {"status": "running"}})
        with self.assertRaisesRegex(AirbyteError, "returned no job id"):
            client.trigger_and_wait(("c1",), poll_interval=0, timeout=0)

    def test_trigger_wait_requires_positive_unique_job_ids(self) -> None:
        for invalid in (0, -1, True, 1.5, "-2", "not-an-id"):
            with self.subTest(invalid=invalid):
                client = ScriptedAirbyte(
                    {("POST", "jobs"): {"jobId": invalid, "status": "running"}}
                )
                with self.assertRaisesRegex(AirbyteError, "positive integer"):
                    client.trigger_and_wait(("c1",), poll_interval=0, timeout=0)

        duplicate = ScriptedAirbyte(
            {
                ("POST", "jobs"): [
                    {"jobId": 10, "status": "running"},
                    {"jobId": 10, "status": "running"},
                ],
                ("GET", "jobs/10"): {"status": "succeeded"},
            }
        )
        with self.assertRaisesRegex(AirbyteError, "distinct connections"):
            duplicate.trigger_and_wait(("c1", "c2"), poll_interval=0, timeout=10)

    def test_connection_ids_reads_all_instances_and_show_json(self) -> None:
        path = self.root / "state.json"
        path.write_text(
            json.dumps(
                {
                    "resources": [
                        {
                            "type": "airbyte_connection",
                            "instances": [
                                {"attributes": {"connection_id": "one"}},
                                {"attributes": {"connection_id": "two"}},
                            ],
                        }
                    ],
                    "values": {
                        "root_module": {
                            "child_modules": [
                                {
                                    "resources": [
                                        {
                                            "type": "airbyte_connection",
                                            "values": {"connection_id": "three"},
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(connection_ids(path), ("one", "two", "three"))

    def test_stage1_applies_terraform_then_triggers_connections(self) -> None:
        elt = self.root / "attempt" / "elt"
        elt.mkdir(parents=True)
        (elt / "main.tf").write_text("terraform {}\n", encoding="utf-8")
        state = {
            "resources": [
                {
                    "type": "airbyte_connection",
                    "instances": [{"attributes": {"connection_id": "abc"}}],
                }
            ]
        }
        runner = FakeRunner(state)
        airbyte = FakeSyncClient()
        state_path = elt / "terraform.tfstate"
        with mock.patch.object(
            execution_mod,
            "_read_regular_file",
            wraps=execution_mod._read_regular_file,
        ) as reader:
            result = run_stage1_submission(
                self.root / "attempt",
                airbyte,
                runner=runner,
                workspace_id="workspace-1",
                poll_interval=0,
            )
        self.assertEqual(result.connection_ids, ("abc",))
        self.assertEqual(airbyte.ids, ("abc",))
        self.assertRegex(result.terraform_state_digest, r"^[0-9a-f]{64}$")
        self.assertRegex(result.terraform_input_tree_digest, r"^[0-9a-f]{64}$")
        self.assertLessEqual(
            datetime.fromisoformat(result.execution_started_at.replace("Z", "+00:00")),
            datetime.fromisoformat(result.execution_completed_at.replace("Z", "+00:00")),
        )
        self.assertEqual(result.job_ids, {})
        state_reads = [
            call
            for call in reader.call_args_list
            if Path(call.args[0]).name == state_path.name
        ]
        self.assertEqual(len(state_reads), 1)
        self.assertEqual(
            runner.calls[0],
            (("terraform", "init", "-input=false"), elt.resolve()),
        )
        self.assertEqual(
            runner.calls[1],
            (
                (
                    "terraform",
                    "apply",
                    "-input=false",
                    "-auto-approve",
                    "-parallelism=1",
                ),
                elt.resolve(),
            ),
        )
        self.assertEqual(
            (elt / "terraform.tfstate").stat().st_mode & 0o777,
            0o600,
        )

    def test_stage1_receipt_revalidation_binds_state_and_jobs(self) -> None:
        workspace, _, result = self._provenance_stage1_execution()

        self.assertEqual(
            validate_stage1_execution_receipt(result),
            ("connection-1", "connection-2"),
        )
        self.assertEqual(result.workspace_dir, workspace.resolve())
        self.assertEqual(
            result.terraform_state_path,
            workspace.resolve() / "elt" / "terraform.tfstate",
        )
        self.assertEqual(result.terraform_state_serial, 7)
        self.assertEqual(
            result.terraform_state_lineage,
            "6f949f38-cb01-43ff-b5f1-67b3bd2bc521",
        )
        self.assertEqual(
            result.terraform_connection_resources,
            (
                ("airbyte_connection.customers_to_warehouse", "connection-1"),
                ("airbyte_connection.orders_to_warehouse", "connection-2"),
            ),
        )

    def test_stage1_receipt_revalidation_refuses_fabricated_fields(self) -> None:
        _, _, result = self._provenance_stage1_execution()
        cases = (
            (replace(result, workspace_dir=None), "workspace/state provenance"),
            (replace(result, terraform_state_digest="0" * 64), "Terraform state"),
            (replace(result, terraform_state_serial=8), "Terraform state"),
            (
                replace(
                    result,
                    terraform_connection_resources=(
                        ("forged", "connection-1"),
                        ("also-forged", "connection-2"),
                    ),
                ),
                "Terraform state",
            ),
            (
                replace(
                    result, job_ids={"connection-1": 0, "connection-2": 102}
                ),
                "positive integers",
            ),
            (
                replace(
                    result, job_ids={"connection-1": 101, "connection-2": 101}
                ),
                "distinct connections",
            ),
            (
                replace(
                    result,
                    statuses={
                        "connection-1": "failed",
                        "connection-2": "succeeded",
                    },
                ),
                "non-successful",
            ),
        )
        for receipt, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ExecutionError, message
            ):
                validate_stage1_execution_receipt(receipt)

    def test_stage1_receipt_revalidation_refuses_state_mutation(self) -> None:
        _, _, result = self._provenance_stage1_execution()
        assert result.terraform_state_path is not None
        state = json.loads(result.terraform_state_path.read_text(encoding="utf-8"))
        state["serial"] = 8
        result.terraform_state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(ExecutionError, "Terraform state"):
            validate_stage1_execution_receipt(result)

    def test_stage1_receipt_revalidation_binds_all_admitted_workspace_files(
        self,
    ) -> None:
        workspace, helper, result = self._provenance_stage1_execution()
        # Runtime-owned dbt output is explicitly outside the admitted input
        # inventory and may appear between Stage 1 and receipt verification.
        logs = workspace / "elt" / "logs"
        logs.mkdir()
        (logs / "dbt.log").write_text("generated\n", encoding="utf-8")
        self.assertEqual(
            validate_stage1_execution_receipt(result),
            ("connection-1", "connection-2"),
        )

        # An otherwise arbitrary regular workspace file is admitted and bound.
        helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ExecutionError, "submission identity changed"):
            validate_stage1_execution_receipt(result)

    def test_stage1_hardens_state_even_when_terraform_apply_fails(self) -> None:
        elt = self.root / "failed-attempt" / "elt"
        elt.mkdir(parents=True)
        (elt / "main.tf").write_text("terraform {}\n", encoding="utf-8")

        class FailingRunner(FakeRunner):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                command = tuple(str(value) for value in argv)
                if "apply" in command:
                    state = Path(kwargs["cwd"]) / "terraform.tfstate"
                    state.chmod(0o666)
                    raise ProcessFailure("injected apply failure")
                return result

        runner = FailingRunner({"resources": []})
        with self.assertRaises(ProcessFailure):
            run_stage1_submission(
                self.root / "failed-attempt",
                FakeSyncClient(),
                runner=runner,
                workspace_id="workspace-1",
                poll_interval=0,
            )
        self.assertEqual(
            (elt / "terraform.tfstate").stat().st_mode & 0o777,
            0o600,
        )

    def test_stage1_refuses_input_tree_mutation_during_apply(self) -> None:
        elt = self.root / "mutated-attempt" / "elt"
        elt.mkdir(parents=True)
        main_tf = elt / "main.tf"
        main_tf.write_text("terraform {}\n", encoding="utf-8")
        state = {
            "resources": [
                {
                    "type": "airbyte_connection",
                    "instances": [{"attributes": {"connection_id": "abc"}}],
                }
            ]
        }

        class MutatingRunner(FakeRunner):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                if "apply" in tuple(str(value) for value in argv):
                    main_tf.write_text("terraform { required_version = \">= 1.0\" }\n")
                return result

        airbyte = FakeSyncClient()
        with self.assertRaisesRegex(ExecutionError, "changed during apply"):
            run_stage1_submission(
                self.root / "mutated-attempt",
                airbyte,
                runner=MutatingRunner(state),
                workspace_id="workspace-1",
                poll_interval=0,
            )
        self.assertIsNone(airbyte.ids)

    def test_stage1_rejects_connection_outside_submitted_workspace_set(self) -> None:
        elt = self.root / "attempt" / "elt"
        elt.mkdir(parents=True)
        (elt / "main.tf").write_text("terraform {}\n", encoding="utf-8")
        state = {
            "resources": [
                {
                    "type": "airbyte_connection",
                    "instances": [{"attributes": {"connection_id": "submitted"}}],
                }
            ]
        }
        runner = FakeRunner(state)

        class ForeignConnection(FakeSyncClient):
            def list_connections(self, workspace_id):
                return [{"connectionId": "foreign"}]

        with self.assertRaisesRegex(ExecutionError, "workspace connection mismatch"):
            run_stage1_submission(
                self.root / "attempt",
                ForeignConnection(),
                runner=runner,
                workspace_id="workspace-1",
                poll_interval=0,
            )

    def test_stage1_resync_reuses_exact_state_connections_without_terraform(
        self,
    ) -> None:
        elt = self.root / "attempt" / "elt"
        elt.mkdir(parents=True)
        state_path = elt / "terraform.tfstate"
        state_path.write_text(
            json.dumps(
                {
                    "resources": [
                        {
                            "type": "airbyte_connection",
                            "instances": [
                                {"attributes": {"connection_id": "abc"}}
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        state_path.chmod(0o644)
        airbyte = FakeSyncClient()

        result = rerun_stage1_syncs(
            self.root / "attempt",
            airbyte,
            workspace_id="workspace-1",
            poll_interval=0,
        )

        self.assertEqual(result.connection_ids, ("abc",))
        self.assertEqual(result.statuses, {"abc": "succeeded"})
        self.assertRegex(result.terraform_state_digest, r"^[0-9a-f]{64}$")
        self.assertRegex(result.terraform_input_tree_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(airbyte.ids, ("abc",))
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_stage1_rejects_invalid_job_ids_in_duck_typed_receipt(self) -> None:
        elt = self.root / "attempt" / "elt"
        elt.mkdir(parents=True)
        (elt / "main.tf").write_text("terraform {}\n", encoding="utf-8")
        state = {
            "resources": [
                {
                    "type": "airbyte_connection",
                    "instances": [
                        {"attributes": {"connection_id": "one"}},
                        {"attributes": {"connection_id": "two"}},
                    ],
                }
            ]
        }

        class InvalidReceiptClient(FakeSyncClient):
            def trigger_and_wait_receipt(self, ids, *, poll_interval, timeout):
                return SimpleNamespace(
                    statuses={value: "succeeded" for value in ids},
                    job_ids={"one": 5, "two": 5},
                )

        with self.assertRaisesRegex(ExecutionError, "distinct connections"):
            run_stage1_submission(
                self.root / "attempt",
                InvalidReceiptClient(),
                runner=FakeRunner(state),
                poll_interval=0,
            )

    def test_stage1_resync_refuses_foreign_workspace_connection(self) -> None:
        elt = self.root / "attempt" / "elt"
        elt.mkdir(parents=True)
        (elt / "terraform.tfstate").write_text(
            json.dumps(
                {
                    "resources": [
                        {
                            "type": "airbyte_connection",
                            "instances": [
                                {"attributes": {"connection_id": "submitted"}}
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        class ForeignConnection(FakeSyncClient):
            def list_connections(self, workspace_id):
                return [{"connectionId": "foreign"}]

        client = ForeignConnection()
        with self.assertRaisesRegex(ExecutionError, "workspace connection mismatch"):
            rerun_stage1_syncs(
                self.root / "attempt",
                client,
                workspace_id="workspace-1",
                poll_interval=0,
            )
        self.assertIsNone(client.ids)

    def _stage2_workspace(
        self,
        *,
        project_rel: str = "elt",
        profiles_rel: str | None = None,
        database: str = "demo_db",
    ) -> Path:
        """A minimal INSTALLED workspace the fail-closed preflight accepts."""
        workspace = self.root / "attempt"
        project = workspace / project_rel
        project.mkdir(parents=True, exist_ok=True)
        profiles = workspace / profiles_rel if profiles_rel else project
        profiles.mkdir(parents=True, exist_ok=True)
        (workspace / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "snowflake": {
                        "config": {
                            "database": database,
                            "schema": "AIRBYTE_SCHEMA",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        (project / "dbt_project.yml").write_text(
            "name: demo\nprofile: demo_profile\n", encoding="utf-8"
        )
        models = project / "models"
        models.mkdir(exist_ok=True)
        (models / "example.sql").write_text("select 1 as value\n", encoding="utf-8")
        (profiles / "profiles.yml").write_text(
            yaml.safe_dump(
                {
                    "demo_profile": {
                        "target": "prod",
                        "outputs": {
                            "prod": {
                                "type": "snowflake",
                                "database": database,
                                "schema": "AIRBYTE_SCHEMA",
                                **_snowflake_profile_bindings(),
                            }
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        return workspace

    def test_stage2_runs_only_existing_dbt_project(self) -> None:
        elt = self.root / "attempt" / "elt"
        elt.mkdir(parents=True)
        runner = FakeRunner()
        with self.assertRaisesRegex(ExecutionError, "dbt_project.yml"):
            run_stage2_submission(self.root / "attempt", runner=runner)
        workspace = self._stage2_workspace()
        result = run_stage2_submission(
            workspace, runner=runner, dbt=("python", "-m", "dbt.cli.main")
        )
        self.assertEqual(result.project_dir, elt.resolve())
        # The fail-closed preflight verifies the pinned dbt versions BEFORE
        # the run command is issued.
        self.assertEqual(
            runner.calls[-2][0],
            ("python", "-m", "dbt.cli.main", "--version"),
        )
        self.assertEqual(runner.calls[-1][0][:4], ("python", "-m", "dbt.cli.main", "run"))
        self.assertEqual(
            runner.calls[-1][0][-4:],
            ("--project-dir", ".", "--profiles-dir", "."),
        )
        self.assertIsNotNone(result.preflight)
        self.assertIs(result.preflight.destination, Destination.SNOWFLAKE)
        self.assertEqual(result.preflight.dbt_core_version, DBT_CORE_VERSION)
        self.assertEqual(result.preflight.namespace, "demo_db")
        self.assertEqual(result.preflight.physical_container, "")
        self.assertRegex(result.dbt_input_tree_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(result.dbt_expected_model_ids, ("model.demo.example",))
        self.assertEqual(result.dbt_observed_model_ids, ())
        self.assertLessEqual(
            datetime.fromisoformat(result.execution_started_at.replace("Z", "+00:00")),
            datetime.fromisoformat(result.execution_completed_at.replace("Z", "+00:00")),
        )

    def test_stage2_captures_fresh_bounded_run_results_provenance(self) -> None:
        workspace = self._stage2_workspace()
        invocation_id = "fresh-dbt-invocation"

        class ProvenanceRunner(FakeRunner):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                command = tuple(str(value) for value in argv)
                if "run" in command:
                    target = command[command.index("--target-path") + 1]
                    target_dir = Path(kwargs["cwd"]) / target
                    (target_dir / "run_results.json").write_text(
                        json.dumps(
                            {
                                "metadata": {"invocation_id": invocation_id},
                                "results": [
                                    {
                                        "status": "success",
                                        "unique_id": "model.demo.example",
                                    }
                                ],
                            },
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                return result

        result = run_stage2_submission(
            workspace,
            runner=ProvenanceRunner(),
            capture_provenance=True,
        )
        self.assertEqual(result.dbt_invocation_id, invocation_id)
        self.assertRegex(result.dbt_run_results_digest, r"^[0-9a-f]{64}$")
        self.assertRegex(result.dbt_input_tree_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(result.dbt_expected_model_ids, ("model.demo.example",))
        self.assertEqual(result.dbt_observed_model_ids, ("model.demo.example",))
        self.assertEqual(
            validate_stage2_execution_receipt(result),
            ("model.demo.example",),
        )
        self.assertIsNotNone(result.run_results_path)
        self.assertTrue(result.run_results_path.is_file())
        self.assertEqual(result.run_results_path.name, "run_results.json")
        self.assertTrue(
            result.run_results_path.parent.name.startswith(
                ".elt-taskgen-dbt-target-"
            )
        )

    def test_stage2_provenance_refuses_a_missing_or_symlinked_result(self) -> None:
        workspace = self._stage2_workspace()
        with self.assertRaisesRegex(ExecutionError, "run_results.json"):
            run_stage2_submission(
                workspace,
                runner=FakeRunner(),
                capture_provenance=True,
            )

        class SymlinkRunner(FakeRunner):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                command = tuple(str(value) for value in argv)
                if "run" in command:
                    target = command[command.index("--target-path") + 1]
                    target_dir = Path(kwargs["cwd"]) / target
                    outside = workspace / "outside-run-results.json"
                    outside.write_text(
                        json.dumps(
                            {"metadata": {"invocation_id": "stale"}}
                        ),
                        encoding="utf-8",
                    )
                    (target_dir / "run_results.json").symlink_to(outside)
                return result

        with self.assertRaisesRegex(ExecutionError, "non-symlink"):
            run_stage2_submission(
                workspace,
                runner=SymlinkRunner(),
                capture_provenance=True,
            )

    def test_stage2_provenance_requires_successful_complete_model_roster(self) -> None:
        workspace = self._stage2_workspace()

        class ResultsRunner(FakeRunner):
            def __init__(self, results):
                super().__init__()
                self.results = results

            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                command = tuple(str(value) for value in argv)
                if "run" in command:
                    target = command[command.index("--target-path") + 1]
                    target_dir = Path(kwargs["cwd"]) / target
                    (target_dir / "run_results.json").write_text(
                        json.dumps(
                            {
                                "metadata": {"invocation_id": "checked"},
                                "results": self.results,
                            }
                        ),
                        encoding="utf-8",
                    )
                return result

        cases = (
            ([], "no model results"),
            (
                [{"status": "error", "unique_id": "model.demo.example"}],
                "did not succeed",
            ),
            (
                [{"status": "success", "unique_id": "model.demo.other"}],
                "model roster mismatch",
            ),
            (
                [
                    {"status": "success", "unique_id": "model.demo.example"},
                    {"status": "success", "unique_id": "model.demo.example"},
                ],
                "duplicate model results",
            ),
        )
        for results, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ExecutionError, message
            ):
                run_stage2_submission(
                    workspace,
                    runner=ResultsRunner(results),
                    capture_provenance=True,
                )

    def test_stage2_refuses_input_tree_mutation_during_run(self) -> None:
        workspace = self._stage2_workspace()
        model = workspace / "elt" / "models" / "example.sql"

        class MutatingRunner(FakeRunner):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                if "run" in tuple(str(value) for value in argv):
                    model.write_text("select 2 as value\n", encoding="utf-8")
                return result

        with self.assertRaisesRegex(ExecutionError, "changed during execution"):
            run_stage2_submission(workspace, runner=MutatingRunner())

    def test_stage2_receipt_revalidation_refuses_artifact_mutation(self) -> None:
        workspace = self._stage2_workspace()

        class ProvenanceRunner(FakeRunner):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                command = tuple(str(value) for value in argv)
                if "run" in command:
                    target = command[command.index("--target-path") + 1]
                    target_dir = Path(kwargs["cwd"]) / target
                    (target_dir / "run_results.json").write_text(
                        json.dumps(
                            {
                                "metadata": {"invocation_id": "fresh"},
                                "results": [
                                    {
                                        "status": "success",
                                        "unique_id": "model.demo.example",
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                return result

        execution = run_stage2_submission(
            workspace,
            runner=ProvenanceRunner(),
            capture_provenance=True,
        )
        assert execution.run_results_path is not None
        execution.run_results_path.write_text(
            json.dumps(
                {
                    "metadata": {"invocation_id": "fresh"},
                    "results": [
                        {
                            "status": "error",
                            "unique_id": "model.demo.example",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ExecutionError, "did not succeed"):
            validate_stage2_execution_receipt(execution)

        outside_target = workspace / ".elt-taskgen-dbt-target-forged"
        outside_target.mkdir()
        outside_artifact = outside_target / "run_results.json"
        outside_artifact.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ExecutionError, "submitted project"):
            validate_stage2_execution_receipt(
                replace(execution, run_results_path=outside_artifact)
            )

    def test_stage2_input_digest_changes_with_model_bytes(self) -> None:
        workspace = self._stage2_workspace()
        model = workspace / "elt" / "models" / "example.sql"
        first = run_stage2_submission(workspace, runner=FakeRunner())
        model.write_text("select 2 as value\n", encoding="utf-8")
        second = run_stage2_submission(workspace, runner=FakeRunner())
        self.assertNotEqual(
            first.dbt_input_tree_digest, second.dbt_input_tree_digest
        )

    def test_stage2_uses_container_portable_workspace_relative_paths(self) -> None:
        workspace = self._stage2_workspace(
            project_rel="dbt", profiles_rel="profiles"
        )
        host = FakeRunner()
        image = "runner@sha256:" + "c" * 64
        runner = DockerRunner(workspace, image, host_runner=host)

        run_stage2_submission(
            workspace,
            runner=runner,
            project_dir=Path("dbt"),
            profiles_dir=Path("profiles"),
        )

        command = host.calls[-1][0]
        self.assertIn("/workspace/dbt", command)
        self.assertEqual(
            command[-7:],
            (
                image,
                "dbt",
                "run",
                "--project-dir",
                ".",
                "--profiles-dir",
                "../profiles",
            ),
        )
        self.assertNotIn(str(workspace), command[-7:])
        # The preflight's dbt --version ran through the SAME confined runner.
        self.assertEqual(host.calls[0][0][-3:], (image, "dbt", "--version"))

    def test_stage2_provenance_target_stays_container_portable(self) -> None:
        workspace = self._stage2_workspace(
            project_rel="dbt", profiles_rel="profiles"
        )

        class ArtifactHost(FakeRunner):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                command = tuple(str(value) for value in argv)
                if "--target-path" in command:
                    relative_target = command[command.index("--target-path") + 1]
                    target_dir = workspace / "dbt" / relative_target
                    (target_dir / "run_results.json").write_text(
                        json.dumps(
                            {
                                "metadata": {"invocation_id": "docker-fresh"},
                                "results": [
                                    {
                                        "status": "success",
                                        "unique_id": "model.demo.example",
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                return result

        host = ArtifactHost()
        image = "runner@sha256:" + "c" * 64
        runner = DockerRunner(workspace, image, host_runner=host)
        result = run_stage2_submission(
            workspace,
            runner=runner,
            project_dir=Path("dbt"),
            profiles_dir=Path("profiles"),
            capture_provenance=True,
        )

        command = host.calls[-1][0]
        target_arg = command[command.index("--target-path") + 1]
        self.assertTrue(target_arg.startswith(".elt-taskgen-dbt-target-"))
        self.assertFalse(Path(target_arg).is_absolute())
        self.assertNotIn(str(workspace), target_arg)
        self.assertEqual(result.dbt_invocation_id, "docker-fresh")
        self.assertEqual(
            validate_stage2_execution_receipt(result),
            ("model.demo.example",),
        )

    def test_stage2_rejects_project_or_profiles_outside_workspace(self) -> None:
        workspace = self.root / "attempt"
        project = workspace / "elt"
        project.mkdir(parents=True)
        (project / "dbt_project.yml").write_text("name: demo\n", encoding="utf-8")
        outside = self.root / "outside"
        outside.mkdir()

        with self.assertRaisesRegex(ExecutionError, "inside the solver workspace"):
            run_stage2_submission(
                workspace,
                runner=FakeRunner(),
                profiles_dir=outside,
            )

    def test_docker_runner_mounts_only_solver_workspace_and_no_host_environment(self) -> None:
        workspace = self.root / "attempt"
        elt = workspace / "elt"
        elt.mkdir(parents=True)
        host = FakeRunner()
        image = "runner@sha256:" + "a" * 64
        runner = DockerRunner(workspace, image, host_runner=host)

        runner.run(("terraform", "validate"), cwd=elt)

        command = host.calls[0][0]
        self.assertEqual(command[0:3], ("docker", "run", "--rm"))
        self.assertIn(
            f"type=bind,src={workspace.resolve()},dst=/workspace",
            command,
        )
        self.assertNotIn(
            f"type=bind,src={workspace.resolve()},dst=/workspace,rw",
            command,
        )
        self.assertNotIn("/var/run/docker.sock", " ".join(command))
        self.assertNotIn("--env", command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertEqual(command[-3:], (image, "terraform", "validate"))

    def test_docker_runner_requires_digest_and_confines_paths(self) -> None:
        workspace = self.root / "attempt"
        workspace.mkdir()
        with self.assertRaisesRegex(ValueError, "immutable @sha256"):
            DockerRunner(workspace, "runner:latest", host_runner=FakeRunner())
        runner = DockerRunner(
            workspace,
            "runner@sha256:" + "b" * 64,
            host_runner=FakeRunner(),
        )
        with self.assertRaisesRegex(ValueError, "inside the solver workspace"):
            runner.run(("dbt", "run"), cwd=self.root)


class Stage2PreflightTests(unittest.TestCase):
    """IR-009: run-stage2's fail-closed dbt/profile/namespace preflight."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work_dir = Path(self.temp.name) / "attempt"
        self.project = self.work_dir / "elt"
        self.project.mkdir(parents=True)

    def write_config(self, payload: dict) -> None:
        (self.work_dir / "config.yaml").write_text(
            yaml.safe_dump(payload), encoding="utf-8"
        )

    def write_project(self, profile: str = "demo_profile") -> None:
        (self.project / "dbt_project.yml").write_text(
            f"name: demo\nprofile: {profile}\n", encoding="utf-8"
        )

    def write_profiles(self, payload: dict) -> None:
        (self.project / "profiles.yml").write_text(
            yaml.safe_dump(payload), encoding="utf-8"
        )

    def snowflake_setup(
        self, *, database: str = "demo_db", output: dict | None = None
    ) -> None:
        self.write_config(
            {
                "snowflake": {
                    "config": {
                        "database": database,
                        "schema": "AIRBYTE_SCHEMA",
                    }
                }
            }
        )
        self.write_project()
        merged: dict = {
            "type": "snowflake",
            "database": database,
            "schema": "AIRBYTE_SCHEMA",
            **_snowflake_profile_bindings(),
        }
        merged.update(output or {})
        self.write_profiles(
            {"demo_profile": {"target": "prod", "outputs": {"prod": merged}}}
        )

    def redshift_setup(self, *, output: dict | None = None) -> FakeRunner:
        adapter, pin = DBT_ADAPTER_CONTRACTS[Destination.REDSHIFT]
        self.write_config(
            {
                "redshift": {
                    "config": {
                        "database": "attempt_database",
                        "schema": "demo_db",
                    }
                }
            }
        )
        self.write_project()
        merged: dict = {
            "type": "redshift",
            "dbname": "attempt_database",
            "schema": "demo_db",
        }
        merged.update(output or {})
        self.write_profiles(
            {"demo_profile": {"target": "prod", "outputs": {"prod": merged}}}
        )
        return FakeRunner(
            version_stdout=_dbt_version_stdout(
                adapter=adapter, adapter_version=pin
            )
        )

    def preflight(self, runner: FakeRunner | None = None, **kwargs):
        return stage2_preflight(
            self.work_dir,
            self.project,
            self.project,
            runner=runner or FakeRunner(),
            **kwargs,
        )

    def assert_code(
        self, code: str, runner: FakeRunner | None = None, **kwargs
    ) -> Stage2PreflightError:
        runner = runner or FakeRunner()
        with self.assertRaises(Stage2PreflightError) as ctx:
            self.preflight(runner=runner, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        self.assertFalse(
            [call for call in runner.calls if "run" in call[0]],
            "the preflight must never issue a dbt run command",
        )
        return ctx.exception

    def test_preflight_returns_verified_versions_and_namespace(self) -> None:
        self.snowflake_setup()
        runner = FakeRunner()
        result = self.preflight(runner=runner)
        self.assertIs(result.destination, Destination.SNOWFLAKE)
        self.assertEqual(result.dbt_core_version, DBT_CORE_VERSION)
        self.assertEqual(
            result.adapter_version,
            DBT_ADAPTER_CONTRACTS[Destination.SNOWFLAKE][1],
        )
        self.assertEqual(result.profile_name, "demo_profile")
        self.assertEqual(result.target_name, "prod")
        self.assertEqual(result.namespace, "demo_db")
        self.assertEqual(runner.calls[0][0], ("dbt", "--version"))

    def test_namespace_comparison_is_trimmed_and_case_insensitive(self) -> None:
        self.snowflake_setup(output={"database": "  DEMO_DB  "})
        self.assertEqual(self.preflight().namespace, "demo_db")

    def test_missing_installed_config_fails_before_any_command(self) -> None:
        runner = FakeRunner()
        self.assert_code("config", runner=runner)
        self.assertEqual(runner.calls, [])

    def test_wrong_core_version_fails_closed(self) -> None:
        self.snowflake_setup()
        self.assert_code(
            "dbt-version",
            runner=FakeRunner(
                version_stdout=_dbt_version_stdout(core="1.10.0")
            ),
        )

    def test_wrong_adapter_version_fails_closed(self) -> None:
        self.snowflake_setup()
        self.assert_code(
            "dbt-version",
            runner=FakeRunner(
                version_stdout=_dbt_version_stdout(adapter_version="1.11.9")
            ),
        )

    def test_absent_adapter_plugin_fails_closed(self) -> None:
        self.snowflake_setup()
        self.assert_code(
            "dbt-version",
            runner=FakeRunner(
                version_stdout=_dbt_version_stdout(
                    adapter="postgres", adapter_version="1.9.1"
                )
            ),
        )

    def test_missing_profiles_file_fails_closed(self) -> None:
        self.snowflake_setup()
        (self.project / "profiles.yml").unlink()
        self.assert_code("profile")

    def test_missing_profile_entry_fails_closed(self) -> None:
        self.snowflake_setup()
        self.write_project(profile="other_profile")
        self.assert_code("profile")

    def test_wrong_output_type_fails_closed(self) -> None:
        self.snowflake_setup(output={"type": "postgres"})
        self.assert_code("profile")

    def test_templated_profile_value_fails_closed(self) -> None:
        self.snowflake_setup(output={"database": "{{ env_var('DBT_DB') }}"})
        error = self.assert_code("profile")
        self.assertIn("templated", str(error))

    def test_snowflake_connection_fields_require_fixed_env_bindings(self) -> None:
        self.snowflake_setup(output={"password": "must-not-leak"})
        error = self.assert_code("profile")
        self.assertIn("fixed ELT_TASKGEN_SNOWFLAKE", str(error))
        self.assertNotIn("must-not-leak", str(error))

        self.snowflake_setup(
            output={"user": "{{ env_var('UNRELATED_HOST_VALUE') }}"}
        )
        error = self.assert_code("profile")
        self.assertIn("invalid fields: ['user']", str(error))

    def test_stage2_forwards_environment_to_version_and_run(self) -> None:
        self.snowflake_setup()
        runner = FakeRunner()
        environment = {"ELT_TASKGEN_SNOWFLAKE_PASSWORD": "scoped-secret"}
        run_stage2_submission(
            self.work_dir, runner=runner, env=environment
        )
        self.assertEqual(
            runner.environments, [environment, environment]
        )

    def test_namespace_mismatch_fails_closed(self) -> None:
        self.snowflake_setup(output={"database": "other_db"})
        error = self.assert_code("namespace")
        self.assertIn("demo_db", str(error))

    def test_snowflake_fixed_schema_must_match_config_and_profile(self) -> None:
        self.snowflake_setup(output={"schema": "other_schema"})
        error = self.assert_code("namespace")
        self.assertIn("AIRBYTE_SCHEMA", str(error))

        self.snowflake_setup()
        config = yaml.safe_load((self.work_dir / "config.yaml").read_text())
        config["snowflake"]["config"]["schema"] = "other_schema"
        self.write_config(config)
        runner = FakeRunner()
        error = self.assert_code("config", runner=runner)
        self.assertIn("AIRBYTE_SCHEMA", str(error))
        self.assertEqual(runner.calls, [])

    def test_session_overrides_fail_closed(self) -> None:
        self.snowflake_setup(
            output={"session_parameters": {"QUERY_TAG": "sneaky"}}
        )
        self.assert_code("session")

    def test_databricks_container_must_match_installed_catalog(self) -> None:
        adapter, pin = DBT_ADAPTER_CONTRACTS[Destination.DATABRICKS]
        runner = FakeRunner(
            version_stdout=_dbt_version_stdout(
                adapter=adapter, adapter_version=pin
            )
        )
        self.write_config(
            {
                "databricks": {
                    "config": {
                        "database": "attempt_catalog",
                        "schema": "demo_db",
                    }
                }
            }
        )
        self.write_project()
        output = {
            "type": "databricks",
            "schema": "demo_db",
            "catalog": "other_catalog",
        }
        self.write_profiles(
            {"demo_profile": {"target": "prod", "outputs": {"prod": output}}}
        )
        self.assert_code("namespace", runner=runner)
        output["catalog"] = "ATTEMPT_CATALOG"
        self.write_profiles(
            {"demo_profile": {"target": "prod", "outputs": {"prod": output}}}
        )
        result = self.preflight(
            runner=FakeRunner(
                version_stdout=_dbt_version_stdout(
                    adapter=adapter, adapter_version=pin
                )
            )
        )
        self.assertIs(result.destination, Destination.DATABRICKS)
        self.assertEqual(result.physical_container, "attempt_catalog")

    def test_databricks_empty_installed_container_fails_closed(self) -> None:
        self.write_config(
            {"databricks": {"config": {"database": "", "schema": "demo_db"}}}
        )
        runner = FakeRunner()
        self.assert_code("config", runner=runner)
        self.assertEqual(runner.calls, [])

    def test_databricks_session_properties_fail_closed(self) -> None:
        adapter, pin = DBT_ADAPTER_CONTRACTS[Destination.DATABRICKS]
        runner = FakeRunner(
            version_stdout=_dbt_version_stdout(
                adapter=adapter, adapter_version=pin
            )
        )
        self.write_config(
            {
                "databricks": {
                    "config": {
                        "database": "attempt_catalog",
                        "schema": "demo_db",
                    }
                }
            }
        )
        self.write_project()
        self.write_profiles(
            {
                "demo_profile": {
                    "target": "prod",
                    "outputs": {
                        "prod": {
                            "type": "databricks",
                            "schema": "demo_db",
                            "catalog": "attempt_catalog",
                            "session_properties": {"spark.sql.session.timeZone": "UTC"},
                        }
                    },
                }
            }
        )
        self.assert_code("session", runner=runner)

    def test_redshift_profile_requires_dbname_schema_and_pinned_adapter(self) -> None:
        runner = self.redshift_setup()
        result = self.preflight(runner=runner)
        self.assertIs(result.destination, Destination.REDSHIFT)
        self.assertEqual(result.namespace, "demo_db")
        self.assertEqual(
            result.adapter_version,
            DBT_ADAPTER_CONTRACTS[Destination.REDSHIFT][1],
        )

        runner = self.redshift_setup(output={"dbname": "other_database"})
        self.assert_code("namespace", runner=runner)

        runner = self.redshift_setup(output={"schema": "other_schema"})
        self.assert_code("namespace", runner=runner)

    def test_redshift_search_path_and_templated_dbname_fail_closed(self) -> None:
        runner = self.redshift_setup(output={"search_path": "public"})
        self.assert_code("session", runner=runner)

        runner = self.redshift_setup(
            output={"dbname": "{{ env_var('REDSHIFT_DB') }}"}
        )
        error = self.assert_code("profile", runner=runner)
        self.assertIn("templated", str(error))

    def test_run_stage2_issues_no_dbt_run_when_preflight_fails(self) -> None:
        self.snowflake_setup()
        runner = FakeRunner(version_stdout=_dbt_version_stdout(core="1.10.0"))
        with self.assertRaises(Stage2PreflightError):
            run_stage2_submission(self.work_dir, runner=runner)
        self.assertEqual([call[0] for call in runner.calls], [("dbt", "--version")])


if __name__ == "__main__":
    unittest.main()

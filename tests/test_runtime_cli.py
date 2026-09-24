from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from elt_taskgen.destinations import Destination
from elt_taskgen.cli import (
    _write_runtime_secret,
    build_parser,
    cmd_runtime_provision_databricks,
    cmd_runtime_provision_redshift,
    cmd_runtime_provision_snowflake,
    cmd_runtime_resync_stage1,
)
from elt_taskgen.runtime.execution import (
    Stage1Execution,
    Stage2Execution,
    Stage2Preflight,
)


class RuntimeCliTests(unittest.TestCase):
    def test_certification_attest_unbound_has_no_release_bootstrap_cycle(
        self,
    ) -> None:
        from elt_taskgen import cli

        args = build_parser().parse_args(
            [
                "runtime",
                "certification",
                "attest-unbound",
                "--mount-root",
                "/tmp/captured-input",
                "--out",
                "/tmp/sandbox-attestation.json",
                "--agents-config",
                "/tmp/agents.yaml",
                "--image-digest",
                "runner@sha256:" + "a" * 64,
                "--workspace-template-sha256",
                "b" * 64,
            ]
        )
        self.assertFalse(hasattr(args, "release"))
        self.assertFalse(hasattr(args, "task_id"))
        record = mock.Mock()
        record.model_dump.return_value = {"run_id": "", "tier": "A"}
        output = io.StringIO()
        with mock.patch(
            "elt_taskgen.runtime.certification_lifecycle."
            "mint_unbound_sandbox_attestation",
            return_value=record,
        ) as mint, contextlib.redirect_stdout(output):
            self.assertEqual(args.func(args), 0)

        mint.assert_called_once_with(
            mount_root=Path("/tmp/captured-input"),
            out=Path("/tmp/sandbox-attestation.json"),
            agents_config=Path("/tmp/agents.yaml"),
            image_digest="runner@sha256:" + "a" * 64,
            workspace_template_sha256="b" * 64,
        )
        self.assertEqual(
            json.loads(output.getvalue()), {"run_id": "", "tier": "A"}
        )

    def test_source_up_waits_for_airbyte_after_seeding(self) -> None:
        from elt_taskgen import cli

        args = build_parser().parse_args(
            [
                "runtime",
                "source-up",
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--environment-dir",
                "/tmp/environment",
            ]
        )
        self.assertEqual(args.airbyte_readiness_timeout, 180.0)
        self.assertEqual(args.airbyte_stability_window, 30.0)
        self.assertEqual(args.airbyte_readiness_poll_interval, 5.0)

        events: list[str] = []
        environment = mock.Mock(network="source-network")
        environment.start.side_effect = lambda **_: events.append("start")
        environment.seed.side_effect = lambda: events.append("seed")

        def wait(**kwargs):
            events.append("wait")
            self.assertEqual(
                kwargs,
                {
                    "airbyte_container": "airbyte-abctl-control-plane",
                    "timeout": 180.0,
                    "stable_for": 30.0,
                    "poll_interval": 5.0,
                },
            )

        output = io.StringIO()
        with mock.patch.object(
            cli, "_runtime_source_environment", return_value=environment
        ), mock.patch(
            "elt_taskgen.runtime.source_environment.wait_for_airbyte_control_plane",
            side_effect=wait,
        ), contextlib.redirect_stdout(output):
            self.assertEqual(cli.cmd_runtime_source_up(args), 0)

        self.assertEqual(events, ["start", "seed", "wait"])
        environment.stop.assert_not_called()
        self.assertIn("running, seeded, and Airbyte-ready", output.getvalue())

    def test_source_up_readiness_timeout_cleans_up_source_stack(self) -> None:
        from elt_taskgen import cli
        from elt_taskgen.runtime.source_environment import SourceEnvironmentError

        args = build_parser().parse_args(
            [
                "runtime",
                "source-up",
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--environment-dir",
                "/tmp/environment",
                "--airbyte-readiness-timeout",
                "90",
                "--airbyte-stability-window",
                "20",
                "--airbyte-readiness-poll-interval",
                "2",
            ]
        )
        environment = mock.Mock(network="source-network")

        with mock.patch.object(
            cli, "_runtime_source_environment", return_value=environment
        ), mock.patch(
            "elt_taskgen.runtime.source_environment.wait_for_airbyte_control_plane",
            side_effect=SourceEnvironmentError("readiness timed out"),
        ) as wait, self.assertRaisesRegex(cli.CliUsageError, "readiness timed out"):
            cli.cmd_runtime_source_up(args)

        environment.start.assert_called_once_with(
            airbyte_container="airbyte-abctl-control-plane"
        )
        environment.seed.assert_called_once_with()
        environment.stop.assert_called_once_with(
            airbyte_container="airbyte-abctl-control-plane"
        )
        wait.assert_called_once_with(
            airbyte_container="airbyte-abctl-control-plane",
            timeout=90.0,
            stable_for=20.0,
            poll_interval=2.0,
        )

    def test_source_up_rejects_impossible_readiness_window_before_mutation(
        self,
    ) -> None:
        from elt_taskgen import cli

        args = build_parser().parse_args(
            [
                "runtime",
                "source-up",
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--environment-dir",
                "/tmp/environment",
                "--airbyte-readiness-timeout",
                "10",
                "--airbyte-stability-window",
                "10",
            ]
        )
        never = mock.Mock(side_effect=AssertionError("must not be reached"))

        with mock.patch.object(
            cli, "_runtime_source_environment", never
        ), self.assertRaisesRegex(
            cli.CliUsageError,
            "stability window must be shorter than the readiness timeout",
        ):
            cli.cmd_runtime_source_up(args)

        never.assert_not_called()

    def test_source_up_interrupt_during_gate_cleans_up_and_propagates(self) -> None:
        from elt_taskgen import cli

        args = build_parser().parse_args(
            [
                "runtime",
                "source-up",
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--environment-dir",
                "/tmp/environment",
            ]
        )
        environment = mock.Mock(network="source-network")

        with mock.patch.object(
            cli, "_runtime_source_environment", return_value=environment
        ), mock.patch(
            "elt_taskgen.runtime.source_environment.wait_for_airbyte_control_plane",
            side_effect=KeyboardInterrupt,
        ), self.assertRaises(KeyboardInterrupt):
            cli.cmd_runtime_source_up(args)

        environment.stop.assert_called_once_with(
            airbyte_container="airbyte-abctl-control-plane"
        )

    def test_source_up_seed_failure_cleans_up_source_stack(self) -> None:
        from elt_taskgen import cli
        from elt_taskgen.runtime.source_environment import SourceEnvironmentError

        args = build_parser().parse_args(
            [
                "runtime",
                "source-up",
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--environment-dir",
                "/tmp/environment",
            ]
        )
        environment = mock.Mock(network="source-network")
        environment.seed.side_effect = SourceEnvironmentError("seed failed")

        with mock.patch.object(
            cli, "_runtime_source_environment", return_value=environment
        ), self.assertRaisesRegex(cli.CliUsageError, "seed failed"):
            cli.cmd_runtime_source_up(args)

        environment.stop.assert_called_once_with(
            airbyte_container="airbyte-abctl-control-plane"
        )

    def test_source_up_reports_cleanup_failure_without_leaking_detail(self) -> None:
        from elt_taskgen import cli
        from elt_taskgen.runtime.process import ProcessFailure
        from elt_taskgen.runtime.source_environment import SourceEnvironmentError

        args = build_parser().parse_args(
            [
                "runtime",
                "source-up",
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--environment-dir",
                "/tmp/environment",
            ]
        )
        environment = mock.Mock(network="source-network")
        environment.stop.side_effect = ProcessFailure("secret cleanup output")

        with mock.patch.object(
            cli, "_runtime_source_environment", return_value=environment
        ), mock.patch(
            "elt_taskgen.runtime.source_environment.wait_for_airbyte_control_plane",
            side_effect=SourceEnvironmentError("readiness timed out"),
        ), self.assertRaisesRegex(
            cli.CliUsageError,
            "readiness timed out; automatic source cleanup failed",
        ) as raised:
            cli.cmd_runtime_source_up(args)

        self.assertNotIn("secret cleanup output", str(raised.exception))

    def test_source_up_readiness_timings_must_be_positive_and_finite(self) -> None:
        base = [
            "runtime",
            "source-up",
            "--release",
            "/tmp/release",
            "--task-id",
            "task",
            "--environment-dir",
            "/tmp/environment",
        ]
        flags = (
            "--airbyte-readiness-timeout",
            "--airbyte-stability-window",
            "--airbyte-readiness-poll-interval",
        )

        for flag in flags:
            for value in ("0", "-1", "nan", "inf"):
                with self.subTest(flag=flag, value=value):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
                        SystemExit
                    ) as raised:
                        build_parser().parse_args([*base, f"{flag}={value}"])
                    self.assertEqual(raised.exception.code, 2)

    def test_run_stage2_prints_closed_reconstructable_receipt(self) -> None:
        from elt_taskgen import cli

        image = "runner@sha256:" + "a" * 64
        preflight = Stage2Preflight(
            destination=Destination.DATABRICKS,
            dbt_core_version="1.10.13",
            adapter_version="1.10.0",
            profile_name="demo",
            target_name="certification",
            namespace="attempt_schema",
            physical_container="attempt_catalog",
        )
        result = Stage2Execution(
            dbt_expected_model_ids=("model.demo.customer_summary",),
            dbt_input_tree_digest="d" * 64,
            dbt_invocation_id="invocation-id",
            dbt_observed_model_ids=("model.demo.customer_summary",),
            dbt_run_results_digest="b" * 64,
            execution_completed_at="2026-09-03T00:00:01Z",
            execution_started_at="2026-09-03T00:00:00Z",
            preflight=preflight,
            profiles_dir=Path("/tmp/work/profiles"),
            project_dir=Path("/tmp/work/elt"),
            run_results_path=Path(
                "/tmp/work/elt/.elt-taskgen-dbt-target-fresh/run_results.json"
            ),
            runner_image=image,
        )
        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args(
                [
                    "runtime",
                    "run-stage2",
                    "--work-dir",
                    directory,
                    "--runner-image",
                    image,
                ]
            )
            output = io.StringIO()
            with mock.patch(
                "elt_taskgen.runtime.process.DockerRunner", return_value=object()
            ), mock.patch.object(
                cli,
                "_runtime_stage2_environment",
                return_value={"DATABRICKS_TOKEN": "must-not-enter-receipt"},
            ), mock.patch(
                "elt_taskgen.runtime.execution.run_stage2_submission",
                return_value=result,
            ), contextlib.redirect_stdout(output):
                self.assertEqual(cli.cmd_runtime_run_stage2(args), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(
            receipt,
            {
                "dbt_expected_model_ids": ["model.demo.customer_summary"],
                "dbt_input_tree_digest": "d" * 64,
                "dbt_invocation_id": "invocation-id",
                "dbt_observed_model_ids": ["model.demo.customer_summary"],
                "dbt_run_results_digest": "b" * 64,
                "execution_completed_at": "2026-09-03T00:00:01Z",
                "execution_started_at": "2026-09-03T00:00:00Z",
                "preflight": {
                    "adapter_version": "1.10.0",
                    "dbt_core_version": "1.10.13",
                    "destination": "databricks",
                    "namespace": "attempt_schema",
                    "physical_container": "attempt_catalog",
                    "profile_name": "demo",
                    "target_name": "certification",
                },
                "profiles_dir": "/tmp/work/profiles",
                "project_dir": "/tmp/work/elt",
                "run_results_path": (
                    "/tmp/work/elt/.elt-taskgen-dbt-target-fresh/"
                    "run_results.json"
                ),
                "runner_image": image,
            },
        )
        self.assertNotIn("must-not-enter-receipt", output.getvalue())

        preflight_payload = receipt["preflight"]
        reconstructed = Stage2Execution(
            project_dir=Path(receipt["project_dir"]),
            profiles_dir=Path(receipt["profiles_dir"]),
            preflight=Stage2Preflight(
                destination=Destination(preflight_payload["destination"]),
                dbt_core_version=preflight_payload["dbt_core_version"],
                adapter_version=preflight_payload["adapter_version"],
                profile_name=preflight_payload["profile_name"],
                target_name=preflight_payload["target_name"],
                namespace=preflight_payload["namespace"],
                physical_container=preflight_payload["physical_container"],
            ),
            run_results_path=Path(receipt["run_results_path"]),
            dbt_invocation_id=receipt["dbt_invocation_id"],
            dbt_run_results_digest=receipt["dbt_run_results_digest"],
            runner_image=receipt["runner_image"],
            execution_started_at=receipt["execution_started_at"],
            execution_completed_at=receipt["execution_completed_at"],
            dbt_input_tree_digest=receipt["dbt_input_tree_digest"],
            dbt_expected_model_ids=tuple(receipt["dbt_expected_model_ids"]),
            dbt_observed_model_ids=tuple(receipt["dbt_observed_model_ids"]),
        )
        self.assertEqual(reconstructed, result)

    def test_resync_stage1_prints_complete_execution_receipt(self) -> None:
        result = Stage1Execution(
            connection_ids=("connection-1",),
            execution_completed_at="2026-09-03T00:00:01Z",
            execution_started_at="2026-09-03T00:00:00Z",
            job_ids={"connection-1": 101},
            runner_image="runner@sha256:" + "a" * 64,
            statuses={"connection-1": "succeeded"},
            terraform_connection_resources=(
                ("airbyte_connection.primary", "connection-1"),
            ),
            terraform_input_tree_digest="b" * 64,
            terraform_state_digest="c" * 64,
            terraform_state_lineage="lineage-1",
            terraform_state_path=Path("/tmp/work/elt/terraform.tfstate"),
            terraform_state_serial=3,
            workspace_dir=Path("/tmp/work"),
        )
        args = SimpleNamespace(
            work_dir="/tmp/work",
            airbyte_credential=None,
            airbyte_url=None,
            poll_interval=0,
            timeout=10,
        )
        config = {
            "Airbyte": {
                "config": {
                    "server_url": "http://airbyte/v1",
                    "workspace_id": "workspace-1",
                }
            }
        }
        output = io.StringIO()
        with mock.patch(
            "elt_taskgen.cli._runtime_yaml", return_value=config
        ), mock.patch(
            "elt_taskgen.cli._airbyte_client_from_credentials", return_value=object()
        ), mock.patch(
            "elt_taskgen.runtime.execution.rerun_stage1_syncs", return_value=result
        ), contextlib.redirect_stdout(output):
            self.assertEqual(cmd_runtime_resync_stage1(args), 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "connection_ids": ["connection-1"],
                "execution_completed_at": "2026-09-03T00:00:01Z",
                "execution_started_at": "2026-09-03T00:00:00Z",
                "job_ids": {"connection-1": 101},
                "runner_image": "runner@sha256:" + "a" * 64,
                "statuses": {"connection-1": "succeeded"},
                "terraform_connection_resources": [
                    ["airbyte_connection.primary", "connection-1"]
                ],
                "terraform_input_tree_digest": "b" * 64,
                "terraform_state_digest": "c" * 64,
                "terraform_state_lineage": "lineage-1",
                "terraform_state_path": "/tmp/work/elt/terraform.tfstate",
                "terraform_state_serial": 3,
                "workspace_dir": "/tmp/work",
            },
        )
        receipt = json.loads(output.getvalue())
        reconstructed = Stage1Execution(
            connection_ids=tuple(receipt["connection_ids"]),
            statuses=receipt["statuses"],
            job_ids=receipt["job_ids"],
            terraform_state_digest=receipt["terraform_state_digest"],
            runner_image=receipt["runner_image"],
            execution_started_at=receipt["execution_started_at"],
            execution_completed_at=receipt["execution_completed_at"],
            terraform_input_tree_digest=receipt["terraform_input_tree_digest"],
            workspace_dir=Path(receipt["workspace_dir"]),
            terraform_state_path=Path(receipt["terraform_state_path"]),
            terraform_state_lineage=receipt["terraform_state_lineage"],
            terraform_state_serial=receipt["terraform_state_serial"],
            terraform_connection_resources=tuple(
                tuple(item) for item in receipt["terraform_connection_resources"]
            ),
        )
        self.assertEqual(reconstructed, result)

    def test_provisioners_reject_existing_output_before_cloud_mutation(self) -> None:
        cases = (
            (
                "snowflake",
                cmd_runtime_provision_snowflake,
                "elt_taskgen.runtime.snowflake.load_credentials",
            ),
            (
                "databricks",
                cmd_runtime_provision_databricks,
                "elt_taskgen.runtime.databricks.load_credentials",
            ),
            (
                "redshift",
                cmd_runtime_provision_redshift,
                "elt_taskgen.runtime.redshift.load_credentials",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.json"
            output.write_text('{"preserve": true}\n', encoding="utf-8")
            for name, command, loader_path in cases:
                with self.subTest(destination=name), mock.patch(
                    loader_path
                ) as loader:
                    with self.assertRaisesRegex(ValueError, "already exists"):
                        command(SimpleNamespace(credential_out=output))
                    loader.assert_not_called()
                    self.assertEqual(
                        json.loads(output.read_text(encoding="utf-8")),
                        {"preserve": True},
                    )

    def test_failed_provisioning_removes_reserved_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "elt_taskgen.runtime.snowflake.load_credentials",
            side_effect=OSError("injected credential read failure"),
        ):
            output = Path(directory) / "scoped.json"
            with self.assertRaisesRegex(ValueError, "injected credential read failure"):
                cmd_runtime_provision_snowflake(
                    SimpleNamespace(
                        admin_snowflake_credential=Path(directory) / "admin.json",
                        credential_out=output,
                    )
                )
            self.assertFalse(output.exists())

    def test_databricks_cli_verifies_solver_identity_before_admin_ddl(self) -> None:
        class Connection:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        solver_connection = Connection()
        admin_connection = Connection()
        events: list[str] = []
        solver = {
            "hostname": "workspace.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/abc",
            "access_token": "solver-token",
            "database": "attempt_catalog",
            "schema": "task",
        }
        admin = {
            "hostname": "workspace.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/abc",
            "access_token": "admin-token",
        }
        scoped = dict(solver)

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "elt_taskgen.runtime.databricks.load_credentials",
            side_effect=(admin, solver),
        ), mock.patch(
            "elt_taskgen.runtime.databricks.connect",
            side_effect=(solver_connection, admin_connection),
        ) as connector, mock.patch(
            "elt_taskgen.runtime.databricks.assert_current_principal",
            side_effect=lambda *_args: events.append("identity"),
        ) as identity, mock.patch(
            "elt_taskgen.runtime.databricks.provision_attempt",
            side_effect=lambda *_args, **_kwargs: (
                events.append("provision") or scoped
            ),
        ), mock.patch("builtins.print"):
            output = Path(directory) / "scoped.json"
            result = cmd_runtime_provision_databricks(
                SimpleNamespace(
                    admin_databricks_credential=Path(directory) / "admin.json",
                    solver_databricks_credential=Path(directory) / "solver.json",
                    catalog="attempt_catalog",
                    schema="task",
                    solver_principal="solver-id",
                    attempt_dedicated_principal=True,
                    allow_create_catalog=True,
                    existing_dedicated_catalog=False,
                    credential_out=output,
                )
            )

            self.assertEqual(result, 0)
            self.assertEqual(events, ["identity", "provision"])
            identity.assert_called_once_with(solver_connection, "solver-id")
            self.assertEqual(
                connector.call_args_list,
                [
                    mock.call(
                        {
                            "hostname": "workspace.cloud.databricks.com",
                            "http_path": "/sql/1.0/warehouses/abc",
                            "access_token": "solver-token",
                        }
                    ),
                    mock.call(admin),
                ],
            )
            self.assertEqual(json.loads(output.read_text()), scoped)
            self.assertTrue(solver_connection.closed)
            self.assertTrue(admin_connection.closed)

    def test_runtime_prepare_refuses_work_dir_under_runs_or_release(self) -> None:
        """Threat row A22 (roadmap 0.F): `prepare` installs LIVE credentials
        into the attempt copy, so a `--work-dir` inside the REPOSITORY's
        `runs/` directory (`workspace.repo_root() / "runs"`, by resolved path)
        or under a release root (the `--release` tree, or any tree carrying
        `release_manifest.json`) is a usage error (exit 2) raised BEFORE any
        credential file or the release is read. The anchor is the checkout,
        not a path component: an unrelated `/home/ci/runs/attempt` proceeds."""
        from elt_taskgen import cli
        from elt_taskgen import workspace as workspace_mod

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "release"
            (release / "public" / "task").mkdir(parents=True)
            (release / "release_manifest.json").write_text("{}", encoding="utf-8")
            other = root / "other_release"
            (other / "x").mkdir(parents=True)
            (other / "release_manifest.json").write_text("{}", encoding="utf-8")

            def args(work_dir: Path) -> SimpleNamespace:
                return SimpleNamespace(
                    release=release,
                    task_id="task",
                    population="primary",
                    work_dir=work_dir,
                    environment_dir=root / "env",
                    airbyte_credential=root / "missing-airbyte.json",
                    destination=None,
                    destination_credential=None,
                    snowflake_credential=root / "missing-snowflake.json",
                    custom_api_definition_id=None,
                    airbyte_server_url=None,
                )

            never = mock.Mock(side_effect=AssertionError("must not be reached"))
            refused = (
                (root / "runs" / "attempt" / "task", "runs/"),
                (root / "runs", "runs/"),
                (release / "attempt" / "task", "release root"),
                (release, "release root"),
                (other / "x" / "task", "release root"),
            )
            # The temporary tree stands in for the checkout, so its runs/ is
            # THE repository runs/ for the duration.
            with mock.patch.object(workspace_mod, "repo_root", lambda: root), mock.patch.object(
                cli, "_runtime_json", never
            ), mock.patch.object(cli, "_require_runtime_release", never), mock.patch(
                "elt_taskgen.runtime.install.install_task", never
            ):
                for work_dir, reason in refused:
                    with self.subTest(work_dir=str(work_dir.relative_to(root))):
                        with self.assertRaises(cli.CliUsageError) as ctx:
                            cli.cmd_runtime_prepare(args(work_dir))
                        self.assertIn(reason, str(ctx.exception))
                never.assert_not_called()
                # Ordinary paths proceed to release verification (mocked here),
                # INCLUDING an unrelated tree that merely contains a `runs`
                # component: the anchor is the repository's runs/, not the name.
                for work_dir in (
                    root / "attempt" / "task",
                    root / "home" / "ci" / "runs" / "attempt" / "task",
                ):
                    with self.subTest(work_dir=str(work_dir.relative_to(root))), mock.patch.object(
                        cli, "_require_runtime_release", side_effect=cli.CliUsageError("verified-here")
                    ):
                        with self.assertRaises(cli.CliUsageError) as ctx:
                            cli.cmd_runtime_prepare(args(work_dir))
                        self.assertEqual("verified-here", str(ctx.exception))
                # Through main: exit 2, the message names the rule, nothing else ran.
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = cli.main(
                        [
                            "runtime", "prepare",
                            "--release", str(release),
                            "--task-id", "task",
                            "--work-dir", str(root / "runs" / "attempt" / "task"),
                            "--environment-dir", str(root / "env"),
                            "--airbyte-credential", str(root / "a.json"),
                            "--airbyte-server-url", "http://host.docker.internal:8000/api/public/v1/",
                            "--snowflake-credential", str(root / "s.json"),
                        ]
                    )
                self.assertEqual(code, 2)
                self.assertIn("runs/", err.getvalue())
                never.assert_not_called()
            # Unpatched, the anchor is the real checkout's runs/ (decided on
            # the path alone; nothing under it is read), and the temporary
            # tree's own `runs` directory is unrelated to it.
            real_runs = (workspace_mod.repo_root() / "runs").resolve()
            with self.assertRaises(cli.CliUsageError) as ctx:
                cli._refuse_confined_work_dir(real_runs / "attempt" / "task", release)
            self.assertIn("repository's runs/", str(ctx.exception))
            self.assertEqual(
                cli._refuse_confined_work_dir(root / "runs" / "attempt" / "task", release),
                (root / "runs" / "attempt" / "task").resolve(),
            )

    def test_run_stage1_uses_proxy_bridge_lane_by_default(self) -> None:
        """`terraform apply` inside the stage-1 runner must reach the Airbyte
        control plane (local abctl via host.docker.internal, or the cloud
        API), so `runtime run-stage1` selects the proxy-bridge lane whenever
        an Airbyte server URL is in play (always, for run-stage1: the URL is
        required) and `none` without one; an explicit `--sandbox-lane` wins.
        Stage 2 (dbt) defaults to `none` but can explicitly use its separate
        cloud-egress lane. Measured on the REAL DockerRunner's argv through a
        capturing host runner."""
        from elt_taskgen import cli
        from elt_taskgen.runtime import process as process_mod

        image = "runner@sha256:" + "a" * 64
        url = "http://localhost:8000/api/public/v1/"
        bridge = process_mod.PROXY_BRIDGE_NETWORK
        gateway = "host.docker.internal:host-gateway"
        # The rule itself: URL -> proxy-bridge, no URL -> none, explicit wins.
        self.assertEqual(
            cli._runtime_sandbox_lane(None, airbyte_url=url), process_mod.DOCKER_LANE_PROXY_BRIDGE
        )
        self.assertEqual(cli._runtime_sandbox_lane(None, airbyte_url=None), process_mod.DOCKER_LANE_NONE)
        self.assertEqual(cli._runtime_sandbox_lane(None, airbyte_url=""), process_mod.DOCKER_LANE_NONE)
        self.assertEqual(cli._runtime_sandbox_lane("none", airbyte_url=url), process_mod.DOCKER_LANE_NONE)
        self.assertEqual(
            cli._runtime_sandbox_lane("proxy-bridge", airbyte_url=None),
            process_mod.DOCKER_LANE_PROXY_BRIDGE,
        )
        with self.assertRaises(cli.CliUsageError):
            cli._runtime_sandbox_lane("host", airbyte_url=url)
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "task"
            work.mkdir()
            base = ["runtime", "run-stage1", "--work-dir", str(work), "--runner-image", image]
            # The parser carries no default of its own (the command derives
            # it from the URL), accepts the declared lanes, and its help
            # tells the operator how to provision the bridge network.
            self.assertIsNone(build_parser().parse_args(base).sandbox_lane)
            for lane in sorted(process_mod.DOCKER_LANES):
                parsed = build_parser().parse_args([*base, "--sandbox-lane", lane])
                self.assertEqual(parsed.sandbox_lane, lane)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args([*base, "--sandbox-lane", "host"])
            help_text = io.StringIO()
            # A wide terminal keeps argparse from hyphen-wrapping the phrase.
            with mock.patch.dict(os.environ, {"COLUMNS": "400"}), contextlib.redirect_stdout(
                help_text
            ), self.assertRaises(SystemExit):
                build_parser().parse_args([*base, "--help"])
            flat_help = " ".join(help_text.getvalue().split())
            self.assertIn(f"docker network create {bridge}", flat_help)
            self.assertIn("'none' otherwise", flat_help)
            stage2 = build_parser().parse_args(
                ["runtime", "run-stage2", "--work-dir", str(work), "--runner-image", image]
            )
            self.assertIsNone(stage2.sandbox_lane)
            self.assertEqual(
                cli._runtime_stage2_sandbox_lane(stage2.sandbox_lane),
                process_mod.DOCKER_LANE_NONE,
            )
            stage2_cloud = build_parser().parse_args(
                [
                    "runtime",
                    "run-stage2",
                    "--work-dir",
                    str(work),
                    "--runner-image",
                    image,
                    "--sandbox-lane",
                    "cloud-egress",
                ]
            )
            self.assertEqual(
                cli._runtime_stage2_sandbox_lane(stage2_cloud.sandbox_lane),
                process_mod.DOCKER_LANE_CLOUD_EGRESS,
            )
            with self.assertRaises(cli.CliUsageError):
                cli._runtime_stage2_sandbox_lane("proxy-bridge")

            # The commands, on the real DockerRunner with a capturing host
            # runner standing in for `docker run`.
            recorded: list[tuple[str, ...]] = []

            class CapturingHostRunner:
                def __init__(self, *, timeout=process_mod.DEFAULT_SUBPROCESS_TIMEOUT_SECONDS, **kw):
                    self.timeout = process_mod._validate_timeout(timeout)

                def run(self, argv, **kwargs):
                    recorded.append(tuple(str(value) for value in argv))
                    return process_mod.CommandResult(0)

            def apply_through(work_dir, client, runner, **kwargs):
                runner.run(("terraform", "apply", "-auto-approve"))
                return SimpleNamespace(
                    connection_ids=[],
                    execution_completed_at="2026-09-03T00:00:01Z",
                    execution_started_at="2026-09-03T00:00:00Z",
                    job_ids={},
                    statuses={},
                    terraform_connection_resources=(),
                    terraform_input_tree_digest="c" * 64,
                    terraform_state_digest="a" * 64,
                    terraform_state_lineage="lineage-1",
                    terraform_state_path=work_dir / "elt" / "terraform.tfstate",
                    terraform_state_serial=3,
                    workspace_dir=work_dir,
                    runner_image="runner@sha256:" + "a" * 64,
                )

            def dbt_through(work_dir, runner, **kwargs):
                runner.run(("dbt", "run"))
                return SimpleNamespace(
                    dbt_expected_model_ids=("model.demo.customer_summary",),
                    dbt_input_tree_digest="d" * 64,
                    dbt_observed_model_ids=("model.demo.customer_summary",),
                    preflight=Stage2Preflight(
                        destination=Destination.SNOWFLAKE,
                        dbt_core_version="1.10.13",
                        adapter_version="1.10.0",
                        profile_name="demo",
                        target_name="default",
                        namespace="attempt_database",
                        physical_container="",
                    ),
                    profiles_dir=work_dir,
                    project_dir=work_dir,
                    run_results_path=work_dir / "target" / "run_results.json",
                    dbt_invocation_id="invocation-id",
                    dbt_run_results_digest="b" * 64,
                    execution_completed_at="2026-09-03T00:00:01Z",
                    execution_started_at="2026-09-03T00:00:00Z",
                    runner_image="runner@sha256:" + "b" * 64,
                )

            config = {"Airbyte": {"config": {"server_url": url, "workspace_id": "ws"}}}
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(cli, "_runtime_yaml", return_value=config))
                stack.enter_context(
                    mock.patch.object(cli, "_airbyte_client_from_credentials", return_value=object())
                )
                stack.enter_context(mock.patch.object(process_mod, "SubprocessRunner", CapturingHostRunner))
                stack.enter_context(
                    mock.patch("elt_taskgen.runtime.execution.run_stage1_submission", side_effect=apply_through)
                )
                stack.enter_context(
                    mock.patch("elt_taskgen.runtime.execution.run_stage2_submission", side_effect=dbt_through)
                )
                stack.enter_context(
                    mock.patch.object(
                        cli, "_runtime_stage2_environment", return_value={}
                    )
                )
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                for lane, on_bridge in ((None, True), ("proxy-bridge", True), ("none", False)):
                    with self.subTest(stage=1, lane=lane):
                        argv = list(base) if lane is None else [*base, "--sandbox-lane", lane]
                        recorded.clear()
                        self.assertEqual(cli.cmd_runtime_run_stage1(build_parser().parse_args(argv)), 0)
                        self.assertEqual(len(recorded), 1)
                        docker = recorded[0]
                        pairs = list(zip(docker, docker[1:]))
                        self.assertEqual(docker[:2], ("docker", "run"))
                        self.assertEqual(docker[-3:], ("terraform", "apply", "-auto-approve"))
                        self.assertEqual(docker.count("--network"), 1)
                        if on_bridge:
                            self.assertIn(("--network", bridge), pairs)
                            self.assertIn(("--add-host", gateway), pairs)
                            self.assertNotIn(("--network", "none"), pairs)
                        else:
                            self.assertIn(("--network", "none"), pairs)
                            self.assertNotIn(bridge, docker)
                            self.assertNotIn(gateway, docker)
                        # Either lane is still the bounded, unprivileged sandbox.
                        self.assertIn("--read-only", docker)
                        self.assertIn("--cidfile", docker)
                        self.assertFalse(docker[docker.index("--user") + 1].startswith("0:"))
                for parsed_stage2, expected_network in (
                    (stage2, "none"),
                    (stage2_cloud, process_mod.CLOUD_EGRESS_NETWORK),
                ):
                    with self.subTest(stage=2, network=expected_network):
                        recorded.clear()
                        self.assertEqual(
                            cli.cmd_runtime_run_stage2(parsed_stage2), 0
                        )
                        self.assertEqual(len(recorded), 1)
                        docker = recorded[0]
                        pairs = list(zip(docker, docker[1:]))
                        self.assertEqual(docker[-2:], ("dbt", "run"))
                        self.assertIn(("--network", expected_network), pairs)
                        self.assertNotIn(gateway, docker)

    def test_snowflake_stage2_credential_builds_fixed_environment(self) -> None:
        from elt_taskgen import cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "snowflake": {
                    "config": {
                        "account": "org-account",
                        "username": "attempt-user",
                        "password": "attempt-password",
                        "role": "attempt-role",
                        "warehouse": "attempt-warehouse",
                        "database": "attempt_database",
                        "schema": "AIRBYTE_SCHEMA",
                    }
                }
            }
            (root / "config.yaml").write_text(
                yaml.safe_dump(config), encoding="utf-8"
            )
            credential = root / "snowflake.json"
            credential.write_text(
                json.dumps(
                    {
                        "account": "org-account",
                        "user": "attempt-user",
                        "password": "attempt-password",
                    }
                ),
                encoding="utf-8",
            )
            environment = cli._runtime_stage2_environment(
                root, destination_credential=credential
            )
            self.assertEqual(
                environment,
                {
                    "ELT_TASKGEN_SNOWFLAKE_ACCOUNT": "org-account",
                    "ELT_TASKGEN_SNOWFLAKE_USER": "attempt-user",
                    "ELT_TASKGEN_SNOWFLAKE_PASSWORD": "attempt-password",
                    "ELT_TASKGEN_SNOWFLAKE_ROLE": "attempt-role",
                    "ELT_TASKGEN_SNOWFLAKE_WAREHOUSE": "attempt-warehouse",
                },
            )
            with self.assertRaisesRegex(
                cli.CliUsageError, "requires --destination-credential"
            ):
                cli._runtime_stage2_environment(
                    root, destination_credential=None
                )
            credential.write_text(
                json.dumps(
                    {
                        "account": "org-account",
                        "user": "different-user",
                        "password": "attempt-password",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                cli.CliUsageError, "does not match"
            ) as raised:
                cli._runtime_stage2_environment(
                    root, destination_credential=credential
                )
            self.assertNotIn("different-user", str(raised.exception))

    def test_runner_timeout_must_be_positive(self) -> None:
        """`--runner-timeout` bounds the sandbox runner, so argparse validates
        it as a finite positive number of seconds: a bad value is a usage
        error (exit 2, the CliUsageError code) reported before any credential
        is read or container started. A programmatic caller that bypasses
        argparse hits the runner's own check, which the command surfaces as
        CliUsageError too."""
        from elt_taskgen import cli

        self.assertTrue(issubclass(cli._UsageArgumentError, cli.CliUsageError))
        image = "runner@sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "task"
            work.mkdir()
            for command in ("run-stage1", "run-stage2"):
                base = ["runtime", command, "--work-dir", str(work), "--runner-image", image]
                self.assertEqual(build_parser().parse_args(base).runner_timeout, 3600.0)
                for text, value in (("90", 90.0), ("1e3", 1000.0), ("0.5", 0.5)):
                    parsed = build_parser().parse_args([*base, "--runner-timeout", text])
                    self.assertEqual(parsed.runner_timeout, value)
                for bad in ("0", "-1", "nan", "inf", "-inf", "abc", ""):
                    with self.subTest(command=command, value=bad):
                        err = io.StringIO()
                        # `=` form: argparse would otherwise read `-inf` as a flag.
                        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                            cli.main([*base, f"--runner-timeout={bad}"])
                        self.assertEqual(ctx.exception.code, 2)
                        self.assertIn("positive number of seconds", err.getvalue())
            # Bypassing argparse: the runner refuses, the command reports usage.
            never = mock.Mock(side_effect=AssertionError("must not be reached"))
            with mock.patch("elt_taskgen.runtime.execution.run_stage2_submission", never):
                for bad in (0, -1.0, float("nan")):
                    with self.subTest(value=bad), self.assertRaises(cli.CliUsageError):
                        cli.cmd_runtime_run_stage2(
                            SimpleNamespace(
                                work_dir=work, runner_image=image, runner_timeout=bad,
                                dbt_command=["dbt"], project_dir=None, profiles_dir=None, target=None,
                            )
                        )
            never.assert_not_called()

    def test_install_airbyte_runner_timeout_is_generous_and_exposed(self) -> None:
        """`abctl local install` pulls images and can outlast the one-hour
        runner default, so the bootstrap runner is bounded by its own larger
        default and `runtime install-airbyte --timeout` exposes it; it is
        still bounded (never `timeout=None`)."""
        from elt_taskgen.runtime import bootstrap
        from elt_taskgen.runtime import process as process_mod

        seen: dict = {}

        class Recorder:
            def __init__(self, *, timeout=process_mod.DEFAULT_SUBPROCESS_TIMEOUT_SECONDS, **kw):
                seen["timeout"] = timeout
                self.timeout = process_mod._validate_timeout(timeout)

            def run(self, argv, **kwargs):
                stdout = "abctl version v0.30.0" if argv[1] == "version" else '{"email": "a", "password": "b"}'
                return process_mod.CommandResult(0, stdout=stdout)

        with mock.patch.object(bootstrap, "SubprocessRunner", Recorder):
            credentials = bootstrap.install_airbyte(chart_version="1.5.0", abctl_version="0.30.0")
        self.assertEqual(credentials.password, "b")
        self.assertEqual(seen["timeout"], bootstrap.DEFAULT_INSTALL_TIMEOUT_SECONDS)
        self.assertGreater(
            bootstrap.DEFAULT_INSTALL_TIMEOUT_SECONDS, process_mod.DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
        )
        with mock.patch.object(bootstrap, "SubprocessRunner", Recorder):
            bootstrap.install_airbyte(chart_version="1.5.0", abctl_version="0.30.0", timeout=7200.0)
        self.assertEqual(seen["timeout"], 7200.0)
        with mock.patch.object(bootstrap, "SubprocessRunner", Recorder), self.assertRaises(ValueError):
            bootstrap.install_airbyte(chart_version="1.5.0", abctl_version="0.30.0", timeout=None)  # type: ignore[arg-type]
        args = build_parser().parse_args(
            ["runtime", "install-airbyte", "--chart-version", "1.5.0", "--abctl-version", "0.30.0",
             "--credential-out", "/tmp/never-written.json"]
        )
        self.assertEqual(args.timeout, bootstrap.DEFAULT_INSTALL_TIMEOUT_SECONDS)
        args = build_parser().parse_args(
            ["runtime", "install-airbyte", "--chart-version", "1.5.0", "--abctl-version", "0.30.0",
             "--credential-out", "/tmp/never-written.json", "--timeout", "7200"]
        )
        self.assertEqual(args.timeout, 7200.0)

    def test_every_runtime_phase_has_a_separate_subcommand(self) -> None:
        parser = build_parser()
        commands = {
            "install-airbyte": [
                "--chart-version",
                "1.2.3",
                "--abctl-version",
                "v1.2.3",
                "--credential-out",
                "/tmp/a.json",
            ],
            "bootstrap-task": [
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--airbyte-url",
                "http://airbyte/v1",
                "--airbyte-credential",
                "/tmp/a.json",
                "--credential-out",
                "/tmp/b.json",
            ],
            "prepare": [
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--work-dir",
                "/tmp/work",
                "--environment-dir",
                "/tmp/env",
                "--airbyte-credential",
                "/tmp/a.json",
                "--airbyte-server-url",
                "http://host.docker.internal:8000/api/public/v1/",
                "--snowflake-credential",
                "/tmp/s.json",
            ],
            "source-up": [
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--environment-dir",
                "/tmp/env",
            ],
            "source-down": [
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--environment-dir",
                "/tmp/env",
            ],
            "provision-snowflake": [
                "--admin-snowflake-credential",
                "/tmp/admin.json",
                "--database",
                "task",
                "--credential-out",
                "/tmp/solver.json",
            ],
            "reset-snowflake": [
                "--snowflake-credential",
                "/tmp/s.json",
                "--database",
                "task",
            ],
            "provision-databricks": [
                "--admin-databricks-credential",
                "/tmp/databricks-admin.json",
                "--solver-databricks-credential",
                "/tmp/databricks-solver.json",
                "--catalog",
                "benchmark",
                "--schema",
                "task",
                "--solver-principal",
                "service-principal-id",
                "--attempt-dedicated-principal",
                "--allow-create-catalog",
                "--credential-out",
                "/tmp/databricks-scoped.json",
            ],
            "provision-redshift": [
                "--admin-redshift-credential",
                "/tmp/redshift-admin.json",
                "--database",
                "task_attempt_1",
                "--schema",
                "task",
                "--attempt-dedicated-deployment",
                "--credential-out",
                "/tmp/redshift-scoped.json",
            ],
            "run-stage1": [
                "--work-dir",
                "/tmp/work",
                "--runner-image",
                "runner@sha256:" + "a" * 64,
            ],
            "resync-stage1": [
                "--work-dir",
                "/tmp/work",
                "--airbyte-credential",
                "/tmp/a.json",
                "--airbyte-url",
                "http://airbyte/v1",
            ],
            "run-stage2": [
                "--work-dir",
                "/tmp/work",
                "--runner-image",
                "runner@sha256:" + "b" * 64,
            ],
            "verify-stage1": [
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--snowflake-credential",
                "/tmp/s.json",
            ],
            "verify-stage2": [
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--snowflake-credential",
                "/tmp/s.json",
            ],
            "verify-end-to-end": [
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--snowflake-credential",
                "/tmp/s.json",
            ],
        }
        for command, argv in commands.items():
            with self.subTest(command=command):
                parsed = parser.parse_args(["runtime", command, *argv])
                self.assertEqual(parsed.runtime_command, command)
                self.assertTrue(callable(parsed.func))

    def test_prepare_installs_a_shipped_extra_destination(self) -> None:
        """A multi-destination release keeps the root's config at the top and
        every other shipped destination's under destinations/<name>/. Naming
        one of those with --destination installs it; naming one the release
        does not ship is still a mismatch."""
        from elt_taskgen import cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "release"
            public = release / "public" / "task"
            public.mkdir(parents=True)
            (release / "release_manifest.json").write_text("{}", encoding="utf-8")
            (public / "config.yaml").write_text(
                "snowflake:\n  config:\n    database: task\n    schema: AIRBYTE_SCHEMA\n",
                encoding="utf-8",
            )
            shipped = public / "destinations" / "databricks" / "config.yaml"
            shipped.parent.mkdir(parents=True)
            shipped.write_text(
                "databricks:\n  config:\n    database: ''\n    schema: task\n",
                encoding="utf-8",
            )
            credential = root / "databricks.json"
            credential.write_text("{}", encoding="utf-8")

            def args(destination: str) -> SimpleNamespace:
                return SimpleNamespace(
                    release=release,
                    task_id="task",
                    population="primary",
                    work_dir=root / "attempt" / "task",
                    environment_dir=root / "env",
                    airbyte_credential=root / "airbyte.json",
                    destination=destination,
                    destination_credential=credential,
                    snowflake_credential=None,
                    custom_api_definition_id=None,
                    airbyte_server_url=None,
                )

            install = mock.Mock(return_value=root / "attempt" / "task")
            with mock.patch.object(cli, "_require_runtime_release", lambda release: None), mock.patch.object(
                cli, "_runtime_json", lambda path, label: {"hostname": "h", "http_path": "/p", "client_id": "c", "secret": "s", "workspace_id": "w"}
            ), mock.patch(
                "elt_taskgen.runtime.source_environment.prepare_source_environment", mock.Mock()
            ), mock.patch("elt_taskgen.runtime.install.install_task", install):
                cli.cmd_runtime_prepare(args("databricks"))
                kwargs = install.call_args.kwargs
                self.assertEqual(kwargs["shipped_destination_config"], shipped)
                self.assertEqual(str(getattr(kwargs["destination"], "value", kwargs["destination"])), "databricks")
                with self.assertRaises(cli.CliUsageError) as ctx:
                    cli.cmd_runtime_prepare(args("redshift"))
                self.assertIn("does not match", str(ctx.exception))

    def test_destination_neutral_prepare_and_verifier_flags_parse(self) -> None:
        parser = build_parser()
        prepare = parser.parse_args(
            [
                "runtime",
                "prepare",
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--work-dir",
                "/tmp/work",
                "--environment-dir",
                "/tmp/env",
                "--airbyte-credential",
                "/tmp/airbyte.json",
                "--airbyte-server-url",
                "http://airbyte/v1",
                "--destination",
                "databricks",
                "--destination-credential",
                "/tmp/databricks.json",
            ]
        )
        self.assertEqual(prepare.destination, "databricks")
        self.assertEqual(
            prepare.destination_credential, Path("/tmp/databricks.json")
        )

        verify = parser.parse_args(
            [
                "runtime",
                "verify-stage2",
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--destination",
                "databricks",
                "--destination-credential",
                "/tmp/databricks.json",
                "--catalog",
                "benchmark",
                "--certification-strict",
            ]
        )
        self.assertEqual(verify.physical_container, "benchmark")
        self.assertTrue(verify.certification_strict)

        append_probe = parser.parse_args(
            [
                "runtime",
                "verify-stage1",
                "--release",
                "/tmp/release",
                "--task-id",
                "task",
                "--destination-credential",
                "/tmp/redshift.json",
                "--expected-repetitions",
                "2",
            ]
        )
        self.assertEqual(append_probe.expected_repetitions, 2)

    def test_runtime_secret_writer_is_owner_only_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.json"
            _write_runtime_secret(path, {"password": "secret"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"password": "secret"})
            with self.assertRaisesRegex(ValueError, "already exists"):
                _write_runtime_secret(path, {"password": "replacement"})
            self.assertEqual(json.loads(path.read_text()), {"password": "secret"})


if __name__ == "__main__":
    unittest.main()

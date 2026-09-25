"""Process-compliance report, workspace teardown, and their CLI surface."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from elt_taskgen.cli import CliUsageError, build_parser, cmd_runtime_teardown_task
from elt_taskgen.runtime.airbyte import AirbyteClient, AirbyteError
from elt_taskgen.runtime.process_compliance import (
    TerraformIntentReport,
    changed_files_outside_elt,
    evaluate_process_compliance,
    find_terraform_state,
    terraform_files,
    terraform_state_connections,
    workspace_connection_streams,
)

CID_A = "11111111-1111-1111-1111-111111111111"
CID_B = "22222222-2222-2222-2222-222222222222"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener_for(calls: list[tuple[str, str]], responses: dict[str, object]):
    def opener(request, timeout=None):
        calls.append((request.get_method(), request.full_url))
        key = request.full_url.split("/api/public/v1/", 1)[1]
        if key not in responses:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        body = responses[key]
        return _Response(b"" if body is None else json.dumps(body).encode("utf-8"))

    return opener


def _state(connections: list[tuple[str, list[str], str | None]]) -> dict:
    resources = [
        {
            "type": "airbyte_connection",
            "name": f"c{i}",
            "instances": [
                {
                    "attributes": {
                        "connection_id": cid,
                        "prefix": prefix,
                        "configurations": {"streams": [{"name": s} for s in streams]},
                    }
                }
            ],
        }
        for i, (cid, streams, prefix) in enumerate(connections)
    ]
    resources.append(
        {"type": "airbyte_source_postgres", "name": "pg", "instances": [{"attributes": {"source_id": "x"}}]}
    )
    return {"resources": resources}


def _mount(root: Path, state: dict | None) -> tuple[Path, Path]:
    inputs = root / "inputs"
    agent = root / "agent"
    for base in (inputs, agent):
        (base / "elt").mkdir(parents=True)
        (base / "config.yaml").write_text("same")
    if state is not None:
        (agent / "elt" / "terraform.tfstate").write_text(json.dumps(state))
    return agent, inputs


class AirbyteWorkspaceClientTests(unittest.TestCase):
    def test_delete_workspace_issues_delete(self) -> None:
        calls: list[tuple[str, str]] = []
        client = AirbyteClient(
            "http://airbyte/api/public/v1/",
            bearer_token="t",
            opener=_opener_for(calls, {f"workspaces/{CID_A}": None}),
        )
        client.delete_workspace(CID_A)
        self.assertEqual(calls, [("DELETE", f"http://airbyte/api/public/v1/workspaces/{CID_A}")])

    def test_delete_workspace_rejects_non_uuid(self) -> None:
        client = AirbyteClient("http://airbyte/api/public/v1/", bearer_token="t", opener=_opener_for([], {}))
        with self.assertRaises(ValueError):
            client.delete_workspace("../applications")

    def test_delete_missing_workspace_surfaces_404(self) -> None:
        client = AirbyteClient("http://airbyte/api/public/v1/", bearer_token="t", opener=_opener_for([], {}))
        with self.assertRaisesRegex(AirbyteError, "HTTP 404"):
            client.delete_workspace(CID_A)

    def test_iter_workspaces_follows_pages(self) -> None:
        calls: list[tuple[str, str]] = []
        page1 = {
            "data": [
                {"workspaceId": f"{i:08d}-0000-0000-0000-000000000000", "name": "ELT-Bench t"}
                for i in range(2)
            ]
        }
        page2 = {"data": [{"workspaceId": "99999999-0000-0000-0000-000000000000", "name": "Default"}]}
        client = AirbyteClient(
            "http://airbyte/api/public/v1/",
            bearer_token="t",
            opener=_opener_for(
                calls, {"workspaces?limit=2&offset=0": page1, "workspaces?limit=2&offset=2": page2}
            ),
        )
        self.assertEqual(len(client.iter_workspaces(page_size=2)), 3)
        self.assertEqual([c[0] for c in calls], ["GET", "GET"])


class TerraformStateTests(unittest.TestCase):
    def test_reads_connection_ids_and_prefixed_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "terraform.tfstate"
            state.write_text(json.dumps(_state([(CID_A, ["users", "orders"], None), (CID_B, ["logs"], "raw_")])))
            self.assertEqual(
                terraform_state_connections(state),
                {CID_A: ("orders", "users"), CID_B: ("raw_logs",)},
            )

    def test_find_prefers_direct_state_and_skips_provider_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            elt = Path(tmp) / "elt"
            (elt / ".terraform" / "x").mkdir(parents=True)
            (elt / ".terraform" / "x" / "terraform.tfstate").write_text("{}")
            self.assertIsNone(find_terraform_state(elt))
            (elt / "infra").mkdir()
            nested = elt / "infra" / "terraform.tfstate"
            nested.write_text("{}")
            self.assertEqual(find_terraform_state(elt), nested)
            direct = elt / "terraform.tfstate"
            direct.write_text("{}")
            self.assertEqual(find_terraform_state(elt), direct)

    def test_terraform_files_puts_main_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("resources.tf", "main.tf", "provider.tf"):
                (Path(tmp) / name).write_text("")
            self.assertEqual(
                [p.name for p in terraform_files(Path(tmp))],
                ["main.tf", "provider.tf", "resources.tf"],
            )


class OutsideEltTests(unittest.TestCase):
    def test_reports_new_changed_and_symlinked_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            agent = root / "agent"
            for base in (inputs, agent):
                (base / "elt").mkdir(parents=True)
                (base / "config.yaml").write_text("same")
                (base / "data_model.yaml").write_text("v1")
            (inputs / "snowflake_credential.json").write_text("{}")
            (agent / "data_model.yaml").write_text("v2")
            (agent / "dbt_project").mkdir()
            (agent / "dbt_project" / "dbt_project.yml").write_text("new")
            (agent / "dbt_project" / "__pycache__").mkdir()
            (agent / "dbt_project" / "__pycache__" / "x.pyc").write_text("")
            (agent / "elt" / "main.tf").write_text("agent-owned")
            (agent / "claude").mkdir()
            (agent / "claude" / "result.json").write_text("{}")
            (agent / "escape").symlink_to(root)
            self.assertEqual(
                changed_files_outside_elt(agent, inputs),
                ("data_model.yaml", "dbt_project/dbt_project.yml", "escape"),
            )


class EvaluateProcessComplianceTests(unittest.TestCase):
    def test_compliant_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, inputs = _mount(Path(tmp), _state([(CID_A, ["users", "orders"], None)]))
            result = evaluate_process_compliance(
                agent_dir=agent,
                inputs_dir=inputs,
                expected_tables=["USERS", "orders"],
                workspace_connections={CID_A: ("orders", "users")},
                terraform_intent=TerraformIntentReport(1.0, (), (), None, ("main.tf",)),
            )
        self.assertTrue(result.compliant)
        self.assertEqual(result.violations, ())
        self.assertEqual(result.to_json()["expected_tables"], ("USERS", "orders"))

    def test_api_created_connection_and_uncovered_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, inputs = _mount(Path(tmp), _state([(CID_A, ["users"], None)]))
            result = evaluate_process_compliance(
                agent_dir=agent,
                inputs_dir=inputs,
                expected_tables=["users", "orders"],
                workspace_connections={CID_A: ("users",), CID_B: ("orders",)},
                terraform_intent=None,
            )
        self.assertFalse(result.compliant)
        self.assertEqual(
            result.violations,
            ("connections_outside_terraform", "expected_tables_not_fed_by_terraform_connections"),
        )
        self.assertEqual(result.api_only_connections, (CID_B,))
        self.assertEqual(result.uncovered_tables, ("orders",))

    def test_state_connection_no_longer_in_workspace_does_not_cover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, inputs = _mount(Path(tmp), _state([(CID_A, ["users"], None)]))
            result = evaluate_process_compliance(
                agent_dir=agent,
                inputs_dir=inputs,
                expected_tables=["users"],
                workspace_connections={},
                terraform_intent=None,
            )
        self.assertEqual(result.stale_state_connections, (CID_A,))
        self.assertEqual(result.uncovered_tables, ("users",))

    def test_missing_state_files_outside_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, inputs = _mount(Path(tmp), None)
            (agent / "notes.md").write_text("x")
            result = evaluate_process_compliance(
                agent_dir=agent,
                inputs_dir=inputs,
                expected_tables=[],
                workspace_connections={},
                terraform_intent=TerraformIntentReport(
                    0.0,
                    ("terraform_hardcoded_credential",),
                    ("terraform_hardcoded_credential",),
                    None,
                    ("main.tf",),
                ),
            )
        self.assertEqual(
            set(result.violations),
            {"terraform_state_missing", "files_outside_elt", "terraform_policy_violation"},
        )


class WorkspaceListingTests(unittest.TestCase):
    def test_workspace_connection_streams(self) -> None:
        listed = [
            {"connectionId": CID_A, "prefix": "p_", "configurations": {"streams": [{"name": "a"}, {"name": "b"}]}},
            {"connectionId": CID_B, "configurations": {}},
            {"name": "no id"},
        ]
        self.assertEqual(workspace_connection_streams(listed), {CID_A: ("p_a", "p_b"), CID_B: ()})


class RuntimeCliSurfaceTests(unittest.TestCase):
    def test_new_runtime_subcommands_parse(self) -> None:
        parser = build_parser()
        common = ["--airbyte-url", "http://airbyte/v1", "--airbyte-credential", "/tmp/t.json"]
        args = parser.parse_args(["runtime", "teardown-task", *common, "--remove-credential"])
        self.assertEqual(args.func.__name__, "cmd_runtime_teardown_task")
        args = parser.parse_args(["runtime", "purge-workspaces", *common, "--keep-credentials", "x/*.json"])
        self.assertEqual(args.func.__name__, "cmd_runtime_purge_workspaces")
        self.assertFalse(args.apply)
        args = parser.parse_args(
            [
                "runtime", "verify-process", "--release", "/r", "--task-id", "t",
                "--agent-dir", "/a", "--inputs-dir", "/i", *common, "--strict",
            ]
        )
        self.assertEqual(args.func.__name__, "cmd_runtime_verify_process")
        self.assertTrue(args.strict)

    def test_teardown_refuses_base_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cred = Path(tmp) / "task.json"
            cred.write_text(json.dumps({"client_id": "a", "client_secret": "b", "workspace_id": CID_A}))
            base = Path(tmp) / "base.json"
            base.write_text(json.dumps({"client_id": "a", "client_secret": "b", "workspace_id": CID_A}))
            args = build_parser().parse_args(
                ["runtime", "teardown-task", "--airbyte-url", "http://airbyte/v1",
                 "--airbyte-credential", str(cred), "--base-credential", str(base)]
            )
            with self.assertRaisesRegex(CliUsageError, "base"):
                cmd_runtime_teardown_task(args)

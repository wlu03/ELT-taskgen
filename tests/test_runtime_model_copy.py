"""Phase 5 item 4: the model-facing copy and the harness-owned replay copy.

Threat row A22 control 2 (``ADDENDUM_constraint_status_security_gaps.md``):
the copy a model is handed carries only the PLACEHOLDER credential file, the
live values go into a SEPARATE harness-owned replay copy created after the
model is gone, and on the cloud lane the attempt-scoped principal is revoked
at cleanup through an injectable hook.

No cloud call, no Docker invocation and no real process is made here: the
revocation hook and the replay/grader runner are offline doubles, and every
credential value in this file is a test constant. Nothing asserts on a
credential VALUE — only on shapes, key names and emptiness.
"""

from __future__ import annotations

import ast
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from elt_taskgen.destinations import Destination
from elt_taskgen.export.eltbench import (
    assert_public_runtime_shape,
    runtime_documentation_filenames,
)
from elt_taskgen.runtime import model_copy as model_copy_module
from elt_taskgen.runtime.model_copy import (
    AGENT_ARGV_MARKERS,
    CLEANUP_RECEIPT_SCHEMA_VERSION,
    GRADER_LANE_PROGRAMS,
    LANE_CLOUD,
    LANE_LOCAL,
    AttemptCopies,
    GraderLaneRunner,
    ModelCopyError,
    PrincipalRevocationError,
    ReplayCredentials,
    ScopedPrincipal,
    assert_candidate_tree_is_image_safe,
    assert_model_copy_is_credential_free,
    assert_no_agent_process,
    assert_placeholder_credential,
    candidate_tree_dotenv_paths,
    docker_lane_for,
    install_task_for_model,
    live_credential_findings,
    placeholder_credential,
    recompute_receipt_digest,
)
from elt_taskgen.runtime.process import (
    DOCKER_LANE_NONE,
    DOCKER_LANE_PROXY_BRIDGE,
    CommandResult,
)


#: The committed public bundle of the semantic-gate fixture release: a real
#: frozen Snowflake + REST task, credential-free by exporter contract.
FIXTURE_PUBLIC_TASK = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "semantic_gate"
    / "release"
    / "public"
    / "gate__five_backend_probe"
)

#: The runner-image build context. `runtime-images/` is FENCED (the concurrent
#: runtime/export effort owns it), so the Table 9 image pins are hand-offs and
#: the image-side test below is skipped until they land. Nothing here builds,
#: pulls or runs an image: the pin is read from the committed build context,
#: which is the only form a no-Docker test can take.
RUNTIME_IMAGES_ROOT = Path(__file__).resolve().parent.parent / "runtime-images"


def _dbt_image_build_context() -> str:
    """Every committed text file of the dbt runner images, concatenated."""

    chunks: list[str] = []
    if not RUNTIME_IMAGES_ROOT.is_dir():
        return ""
    for path in sorted(RUNTIME_IMAGES_ROOT.glob("dbt*/**/*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):  # pragma: no cover - lock files
            continue
    return "\n".join(chunks)


def _dbt_image_declares_candidate_tree_guard() -> bool:
    text = _dbt_image_build_context()
    return "DBT_ENGINE_PROFILES_DIR" in text and ".env" in text


#: Test constants, never real credentials.
AIRBYTE_LIVE = {
    "workspace_id": "workspace-abc",
    "client_id": "airbyte-client",
    "client_secret": "airbyte-client-secret-value",
    "custom_api_definition_id": "custom-api-def",
}
DESTINATION_LIVE: dict[Destination, dict[str, object]] = {
    Destination.SNOWFLAKE: {
        "account": "organization-account",
        "user": "attempt_user",
        "password": "attempt-password-value",
        "role": "attempt_role",
        "warehouse": "attempt_warehouse",
    },
    Destination.DATABRICKS: {
        "hostname": "workspace.cloud.databricks.example",
        "http_path": "/sql/1.0/warehouses/abc123",
        "client_id": "attempt-client",
        "secret": "attempt-secret-value",
        "database": "benchmark_catalog",
    },
    Destination.REDSHIFT: {
        "host": "cluster.example.redshift.invalid",
        "port": 5439,
        "database": "benchmark_database",
        "username": "attempt_user",
        "password": "attempt-password-value",
        "s3_bucket_name": "benchmark-staging",
        "s3_bucket_region": "us-west-2",
        "access_key_id": "AKIAEXAMPLE",
        "secret_access_key": "aws-secret-value",
    },
}

CREDENTIAL_FILENAME = {
    Destination.SNOWFLAKE: "snowflake_credential.json",
    Destination.DATABRICKS: "databricks_credential.json",
    Destination.REDSHIFT: "redshift_credential.json",
}


def _live_credentials(destination: Destination) -> ReplayCredentials:
    return ReplayCredentials(
        airbyte=dict(AIRBYTE_LIVE),
        destination=dict(DESTINATION_LIVE[destination]),
        custom_api_definition_id=None,
    )


class _RecordingRunner:
    """An offline ``Runner`` double: it records argv and runs nothing."""

    def __init__(self) -> None:
        self.argv: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd=None, env=None, stdin_path=None) -> CommandResult:
        self.argv.append(tuple(str(value) for value in argv))
        return CommandResult(0, "", "")


def _make_bundle(root: Path, destination: Destination) -> Path:
    """One minimal credential-free public bundle per destination.

    The committed fixture covers Snowflake; Databricks and Redshift need a
    bundle of their own to prove the sentinel install satisfies every
    destination contract in ``runtime/install.py``.
    """
    source = root / f"{destination.value}-task"
    (source / "schemas").mkdir(parents=True)
    (source / "documentation").mkdir()
    (source / "elt").mkdir()
    (source / "schemas" / "users.csv").write_text(
        "column_name,column_description\nid,Primary key\n", encoding="utf-8"
    )
    (source / "data_model.yaml").write_text("models: []\n", encoding="utf-8")
    (source / "check_job_status.py").write_text("# helper\n", encoding="utf-8")
    (source / "elt" / "main.tf").write_text(
        "terraform {\n"
        "  required_providers {\n"
        "    airbyte = {\n"
        '      source  = "airbytehq/airbyte"\n'
        '      version = "0.6.5"\n'
        "    }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    for name in runtime_documentation_filenames(destination):
        (source / "documentation" / name).write_text(f"# {name}\n", encoding="utf-8")

    airbyte = {
        "namespace_definition": "destination",
        "password": "",
        "server_url": "http://airbyte-abctl-control-plane:80/api/public/v1/",
        "postgres_definition_id": "decd338e-5647-4c0b-adf4-da0e75f5a750",
        "username": "",
        "workspace_id": "",
    }
    config: dict[str, object] = {
        "postgres": {
            "config": {
                "database": "task_database",
                "host": "elt-postgres",
                "password": "testelt",
                "port": 5432,
                "schema": "public",
                "sync_mode": "full_refresh_append",
                "tables": ["users"],
                "user": "postgres",
            }
        },
        "Airbyte": {"config": airbyte},
    }
    if destination is Destination.SNOWFLAKE:
        config["snowflake"] = {
            "config": {
                "account": "",
                "database": "task_database",
                "password": "",
                "role": "",
                "schema": "AIRBYTE_SCHEMA",
                "username": "",
                "warehouse": "",
            }
        }
        airbyte["snowflake_definition_id"] = "424892c4-daac-4491-b35d-c6688ba547ba"
        template: dict[str, object] = {"account": "", "user": "", "password": ""}
    elif destination is Destination.DATABRICKS:
        config["databricks"] = {
            "config": {
                "database": "",
                "hostname": "",
                "client_id": "",
                "secret": "",
                "http_path": "",
                "schema": "task_database",
            }
        }
        airbyte["databricks_definition_id"] = "072d5540-f236-4294-ba7c-ade8fd918496"
        template = {"client_id": "", "hostname": "", "http_path": "", "secret": ""}
    else:
        config["redshift"] = {
            "config": {
                "access_key_id": "",
                "database": "",
                "host": "",
                "password": "",
                "port": 5439,
                "s3_bucket_name": "",
                "s3_bucket_region": "",
                "schema": "task_database",
                "secret_access_key": "",
                "username": "",
            }
        }
        airbyte["redshift_definition_id"] = "f7a7d195-377f-cf5b-70a5-be6b819019dc"
        template = {
            "database": "",
            "host": "",
            "password": "",
            "port": 5439,
            "username": "",
        }
    (source / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    (source / CREDENTIAL_FILENAME[destination]).write_text(
        json.dumps(template, indent=2) + "\n", encoding="utf-8"
    )
    return source


class ModelFacingCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    # -- A22 control 2: the model copy keeps the placeholder ----------------

    def test_install_task_without_injection_leaves_placeholder(self) -> None:
        """The copy handed to a model carries ONLY the placeholder credential
        file — every string value empty, exactly the public runtime shape —
        while the separate harness-owned replay copy is the one that receives
        live values, and only after the model is gone."""
        copies = install_task_for_model(
            FIXTURE_PUBLIC_TASK, self.root / "attempt-1"
        )
        self.assertTrue(copies.model_dir.is_dir())
        self.assertEqual(copies.model_dir.name, "task")
        self.assertEqual(copies.destination, Destination.SNOWFLAKE)
        self.assertEqual(copies.credential_filename, "snowflake_credential.json")

        # 1. The credential file is the exporter's placeholder: keys present,
        #    every string value empty (no value is ever read out of it).
        installed = json.loads(
            (copies.model_dir / copies.credential_filename).read_text(
                encoding="utf-8"
            )
        )
        expected = placeholder_credential(FIXTURE_PUBLIC_TASK)
        self.assertEqual(sorted(installed), sorted(expected))
        strings = [
            value for value in installed.values() if isinstance(value, str)
        ]
        self.assertTrue(strings)  # the check below is not vacuous
        self.assertEqual(strings, [""] * len(strings))
        assert_placeholder_credential(installed)

        # 2. config.yaml is byte-identical to the frozen public bundle's, so
        #    no live Airbyte or warehouse value slipped in either, and the
        #    copy still satisfies the strict public runtime gate.
        self.assertEqual(
            (copies.model_dir / "config.yaml").read_bytes(),
            (FIXTURE_PUBLIC_TASK / "config.yaml").read_bytes(),
        )
        self.assertTrue(copies.public_shape_verified)
        assert_public_runtime_shape(copies.model_dir)
        airbyte = yaml.safe_load(
            (copies.model_dir / "config.yaml").read_text(encoding="utf-8")
        )["Airbyte"]["config"]
        for field in ("password", "username", "workspace_id"):
            self.assertEqual(airbyte[field], "")
        for field in ("client_id", "client_secret"):
            self.assertNotIn(field, airbyte)

        # 3. The whole tree is credential-free by the Phase 0 sweep, and
        #    owner-private.
        self.assertEqual(live_credential_findings(copies.model_dir), [])
        assert_model_copy_is_credential_free(copies.model_dir)
        mode = stat.S_IMODE(
            (copies.model_dir / copies.credential_filename).stat().st_mode
        )
        self.assertEqual(mode & 0o077, 0)
        self.assertEqual(mode & 0o600, 0o600)

        # 4. The replay copy does not even exist while the model could be
        #    running, and is a SEPARATE directory when it does.
        self.assertFalse(copies.replay_dir.exists())
        self.assertNotEqual(copies.replay_dir, copies.model_dir)
        self.assertFalse(
            copies.replay_dir.is_relative_to(copies.model_dir)
        )

        copies.release_model()
        copies.install_replay(_live_credentials(Destination.SNOWFLAKE))
        replay = json.loads(
            (copies.replay_dir / copies.credential_filename).read_text(
                encoding="utf-8"
            )
        )
        # Shape only: the replay copy is the one that carries values.
        self.assertEqual(sorted(replay), sorted(expected))
        self.assertTrue(
            all(value != "" for value in replay.values() if isinstance(value, str))
        )
        with self.assertRaises(ModelCopyError):
            assert_placeholder_credential(replay)
        self.assertTrue(
            any(
                finding.path == copies.credential_filename
                for finding in live_credential_findings(copies.replay_dir)
            )
        )
        # The model copy is untouched by the replay install.
        assert_model_copy_is_credential_free(copies.model_dir)

    def test_every_destination_installs_a_placeholder_only_model_copy(self) -> None:
        """The sentinel install satisfies all three destination contracts in
        ``runtime/install.py`` and leaves the placeholder in every case."""
        for destination in Destination:
            with self.subTest(destination=destination.value):
                source = _make_bundle(self.root, destination)
                copies = install_task_for_model(
                    source, self.root / f"attempt-{destination.value}"
                )
                self.assertTrue(copies.public_shape_verified)
                assert_model_copy_is_credential_free(copies.model_dir)
                payload = json.loads(
                    (copies.model_dir / copies.credential_filename).read_text(
                        encoding="utf-8"
                    )
                )
                assert_placeholder_credential(payload)
                self.assertEqual(
                    payload, placeholder_credential(source)
                )
                copies.release_model()
                copies.install_replay(_live_credentials(destination))
                self.assertTrue(copies.replay_dir.is_dir())
                self.assertNotEqual(live_credential_findings(copies.replay_dir), [])

    def test_replay_copy_is_refused_until_the_model_is_gone(self) -> None:
        copies = install_task_for_model(
            FIXTURE_PUBLIC_TASK, self.root / "attempt-2"
        )
        with self.assertRaisesRegex(ModelCopyError, "after the model is gone"):
            copies.install_replay(_live_credentials(Destination.SNOWFLAKE))
        self.assertFalse(copies.replay_dir.exists())
        copies.release_model()
        with self.assertRaisesRegex(ModelCopyError, "already released"):
            copies.release_model()
        copies.install_replay(_live_credentials(Destination.SNOWFLAKE))
        with self.assertRaisesRegex(ModelCopyError, "already installed"):
            copies.install_replay(_live_credentials(Destination.SNOWFLAKE))

    def test_credentials_provider_is_called_only_at_replay_activation(self) -> None:
        """Live values need never exist while the policy runs: the provider
        is called at activation and its result is never retained."""
        calls: list[str] = []

        def provider() -> ReplayCredentials:
            calls.append("resolved")
            return _live_credentials(Destination.SNOWFLAKE)

        copies = install_task_for_model(
            FIXTURE_PUBLIC_TASK,
            self.root / "attempt-3",
            credentials_provider=provider,
        )
        self.assertEqual(calls, [])
        copies.release_model()
        self.assertEqual(calls, [])
        copies.install_replay()
        self.assertEqual(calls, ["resolved"])

    def test_model_copy_is_destroyed_when_it_cannot_be_proven_clean(self) -> None:
        """Fail closed: a copy whose placeholder guarantee cannot be verified
        never survives on disk for a policy to be handed."""
        work = self.root / "attempt-4"
        with mock.patch.object(
            model_copy_module,
            "assert_public_runtime_shape",
            side_effect=[None, ValueError("shape refused")],
        ):
            with self.assertRaises(ValueError):
                install_task_for_model(FIXTURE_PUBLIC_TASK, work)
        self.assertFalse((work / "task").exists())
        self.assertEqual(list(work.glob("*")), [])

    def test_populated_source_credential_is_refused_before_any_copy(self) -> None:
        source = _make_bundle(self.root, Destination.SNOWFLAKE)
        (source / "snowflake_credential.json").write_text(
            json.dumps({"account": "a", "user": "u", "password": "p"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ModelCopyError, "populated credential fields"):
            install_task_for_model(source, self.root / "attempt-5")
        self.assertFalse((self.root / "attempt-5" / "task").exists())

    def test_placeholder_assertion_names_keys_never_values(self) -> None:
        with self.assertRaises(ModelCopyError) as caught:
            assert_placeholder_credential(
                {"account": "", "password": "s3cret-value", "user": "someone"}
            )
        message = str(caught.exception)
        self.assertIn("password", message)
        self.assertIn("user", message)
        self.assertNotIn("s3cret-value", message)
        self.assertNotIn("someone", message)
        # A nested or boolean value is not a shape this format ever has.
        with self.assertRaises(ModelCopyError):
            assert_placeholder_credential({"account": {"nested": ""}})
        with self.assertRaises(ModelCopyError):
            assert_placeholder_credential({"account": True})
        # The Redshift integer port is a placeholder shape.
        assert_placeholder_credential({"host": "", "port": 5439})

    def test_policy_authored_credential_file_is_reported_not_a_refusal(self) -> None:
        """A policy that fabricates a credential-shaped file under ``elt/``
        is REPORTED at release; it can never wedge the harness's teardown."""
        copies = install_task_for_model(
            FIXTURE_PUBLIC_TASK, self.root / "attempt-6"
        )
        (copies.model_dir / "elt" / "profiles.yml").write_text(
            "elt:\n  outputs:\n    dev:\n      password: invented-by-the-policy\n",
            encoding="utf-8",
        )
        findings = copies.release_model()
        self.assertEqual([finding.path for finding in findings], ["elt/profiles.yml"])
        self.assertEqual(findings[0].secret_keys, ("password",))
        receipt = copies.cleanup()
        self.assertEqual(receipt.policy_credential_findings, 1)
        # A live credential outside the policy tree is a refusal, not a report.
        other = install_task_for_model(FIXTURE_PUBLIC_TASK, self.root / "attempt-7")
        (other.model_dir / "snowflake_credential.json").write_text(
            json.dumps({"account": "a", "user": "u", "password": "injected"}),
            encoding="utf-8",
        )
        with self.assertRaises(ModelCopyError):
            other.release_model()


class ScopedPrincipalCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.revoked: list[ScopedPrincipal] = []
        self.principal = ScopedPrincipal(
            principal_id="elt-attempt-0007",
            destination=Destination.SNOWFLAKE,
            attempt_id="attempt-0007",
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _cloud_copies(self, name: str, *, hook=None) -> AttemptCopies:
        return install_task_for_model(
            FIXTURE_PUBLIC_TASK,
            self.root / name,
            lane=LANE_CLOUD,
            principal=self.principal,
            revoke_principal=hook or self.revoked.append,
        )

    def test_attempt_cleanup_revokes_scoped_principal(self) -> None:
        """Cloud lane only (A22 control 2, WAREHOUSE_CONNECTORS.md:119-127):
        the attempt-scoped principal is revoked at cleanup through the
        injectable hook — no cloud call is made here — the replay tree that
        held the live values is removed first, and the sealed receipt records
        it. The ordinary local lane has no principal to revoke."""
        copies = self._cloud_copies("attempt-cloud")
        self.assertEqual(copies.lane, LANE_CLOUD)
        self.assertEqual(copies.docker_lane, DOCKER_LANE_PROXY_BRIDGE)
        copies.release_model()
        replay = copies.install_replay(_live_credentials(Destination.SNOWFLAKE))
        self.assertTrue(replay.is_dir())
        self.assertEqual(self.revoked, [])

        receipt = copies.cleanup()
        self.assertEqual(self.revoked, [self.principal])
        self.assertTrue(receipt.principal_revoked)
        self.assertEqual(receipt.principal_id, "elt-attempt-0007")
        self.assertEqual(receipt.lane, LANE_CLOUD)
        self.assertEqual(receipt.docker_lane, DOCKER_LANE_PROXY_BRIDGE)
        self.assertEqual(receipt.destination, "snowflake")
        self.assertEqual(receipt.schema_version, CLEANUP_RECEIPT_SCHEMA_VERSION)
        self.assertTrue(receipt.replay_installed)
        self.assertTrue(receipt.replay_removed)
        self.assertFalse(replay.exists())
        self.assertFalse(copies.model_dir.exists())
        self.assertEqual(receipt.live_credential_files, 0)
        self.assertEqual(recompute_receipt_digest(receipt), receipt.receipt_digest)
        # Idempotent: a second cleanup never revokes twice.
        self.assertEqual(copies.cleanup(), receipt)
        self.assertEqual(self.revoked, [self.principal])

        # The ordinary (DuckDB) lane has no cloud principal at all.
        local = install_task_for_model(FIXTURE_PUBLIC_TASK, self.root / "attempt-local")
        self.assertEqual(local.lane, LANE_LOCAL)
        self.assertEqual(local.docker_lane, DOCKER_LANE_NONE)
        self.assertIsNone(local.principal)
        local_receipt = local.cleanup()
        self.assertFalse(local_receipt.principal_revoked)
        self.assertEqual(local_receipt.principal_id, "")
        self.assertEqual(self.revoked, [self.principal])

    def test_cloud_lane_requires_a_principal_and_the_local_lane_refuses_one(
        self,
    ) -> None:
        with self.assertRaisesRegex(ModelCopyError, "attempt-scoped principal"):
            install_task_for_model(
                FIXTURE_PUBLIC_TASK, self.root / "attempt-8", lane=LANE_CLOUD
            )
        with self.assertRaisesRegex(ModelCopyError, "no cloud principal"):
            install_task_for_model(
                FIXTURE_PUBLIC_TASK,
                self.root / "attempt-9",
                principal=self.principal,
                revoke_principal=self.revoked.append,
            )
        with self.assertRaises(ModelCopyError):
            install_task_for_model(
                FIXTURE_PUBLIC_TASK, self.root / "attempt-10", lane="hosted"
            )
        self.assertEqual(docker_lane_for(LANE_LOCAL), DOCKER_LANE_NONE)
        self.assertEqual(docker_lane_for(LANE_CLOUD), DOCKER_LANE_PROXY_BRIDGE)

    def test_failed_revocation_is_loud_and_carries_the_receipt(self) -> None:
        """An unrevoked principal outlives its attempt: cleanup raises, the
        plaintext copy is already gone, and the error names no cause text."""

        def failing(principal: ScopedPrincipal) -> None:
            raise RuntimeError("token 12345 rejected by the warehouse")

        copies = self._cloud_copies("attempt-11", hook=failing)
        copies.release_model()
        copies.install_replay(_live_credentials(Destination.SNOWFLAKE))
        with self.assertRaises(PrincipalRevocationError) as caught:
            copies.cleanup()
        self.assertNotIn("12345", str(caught.exception))
        self.assertIn("RuntimeError", str(caught.exception))
        self.assertFalse(caught.exception.receipt.principal_revoked)
        self.assertTrue(caught.exception.receipt.replay_removed)
        self.assertFalse(copies.replay_dir.exists())

    def test_scoped_principal_has_nowhere_to_put_a_secret(self) -> None:
        with self.assertRaises(ModelCopyError):
            ScopedPrincipal(principal_id="", destination=Destination.SNOWFLAKE)
        with self.assertRaises(ModelCopyError):
            ScopedPrincipal(
                principal_id="a" * 200, destination=Destination.SNOWFLAKE
            )
        with self.assertRaises(ModelCopyError):
            ScopedPrincipal(
                principal_id="has space", destination=Destination.SNOWFLAKE
            )
        self.assertEqual(
            sorted(ScopedPrincipal.__dataclass_fields__),
            ["attempt_id", "destination", "principal_id"],
        )


class GraderLaneTests(unittest.TestCase):
    """The replay/grader container replays a SEALED artifact: zero model
    calls (C2/C5; S7 §7.1.8)."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_grader_container_never_ran_agent_process(self) -> None:
        # The whole attempt runs with every model entry point armed to fail:
        # if any code path here reached a provider, this test would error.
        from elt_taskgen.review import providers as providers_module

        def never(*args, **kwargs):
            raise AssertionError("the certification lane makes no model call")

        with mock.patch.object(
            providers_module.RoutedProvider, "complete", never
        ), mock.patch.object(providers_module.RoutedProvider, "run_session", never):
            copies = install_task_for_model(
                FIXTURE_PUBLIC_TASK, self.root / "attempt-grader"
            )
            copies.release_model()
            copies.install_replay(_live_credentials(Destination.SNOWFLAKE))
            runner = _RecordingRunner()
            gate = copies.grader_runner(runner)
            self.assertIsInstance(gate, GraderLaneRunner)

            # The harness verbs the lane exists for.
            gate.run(["terraform", "validate", "-json"], cwd=copies.replay_dir)
            gate.run(["dbt", "build", "--target", "certification"])
            self.assertEqual(gate.command_count, 2)
            self.assertEqual(len(runner.argv), 2)

            # An agent process is refused BEFORE the runner sees it, whether
            # it arrives as a program, a module or an argument.
            agent_commands = (
                ["claude", "-p", "solve the task"],
                ["python", "-m", "elt_taskgen.review.session"],
                ["docker", "run", "--rm", "anthropic/agent:latest"],
                ["python", "-m", "elt_taskgen.review.providers"],
                ["dbt", "run", "--vars", "run_bounded_session=1"],
            )
            for argv in agent_commands:
                with self.subTest(argv=argv[0]):
                    with self.assertRaises(ModelCopyError):
                        gate.run(argv)
            # An ordinary interpreter is refused too: `python -m <anything>`
            # would be a hole the size of every agent runner ever written.
            with self.assertRaises(ModelCopyError):
                gate.run(["python", "-c", "print(1)"])

            self.assertEqual(gate.refused_count, len(agent_commands) + 1)
            self.assertEqual(gate.agent_process_count, 0)
            self.assertEqual(gate.command_count, 2)
            self.assertEqual(len(runner.argv), 2)
            for argv in runner.argv:
                self.assertIn(Path(argv[0]).name, GRADER_LANE_PROGRAMS)
                for marker in AGENT_ARGV_MARKERS:
                    self.assertNotIn(marker, " ".join(argv).casefold())

            receipt = copies.cleanup()

        self.assertEqual(receipt.agent_process_count, 0)
        self.assertEqual(receipt.model_call_count, 0)
        self.assertEqual(receipt.grader_commands, 2)
        self.assertEqual(recompute_receipt_digest(receipt), receipt.receipt_digest)

        # The module itself has no provider seam: nothing it imports can make
        # a model call, and it starts no process of its own.
        imported: set[str] = set()
        tree = ast.parse(
            Path(model_copy_module.__file__).read_text(encoding="utf-8")
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for forbidden in (
            "anthropic",
            "openai",
            "boto3",
            "requests",
            "socket",
            "subprocess",
            "http.client",
            "urllib.request",
            "snowflake.connector",
            "elt_taskgen.review.providers",
            "elt_taskgen.review.session",
            "elt_taskgen.cli",
        ):
            self.assertNotIn(forbidden, imported)

    def test_the_attested_mount_is_never_an_attempt_task_directory(self) -> None:
        """Which mount `credential_files_in_mount` counts, pinned across tracks.

        The release gate refuses a labelled batch whose attestation records a
        credential-shaped file in the mount, and the attestation's counter
        matches NAMES only (it must never open what it counts). An attempt
        `task/` directory therefore counts at least 1 by upstream contract:
        the ELT-Bench format puts `<destination>_credential.json` there, and
        the exporter's PLACEHOLDER is indistinguishable from a live file
        without reading it. Roadmap §8's preamble is explicit that this file
        is harness-only; the surface that must be credential-free is the one
        the labelled run and the grader execute in.

        Left unstated, the two tracks contradict each other: pass the model
        copy and every batch is refused. This test states it, and pins that
        the mistake fails CLOSED (a refusal, never a silent pass).
        """
        from elt_taskgen.runtime import attestation as att

        copies = install_task_for_model(
            FIXTURE_PUBLIC_TASK, self.root / "attempt-mount"
        )
        # The model copy is credential-FREE in the sense that matters (no live
        # value anywhere) and still counts one credential-shaped NAME.
        assert_model_copy_is_credential_free(copies.model_dir)
        self.assertGreaterEqual(
            att.count_credential_files(copies.model_dir), 1
        )

        # The policy's own surface — what a labelled run executes in — is 0.
        self.assertEqual(att.count_credential_files(copies.model_dir / "elt"), 0)

    def test_candidate_tree_dotenv_is_refused_before_any_mount(self) -> None:
        """A `.env` in a candidate tree never reaches the runner (Table 9).

        `.env` is an out-of-band configuration channel into the dbt and
        Terraform runners: it can redirect `DBT_PROFILES_DIR`, name another
        target, or carry warehouse values the harness never wrote, and none of
        that appears in the sealed artifact the lane claims to be replaying.
        The IMAGE-side rejection is fenced (an owner hand-off); this is the
        harness-side twin at the seam that launches the container, so the
        property holds today and is defence in depth once the image lands.

        The vocabulary is the repository's shared `dotenv` name rule, not a
        second list, and the refusal is about the file EXISTING: an empty
        `.env` still redirects dbt, and nothing here is opened.
        """
        tree = self.root / "candidate"
        (tree / "models").mkdir(parents=True)
        (tree / "dbt_project.yml").write_text("name: candidate\n", encoding="utf-8")
        (tree / "models" / "mart.sql").write_text("select 1\n", encoding="utf-8")
        self.assertEqual(candidate_tree_dotenv_paths(tree), ())
        assert_candidate_tree_is_image_safe(tree)

        gate = GraderLaneRunner(_RecordingRunner())
        gate.run(["dbt", "build"], cwd=tree)
        self.assertEqual(gate.command_count, 1)

        # Every shape the shared rule calls a dotenv file, empty included.
        for name in (".env", ".env.local", "prod.env", ".envrc"):
            with self.subTest(name=name):
                dotted = tree / name
                dotted.write_text("", encoding="utf-8")
                self.assertEqual(candidate_tree_dotenv_paths(tree), (name,))
                with self.assertRaises(ModelCopyError) as caught:
                    assert_candidate_tree_is_image_safe(tree)
                self.assertIn(name, str(caught.exception))
                fresh = GraderLaneRunner(_RecordingRunner())
                with self.assertRaises(ModelCopyError):
                    fresh.run(["dbt", "build"], cwd=tree)
                self.assertEqual(fresh.command_count, 0)
                self.assertEqual(fresh.refused_count, 1)
                dotted.unlink()

        # Nested, and a directory named `.env` is not a dotenv FILE.
        nested = tree / "models" / ".env"
        nested.write_text("", encoding="utf-8")
        self.assertEqual(candidate_tree_dotenv_paths(tree), ("models/.env",))
        nested.unlink()
        (tree / ".env").mkdir()
        self.assertEqual(candidate_tree_dotenv_paths(tree), ())
        assert_candidate_tree_is_image_safe(tree)

        # A missing tree is not a candidate tree; nothing to refuse.
        self.assertEqual(candidate_tree_dotenv_paths(tree / "absent"), ())

    @unittest.skipUnless(
        _dbt_image_declares_candidate_tree_guard(),
        "the dbt runner images do not declare the candidate-tree guard yet; "
        "runtime-images/ is fenced and the pin is a Phase 5 owner hand-off",
    )
    def test_cloud_image_rejects_dotenv_in_candidate_tree(self) -> None:
        """The image pin itself, read from the committed build context.

        Table 9 asks the dbt-core 1.12.0 images to set
        `DBT_ENGINE_PROFILES_DIR` and `DBT_PROFILES_DIR` explicitly, to strip
        `DBT_` / `DBT_ENGINE_` prefixes from the inherited environment, and to
        REJECT `.env` in candidate trees. `runtime-images/` is fenced, so this
        test skips until the owner lands the pin and then holds it. It builds,
        pulls and runs nothing: no test in this repository invokes Docker.
        """
        text = _dbt_image_build_context()
        for variable in ("DBT_ENGINE_PROFILES_DIR", "DBT_PROFILES_DIR"):
            with self.subTest(variable=variable):
                self.assertIn(variable, text)
        self.assertIn(".env", text)
        # The image and the harness must refuse the same set, so the harness
        # twin cannot pass a tree the image would then reject inside the
        # container, where the failure has no diagnosis.
        tree = self.root / "image-parity"
        tree.mkdir(parents=True)
        (tree / ".env").write_text("", encoding="utf-8")
        with self.assertRaises(ModelCopyError):
            assert_candidate_tree_is_image_safe(tree)

    def test_grader_lane_gate_refuses_an_empty_or_unknown_program(self) -> None:
        gate = GraderLaneRunner(_RecordingRunner())
        with self.assertRaises(ModelCopyError):
            gate.run([])
        with self.assertRaises(ModelCopyError):
            gate.run(["curl", "https://example.invalid"])
        with self.assertRaises(ModelCopyError):
            assert_no_agent_process([])
        assert_no_agent_process(["terraform", "validate", "-json"])
        # An absolute path is judged by its basename, not by its directory.
        gate.run(["/usr/local/bin/terraform", "validate"])
        self.assertEqual(gate.command_count, 1)


class RepairPassModelCopyTests(unittest.TestCase):
    """The Phase 4/5 repair pass: findings p5-6 and p5-7."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.revoked: list[ScopedPrincipal] = []
        self.principal = ScopedPrincipal(
            principal_id="elt-attempt-0009",
            destination=Destination.SNOWFLAKE,
            attempt_id="attempt-0009",
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_cleanup_receipt_counts_the_credentials_that_survived(self) -> None:
        """finding p5-6: `live_credential_files` was a LITERAL 0.

        The field's own docstring claimed "a non-zero value never reaches a
        receipt, because release fails closed first", and `cleanup()` neither
        checked that `release_model()` had run nor looked at the replay tree —
        so a `remove_replay=False` cleanup on the cloud lane sealed a receipt
        asserting a verification that never happened while the replay tree
        with LIVE values was still on disk.
        """
        copies = install_task_for_model(
            FIXTURE_PUBLIC_TASK,
            self.root / "attempt-survivor",
            lane=LANE_CLOUD,
            principal=self.principal,
            revoke_principal=self.revoked.append,
        )
        copies.release_model()
        replay = copies.install_replay(_live_credentials(Destination.SNOWFLAKE))
        surviving = live_credential_findings(replay)
        self.assertTrue(surviving, "the replay tree holds live values by design")

        receipt = copies.cleanup(remove_replay=False, remove_model=False)
        self.assertTrue(replay.is_dir())
        self.assertFalse(receipt.replay_removed)
        # THE FIX: the receipt reports what is still there.
        self.assertEqual(receipt.live_credential_files, len(surviving))
        self.assertIs(receipt.model_released, True)
        self.assertEqual(recompute_receipt_digest(receipt), receipt.receipt_digest)

        # A cleanup with NO release at all says so, instead of asserting a
        # verification that never ran.
        never = install_task_for_model(
            FIXTURE_PUBLIC_TASK, self.root / "attempt-unreleased"
        )
        unreleased = never.cleanup()
        self.assertIs(unreleased.model_released, False)
        self.assertEqual(unreleased.live_credential_files, 0)

    def test_grader_lane_refuses_docker_interpreter_escapes(self) -> None:
        """finding p5-7: the allowlist judged argv[0] alone, and `docker` is on
        it — so an allowed program ran an arbitrary interpreter or agent inside
        the grader container while `agent_process_count` stayed a constant 0.
        """
        runner = _RecordingRunner()
        gate = GraderLaneRunner(runner, check_candidate_tree=False)
        for argv in (
            ["docker", "run", "--entrypoint", "python", "img", "-c", "import _socket"],
            ["docker", "run", "img", "python3", "/agent/loop.py"],
            ["docker", "run", "--rm", "-v", "/:/host", "img", "sh", "-c", "cat /host/etc/passwd"],
            ["docker", "exec", "c", "bash", "-lc", "curl https://example.invalid"],
            ["docker", "run", "--privileged", "img", "dbt", "build"],
            ["docker", "build", "."],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(ModelCopyError):
                    gate.run(argv)
        self.assertEqual(gate.command_count, 0)
        self.assertEqual(gate.refused_count, 6)
        self.assertEqual(runner.argv, [])
        # The legitimate replay commands still run.
        for argv in (
            ["docker", "run", "--rm", "runner@sha256:" + "a" * 64, "dbt", "build"],
            ["dbt", "run", "--select", "nodes"],
            ["terraform", "validate", "-json"],
        ):
            gate.run(argv)
        self.assertEqual(gate.command_count, 3)
        # `agent_process_count` is now DERIVED from what was forwarded, so it
        # reports what happened rather than what the design intended.
        self.assertEqual(gate.agent_process_count, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Credential hygiene (roadmap 0.F; threat row A22).

`review/tools/credential_sweep.py` reports credential-shaped files by NAME
(`*_credential.json`, `.env`, `profiles.yml`, Terraform state, key material)
and by live secret-shaped VALUE (a `password`/`secret`/`token`/`*_key` key
holding a non-empty, non-placeholder, non-fixture string), and never prints a
value. Three contracts live here: the sweep itself on a synthetic tree; the
model-facing attempt workspace carries no live credential; and the operator's
`runs/` tree carries none — that last one gated behind
`ELT_TASKGEN_ENFORCE_RUNS_SWEEP=1` until the owner has rotated and purged the
canary attempt copies under `runs/runtime_canary_*/**/live/`.

Nothing here reads `secrets/`, `runs/**/live/` or a real `*_credential.json`:
every fixture is synthetic, and the gated test is the owner's to run.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import yaml

from elt_taskgen.review.tools import credential_sweep as CS

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Synthetic secret-shaped values (never real). Every test that plants one
#: asserts it never surfaces in a finding, a repr or a report.
LIVE_SECRET = "s3cr3t-0f4b1c9e-7d2a-4e8f-b1c3-9a8d7e6f5c4b"
LIVE_TOKEN = "dapi0123456789abcdef0123456789abcdef"
LIVE_PASSWORD = "Tr0ub4dor&3-hunter"

_TERRAFORM_MAIN = (
    "terraform {\n"
    "  required_providers {\n"
    "    airbyte = {\n"
    '      source  = "airbytehq/airbyte"\n'
    '      version = "0.6.5"\n'
    "    }\n"
    "  }\n"
    "}\n"
)


def _vendored(rel: Path) -> bool:
    """Third-party package trees fetched into a drive (`dbt deps`): their CI
    fixtures carry throwaway container passwords that are not ours and that
    no harness code ever installed."""
    return bool({"dbt_packages", "node_modules", "site-packages"} & set(rel.parts))


class CredentialSweepTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def _write(self, rel: str, content: str | bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _by_path(findings: list[CS.CredentialFinding]) -> dict[str, CS.CredentialFinding]:
        return {finding.path: finding for finding in findings}

    def test_sweep_reports_credential_shaped_files_by_name_and_by_value(self) -> None:
        self._write(
            "task/databricks_credential.json",
            json.dumps({"hostname": "h", "http_path": "/sql", "client_id": "c", "secret": LIVE_SECRET}),
        )
        self._write("ops/databricks-attempt.json", json.dumps({"hostname": "h", "access_token": LIVE_TOKEN}))
        self._write(
            "task/config.yaml",
            yaml.safe_dump({"Airbyte": {"config": {"username": "airbyte", "password": LIVE_PASSWORD, "workspace_id": "w"}}}),
        )
        self._write("env/.env", f"export AIRBYTE_PASSWORD='{LIVE_PASSWORD}'\nDB_PORT=5432\n")
        self._write(
            "task/elt/main.tf",
            'resource "airbyte_destination_snowflake" "d" {\n'
            f'  password = "{LIVE_PASSWORD}"  # inlined by a solver\n'
            '  host = "x"\n}\n',
        )
        self._write("keys/id_rsa", "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n")
        self._write("keys/server.pem", "-----BEGIN CERTIFICATE-----\nMIIB\n")
        self._write("plain/notes.md", f"the token is {LIVE_TOKEN}\n")  # prose: not a config shape

        findings = CS.sweep_credential_shaped_files(self.root)
        by_path = self._by_path(findings)
        self.assertEqual(
            sorted(by_path),
            [
                "env/.env",
                "keys/id_rsa",
                "keys/server.pem",
                "ops/databricks-attempt.json",
                "task/config.yaml",
                "task/databricks_credential.json",
                "task/elt/main.tf",
            ],
        )
        self.assertEqual([f.path for f in findings], sorted(by_path))
        self.assertTrue(all(f.live for f in findings))
        credential = by_path["task/databricks_credential.json"]
        self.assertEqual(credential.name_pattern, "credential_json")
        self.assertEqual(credential.secret_keys, ("secret",))
        self.assertFalse(credential.opaque)
        # An operator's scoped credential file: no name rule, caught by value.
        self.assertIsNone(by_path["ops/databricks-attempt.json"].name_pattern)
        self.assertEqual(by_path["ops/databricks-attempt.json"].secret_keys, ("access_token",))
        self.assertEqual(by_path["task/config.yaml"].secret_keys, ("password",))
        self.assertEqual(by_path["env/.env"].name_pattern, "dotenv")
        self.assertEqual(by_path["env/.env"].secret_keys, ("airbyte_password",))
        self.assertEqual(by_path["task/elt/main.tf"].secret_keys, ("password",))
        for opaque in ("keys/id_rsa", "keys/server.pem"):
            self.assertEqual(by_path[opaque].name_pattern, "key_material", opaque)
            self.assertTrue(by_path[opaque].opaque, opaque)
            self.assertEqual(by_path[opaque].secret_keys, (), opaque)
        self.assertEqual(by_path["env/.env"].size_bytes, (self.root / "env" / ".env").stat().st_size)

    def test_sweep_treats_placeholders_fixtures_and_counters_as_clean(self) -> None:
        # The exporter's public bundle: placeholder credential file, placeholder
        # Airbyte block, the public source-fixture values.
        self._write(
            "release/public/t/snowflake_credential.json",
            json.dumps({"account": "", "user": "", "password": ""}, indent=2),
        )
        self._write(
            "release/public/t/config.yaml",
            yaml.safe_dump(
                {
                    "Airbyte": {"config": {"username": "", "password": "", "workspace_id": ""}},
                    "snowflake": {"config": {"account": "", "password": "", "username": ""}},
                    "postgres": {"config": {"password": "testelt", "user": "postgres", "port": 5432}},
                    "aws_s3": {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"},
                }
            ),
        )
        # Vendored dbt package CI fixtures: env references and Actions secrets.
        self._write("vendor/.env/snowflake.env", "SNOWFLAKE_TEST_PASSWORD=\nSNOWFLAKE_TEST_ACCOUNT=${SNOWFLAKE_TEST_ACCOUNT}\n")
        self._write("vendor/ci.yml", "env:\n  DBT_ENV_SECRET_PASS: ${{ secrets.PASS }}\n")
        self._write(
            "task/elt/profiles.yml",
            yaml.safe_dump({"default": {"outputs": {"dev": {"password": "{{ env_var('PG_PASS') }}", "type": "postgres"}}}}),
        )
        self._write("task/elt/docs.yml", "password: <your-password-here>\ntoken: '[REDACTED]'\nsecret: '***'\n")
        # A transcript: token COUNTERS and identifiers are not secrets.
        self._write(
            "transcripts/x.json",
            json.dumps({"usage": {"input_tokens": 12, "output_tokens": 3, "cache_read_input_tokens": 0}, "token": "", "client_id": "abc", "max_tokens": 4096}),
        )
        self._write("task/elt/dev.duckdb", b"\x00\x01binary")
        self._write("empty/databricks_credential.json", "")

        self.assertEqual([], CS.sweep_credential_shaped_files(self.root))
        listing = self._by_path(CS.sweep_credential_shaped_files(self.root, live_only=False))
        self.assertEqual(
            sorted(listing),
            [
                "empty/databricks_credential.json",
                "release/public/t/snowflake_credential.json",
                "task/elt/profiles.yml",
                "vendor/.env/snowflake.env",
            ],
        )
        self.assertFalse(any(f.live for f in listing.values()))
        self.assertEqual(listing["release/public/t/snowflake_credential.json"].secret_keys, ())
        self.assertEqual(listing["release/public/t/snowflake_credential.json"].name_pattern, "credential_json")

    def test_sweep_never_returns_or_prints_a_value(self) -> None:
        self._write("task/databricks_credential.json", json.dumps({"secret": LIVE_SECRET}))
        self._write("ops/.env", f"TOKEN={LIVE_TOKEN}\n")
        findings = CS.sweep_credential_shaped_files(self.root)
        self.assertEqual(len(findings), 2)
        rendered = "\n".join(
            (repr(findings), str(findings), CS.format_findings(findings), *(f.describe() for f in findings))
        )
        for secret in (LIVE_SECRET, LIVE_TOKEN):
            self.assertNotIn(secret, rendered)
        self.assertIn("task/databricks_credential.json", rendered)
        self.assertIn("keys=secret", rendered)
        # The finding type has no field that could hold a value.
        self.assertEqual(
            set(CS.CredentialFinding.__dataclass_fields__),
            {"path", "size_bytes", "name_pattern", "secret_keys", "opaque"},
        )
        self.assertEqual(CS.format_findings([]), "no credential-shaped files")

    def test_sweep_never_follows_symlinks_and_reports_unvettable_files(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "snowflake_credential.json"
            outside.write_text(json.dumps({"password": LIVE_SECRET}), encoding="utf-8")
            (self.root / "link_credential.json").symlink_to(outside)
            (self.root / "linkdir").symlink_to(Path(other))
            self._write("big/databricks_credential.json", "{" + " " * 2048 + "}")
            self._write("bin/redshift_credential.json", b"\xff\xfe\x00 not utf-8")
            self._write("vendor/dbt_packages/x/.env", f"PASSWORD={LIVE_PASSWORD}\n")
            findings = self._by_path(CS.sweep_credential_shaped_files(self.root, max_bytes=1024))
            self.assertEqual(
                sorted(findings),
                ["big/databricks_credential.json", "bin/redshift_credential.json", "vendor/dbt_packages/x/.env"],
            )
            for opaque in ("big/databricks_credential.json", "bin/redshift_credential.json"):
                self.assertTrue(findings[opaque].opaque and findings[opaque].live, opaque)
            excluded = CS.sweep_credential_shaped_files(self.root, max_bytes=1024, exclude=_vendored)
            self.assertNotIn("vendor/dbt_packages/x/.env", [f.path for f in excluded])
        with self.assertRaises(NotADirectoryError):
            CS.sweep_credential_shaped_files(self.root / "nope")

    def test_secret_key_and_placeholder_vocabulary(self) -> None:
        for key in (
            "password", "PASSWORD", "Airbyte-Password", "client_secret", "api_key", "apikey",
            "access_key", "secret_access_key", "AWS_SECRET_ACCESS_KEY", "private_key",
            "access_token", "personal_access_token", "DBT_ENV_SECRET_POSTGRES_PASS", "bearer",
            "aws.secret_access_key", "passphrase",
        ):
            self.assertTrue(CS.is_secret_key(key), key)
        for key in (
            "input_tokens", "max_tokens", "token_count", "client_id", "access_key_id",
            "workspace_id", "password_hash", "bypass", "passthrough", "hostname", "http_path",
            "username", "secret_free", "", None, 3,
        ):
            self.assertFalse(CS.is_secret_key(key), repr(key))
        for value in (
            "", "   ", "${SNOWFLAKE_PASSWORD}", "$PASSWORD", "{{ env_var('X') }}",
            "${{ secrets.X }}", "<your-token>", "[REDACTED]", "***", "xxxx", "placeholder",
            "CHANGEME", "your_password_here", '""', "''", None, 5439, True,
        ):
            self.assertTrue(CS.is_placeholder_value(value), repr(value))
        for value in (LIVE_SECRET, LIVE_TOKEN, "a", "hunter2", "postgres"):
            self.assertFalse(CS.is_placeholder_value(value), value)
        for value in ("testelt", "test", '"test"'):
            self.assertTrue(CS.is_public_fixture_value(value), value)
        for value in ("testelt2", "", None, LIVE_SECRET):
            self.assertFalse(CS.is_public_fixture_value(value), repr(value))

    def test_public_fixture_values_are_the_exporters_constants(self) -> None:
        """The allowance is exactly what `export/eltbench.py` writes into every
        public bundle; a new fixture value must be added in both places."""
        source = (REPO_ROOT / "src" / "elt_taskgen" / "export" / "eltbench.py").read_text(encoding="utf-8")
        self.assertIn('"password": "testelt"', source)
        self.assertIn('"AWS_ACCESS_KEY_ID": "test"', source)
        self.assertIn('"AWS_SECRET_ACCESS_KEY": "test"', source)
        self.assertEqual(CS.PUBLIC_SOURCE_FIXTURE_VALUES, frozenset({"testelt", "test"}))


class AttemptWorkspaceTest(unittest.TestCase):
    """Threat row A22, control 2: the model-facing attempt copy has the public
    task shape with placeholder credentials only. The sweep is the harness's
    check; an installed (credential-injected) copy is the negative control."""

    @staticmethod
    def _synthetic_attempt(root: Path) -> Path:
        task = root / "attempt" / "task"
        for sub in ("elt", "schemas", "documentation"):
            (task / sub).mkdir(parents=True)
        (task / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "Airbyte": {
                        "config": {
                            "server_url": "",
                            "username": "",
                            "password": "",
                            "workspace_id": "",
                            "snowflake_definition_id": "424892c4-daac-4491-b35d-c6688ba547ba",
                        }
                    },
                    "snowflake": {
                        "config": {
                            "account": "", "database": "task_db", "password": "", "role": "",
                            "schema": "AIRBYTE_SCHEMA", "username": "", "warehouse": "",
                        }
                    },
                    "postgres": {
                        "config": {
                            "database": "task_db", "host": "elt-postgres", "password": "testelt",
                            "port": 5432, "schema": "public", "sync_mode": "full_refresh_overwrite",
                            "tables": ["users"], "user": "postgres",
                        }
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (task / "snowflake_credential.json").write_text(
            json.dumps({"account": "", "user": "", "password": ""}, indent=2) + "\n", encoding="utf-8"
        )
        (task / "elt" / "main.tf").write_text(_TERRAFORM_MAIN, encoding="utf-8")
        (task / "elt" / "profiles.yml").write_text(
            yaml.safe_dump(
                {"task": {"target": "dev", "outputs": {"dev": {"type": "snowflake", "password": "{{ env_var('SNOWFLAKE_PASSWORD') }}"}}}}
            ),
            encoding="utf-8",
        )
        (task / "schemas" / "users.csv").write_text("column_name,column_description\nid,Primary key\n", encoding="utf-8")
        (task / "data_model.yaml").write_text("models: []\n", encoding="utf-8")
        (task / "check_job_status.py").write_text("# helper\n", encoding="utf-8")
        sources = root / "attempt" / "sources"
        sources.mkdir()
        (sources / "plan.json").write_text(json.dumps({"services": ["elt-postgres"], "population": "primary"}), encoding="utf-8")
        return root / "attempt"

    def test_attempt_workspace_contains_no_live_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attempt = self._synthetic_attempt(Path(directory).resolve())
            findings = CS.sweep_credential_shaped_files(attempt)
            self.assertEqual([], findings, CS.format_findings(findings))
            listing = CS.sweep_credential_shaped_files(attempt, live_only=False)
            self.assertEqual(
                [(f.path, f.name_pattern, f.live) for f in listing],
                [("task/elt/profiles.yml", "profiles_yml", False), ("task/snowflake_credential.json", "credential_json", False)],
            )
            # Negative control: an installed copy (`install_task` writes live
            # values into config.yaml and the credential file) is caught, by
            # key name only.
            (attempt / "task" / "snowflake_credential.json").write_text(
                json.dumps({"account": "acct", "user": "solver", "password": LIVE_PASSWORD}), encoding="utf-8"
            )
            config = yaml.safe_load((attempt / "task" / "config.yaml").read_text(encoding="utf-8"))
            config["Airbyte"]["config"]["password"] = LIVE_PASSWORD
            config["Airbyte"]["config"]["username"] = "airbyte"
            (attempt / "task" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
            caught = CS.sweep_credential_shaped_files(attempt)
            self.assertEqual(
                {f.path: f.secret_keys for f in caught},
                {"task/config.yaml": ("password",), "task/snowflake_credential.json": ("password",)},
            )
            self.assertNotIn(LIVE_PASSWORD, CS.format_findings(caught) + repr(caught))


class RunsTreeTest(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("ELT_TASKGEN_ENFORCE_RUNS_SWEEP") == "1",
        "set ELT_TASKGEN_ENFORCE_RUNS_SWEEP=1 once the owner has rotated and purged the "
        "attempt copies under runs/runtime_canary_*/**/live/ (roadmap 0.F; threat row A22)",
    )
    def test_runs_tree_carries_no_credential_shaped_files(self) -> None:
        """The operator's `runs/` tree: no `*_credential.json` with a live
        value, no installed `config.yaml`, no `.env`, key or state file with a
        secret. Vendored third-party package trees are excluded (their CI
        fixtures are throwaway container passwords no harness code installed).
        The failure message carries paths and key names only."""
        root = REPO_ROOT / "runs"
        if not root.is_dir():
            self.skipTest("this checkout has no runs/ tree")
        findings = CS.sweep_credential_shaped_files(root, exclude=_vendored)
        self.assertEqual(
            [],
            findings,
            "\ncredential-shaped files under runs/ (paths and key names only):\n" + CS.format_findings(findings),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

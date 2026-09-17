from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from elt_taskgen.destinations import (
    DESTINATION_CONTRACTS,
    SOURCE_CONNECTOR_CONTRACTS,
    Destination,
)
from elt_taskgen.runtime.bootstrap import (
    BootstrapError,
    bootstrap_task_airbyte,
    install_airbyte,
    parse_abctl_credentials,
)
from elt_taskgen.runtime.process import CommandResult


class FakeRunner:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd=None, env=None, stdin_path=None) -> CommandResult:
        self.calls.append(tuple(str(value) for value in argv))
        return CommandResult(0, self.outputs.pop(0) if self.outputs else "")


class FakeAirbyte:
    def __init__(self, destination_ids: tuple[str, ...] | None = None) -> None:
        self.published = None
        self.destination_ids = destination_ids or (
            DESTINATION_CONTRACTS[Destination.SNOWFLAKE].definition_id,
        )

    def create_workspace(self, name: str) -> str:
        self.workspace_name = name
        return "workspace-1"

    def list_source_definitions(self, workspace_id: str):
        return [
            {
                "id": contract.definition_id,
                "dockerImageTag": contract.connector_version,
            }
            for contract in (
                SOURCE_CONNECTOR_CONTRACTS["postgres"],
                SOURCE_CONNECTOR_CONTRACTS["flat_files"],
            )
        ]

    def list_destination_definitions(self, workspace_id: str):
        rows = []
        for definition_id in self.destination_ids:
            row = {"id": definition_id}
            for contract in DESTINATION_CONTRACTS.values():
                if contract.definition_id == definition_id:
                    if contract.connector_version is not None:
                        row["dockerImageTag"] = contract.connector_version
                    break
            rows.append(row)
        return rows

    def publish_declarative_source_definition(self, workspace_id, *, name, manifest):
        self.published = (workspace_id, name, manifest)
        return "custom-id"


class RuntimeBootstrapTests(unittest.TestCase):
    @staticmethod
    def _task_config(
        destination: Destination,
        *,
        airbyte_updates: dict[str, object] | None = None,
        rest: bool = False,
    ) -> dict[str, object]:
        contract = DESTINATION_CONTRACTS[destination]
        airbyte: dict[str, object] = {
            contract.definition_key: contract.definition_id,
        }
        airbyte.update(airbyte_updates or {})
        config: dict[str, object] = {
            contract.config_section: {"config": {}},
            "Airbyte": {"config": airbyte},
        }
        for source_contract in SOURCE_CONNECTOR_CONTRACTS.values():
            if source_contract.definition_key in airbyte:
                config[source_contract.config_section] = {
                    "config": {"sync_mode": "full_refresh_append"}
                }
        if rest:
            config["custom_api"] = {
                "config": {
                    "sync_mode": "full_refresh_append",
                    "tables": ["events"],
                }
            }
        return config

    def test_parse_abctl_credentials_supports_current_labels(self) -> None:
        parsed = parse_abctl_credentials(
            "Email: admin@example.com\nPassword: secret\n"
            "Client-Id: client\nClient-Secret: client-secret\n"
        )
        self.assertEqual(parsed.username, "admin@example.com")
        self.assertEqual(parsed.password, "secret")
        self.assertEqual(parsed.client_id, "client")

        current = parse_abctl_credentials(
            '{"password":"ui-password","client-id":"client",'
            '"client-secret":"client-secret"}'
        )
        self.assertIsNone(current.username)
        self.assertEqual(current.client_id, "client")

    def test_install_requires_and_checks_pinned_versions(self) -> None:
        runner = FakeRunner(
            [
                "abctl version v0.29.0\n",
                "",
                "",
                "Email: user\nPassword: pass\n",
            ]
        )
        credentials = install_airbyte(
            chart_version="1.6.0", abctl_version="v0.29.0", runner=runner
        )
        self.assertEqual(credentials.username, "user")
        self.assertEqual(
            runner.calls[1],
            (
                "abctl",
                "local",
                "install",
                "--chart-version",
                "1.6.0",
                "--no-browser",
            ),
        )
        with self.assertRaisesRegex(BootstrapError, "versions must be pinned"):
            install_airbyte(chart_version="", abctl_version="", runner=runner)
        for alias in ("latest", "stable", "1.6", ">=1.6.0"):
            with self.subTest(alias=alias):
                with self.assertRaisesRegex(BootstrapError, "pinned exactly"):
                    install_airbyte(
                        chart_version=alias,
                        abctl_version="v0.29.0",
                        runner=runner,
                    )

    def test_task_bootstrap_verifies_ids_and_publishes_rest_definition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            answer = root / "answer"
            public.mkdir()
            answer.mkdir()
            (public / "config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "snowflake": {"config": {}},
                        "custom_api": {
                            "config": {
                                "sync_mode": "full_refresh_append",
                                "tables": ["events"],
                            }
                        },
                        "postgres": {
                            "config": {"sync_mode": "full_refresh_append"}
                        },
                        "flat_files": {"config": {}},
                        "Airbyte": {
                            "config": {
                                "postgres_definition_id": (
                                    SOURCE_CONNECTOR_CONTRACTS[
                                        "postgres"
                                    ].definition_id
                                ),
                                "files_definition_id": (
                                    SOURCE_CONNECTOR_CONTRACTS[
                                        "flat_files"
                                    ].definition_id
                                ),
                                "snowflake_definition_id": (
                                    DESTINATION_CONTRACTS[
                                        Destination.SNOWFLAKE
                                    ].definition_id
                                ),
                                "custom_api_definition_id": "",
                            }
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (answer / "airbyte_custom_api_manifest.yaml").write_text(
                "version: 6.48.15\ntype: DeclarativeSource\n", encoding="utf-8"
            )
            client = FakeAirbyte()
            result = bootstrap_task_airbyte(
                public, answer, "demo", client  # type: ignore[arg-type]
            )
            self.assertEqual(result.workspace_id, "workspace-1")
            self.assertEqual(result.custom_api_definition_id, "custom-id")
            self.assertEqual(
                result.required_source_definition_ids,
                tuple(
                    sorted(
                        (
                            SOURCE_CONNECTOR_CONTRACTS[
                                "flat_files"
                            ].definition_id,
                            SOURCE_CONNECTOR_CONTRACTS[
                                "postgres"
                            ].definition_id,
                        )
                    )
                ),
            )
            self.assertEqual(
                result.required_source_connector_versions,
                tuple(
                    sorted(
                        (
                            (
                                "flat_files",
                                SOURCE_CONNECTOR_CONTRACTS[
                                    "flat_files"
                                ].connector_version,
                            ),
                            (
                                "postgres",
                                SOURCE_CONNECTOR_CONTRACTS[
                                    "postgres"
                                ].connector_version,
                            ),
                        )
                    )
                ),
            )
            self.assertIs(result.destination, Destination.SNOWFLAKE)
            self.assertEqual(
                result.destination_definition_id,
                DESTINATION_CONTRACTS[Destination.SNOWFLAKE].definition_id,
            )
            self.assertEqual(
                result.snowflake_definition_id,
                result.destination_definition_id,
            )
            self.assertEqual(client.published[0], "workspace-1")

    def test_task_bootstrap_supports_every_registered_destination(self) -> None:
        for destination, contract in DESTINATION_CONTRACTS.items():
            with self.subTest(destination=destination.value):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    public = root / "public"
                    answer = root / "answer"
                    public.mkdir()
                    answer.mkdir()
                    (public / "config.yaml").write_text(
                        yaml.safe_dump(
                            self._task_config(
                                destination,
                                airbyte_updates={
                                    "postgres_definition_id": (
                                        SOURCE_CONNECTOR_CONTRACTS[
                                            "postgres"
                                        ].definition_id
                                    )
                                },
                            ),
                            sort_keys=False,
                        ),
                        encoding="utf-8",
                    )
                    client = FakeAirbyte((contract.definition_id,))
                    result = bootstrap_task_airbyte(
                        public,
                        answer,
                        "demo",
                        client,  # type: ignore[arg-type]
                    )
                    self.assertIs(result.destination, destination)
                    self.assertEqual(
                        result.destination_definition_id, contract.definition_id
                    )
                    self.assertEqual(
                        result.required_source_definition_ids,
                        (
                            SOURCE_CONNECTOR_CONTRACTS[
                                "postgres"
                            ].definition_id,
                        ),
                    )
                    expected_snowflake = (
                        contract.definition_id
                        if destination is Destination.SNOWFLAKE
                        else None
                    )
                    self.assertEqual(
                        result.snowflake_definition_id, expected_snowflake
                    )

    def test_task_bootstrap_rejects_a_destination_connector_version_drift(self) -> None:
        destination = Destination.DATABRICKS
        contract = DESTINATION_CONTRACTS[destination]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            answer = root / "answer"
            public.mkdir()
            answer.mkdir()
            (public / "config.yaml").write_text(
                yaml.safe_dump(
                    self._task_config(destination),
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            client = FakeAirbyte((contract.definition_id,))
            client.list_destination_definitions = lambda _workspace_id: [  # type: ignore[method-assign]
                {
                    "id": contract.definition_id,
                    "dockerImageTag": "4.0.1",
                }
            ]
            with self.assertRaisesRegex(
                BootstrapError,
                r"databricks connector version must be 4\.0\.2, found 4\.0\.1",
            ):
                bootstrap_task_airbyte(
                    public,
                    answer,
                    "demo",
                    client,  # type: ignore[arg-type]
                )

    def test_task_bootstrap_rejects_a_source_connector_version_drift(self) -> None:
        source = SOURCE_CONNECTOR_CONTRACTS["postgres"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            answer = root / "answer"
            public.mkdir()
            answer.mkdir()
            (public / "config.yaml").write_text(
                yaml.safe_dump(
                    self._task_config(
                        Destination.SNOWFLAKE,
                        airbyte_updates={source.definition_key: source.definition_id},
                    ),
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            client = FakeAirbyte()
            client.list_source_definitions = lambda _workspace_id: [  # type: ignore[method-assign]
                {
                    "id": source.definition_id,
                    "dockerImageTag": "3.8.3",
                }
            ]
            with self.assertRaisesRegex(
                BootstrapError,
                r"postgres source connector version must be 3\.8\.5, found 3\.8\.3",
            ):
                bootstrap_task_airbyte(
                    public,
                    answer,
                    "demo",
                    client,  # type: ignore[arg-type]
                )

    def test_task_bootstrap_rejects_ambiguous_or_mismatched_destination(self) -> None:
        snowflake = DESTINATION_CONTRACTS[Destination.SNOWFLAKE]
        databricks = DESTINATION_CONTRACTS[Destination.DATABRICKS]
        cases = (
            (
                "exactly one registered destination section",
                {
                    "Airbyte": {
                        "config": {
                            snowflake.definition_key: snowflake.definition_id
                        }
                    }
                },
            ),
            (
                "exactly one registered destination section",
                {
                    snowflake.config_section: {"config": {}},
                    databricks.config_section: {"config": {}},
                    "Airbyte": {
                        "config": {
                            snowflake.definition_key: snowflake.definition_id
                        }
                    },
                },
            ),
            (
                "exactly one registered destination definition key",
                {
                    snowflake.config_section: {"config": {}},
                    "Airbyte": {
                        "config": {
                            snowflake.definition_key: snowflake.definition_id,
                            # Even an empty foreign key is destination metadata,
                            # never a source definition to validate accidentally.
                            databricks.definition_key: "",
                        }
                    },
                },
            ),
            (
                "do not agree",
                {
                    snowflake.config_section: {"config": {}},
                    "Airbyte": {
                        "config": {
                            databricks.definition_key: databricks.definition_id
                        }
                    },
                },
            ),
            (
                "unrecognized snowflake",
                {
                    snowflake.config_section: {"config": {}},
                    "Airbyte": {
                        "config": {
                            snowflake.definition_key: "not-the-pinned-id"
                        }
                    },
                },
            ),
        )
        for expected, config in cases:
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    public = root / "public"
                    answer = root / "answer"
                    public.mkdir()
                    answer.mkdir()
                    (public / "config.yaml").write_text(
                        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(BootstrapError, expected):
                        bootstrap_task_airbyte(
                            public,
                            answer,
                            "demo",
                            FakeAirbyte(),  # type: ignore[arg-type]
                        )

    def test_task_bootstrap_fails_when_builtin_definition_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            answer = root / "answer"
            public.mkdir()
            answer.mkdir()
            (public / "config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "snowflake": {"config": {}},
                        "mongodb": {
                            "config": {"sync_mode": "full_refresh_append"}
                        },
                        "Airbyte": {
                            "config": {
                                "mongodb_definition_id": (
                                    SOURCE_CONNECTOR_CONTRACTS[
                                        "mongodb"
                                    ].definition_id
                                ),
                                "snowflake_definition_id": (
                                    DESTINATION_CONTRACTS[
                                        Destination.SNOWFLAKE
                                    ].definition_id
                                ),
                            }
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                BootstrapError,
                SOURCE_CONNECTOR_CONTRACTS["mongodb"].definition_id,
            ):
                bootstrap_task_airbyte(
                    public, answer, "demo", FakeAirbyte()  # type: ignore[arg-type]
                )

    def _bootstrap_config(
        self, config: dict[str, object], *, client: FakeAirbyte | None = None
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            answer = root / "answer"
            public.mkdir()
            answer.mkdir()
            (public / "config.yaml").write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
            )
            return bootstrap_task_airbyte(
                public,
                answer,
                "demo",
                client or FakeAirbyte(),  # type: ignore[arg-type]
            )

    def test_task_bootstrap_derives_private_versions_from_original_config(self) -> None:
        config = self._task_config(
            Destination.SNOWFLAKE,
            airbyte_updates={
                "postgres_definition_id": (
                    SOURCE_CONNECTOR_CONTRACTS["postgres"].definition_id
                )
            },
        )
        result = self._bootstrap_config(config)
        self.assertIs(result.destination, Destination.SNOWFLAKE)
        self.assertEqual(
            result.destination_connector_version,
            DESTINATION_CONTRACTS[Destination.SNOWFLAKE].connector_version,
        )

    def test_task_bootstrap_rejects_public_destination_version_metadata(self) -> None:
        config = self._task_config(Destination.SNOWFLAKE)
        config["Airbyte"]["config"]["snowflake_connector_version"] = "4.1.2"  # type: ignore[index]
        with self.assertRaisesRegex(BootstrapError, "must not expose"):
            self._bootstrap_config(config)

    def test_task_bootstrap_rejects_public_source_version_metadata(self) -> None:
        config = self._task_config(Destination.SNOWFLAKE)
        config["Airbyte"]["config"]["postgres_connector_version"] = "3.8.5"  # type: ignore[index]
        with self.assertRaisesRegex(BootstrapError, "must not expose"):
            self._bootstrap_config(config)

    def test_task_bootstrap_requires_identity_for_each_present_source_section(
        self,
    ) -> None:
        config = self._task_config(
            Destination.SNOWFLAKE,
        )
        config["postgres"] = {
            "config": {"sync_mode": "full_refresh_append"}
        }
        with self.assertRaisesRegex(
            BootstrapError, "source section and Airbyte source definition key"
        ):
            self._bootstrap_config(config)

    def test_task_bootstrap_rejects_unsupported_sync_modes(self) -> None:
        for tamper in ("incremental_append", "overwrite", None):
            with self.subTest(mode=tamper):
                config = self._task_config(
                    Destination.SNOWFLAKE,
                    airbyte_updates={
                        "postgres_definition_id": (
                            SOURCE_CONNECTOR_CONTRACTS["postgres"].definition_id
                        )
                    },
                )
                postgres = config["postgres"]["config"]  # type: ignore[index]
                if tamper is None:
                    postgres.pop("sync_mode")  # type: ignore[union-attr]
                else:
                    postgres["sync_mode"] = tamper  # type: ignore[index]
                with self.assertRaisesRegex(
                    BootstrapError,
                    "postgres.config.sync_mode declares unsupported sync mode",
                ):
                    self._bootstrap_config(config)


if __name__ == "__main__":
    unittest.main()

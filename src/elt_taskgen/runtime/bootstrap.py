"""Bootstrap Airbyte infrastructure without solving a task.

Create a workspace, verify connector definitions, and publish the generated
REST definition. Task sources, destinations, and connections remain solver
work.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from elt_taskgen.destinations import (
    DESTINATION_CONTRACTS,
    SOURCE_CONNECTOR_CONTRACTS,
    Destination,
    config_sync_mode_declarations,
    normalize_sync_mode,
)
from elt_taskgen.runtime.airbyte import (
    AirbyteClient,
    AirbyteError,
    definition_ids,
    definition_versions,
)
from elt_taskgen.runtime.process import Runner, SubprocessRunner


class BootstrapError(RuntimeError):
    """The shared Airbyte runtime is unavailable or incompatible."""


_EXACT_VERSION = re.compile(
    r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?$"
)


def _pinned_version(value: str, *, label: str) -> str:
    version = str(value).strip()
    if not _EXACT_VERSION.fullmatch(version):
        raise BootstrapError(
            f"{label} must be one exact semantic version, not an alias or range"
        )
    return version


@dataclass(frozen=True)
class AbctlCredentials:
    username: str | None
    password: str
    client_id: str | None = None
    client_secret: str | None = None


@dataclass(frozen=True)
class TaskAirbyteBootstrap:
    workspace_id: str
    custom_api_definition_id: str | None
    required_source_definition_ids: tuple[str, ...]
    required_source_connector_versions: tuple[tuple[str, str], ...]
    destination: Destination
    destination_definition_id: str
    destination_connector_version: str | None

    @property
    def snowflake_definition_id(self) -> str | None:
        """Historical spelling retained for Snowflake runtime callers."""

        if self.destination is Destination.SNOWFLAKE:
            return self.destination_definition_id
        return None


def parse_abctl_credentials(output: str) -> AbctlCredentials:
    """Parse current JSON or historical text ``abctl`` credential output."""
    fields: dict[str, str] = {}
    aliases = {
        "email": "username",
        "username": "username",
        "password": "password",
        "client-id": "client_id",
        "client id": "client_id",
        "client-secret": "client_secret",
        "client secret": "client_secret",
    }
    try:
        decoded = json.loads(str(output))
    except (TypeError, json.JSONDecodeError):
        decoded = None
    if isinstance(decoded, dict):
        for raw_key, raw_value in decoded.items():
            key = aliases.get(str(raw_key).strip().strip('"').lower())
            if key and isinstance(raw_value, str) and raw_value:
                fields[key] = raw_value
    for line in str(output).splitlines():
        match = re.match(r"\s*([^:=]+?)\s*[:=]\s*(\S.*)\s*$", line)
        if not match:
            continue
        key = aliases.get(match.group(1).strip().strip('"').lower())
        if key:
            fields[key] = match.group(2).strip().strip(',').strip('"')
    has_client = bool(fields.get("client_id") and fields.get("client_secret"))
    has_basic = bool(fields.get("username") and fields.get("password"))
    if not has_client and not has_basic:
        raise BootstrapError(
            "abctl credentials output contained neither a client credential pair "
            "nor an email/username and password"
        )
    return AbctlCredentials(
        username=fields.get("username"),
        password=fields.get("password", ""),
        client_id=fields.get("client_id"),
        client_secret=fields.get("client_secret"),
    )


#: First installs may pull control-plane images, so use a separate three-hour
#: timeout exposed as `runtime install-airbyte --timeout`.
DEFAULT_INSTALL_TIMEOUT_SECONDS = 3 * 3600.0


def install_airbyte(
    *,
    chart_version: str,
    abctl_version: str,
    runner: Runner | None = None,
    abctl: str = "abctl",
    timeout: float = DEFAULT_INSTALL_TIMEOUT_SECONDS,
) -> AbctlCredentials:
    """Install a version-pinned Airbyte OSS control plane with abctl.

    Both versions are required so a caller cannot accidentally certify against
    ``latest``.  This is intentionally explicit and may be run once per shared
    benchmark host; population replay uses fresh workspaces and source stacks.
    """
    try:
        chart_version = _pinned_version(chart_version, label="Airbyte chart version")
        abctl_version = _pinned_version(abctl_version, label="abctl version")
    except BootstrapError as exc:
        raise BootstrapError(
            "both abctl and Airbyte chart versions must be pinned exactly: "
            + str(exc)
        ) from None
    runner = runner or SubprocessRunner(timeout=timeout)
    version = runner.run((abctl, "version"))
    installed_versions = re.findall(
        r"(?<![0-9A-Za-z.])v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?",
        version.stdout,
    )
    if abctl_version.lstrip("v") not in {value.lstrip("v") for value in installed_versions}:
        raise BootstrapError(
            "installed abctl version does not match the certified runtime version"
        )
    runner.run(
        (
            abctl,
            "local",
            "install",
            "--chart-version",
            chart_version,
            "--no-browser",
        )
    )
    runner.run((abctl, "local", "status"))
    credentials = runner.run((abctl, "local", "credentials"))
    return parse_abctl_credentials(credentials.stdout)


def _mapping(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BootstrapError(f"invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"invalid {label}: {path}")
    return value


def _definition_contract(
    public_task_dir: Path,
) -> tuple[set[str], Destination, str, bool]:
    config = _mapping(public_task_dir / "config.yaml", label="task config")
    airbyte = (config.get("Airbyte") or {}).get("config")
    if not isinstance(airbyte, dict):
        raise BootstrapError("task config has no Airbyte.config mapping")

    sections = [
        destination
        for destination, contract in DESTINATION_CONTRACTS.items()
        if contract.config_section in config
    ]
    if len(sections) != 1:
        found = [destination.value for destination in sections]
        raise BootstrapError(
            "task config must contain exactly one registered destination "
            f"section (found {found})"
        )
    destination = sections[0]
    destination_contract = DESTINATION_CONTRACTS[destination]
    section = config.get(destination_contract.config_section)
    if not isinstance(section, dict) or not isinstance(section.get("config"), dict):
        raise BootstrapError(
            f"task config has no {destination_contract.config_section}.config mapping"
        )

    definitions_by_key = {
        contract.definition_key: registered_destination
        for registered_destination, contract in DESTINATION_CONTRACTS.items()
    }
    destination_keys = [key for key in definitions_by_key if key in airbyte]
    if len(destination_keys) != 1:
        raise BootstrapError(
            "task Airbyte config must contain exactly one registered destination "
            f"definition key (found {sorted(destination_keys)})"
        )
    definition_key = destination_keys[0]
    key_destination = definitions_by_key[definition_key]
    if key_destination is not destination:
        raise BootstrapError(
            "task destination section and Airbyte destination definition key "
            "do not agree"
        )
    destination_id = airbyte.get(definition_key)
    if not isinstance(destination_id, str) or not destination_id:
        raise BootstrapError(
            f"task Airbyte config has no {destination.value} destination definition id"
        )
    if destination_id != destination_contract.definition_id:
        raise BootstrapError(
            f"task Airbyte config has an unrecognized {destination.value} "
            "destination definition id"
        )
    destination_definition_keys = set(definitions_by_key)
    source_definition_keys = {
        contract.definition_key for contract in SOURCE_CONNECTOR_CONTRACTS.values()
    }
    source_ids: set[str] = set()
    for source_contract in SOURCE_CONNECTOR_CONTRACTS.values():
        section_present = source_contract.config_section in config
        key_present = source_contract.definition_key in airbyte
        if section_present != key_present:
            raise BootstrapError(
                "task source section and Airbyte source definition key "
                "disagree for "
                f"{source_contract.config_section}"
            )
        if key_present:
            value = airbyte.get(source_contract.definition_key)
            if value != source_contract.definition_id:
                raise BootstrapError(
                    f"task Airbyte config has an unrecognized "
                    f"{source_contract.config_section} source definition id"
                )
            source_ids.add(source_contract.definition_id)
    unexpected_source_keys = sorted(
        key
        for key in airbyte
        if key.endswith("_definition_id")
        and key not in destination_definition_keys
        and key not in source_definition_keys
        and key != "custom_api_definition_id"
    )
    if unexpected_source_keys:
        raise BootstrapError(
            "task Airbyte config has unregistered source definition keys: "
            + ", ".join(unexpected_source_keys)
        )
    public_version_keys = sorted(
        key for key in airbyte if key.endswith("_connector_version")
    )
    if public_version_keys:
        raise BootstrapError(
            "task Airbyte config must not expose harness connector-version "
            "metadata: " + ", ".join(public_version_keys)
        )
    for path, declared_mode in config_sync_mode_declarations(config):
        try:
            normalize_sync_mode(declared_mode)
        except ValueError:
            raise BootstrapError(
                f"task config {path} declares unsupported sync mode "
                f"{declared_mode!r}"
            ) from None
    has_custom_api = "custom_api" in config
    custom_key_present = "custom_api_definition_id" in airbyte
    if not has_custom_api and custom_key_present:
        raise BootstrapError(
            "task custom_api section and definition placeholder do not agree"
        )
    if has_custom_api and custom_key_present and airbyte.get(
        "custom_api_definition_id"
    ) != "":
        raise BootstrapError(
            "task custom_api definition placeholder must be empty before bootstrap"
        )
    return source_ids, destination, destination_id, has_custom_api


def bootstrap_task_airbyte(
    public_task_dir: Path,
    answer_key_dir: Path,
    task_id: str,
    client: AirbyteClient,
    *,
    workspace_id: str | None = None,
) -> TaskAirbyteBootstrap:
    """Create/validate only the Airbyte environment definition for one replay."""
    source_ids, destination, destination_id, has_rest = _definition_contract(
        Path(public_task_dir)
    )
    if workspace_id is None:
        workspace_id = client.create_workspace(f"ELT-Bench {task_id}")
    elif not str(workspace_id):
        raise BootstrapError("workspace_id must be non-empty when supplied")

    source_definitions = client.list_source_definitions(workspace_id)
    available_sources = definition_ids(source_definitions)
    missing_sources = sorted(source_ids - available_sources)
    if missing_sources:
        raise BootstrapError(
            "Airbyte is missing required source connector definition ids: "
            + ", ".join(missing_sources)
        )
    source_versions = definition_versions(source_definitions)
    required_source_versions: list[tuple[str, str]] = []
    contracts_by_id = {
        contract.definition_id: contract
        for contract in SOURCE_CONNECTOR_CONTRACTS.values()
    }
    for definition_id in sorted(source_ids):
        source_contract = contracts_by_id[definition_id]
        expected_source_version = source_contract.connector_version
        actual_source_version = source_versions.get(definition_id)
        if actual_source_version != expected_source_version:
            shown = actual_source_version or "unreported"
            raise BootstrapError(
                f"Airbyte {source_contract.config_section} source connector "
                f"version must be {expected_source_version}, found {shown}"
            )
        required_source_versions.append(
            (source_contract.config_section, expected_source_version)
        )
    destination_definitions = client.list_destination_definitions(workspace_id)
    available_destinations = definition_ids(destination_definitions)
    if destination_id not in available_destinations:
        raise BootstrapError(
            f"Airbyte is missing the required {destination.value} destination "
            "definition id"
        )
    expected_version = DESTINATION_CONTRACTS[destination].connector_version
    if expected_version is not None:
        actual_version = definition_versions(destination_definitions).get(
            destination_id
        )
        if actual_version != expected_version:
            shown = actual_version or "unreported"
            raise BootstrapError(
                f"Airbyte {destination.value} connector version must be "
                f"{expected_version}, found {shown}"
            )

    custom_id: str | None = None
    if has_rest:
        manifest_path = Path(answer_key_dir) / "airbyte_custom_api_manifest.yaml"
        manifest = _mapping(manifest_path, label="Airbyte declarative source manifest")
        try:
            custom_id = client.publish_declarative_source_definition(
                workspace_id,
                name=f"ELT Bench {task_id}",
                manifest=manifest,
            )
        except AirbyteError as exc:
            raise BootstrapError(
                "could not publish the task's Airbyte declarative REST definition"
            ) from exc
    return TaskAirbyteBootstrap(
        workspace_id=workspace_id,
        custom_api_definition_id=custom_id,
        required_source_definition_ids=tuple(sorted(source_ids)),
        required_source_connector_versions=tuple(required_source_versions),
        destination=destination,
        destination_definition_id=destination_id,
        destination_connector_version=expected_version,
    )

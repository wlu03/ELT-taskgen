"""Install a credential-free public task in an isolated runtime directory.

Copy the immutable release to a new directory and inject environment-specific
Airbyte and warehouse values. Attempt state is never reused; source services
and warehouse execution remain outside this module.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from copy import deepcopy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from elt_taskgen.destinations import (
    Destination,
    destination_contract,
    destination_from_config,
    normalize_destination,
)
from elt_taskgen.export.eltbench import assert_public_runtime_shape


_AIRBYTE_AUTH_FIELDS = ("client_id", "client_secret", "password", "username")
_AIRBYTE_FIELDS = (*_AIRBYTE_AUTH_FIELDS, "workspace_id")
_ORIGINAL_AIRBYTE_SERVER_URL = (
    "http://airbyte-abctl-control-plane:80/api/public/v1/"
)


def _exists(path: Path) -> bool:
    """Return true for files, directories, and broken symlinks."""

    return os.path.lexists(os.fspath(path))


def _required_text(
    values: Mapping[str, object], field: str, *, credential_kind: str
) -> str:
    """Read a required secret without ever putting its value in an error."""

    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"missing required {credential_kind} credential field: {field}"
        )
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string when supplied")
    return value


def _custom_api_id(
    airbyte_credentials: Mapping[str, object], explicit: str | None
) -> str | None:
    """Resolve both the upstream input name and the runtime config name."""

    candidates = [
        value
        for value in (
            _optional_text(explicit, field="custom_api_definition_id"),
            _optional_text(
                airbyte_credentials.get("custom_api_definition_id"),
                field="custom_api_definition_id",
            ),
            _optional_text(
                airbyte_credentials.get("api_definition_id"),
                field="api_definition_id",
            ),
        )
        if value is not None
    ]
    if not candidates:
        return None
    if any(value != candidates[0] for value in candidates[1:]):
        raise ValueError("conflicting custom API definition identifiers supplied")
    return candidates[0]


def _load_yaml_mapping(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        # YAML parser errors can quote the offending line.  Do not chain one:
        # this file may be a previously installed config containing secrets.
        raise ValueError(f"source bundle has an invalid {label}") from None
    if not isinstance(value, dict):
        raise ValueError(f"source bundle has an invalid {label}")
    return value


def _section_config(config: Mapping[str, Any], section: str) -> dict[str, Any]:
    outer = config.get(section)
    inner = outer.get("config") if isinstance(outer, dict) else None
    if not isinstance(inner, dict):
        raise ValueError(f"source config is missing {section}.config")
    return inner


def _assert_public_source_shape(source: Path, config: Mapping[str, Any]) -> None:
    """Validate generated and upstream pre-install source shapes.

    Use a temporary secret-free copy to add permitted omitted fields without
    changing the release bundle.
    """

    normalized = deepcopy(dict(config))
    airbyte = _section_config(normalized, "Airbyte")
    changed = False
    for field in ("password", "username", "workspace_id"):
        if field not in airbyte:
            airbyte[field] = ""
            changed = True
    if not airbyte.get("server_url"):
        airbyte["server_url"] = _ORIGINAL_AIRBYTE_SERVER_URL
        changed = True
    if "custom_api" in normalized and "custom_api_definition_id" not in airbyte:
        airbyte["custom_api_definition_id"] = ""
        changed = True
    if not changed:
        assert_public_runtime_shape(source)
        return

    with tempfile.TemporaryDirectory(prefix="elt-taskgen-public-shape-") as directory:
        shadow = Path(directory) / "task"
        shutil.copytree(source, shadow, ignore=_ignore_attempt_state)
        shadow_config = shadow / "config.yaml"
        os.chmod(shadow_config, 0o600)
        shadow_config.write_text(
            yaml.safe_dump(normalized, sort_keys=False), encoding="utf-8"
        )
        assert_public_runtime_shape(shadow)


def _validate_uninstalled_source(source: Path) -> dict[str, Any]:
    """Validate placeholders before copying any potentially installed tree."""

    if not source.is_dir():
        raise FileNotFoundError("combined public task directory does not exist")
    if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("source bundle must not contain symbolic links")

    if (source / "sources").exists():
        raise ValueError(
            "source bundle is not a credential-free combined runtime task"
        )
    forbidden_tokens = ("load_plan", "sql_by_mart", "standalone duckdb")
    for path in source.rglob("*"):
        relative = path.relative_to(source).as_posix().casefold()
        if path.is_file() and path.suffix.casefold() == ".duckdb":
            raise ValueError(
                "source bundle is not a credential-free combined runtime task"
            )
        if any(token in relative for token in forbidden_tokens[:2]):
            raise ValueError(
                "source bundle is not a credential-free combined runtime task"
            )
        if not path.is_file() or _is_attempt_state(path.name):
            continue
        normalized = " ".join(
            path.read_bytes().decode("utf-8", errors="ignore").casefold().split()
        )
        if any(token in normalized for token in forbidden_tokens):
            raise ValueError(
                "source bundle is not a credential-free combined runtime task"
            )

    config = _load_yaml_mapping(source / "config.yaml", label="config.yaml")
    airbyte = _section_config(config, "Airbyte")
    populated_airbyte = [
        field
        for field in _AIRBYTE_FIELDS
        if field in airbyte and airbyte.get(field) != ""
    ]
    if populated_airbyte:
        raise ValueError(
            "source bundle contains populated Airbyte credentials; expected "
            "empty placeholders"
        )

    server_url = airbyte.get("server_url")
    if server_url is not None and not isinstance(server_url, str):
        raise ValueError("source config has an invalid Airbyte server_url")

    has_rest = "custom_api" in config
    if not has_rest and "custom_api_definition_id" in airbyte:
        raise ValueError(
            "source config has a custom API definition without a custom_api source"
        )
    if (
        "custom_api_definition_id" in airbyte
        and airbyte["custom_api_definition_id"] != ""
    ):
        raise ValueError(
            "source bundle contains a populated custom API definition identifier"
        )

    # This also requires schemas, runtime documentation, the status helper, and
    # the Terraform scaffold.  Collapse its diagnostics so malformed YAML can
    # never echo a previously injected secret through an exception chain.
    try:
        _assert_public_source_shape(source, config)
    except (OSError, ValueError):
        raise ValueError(
            "source bundle is not a credential-free combined runtime task"
        ) from None
    return config


def _is_attempt_state(name: str) -> bool:
    """Match state that must not cross from one solver attempt to another."""

    return (
        name in {".terraform", "target"}
        or name == "terraform.tfstate"
        or name.startswith("terraform.tfstate.")
    )


def _ignore_attempt_state(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if _is_attempt_state(name)}


def _make_attempt_tree_private(root: Path) -> None:
    """Remove group and world permissions from an installed attempt tree.

    Preserve owner execution on scripts and owner traversal on directories.
    """

    paths = (root, *root.rglob("*"))
    for path in paths:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):  # source validation already forbids it
            raise ValueError("installed task must not contain symbolic links")
        owner_bits = stat.S_IMODE(status.st_mode) & 0o700
        if stat.S_ISDIR(status.st_mode):
            path.chmod(owner_bits | 0o700)
        elif stat.S_ISREG(status.st_mode):
            path.chmod(owner_bits | 0o600)


def _aliased_text(
    values: Mapping[str, object],
    first: str,
    second: str,
    *,
    credential_kind: str,
) -> str:
    """Resolve one required alias pair without echoing either supplied value."""

    left = values.get(first)
    right = values.get(second)
    supplied = [value for value in (left, right) if value is not None]
    if not supplied:
        raise ValueError(
            f"missing required {credential_kind} credential field: {first}"
        )
    if any(not isinstance(value, str) or not value.strip() for value in supplied):
        raise ValueError(
            f"missing required {credential_kind} credential field: {first}"
        )
    if len(supplied) == 2 and supplied[0] != supplied[1]:
        raise ValueError(
            f"conflicting {credential_kind} credential aliases: {first}/{second}"
        )
    return str(supplied[0])


def _port(values: Mapping[str, object], *, default: int, kind: str) -> int:
    raw = values.get("port", default)
    if isinstance(raw, bool):
        raise ValueError(f"{kind} credential port must be an integer")
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{kind} credential port must be an integer") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{kind} credential port must be between 1 and 65535")
    return port


def _redshift_staging_prefix(value: object) -> str:
    """Require one non-root, attempt-scoped S3 key prefix.

    Airbyte stages Redshift files before COPY. Require the provisioned prefix,
    or an equivalently scoped harness prefix, to keep attempts off the bucket
    root and separate from one another.
    """

    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 900
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(
            "Redshift credential s3_bucket_path must be a safe, non-empty "
            "attempt-scoped prefix"
        )
    return value


def _destination_install_values(
    destination: Destination,
    credentials: Mapping[str, object],
    *,
    logical_namespace: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return Airbyte-config updates and the scoped DB-API credential file."""

    if destination is Destination.SNOWFLAKE:
        values = {
            field: _required_text(credentials, field, credential_kind="Snowflake")
            for field in ("account", "user", "password", "role", "warehouse")
        }
        return (
            {
                "account": values["account"],
                "password": values["password"],
                "role": values["role"],
                "username": values["user"],
                "warehouse": values["warehouse"],
            },
            {
                "account": values["account"],
                "user": values["user"],
                "password": values["password"],
            },
        )

    if destination is Destination.DATABRICKS:
        credential_schema = credentials.get("schema")
        if credential_schema is not None and credential_schema != logical_namespace:
            raise ValueError(
                "Databricks credential schema does not match the public task namespace"
            )
        hostname = _aliased_text(
            credentials,
            "hostname",
            "server_hostname",
            credential_kind="Databricks",
        )
        http_path = _required_text(
            credentials, "http_path", credential_kind="Databricks"
        )
        catalog = _aliased_text(
            credentials, "database", "catalog", credential_kind="Databricks"
        )
        if "access_token" in credentials or "personal_access_token" in credentials:
            raise ValueError(
                "original ELT-Bench Databricks config requires OAuth "
                "client_id and secret credentials"
            )
        oauth_id = _required_text(
            credentials, "client_id", credential_kind="Databricks"
        )
        oauth_secret = _aliased_text(
            credentials,
            "secret",
            "client_secret",
            credential_kind="Databricks",
        )
        return (
            {
                "database": catalog,
                "hostname": hostname,
                "http_path": http_path,
                "client_id": oauth_id,
                "secret": oauth_secret,
            },
            {
                "hostname": hostname,
                "http_path": http_path,
                "client_id": oauth_id,
                "secret": oauth_secret,
            },
        )

    credential_schema = credentials.get("schema")
    if credential_schema is not None and credential_schema != logical_namespace:
        raise ValueError(
            "Redshift credential schema does not match the public task namespace"
        )
    host = _required_text(credentials, "host", credential_kind="Redshift")
    database = _required_text(
        credentials, "database", credential_kind="Redshift"
    )
    username = _aliased_text(
        credentials, "user", "username", credential_kind="Redshift"
    )
    password = _required_text(
        credentials, "password", credential_kind="Redshift"
    )
    port = _port(credentials, default=5439, kind="Redshift")
    bucket = _required_text(
        credentials, "s3_bucket_name", credential_kind="Redshift"
    )
    region = _required_text(
        credentials, "s3_bucket_region", credential_kind="Redshift"
    )
    access_key = _required_text(
        credentials, "access_key_id", credential_kind="Redshift"
    )
    secret_key = _required_text(
        credentials, "secret_access_key", credential_kind="Redshift"
    )
    expected_bucket_path = f"elt-bench/{logical_namespace}"
    supplied_bucket_path = credentials.get("s3_bucket_path")
    if supplied_bucket_path is not None and (
        _redshift_staging_prefix(supplied_bucket_path) != expected_bucket_path
    ):
        raise ValueError(
            "Redshift credential s3_bucket_path must equal the deterministic "
            "ELT-Bench attempt prefix"
        )
    return (
        {
            "access_key_id": access_key,
            "database": database,
            "host": host,
            "password": password,
            "port": port,
            "s3_bucket_name": bucket,
            "s3_bucket_region": region,
            "secret_access_key": secret_key,
            "username": username,
        },
        {
            "database": database,
            "host": host,
            "password": password,
            "port": port,
            "username": username,
        },
    )


def install_task(
    public_task_dir: Path,
    work_dir: Path,
    *,
    airbyte_credentials: Mapping[str, object],
    destination_credentials: Mapping[str, object] | None = None,
    snowflake_credentials: Mapping[str, object] | None = None,
    destination: Destination | str | None = None,
    custom_api_definition_id: str | None = None,
    airbyte_server_url: str | None = None,
) -> Path:
    """Install a public task and inject scoped runtime credentials.

    Airbyte requires a workspace ID and one complete authentication pair. REST
    tasks also accept a custom connector ID. Destination credentials select the
    warehouse; ``snowflake_credentials`` is a compatibility alias. Publish by
    one same-filesystem rename and reject an existing destination.
    """

    source = Path(public_task_dir)
    install_dir = Path(work_dir)
    if _exists(install_dir):
        raise FileExistsError("runtime work directory already exists")

    config = _validate_uninstalled_source(source)
    selected = destination_from_config(config)
    if destination is not None:
        explicit_destination = normalize_destination(destination)
        if explicit_destination is not selected:
            raise ValueError(
                "explicit destination does not match the public task config"
            )
    if destination_credentials is not None and snowflake_credentials is not None:
        raise ValueError(
            "supply destination_credentials or snowflake_credentials, not both"
        )
    if snowflake_credentials is not None:
        if selected is not Destination.SNOWFLAKE:
            raise ValueError(
                "snowflake_credentials cannot install a non-Snowflake task"
            )
        resolved_destination_credentials = snowflake_credentials
    elif destination_credentials is not None:
        resolved_destination_credentials = destination_credentials
    else:
        raise ValueError(
            f"missing required {selected.value} destination credentials"
        )
    contract = destination_contract(selected)
    destination_config = _section_config(config, contract.config_section)
    logical_namespace = destination_config.get(contract.logical_namespace_field)
    if not isinstance(logical_namespace, str) or not logical_namespace:
        raise ValueError(
            f"source config has no {selected.value} logical namespace"
        )
    destination_updates, destination_file_values = _destination_install_values(
        selected,
        resolved_destination_credentials,
        logical_namespace=logical_namespace,
    )
    source_resolved = source.resolve(strict=True)
    destination_resolved = install_dir.resolve(strict=False)
    if (
        source_resolved == destination_resolved
        or source_resolved in destination_resolved.parents
    ):
        raise ValueError("runtime work directory must not be inside the source bundle")

    airbyte_values: dict[str, str] = {}
    airbyte_values["workspace_id"] = _required_text(
        airbyte_credentials, "workspace_id", credential_kind="Airbyte"
    )
    client_id = _optional_text(
        airbyte_credentials.get("client_id"), field="Airbyte client_id"
    )
    client_secret = _optional_text(
        airbyte_credentials.get("client_secret"), field="Airbyte client_secret"
    )
    username = _optional_text(
        airbyte_credentials.get("username") or airbyte_credentials.get("email"),
        field="Airbyte username",
    )
    password = _optional_text(
        airbyte_credentials.get("password"), field="Airbyte password"
    )
    has_client = bool(client_id or client_secret)
    has_basic = bool(username or password)
    if has_client and not (client_id and client_secret):
        raise ValueError("Airbyte client_id and client_secret must be supplied together")
    if has_client:
        airbyte_values["client_id"] = client_id or ""
        airbyte_values["client_secret"] = client_secret or ""
    else:
        if has_basic and not (username and password):
            raise ValueError("Airbyte username and password must be supplied together")
        if not has_basic:
            raise ValueError("missing Airbyte authentication credentials")
        airbyte_values["username"] = username or ""
        airbyte_values["password"] = password or ""
    resolved_server_url = _optional_text(
        airbyte_server_url, field="airbyte_server_url"
    ) or _optional_text(
        airbyte_credentials.get("server_url"), field="Airbyte server_url"
    )
    if resolved_server_url is None:
        configured_server_url = _section_config(config, "Airbyte").get("server_url")
        if configured_server_url != "":
            resolved_server_url = _optional_text(
                configured_server_url, field="source Airbyte server_url"
            )
    if resolved_server_url is None:
        raise ValueError("missing required Airbyte runtime field: server_url")
    has_rest = "custom_api" in config
    api_definition_id: str | None = None
    if has_rest:
        # Upstream reads api_definition_id only for custom_api tasks; preserve
        # that behavior so a non-REST environment may leave this optional
        # credential blank.
        api_definition_id = _custom_api_id(
            airbyte_credentials, custom_api_definition_id
        )
        if api_definition_id is None:
            raise ValueError(
                "missing required Airbyte credential field for REST task: "
                "custom_api_definition_id (upstream api_definition_id)"
            )

    install_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{install_dir.name}.install-", dir=install_dir.parent
        )
    )
    published = False
    try:
        shutil.copytree(
            source,
            stage,
            dirs_exist_ok=True,
            ignore=_ignore_attempt_state,
        )
        _make_attempt_tree_private(stage)

        staged_config_path = stage / "config.yaml"
        # Frozen releases strip write bits from every file. The installed copy
        # is attempt state: credentials must be injectable and solver-owned
        # files under elt/ must be editable without mutating the release.
        os.chmod(staged_config_path, 0o600)
        staged_config = _load_yaml_mapping(
            staged_config_path, label="staged config.yaml"
        )
        staged_airbyte = _section_config(staged_config, "Airbyte")
        staged_airbyte.update(airbyte_values)
        staged_airbyte["server_url"] = resolved_server_url
        if has_rest:
            staged_airbyte["custom_api_definition_id"] = api_definition_id
        staged_destination = _section_config(
            staged_config, contract.config_section
        )
        staged_destination.update(destination_updates)
        staged_config_path.write_text(
            yaml.safe_dump(staged_config, sort_keys=False), encoding="utf-8"
        )
        os.chmod(staged_config_path, 0o600)

        staged_destination_credential = stage / contract.credential_filename
        os.chmod(staged_destination_credential, 0o600)
        staged_destination_credential.write_text(
            json.dumps(destination_file_values, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(staged_destination_credential, 0o600)
        for solver_file in (stage / "elt").rglob("*"):
            if solver_file.is_file():
                os.chmod(
                    solver_file,
                    (stat.S_IMODE(solver_file.stat().st_mode) | 0o600) & 0o700,
                )

        # Re-check immediately before publication.  This protects the stated
        # refusal semantics even if another local actor claimed the path while
        # the staged copy was being built.
        if _exists(install_dir):
            raise FileExistsError("runtime work directory already exists")
        os.rename(stage, install_dir)
        published = True
    finally:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)

    return install_dir

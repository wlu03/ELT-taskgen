"""Emit an atomic, upstream-compatible ELT-Bench task bundle.

Keep answer keys and evaluation data private. Normalize runtime names, join S3
parts before sync, and require serving rules for every configured table.

PATH MAP. A bundle is not laid out the way upstream lays one out; it carries
the same content under its own paths, and ``upstream_layout`` converts. The
correspondence, one row per artifact:

    public/<parent>/config.yaml, data_model.yaml -> elt-bench/snowflake/<db>/
    public/<parent>/schemas/<table>.csv          -> elt-bench/schemas/<db>/<table>.csv
    private/<parent>/answer_key/table.json       -> evaluation/table.json[<db>]
    private/.../evaluation/sql/<mart>.sql        -> evaluation/sql/<db>/<mart>.sql
    private/<parent>/answer_key/gold/<pop>/      -> agent_results/gt_<warehouse>/<db>/

NAMING. ``database_name`` derives the warehouse name from the task id and
bounds it by ``DATABASE_NAME_MAX_LEN``, which is ``S3_BUCKET_MAX_LEN`` less
the ``-bucket`` suffix, so every name a task implies is creatable on S3,
PostgreSQL and MongoDB alike. A longer name is truncated and given a digest of
the full task id, so the emitted name is not recoverable from the task id by
string rules; config.yaml is therefore the AUTHORITATIVE source of the runtime
names, and nothing downstream should re-derive them.

S3 SHAPE. A renderer may split one table into ``part-*.jsonl`` chunks, but the
serving contract is a single object: concatenate every part in lexicographic
order and upload that concatenation as s3://<bucket>/<table>.jsonl, the object
config.yaml declares. Uploading only the first part silently loses rows
whenever a population crosses a chunk boundary.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

import yaml

from elt_taskgen.airbyte_connector_config import (
    AIRBYTE_CONNECTOR_TFVARS_FILENAME,
    build_airbyte_connector_contract,
)
from elt_taskgen.destinations import (
    CUSTOM_API_MANIFEST_VERSION,
    DEFAULT_SYNC_MODE,
    DESTINATION_CONTRACTS,
    SOURCE_CONNECTOR_CONTRACTS,
    Destination,
    config_sync_mode_declarations,
    destination_contract,
    destination_from_config,
    normalize_destination,
    normalize_sync_mode,
)
from elt_taskgen.generation.mart_plan import (
    solver_safe_mart_requirements,
    solver_safe_plan_requirements,
    solver_safe_plan_summary,
)
from elt_taskgen.models import (
    Backend,
    MartSpec,
    TableSpec,
    TaskIR,
    TaskVariant,
    canonical_json,
    readable_json,
    sha256_hex,
    variant_task_id,
)
from elt_taskgen.sql_identifiers import quote_sql_identifier, quote_sql_path

#: The population whose counts/gold feed the upstream-shaped evaluation
#: artifacts. Only its GOLD is hidden; its source data ships.
PRIMARY_POPULATION = "primary"

SCHEMA_CSV_HEADER = ("column_name", "column_description")

#: Airbyte connector settings copied from the pinned upstream configuration.
_AIRBYTE_BASE: dict[str, Any] = {
    "namespace_definition": "destination",
    "password": "",
    "server_url": "http://airbyte-abctl-control-plane:80/api/public/v1/",
    "username": "",
    "workspace_id": "",
}

_RUNTIME_DOCUMENTATION: dict[str, str] = {
    "README.md": """# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.
""",
    "airbyte_Provider.md": """# Airbyte Terraform provider

Use the Airbyte server, username, password, and workspace ID injected into
`config.yaml` by the benchmark installer. Define the provider and connector
resources in `elt/main.tf`; do not hard-code credentials in submitted files.
The task config carries the connector definition IDs used by the original
ELT-Bench Terraform provider contract.
""",
    "connection.md": """# Airbyte connections

Create one Airbyte source for each source section in `config.yaml`, one
destination from the single destination section, and connections selecting
exactly the tables declared for that source. Use `full_refresh_append` and the
destination namespace.
""",
    "source_postgres.md": """# PostgreSQL source

When `postgres` is present in `config.yaml`, create an Airbyte PostgreSQL source
from that block and select exactly its listed tables from the declared schema.
""",
    "source_mongodb_v2.md": """# MongoDB v2 source

When `mongodb` is present, create an Airbyte MongoDB v2 source using its
connection string and database, then select exactly the listed collections.
""",
    "source_custom_api.md": """# Custom API source

When `custom_api` is present, use the custom connector definition ID injected
into the Airbyte config and select exactly the declared streams.
""",
    "source_s3.md": """# S3 source

For every `aws_s3.data` entry, configure the Airbyte S3 source with the given
endpoint, single-object bucket path, credentials, table name, and sync mode.
""",
    "source_file.md": """# File source

For every `flat_files` entry, configure the Airbyte Files source with its HTTP
URL and format. The benchmark file service, not the solver workspace, hosts
the source records.
""",
    "trigger_job.md": """# Trigger and monitor syncs

After `terraform apply`, retrieve each Airbyte connection ID, trigger a sync,
and wait until every job succeeds. `check_job_status.py` can monitor the
connections recorded in `elt/terraform.tfstate`.
""",
    "databricks_authentication.md": """# Databricks authentication

The original ELT-Bench Databricks input uses the `client_id` and `secret`
fields in `databricks.config`. Obtain those values from the benchmark-provided
credential file and do not commit live credentials.
""",
}

_DESTINATION_DOCUMENTATION: dict[Destination, tuple[str, str]] = {
    Destination.SNOWFLAKE: (
        "destination_snowflake.md",
        """# Snowflake destination

Configure Airbyte with the Snowflake database, schema, role, warehouse, user,
password, and account supplied at installation. Raw streams must land in the
task database under `AIRBYTE_SCHEMA`.
""",
    ),
    Destination.DATABRICKS: (
        "destination_databricks.md",
        """# Databricks destination

Configure the Airbyte Databricks Lakehouse destination with the injected SQL
Warehouse hostname, HTTP path, Unity Catalog, and authentication.
The task name in `databricks.config.schema` is the isolated Unity Catalog
schema. Use the human-facing `database`, `hostname`, `client_id`, `secret`, and
`http_path` values supplied by the benchmark installer.
""",
    ),
    Destination.REDSHIFT: (
        "destination_redshift.md",
        """# Amazon Redshift destination

Configure the Airbyte Redshift destination with the injected cluster,
database, user, and flat S3 staging fields. The task name in
`redshift.config.schema` is the isolated schema inside that database. Use it as
the destination namespace; Airbyte writes final stream tables directly there.

With the pinned `airbytehq/airbyte` Terraform provider `0.6.5`, translate the
flat task fields into the provider's discriminated upload-strategy shape:

```hcl
configuration = {
  # database, host, password, port, schema, and username omitted here
  uploading_method = {
    awss3_staging = {
      access_key_id      = local.config.redshift.config.access_key_id
      secret_access_key  = local.config.redshift.config.secret_access_key
      s3_bucket_name     = local.config.redshift.config.s3_bucket_name
      s3_bucket_path     = "elt-bench/${local.config.redshift.config.schema}"
      s3_bucket_region   = local.config.redshift.config.s3_bucket_region
      purge_staging_data = true
    }
  }
}
```

Do not put `method = "S3 Staging"` in the nested provider object. That string
is part of the connector API payload, not the provider `0.6.5` HCL schema.
""",
    ),
}

_TERRAFORM_MAIN = """terraform {
  required_providers {
    airbyte = {
      source  = "airbytehq/airbyte"
      version = "0.6.5"
    }
  }
}
"""

_CHECK_JOB_STATUS = '''"""Monitor Airbyte jobs created by the task Terraform state."""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.error
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


def connection_ids(state_path: Path) -> list[str]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    return sorted(
        str(instance["attributes"]["connection_id"])
        for resource in state.get("resources", [])
        if resource.get("type") == "airbyte_connection"
        for instance in resource.get("instances", [])
    )


def airbyte_api_root(server: str) -> str:
    """Return a normalized Airbyte public-API root.

    A bare host retains the helper's original HTTP shorthand.  Full HTTP(S)
    URLs are used as written, so an installed ``.../api/public/v1/`` URL is
    never prefixed with a second scheme or API path.  An origin-only URL gets
    Airbyte OSS's conventional public-API path.
    """
    value = str(server).strip()
    if not value:
        raise ValueError("Airbyte server URL is required")
    if "://" not in value:
        value = "http://" + value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Airbyte server must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Airbyte credentials must not be embedded in the URL")
    if parsed.query or parsed.fragment:
        raise ValueError("Airbyte server URL must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/api/public/v1"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path + "/", "", ""))


def _basic_headers(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(
        f"{username}:{password}".encode("utf-8")
    ).decode("ascii")
    return {"Authorization": f"Basic {token}", "accept": "application/json"}


def _oauth_headers(api_root: str, client_id: str, client_secret: str) -> dict[str, str]:
    body = json.dumps(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant-type": "client_credentials",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        urljoin(api_root, "applications/token"),
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = urlopen(request, timeout=30)
        with response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ):
        # Airbyte error bodies can echo credentials; never include them here.
        raise RuntimeError("could not obtain an Airbyte access token") from None
    if not isinstance(payload, dict):
        raise RuntimeError("Airbyte token response is not an object")
    token = payload.get("access_token") or payload.get("accessToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Airbyte token response contains no access token")
    return {"Authorization": f"Bearer {token}", "accept": "application/json"}


def _job_payload(url: str, headers: dict[str, str]) -> dict:
    query = urlencode({"limit": 100, "orderBy": "createdAt|DESC"})
    request = Request(url + "?" + query, headers=headers, method="GET")
    try:
        response = urlopen(request, timeout=30)
        with response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ):
        raise RuntimeError("could not query Airbyte jobs") from None
    if not isinstance(payload, dict):
        raise RuntimeError("Airbyte jobs response is not an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--state", type=Path, default=Path("elt/terraform.tfstate"))
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=3600.0)
    args = parser.parse_args()
    basic_supplied = args.username is not None or args.password is not None
    oauth_supplied = args.client_id is not None or args.client_secret is not None
    if basic_supplied and oauth_supplied:
        parser.error("choose Basic or OAuth Airbyte authentication, not both")
    if basic_supplied and (not args.username or not args.password):
        parser.error("--username and --password must be supplied together")
    if oauth_supplied and (not args.client_id or not args.client_secret):
        parser.error("--client-id and --client-secret must be supplied together")
    if not basic_supplied and not oauth_supplied:
        parser.error(
            "supply --username/--password or --client-id/--client-secret"
        )
    wanted = set(connection_ids(args.state))
    if not wanted:
        raise SystemExit("no Airbyte connections found in Terraform state")
    try:
        api_root = airbyte_api_root(args.server)
    except ValueError as exc:
        parser.error(str(exc))
    url = urljoin(api_root, "jobs")
    deadline = time.monotonic() + args.timeout
    while True:
        if oauth_supplied:
            headers = _oauth_headers(api_root, args.client_id, args.client_secret)
        else:
            headers = _basic_headers(args.username, args.password)
        statuses = {}
        for job in _job_payload(url, headers).get("data", []):
            connection_id = str(job.get("connectionId"))
            if connection_id in wanted and connection_id not in statuses:
                statuses[connection_id] = str(job.get("status", "")).lower()
        failed = sorted(
            cid
            for cid, status in statuses.items()
            if status in {"failed", "cancelled", "canceled", "incomplete"}
        )
        if failed:
            print("failed:", ", ".join(failed))
            return 1
        if wanted and all(statuses.get(cid) == "succeeded" for cid in wanted):
            print("succeeded:", ", ".join(sorted(wanted)))
            return 0
        if time.monotonic() >= deadline:
            pending = sorted(cid for cid in wanted if statuses.get(cid) != "succeeded")
            print("timed out:", ", ".join(pending))
            return 1
        time.sleep(max(0.0, args.poll_interval))


if __name__ == "__main__":
    raise SystemExit(main())
'''

_SNOWFLAKE_CREDENTIAL_TEMPLATE = {
    "account": "",
    "user": "",
    "password": "",
}

_DATABRICKS_CREDENTIAL_TEMPLATE = {
    "hostname": "",
    "http_path": "",
    "client_id": "",
    "secret": "",
}

_REDSHIFT_CREDENTIAL_TEMPLATE = {
    "database": "",
    "host": "",
    "password": "",
    "port": 5439,
    "username": "",
}

_DESTINATION_CREDENTIAL_TEMPLATES: dict[Destination, dict[str, Any]] = {
    Destination.SNOWFLAKE: _SNOWFLAKE_CREDENTIAL_TEMPLATE,
    Destination.DATABRICKS: _DATABRICKS_CREDENTIAL_TEMPLATE,
    Destination.REDSHIFT: _REDSHIFT_CREDENTIAL_TEMPLATE,
}


def _runtime_documentation(destination: Destination | str) -> dict[str, str]:
    # Upstream copies one shared documentation directory into every solver
    # input, including references for all three warehouses. Keep the argument
    # for API compatibility while validating it as before.
    normalize_destination(destination)
    destination_docs = {
        filename: content
        for filename, content in _DESTINATION_DOCUMENTATION.values()
    }
    return _RUNTIME_DOCUMENTATION | destination_docs


def runtime_documentation_filenames(
    destination: Destination | str = Destination.SNOWFLAKE,
) -> tuple[str, ...]:
    """Return the shared original ELT-Bench documentation filename set."""

    return tuple(sorted(_runtime_documentation(destination)))


# The original installer copies this same shared set for every destination.
RUNTIME_DOCUMENTATION_FILENAMES = runtime_documentation_filenames()


def assert_public_runtime_shape(public_dir: Path) -> None:
    """Require the installable, credential-free original ELT-Bench shape."""
    try:
        config = yaml.safe_load(
            (public_dir / "config.yaml").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError("public runtime contract: config.yaml is invalid") from exc
    if not isinstance(config, dict):
        raise ValueError("public runtime contract: config.yaml is invalid")
    try:
        selected = destination_from_config(config)
    except ValueError as exc:
        raise ValueError(f"public runtime contract: {exc}") from None
    contract = destination_contract(selected)

    required_files = (
        "config.yaml",
        "data_model.yaml",
        "check_job_status.py",
        contract.credential_filename,
        "elt/main.tf",
    )
    missing = [name for name in required_files if not (public_dir / name).is_file()]
    if missing:
        raise ValueError(f"public runtime contract: missing files {missing}")
    forbidden_files = (
        "documentation.md",
        f"elt/{AIRBYTE_CONNECTOR_TFVARS_FILENAME}",
    )
    present_forbidden = [
        name for name in forbidden_files if (public_dir / name).exists()
    ]
    if present_forbidden:
        raise ValueError(
            "public runtime contract: non-upstream solver files are present "
            f"{present_forbidden}"
        )
    unexpected_credentials = [
        item.credential_filename
        for item in DESTINATION_CONTRACTS.values()
        if item.destination is not selected
        and (public_dir / item.credential_filename).exists()
    ]
    if unexpected_credentials:
        raise ValueError(
            "public runtime contract: credentials for unselected destinations "
            f"are present {sorted(unexpected_credentials)}"
        )
    schemas = public_dir / "schemas"
    if not schemas.is_dir() or not any(schemas.glob("*.csv")):
        raise ValueError("public runtime contract: schemas/ has no CSV declarations")
    documentation = public_dir / "documentation"
    expected_docs = set(_runtime_documentation(selected))
    actual_docs = {
        path.name for path in documentation.iterdir() if path.is_file()
    } if documentation.is_dir() else set()
    if actual_docs != expected_docs:
        missing_docs = sorted(expected_docs - actual_docs)
        extra_docs = sorted(actual_docs - expected_docs)
        raise ValueError(
            "public runtime contract: documentation/ does not match the "
            f"original shared set (missing {missing_docs}; extra {extra_docs})"
        )
    try:
        terraform_main = (public_dir / "elt" / "main.tf").read_text(
            encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - covered by required_files
        raise ValueError("public runtime contract: elt/main.tf is invalid") from exc
    if terraform_main != _TERRAFORM_MAIN:
        raise ValueError(
            "public runtime contract: elt/main.tf must contain only the "
            "airbytehq/airbyte 0.6.5 provider requirement"
        )
    try:
        credential = json.loads(
            (public_dir / contract.credential_filename).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"public runtime contract: {contract.credential_filename} is invalid"
        ) from exc
    if credential != _DESTINATION_CREDENTIAL_TEMPLATES[selected]:
        raise ValueError(
            f"public runtime contract: {contract.credential_filename} must "
            "contain only the destination's credential placeholders; inject "
            "secrets at installation time"
        )
    airbyte = (
        (config.get("Airbyte") or {}).get("config")
    )
    if not isinstance(airbyte, dict):
        raise ValueError("public runtime contract: Airbyte.config is invalid")
    required_airbyte = set(_AIRBYTE_BASE) | {contract.definition_key}
    if "custom_api" in config:
        required_airbyte.add("custom_api_definition_id")
    for source_contract in SOURCE_CONNECTOR_CONTRACTS.values():
        if source_contract.config_section in config:
            required_airbyte.add(source_contract.definition_key)

    known_destination_keys = {
        item.definition_key for item in DESTINATION_CONTRACTS.values()
    }
    present_destination_keys = known_destination_keys & set(airbyte)
    if present_destination_keys != {contract.definition_key}:
        raise ValueError(
            "public runtime contract: Airbyte.config must contain exactly the "
            f"{selected.value} destination definition id"
        )
    if set(airbyte) != required_airbyte:
        missing = sorted(required_airbyte - set(airbyte))
        extra = sorted(set(airbyte) - required_airbyte)
        raise ValueError(
            "public runtime contract: config.yaml Airbyte fields do not match "
            f"the original ELT-Bench shape (missing {missing}; extra {extra})"
        )
    populated = [
        key
        for key in ("password", "username", "workspace_id")
        if airbyte.get(key)
    ]
    if populated:
        raise ValueError(
            "public runtime contract: Airbyte credentials/workspace placeholders "
            f"must be empty before release ({populated}); inject them at "
            "installation time"
        )
    if airbyte.get("namespace_definition") != "destination":
        raise ValueError(
            "public runtime contract: Airbyte namespace_definition must be "
            "'destination'"
        )
    if airbyte.get("server_url") != _AIRBYTE_BASE["server_url"]:
        raise ValueError(
            "public runtime contract: Airbyte server_url must match the "
            "original ELT-Bench abctl endpoint"
        )
    destination_id = airbyte.get(contract.definition_key)
    if destination_id != contract.definition_id:
        raise ValueError(
            "public runtime contract: Airbyte destination definition id does "
            "not match the selected destination"
        )
    for source_contract in SOURCE_CONNECTOR_CONTRACTS.values():
        section_present = source_contract.config_section in config
        key_present = source_contract.definition_key in airbyte
        if section_present != key_present:
            raise ValueError(
                "public runtime contract: source section, Airbyte source "
                "definition key disagree for "
                f"{source_contract.config_section}"
            )
        if key_present and airbyte.get(source_contract.definition_key) != (
            source_contract.definition_id
        ):
            raise ValueError(
                "public runtime contract: Airbyte source definition id does "
                f"not match {source_contract.config_section}"
            )
    for path, declared_mode in config_sync_mode_declarations(config):
        try:
            normalize_sync_mode(declared_mode)
        except ValueError:
            raise ValueError(
                f"public runtime contract: {path} declares unsupported sync "
                f"mode {declared_mode!r}"
            ) from None

    destination_config = (config.get(contract.config_section) or {}).get("config")
    if not isinstance(destination_config, dict):
        raise ValueError(
            f"public runtime contract: config.yaml has no {selected.value}.config"
        )
    if selected is Destination.SNOWFLAKE:
        required = {
            "account", "database", "password", "role", "schema", "username",
            "warehouse",
        }
        secret_fields = ("account", "password", "role", "username", "warehouse")
        valid_shape = (
            required == set(destination_config)
            and isinstance(destination_config.get("database"), str)
            and bool(destination_config.get("database"))
            and destination_config.get("schema") == "AIRBYTE_SCHEMA"
        )
    elif selected is Destination.DATABRICKS:
        required = {
            "client_id", "database", "hostname", "http_path", "schema",
            "secret",
        }
        valid_shape = (
            required == set(destination_config)
            and isinstance(destination_config.get("schema"), str)
            and bool(destination_config.get("schema"))
        )
        secret_fields = ("client_id", "database", "hostname", "http_path", "secret")
    else:
        required = {
            "access_key_id", "database", "host", "password", "port", "schema",
            "s3_bucket_name", "s3_bucket_region", "secret_access_key", "username",
        }
        valid_shape = (
            required == set(destination_config)
            and isinstance(destination_config.get("schema"), str)
            and bool(destination_config.get("schema"))
            and destination_config.get("port") == 5439
        )
        secret_fields = (
            "access_key_id", "database", "host", "password", "s3_bucket_name",
            "s3_bucket_region", "secret_access_key", "username",
        )
    if not valid_shape:
        missing = sorted(required - set(destination_config))
        raise ValueError(
            f"public runtime contract: config.yaml {selected.value} placeholders "
            f"have an invalid shape (missing {missing})"
        )
    logical_namespace = destination_config.get(contract.logical_namespace_field)
    try:
        assert_source_identifiers(logical_namespace)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"public runtime contract: invalid {selected.value} logical "
            f"namespace source identifier {logical_namespace!r}"
        ) from exc
    populated_destination = [
        field for field in secret_fields if destination_config.get(field)
    ]
    if populated_destination:
        raise ValueError(
            f"public runtime contract: {selected.value} runtime placeholders "
            f"must be empty before release ({populated_destination})"
        )
    if "custom_api" in config:
        if "custom_api_definition_id" not in airbyte:
            raise ValueError(
                "public runtime contract: REST task has no "
                "custom_api_definition_id placeholder"
            )
        if airbyte.get("custom_api_definition_id"):
            raise ValueError(
                "public runtime contract: custom_api_definition_id must be "
                "injected at installation time"
            )
    elif "custom_api_definition_id" in airbyte:
        raise ValueError(
            "public runtime contract: custom_api_definition_id is present "
            "without a custom_api source section"
        )

#: S3 bucket names are 3..63 characters (AWS; both LocalStack providers
#: enforce it verbatim and REFUSE CreateBucket outside it).
S3_BUCKET_MAX_LEN = 63

#: PostgreSQL identifiers are limited to 63 bytes and longer names truncate.
POSTGRES_IDENTIFIER_MAX_BYTES = 63

#: MongoDB database names must be under 64 characters.
MONGODB_DATABASE_MAX_LEN = 63

#: Suffix `bucket_name` appends to the database name.
_BUCKET_SUFFIX = "-bucket"

#: Leave space for ``-bucket`` while satisfying all database name limits.
DATABASE_NAME_MAX_LEN = S3_BUCKET_MAX_LEN - len(_BUCKET_SUFFIX)

#: Hex characters of sha256(task_id) appended when a name must be shortened
#: (same shape as models.slugify_family).
_DB_DIGEST_LEN = 12

#: S3 bucket grammar this exporter emits (a subset of AWS's rules: lowercase
#: alphanumerics and hyphens, not starting or ending with a hyphen).
_S3_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]")

#: Database/schema grammar this exporter emits (Snowflake/DuckDB-safe).
_DATABASE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_]*")

#: Environment variable that overrides the flat-file serving base URL at
#: export time (deployment configuration, not task content).
FLAT_FILES_BASE_URL_ENV = "ELT_TASKGEN_FLAT_FILES_BASE_URL"

#: Deterministic default: a static file server on the harness docker network,
#: serving the active population's rendered `files/` directory at /<db>/.
DEFAULT_FLAT_FILES_BASE_URL = "http://elt-files:8080"

#: Environment variable that overrides the REST (custom_api) serving base URL.
REST_BASE_URL_ENV = "ELT_TASKGEN_REST_BASE_URL"

#: Upstream REST URL used by the pinned Docker configurations.
DEFAULT_REST_BASE_URL = "http://elt-api:5005"

#: Private answer-key manifest telling the harness which rendered artifact to
#: serve at which emitted flat-file URL.
FLAT_FILES_SERVING_MANIFEST = "flat_files_serving.json"

#: Private source-service manifest for every backend.
SOURCES_SERVING_MANIFEST = "sources_serving.json"

#: Private connector data used only by the runtime harness.
PRIVATE_AIRBYTE_CONNECTOR_CONTRACT = (
    "runtime/airbyte_connector_contract.json"
)

#: Private Airbyte DeclarativeSource manifest for the REST tables, shaped like
#: upstream's setup/elt_snowflake.yaml (emitted only when the task uses REST).
AIRBYTE_CUSTOM_API_MANIFEST = "airbyte_custom_api_manifest.yaml"

#: Private per-variant evaluator and answer-key manifest.
REWARD_MANIFEST = "reward.json"

#: Transform bundle directory containing source-only starting warehouses.
WAREHOUSE_DIRNAME = "warehouse"

#: Rendered EL source root relative to the parent task directory.
EL_SOURCES_REL_TEMPLATE = "populations/{pop}/rendered"

#: Solver-facing objective text file in the EXTRACT_LOAD variant bundle
#: (the EL bundle ships no data_model.yaml, so the objective must be stated).
EL_DOCUMENTATION_FILENAME = "documentation.md"

#: Recorded warehouse-census evidence, relative to tasks/<task_id>/. Keep in
#: sync with gates.WAREHOUSE_CENSUS_EVIDENCE_REL, which consumes it.
WAREHOUSE_CENSUS_EVIDENCE_REL = "reports/warehouse_census.json"
WAREHOUSE_CENSUS_KIND = "warehouse-census"

#: Census algorithm version. Verification compares only reproducible versions;
#: v2 adds typed columns, distinct NULLs, and the catalog census.
CENSUS_VERSION = "2"


def resolve_flat_files_base_url(base_url: str | None = None) -> str:
    """Resolve and validate the flat-file serving base URL.

    Precedence: explicit argument > ELT_TASKGEN_FLAT_FILES_BASE_URL env var >
    DEFAULT_FLAT_FILES_BASE_URL. Fail closed on anything the Airbyte Files
    connector could not fetch: the base must be absolute http(s) with a host
    and no whitespace (upstream uses https URLs for every flat_files entry).
    """
    resolved = base_url if base_url is not None else os.environ.get(
        FLAT_FILES_BASE_URL_ENV, DEFAULT_FLAT_FILES_BASE_URL
    )
    resolved = resolved.rstrip("/")
    if not re.fullmatch(r"https?://[^\s/]+(?:/[^\s]*)?", resolved):
        raise ValueError(
            "flat-file base URL must be an absolute http(s) URL the Airbyte "
            f"Files connector can fetch, got {resolved!r} (set "
            f"{FLAT_FILES_BASE_URL_ENV} or pass flat_files_base_url)"
        )
    return resolved


def flat_files_url(db: str, table: str, fmt: str, *, base_url: str | None = None) -> str:
    """Fetchable URL for one flat-file table: <base>/<db>/<table>.<format>."""
    return f"{resolve_flat_files_base_url(base_url)}/{db}/{table}.{fmt}"


def resolve_rest_base_url(base_url: str | None = None) -> str:
    """Resolve and validate the REST (custom_api) serving base URL.

    Same precedence and fail-closed rule as the flat-file base; the default is
    upstream's own `url_base`, so both name the same host. Upstream's
    config.yaml carries NO url for `custom_api`, so this base never enters it —
    only the private serving manifest and the Airbyte manifest.
    """
    resolved = base_url if base_url is not None else os.environ.get(
        REST_BASE_URL_ENV, DEFAULT_REST_BASE_URL
    )
    resolved = resolved.rstrip("/")
    if not re.fullmatch(r"https?://[^\s/]+(?:/[^\s]*)?", resolved):
        raise ValueError(
            "REST base URL must be an absolute http(s) URL the Airbyte "
            f"custom-api connector can fetch, got {resolved!r} (set "
            f"{REST_BASE_URL_ENV} or pass rest_base_url)"
        )
    return resolved


@runtime_checkable
class GoldLike(Protocol):
    """Structural view of reference.gold.GoldBundle (owned by another builder)."""

    task_id: str
    task_content_hash: str
    stage1: Mapping[str, Mapping[str, int]]        # population -> table -> count
    stage2_csv: Mapping[str, Mapping[str, str]]    # population -> mart -> CSV text


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def database_name(task: TaskIR) -> str:
    """Upstream-style database/schema name derived from the task id.

    Names use lowercase ``[a-z0-9_]`` and are bounded by
    ``DATABASE_NAME_MAX_LEN``. Names within the limit remain unchanged. Longer
    names receive a digest of the full task ID to avoid prefix collisions.
    """
    name = re.sub(r"[^a-z0-9_]", "_", task.task_id.lower()).strip("_")
    if not name:
        raise ValueError(f"task_id {task.task_id!r} yields an empty database name")
    if len(name) > DATABASE_NAME_MAX_LEN:
        digest = sha256_hex(task.task_id)[:_DB_DIGEST_LEN]
        head = name[: DATABASE_NAME_MAX_LEN - _DB_DIGEST_LEN - 1].rstrip("_")
        name = f"{head}_{digest}"
    assert_source_identifiers(name)
    return name


def bucket_name(db: str) -> str:
    """S3 bucket name in the anchor style (`tiktok-ads-bucket`).

    Fails closed on anything provisioning could not create. Consecutive hyphens
    are legal and do occur, from `__` in a task id.
    """
    bucket = db.replace("_", "-") + _BUCKET_SUFFIX
    if len(bucket) > S3_BUCKET_MAX_LEN or not _S3_BUCKET_RE.fullmatch(bucket):
        raise ValueError(
            f"S3 bucket name {bucket!r} ({len(bucket)} chars) derived from "
            f"database {db!r} is not creatable: buckets must match "
            f"{_S3_BUCKET_RE.pattern} and be at most {S3_BUCKET_MAX_LEN} "
            "characters (AWS and LocalStack both refuse CreateBucket otherwise)"
        )
    return bucket


def assert_source_identifiers(db: str) -> None:
    """Fail closed unless `db` (and the bucket derived from it) is provisionable.

    Re-checked inside `build_config` so a config.yaml from pre-fix code cannot
    be frozen as legal. Not a reward gate: the answer key never reads the name.
    """
    if not _DATABASE_NAME_RE.fullmatch(db):
        raise ValueError(
            f"source identifier {db!r} is not a legal database name: expected "
            f"{_DATABASE_NAME_RE.pattern}"
        )
    limit = min(
        POSTGRES_IDENTIFIER_MAX_BYTES, MONGODB_DATABASE_MAX_LEN, DATABASE_NAME_MAX_LEN
    )
    size = len(db.encode("utf-8"))
    if size > limit:
        raise ValueError(
            f"source identifier {db!r} is {size} bytes, over the {limit}-byte "
            "bound: PostgreSQL truncates at 63 bytes (aliasing sibling tasks "
            "onto one database), MongoDB refuses 64+, and the derived S3 "
            f"bucket would exceed {S3_BUCKET_MAX_LEN} characters"
        )
    bucket_name(db)  # raises if the derived bucket is not creatable


# ---------------------------------------------------------------------------
# Public bundle pieces
# ---------------------------------------------------------------------------

def build_config(
    task: TaskIR,
    *,
    flat_files_base_url: str | None = None,
    destination: Destination | str = Destination.SNOWFLAKE,
    sync_mode: str = DEFAULT_SYNC_MODE,
) -> dict[str, Any]:
    """Build an upstream-compatible ``config.yaml`` mapping.

    Include only used source connectors, Airbyte, and one destination. Validate
    ``sync_mode`` and apply it to each connector.
    """
    selected = normalize_destination(destination)
    contract = destination_contract(selected)
    mode = normalize_sync_mode(sync_mode)
    db = database_name(task)
    # Defence in depth: this function writes the identifier into every
    # connector stanza, so it re-checks rather than trusting its caller.
    assert_source_identifiers(db)
    by_backend: dict[Backend, list[str]] = {}
    for assignment in task.backends:
        by_backend.setdefault(assignment.backend, []).append(assignment.table)
    for tables in by_backend.values():
        tables.sort()

    if selected is Destination.SNOWFLAKE:
        destination_block: dict[str, Any] = {
            "config": {
                "account": "",
                "database": db,
                "password": "",
                "role": "",
                "schema": "AIRBYTE_SCHEMA",
                "username": "",
                "warehouse": "",
            }
        }
    elif selected is Destination.DATABRICKS:
        destination_block = {
            "config": {
                "client_id": "",
                "database": "",
                "hostname": "",
                "http_path": "",
                "schema": db,
                "secret": "",
            }
        }
    else:
        destination_block = {
            "config": {
                "access_key_id": "",
                "database": "",
                "host": "",
                "password": "",
                "port": 5439,
                "schema": db,
                "s3_bucket_name": "",
                "s3_bucket_region": "",
                "secret_access_key": "",
                "username": "",
            }
        }

    config: dict[str, Any] = {contract.config_section: destination_block}

    if Backend.POSTGRES in by_backend:
        config["postgres"] = {
            "config": {
                "database": db,
                "host": "elt-postgres",
                "password": "testelt",
                "port": 5432,
                "schema": "public",
                "sync_mode": mode,
                "tables": by_backend[Backend.POSTGRES],
                "user": "postgres",
            }
        }
    if Backend.MONGODB in by_backend:
        config["mongodb"] = {
            "config": {
                "connection_string": "mongodb://elt-mongodb:27017/?directConnection=true",
                "database": db,
                "sync_mode": mode,
                "tables": by_backend[Backend.MONGODB],
            }
        }
    if Backend.REST in by_backend:
        config["custom_api"] = {
            "config": {
                "configuration": {},
                "sync_mode": mode,
                "tables": by_backend[Backend.REST],
            }
        }
    if Backend.S3 in by_backend:
        config["aws_s3"] = {
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_ENDPOINT_URL": "http://elt-localstack:4566",
            "AWS_SECRET_ACCESS_KEY": "test",
            "data": [
                {
                    # Preserve the original ELT-Bench single-object contract.
                    # The private serving manifest tells the harness to
                    # concatenate all rendered chunks into this key.
                    "path": f"s3://{bucket_name(db)}/{table}.jsonl",
                    "sync_mode": mode,
                    "table": table,
                }
                for table in by_backend[Backend.S3]
            ],
        }
    if Backend.FILES in by_backend:
        entries = []
        for table in by_backend[Backend.FILES]:
            fmt = task.backend_for(table).options.get("format", "csv")
            entries.append(
                {
                    "format": fmt,
                    "path": flat_files_url(db, table, fmt, base_url=flat_files_base_url),
                    "sync_mode": mode,
                    "table": table,
                }
            )
        config["flat_files"] = entries

    # Airbyte stanza last: base keys plus a definition id for exactly the
    # source connectors this task's config carries (upstream convention).
    airbyte: dict[str, Any] = dict(_AIRBYTE_BASE)
    airbyte[contract.definition_key] = contract.definition_id
    for source_contract in SOURCE_CONNECTOR_CONTRACTS.values():
        if source_contract.config_section in config:
            airbyte[source_contract.definition_key] = (
                source_contract.definition_id
            )
    if Backend.REST in by_backend:
        # Environment-specific: bootstrap publishes the generated declarative
        # source and injects its definition id during installation.
        airbyte["custom_api_definition_id"] = ""
    config["Airbyte"] = {"config": airbyte}
    return config


def solver_visible_plan(task: TaskIR, mart: MartSpec) -> dict[str, object]:
    """Return the public declarative transform contract.

    Include source inputs, output fields, joins, conditions, and semantic
    parameters. Exclude compiler details, SQL, gold rows, and result values.
    """

    return solver_safe_plan_requirements(task, mart)


def build_data_model(task: TaskIR) -> dict[str, Any]:
    """Build one ``data_model.yaml`` entry per mart.

    Include the grain, key columns, types, and transformation rules used during
    calibration.
    """
    return {
        "models": [
            solver_safe_mart_requirements(task, mart) for mart in task.marts
        ]
    }


#: Documentation filename used by split EL/T task-unit exports. The combined
#: original-shaped bundle appends this specification to documentation/README.md.
DOCUMENTATION_FILENAME = "documentation.md"


def _source_schema_markdown(task: TaskIR) -> list[str]:
    """Source tables with backend, typed/nullable columns, keys and relations.

    `schemas/<table>.csv` is pinned to the upstream two-column header and must
    not grow columns, so the typed view lives here instead.
    """
    lines = ["## Source tables", ""]
    for table in task.tables:
        backend = task.backend_for(table.name).backend.value
        lines.append(f"### {table.name}  (source backend: {backend})")
        if table.description:
            lines.append(table.description)
        lines.append("")
        for col in table.columns:
            null = "NULL" if col.nullable else "NOT NULL"
            enum = (
                f" one of: {', '.join(col.enum_values)}." if col.enum_values else ""
            )
            desc = f" {col.description}" if col.description else ""
            lines.append(f"- `{col.name}`: {col.type.value} {null} —{desc}{enum}")
        if table.primary_key:
            lines.append(f"- primary key: {', '.join(table.primary_key)}")
        if table.business_key:
            lines.append(f"- business key: {', '.join(table.business_key)}")
        lines.append("")
    if task.relationships:
        lines += ["### Relationships", ""]
        for rel in task.relationships:
            opt = "required" if rel.required else "optional (may be NULL/dangling)"
            lines.append(
                f"- {rel.child_table}({', '.join(rel.child_columns)}) -> "
                f"{rel.parent_table}({', '.join(rel.parent_columns)}) [{opt}]"
            )
        lines.append("")
    return lines


def _transformation_specification_markdown(task: TaskIR) -> list[str]:
    """Plan-derived public specification shared by combined and T exports."""
    lines = [
        "## Transformation specification",
        "",
        "Build every mart below using the ordered semantic rules. The rules",
        "define required source inputs, matching behavior, filters, aggregates,",
        "null handling, and deterministic tie behavior; they do not prescribe",
        "a particular SQL implementation.",
        "",
    ]
    for mart in task.marts:
        lines += [
            f"### `{mart.name}`",
            "",
            f"- Grain: {mart.grain}",
            f"- Unique key: {', '.join(mart.key_columns)}",
            "- Required columns: " + ", ".join(col.name for col in mart.columns),
            "",
            "```text",
            solver_safe_plan_summary(task, mart),
            "```",
            "",
        ]
    return lines


def public_documentation(task: TaskIR) -> str:
    """Build the public task specification.

    Retain authored text and add a plan-derived section without predicates,
    expressions, aliases, SQL, or gold output.
    """
    lines = [f"# {task.title or task.task_id}", ""]
    if task.solver_prompt.strip():
        lines += ["## Specification", "", task.solver_prompt.strip(), ""]
    lines += _transformation_specification_markdown(task)
    lines += _source_schema_markdown(task)
    return "\n".join(lines) + "\n"


def public_column_description(col) -> str:
    """Build a public column description with enum and nullability rules.

    The reviewer and exported schema must use the same text.
    """
    base = col.description.strip().rstrip(".")
    parts = [base]
    if col.enum_values:
        parts.append(
            "always exactly one of "
            + ", ".join(f"'{v}'" for v in col.enum_values)
        )
    # An IR description that already states nullability ("...; may be NULL")
    # must not get the guarantee appended a second time.
    if col.nullable and "may be null" not in base.lower():
        parts.append("may be NULL")
    return ". ".join(parts) + "."


def schema_csv(table: TableSpec) -> str:
    """schemas/<table>.csv text with the exact upstream header."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(SCHEMA_CSV_HEADER)
    for col in table.columns:
        writer.writerow([col.name, public_column_description(col)])
    return buf.getvalue()


# Private evaluation pieces.

def _ordered_columns(mart: MartSpec) -> list[str]:
    """Key columns first, then every remaining column in declared order."""
    ordered = list(mart.key_columns)
    ordered.extend(c.name for c in mart.columns if c.name not in set(mart.key_columns))
    return ordered


def evaluation_sql(mart: MartSpec, *, database: str | None = None) -> str:
    """Return a per-mart evaluation query with a total row order.

    Unlike the upstream query, it orders by every column so CSV comparison is
    deterministic when sort keys tie. Keywords remain lowercase to match the
    pinned files.
    """
    qualified = (
        quote_sql_path((database, mart.name), dialect="snowflake")
        if database
        else quote_sql_identifier(mart.name, dialect="snowflake")
    )
    order_by = ", ".join(
        quote_sql_identifier(column, dialect="snowflake")
        for column in _ordered_columns(mart)
    )
    return f"select * from {qualified} order by {order_by};\n"


# Gold binding + leak guard.

def _pop_key(key: Any) -> str:
    return str(getattr(key, "value", key))


def _normalize_pops(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    return {_pop_key(k): v for k, v in mapping.items()}


def _check_gold_binding(task: TaskIR, gold: GoldLike) -> None:
    if gold.task_id != task.task_id:
        raise ValueError(
            f"gold bundle is for task {gold.task_id!r}, not {task.task_id!r}"
        )
    current = task.content_hash()
    if gold.task_content_hash != current:
        raise ValueError(
            "gold bundle is stale: frozen against content hash "
            f"{gold.task_content_hash} but task is now {current}"
        )


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).lower()


def _leak_markers(task: TaskIR) -> dict[str, str]:
    """Marker name -> normalized text that must never appear publicly."""
    markers: dict[str, str] = {"answer_key-path": "answer_key"}
    if task.reference is not None:
        for mart_name, sql in sorted(task.reference.sql_by_mart.items()):
            normalized = _normalize_text(sql)
            if normalized:
                markers[f"reference-sql:{mart_name}"] = normalized
    for pop in task.populations:
        markers[f"population-seed:{_pop_key(pop.name)}"] = str(pop.seed)
    return markers


def _duckdb_table_names(path: Path) -> set[str]:
    """Relation names inside one DuckDB file (read-only; fail closed).

    Schema-qualified for anything outside `main`, so a relation hiding in
    another schema is visible to callers that only look at names.
    """
    import duckdb  # local import: only warehouse-bearing trees pay for it

    con = duckdb.connect(str(path), read_only=True)
    try:
        return {_census_key(schema, name) for schema, name, _t in _relations(con)}
    finally:
        con.close()


#: Catalog groups a source-only warehouse must not contain at all. Constraints
#: are deliberately absent (create_table emits NOT NULL, so the census pins
#: them); adding PRIMARY KEY there would need an allowance here.
_FORBIDDEN_CATALOG_GROUPS: tuple[str, ...] = (
    "macros",
    "views",
    "sequences",
    "types",
    "indexes",
    "schemas",
    "table_comments",
    "column_comments",
)


def _assert_warehouse_clean(task: TaskIR, path: Path) -> None:
    """Require a public warehouse to contain only declared source tables.

    Check relation names, catalog text, and source column shapes.
    """
    import duckdb  # local import: only warehouse-bearing trees pay for it

    from elt_taskgen.reference.solution import duckdb_type

    allowed = {t.name.lower(): t for t in task.tables}
    mart_names = {m.name.lower() for m in task.marts}
    forbidden = {"gold", "gt", "answer_key", "table", "sort_key"}

    con = duckdb.connect(str(path), read_only=True)
    try:
        relations = _relations(con)
        for schema, name, table_type in relations:
            key = _census_key(schema, name)
            lowered = name.strip().lower()
            if lowered in mart_names:
                raise ValueError(
                    f"leak: mart table {key!r} present in public warehouse {path}"
                )
            if lowered in forbidden:
                raise ValueError(
                    f"leak: forbidden relation {key!r} in public warehouse {path}"
                )
            if schema != "main" or table_type != "BASE TABLE" or lowered not in allowed:
                raise ValueError(
                    f"leak: unexpected relation {key!r} ({table_type}) in public "
                    f"warehouse {path} (source BASE TABLEs in `main` only)"
                )
            spec = allowed[lowered]
            want = [
                [c.name, duckdb_type(c.type), "YES" if c.nullable else "NO"]
                for c in spec.columns
            ]
            got = _relation_columns(con, schema, name)
            if got != want:
                raise ValueError(
                    f"shape: relation {key!r} in public warehouse {path} does not "
                    f"match the declared source table (declared {want}, found "
                    f"{got}) — a retyped or reordered column changes what a "
                    "correct query returns"
                )
        catalog = _catalog_census(con)
    finally:
        con.close()

    # Marker scan FIRST, so a macro that literally carries the reference SQL
    # is reported as that ("leak: marker 'reference-sql:<mart>' ...") rather
    # than as an anonymous extra catalog object.
    markers = _leak_markers(task)
    for group, rows in sorted(catalog.items()):
        text = _normalize_text(" ".join(" ".join(row) for row in rows))
        if not text:
            continue
        for name, marker in markers.items():
            if marker and marker in text:
                raise ValueError(
                    f"leak: marker {name!r} found in the {group} catalog of "
                    f"public warehouse {path}"
                )
    for group in _FORBIDDEN_CATALOG_GROUPS:
        rows = catalog.get(group) or []
        if rows:
            raise ValueError(
                f"leak: {len(rows)} {group} object(s) in public warehouse {path} "
                f"(first: {rows[0]}) — a source-only warehouse carries source "
                "tables and nothing else; a macro/view/comment can carry the "
                "answer without moving a single row"
            )


def assert_public_tree_clean(task: TaskIR, public_dir: Path) -> None:
    """Fail closed if any private marker leaks into the public tree."""
    markers = _leak_markers(task)
    forbidden_names = {"table.json", "sort_key.json", "answer_key", "gt", "gold", REWARD_MANIFEST}
    for path in sorted(public_dir.rglob("*")):
        if path.name in forbidden_names:
            raise ValueError(f"leak: forbidden name {path.name!r} in public tree: {path}")
        if not path.is_file():
            continue
        if path.suffix == ".duckdb":
            # Binary warehouse: the meaningful leak surface is its CATALOG
            # (relations, their column shape, macros/views/comments), not a
            # byte-scan of storage pages.
            _assert_warehouse_clean(task, path)
            continue
        content = _normalize_text(path.read_bytes().decode("utf-8", errors="ignore"))
        for name, marker in markers.items():
            if marker and marker in content:
                raise ValueError(f"leak: marker {name!r} found in public file {path}")


_FORBIDDEN_PUBLIC_RUNTIME_TOKENS = (
    "load_plan",
    "sql_by_mart",
    "standalone duckdb",
)


def assert_public_runtime_tree_clean(task: TaskIR, public_dir: Path) -> None:
    """Enforce the solver-facing Airbyte/Snowflake/dbt release boundary.

    Private curation variants may contain source-only DuckDB warehouses. A
    public end-to-end task may not contain DuckDB or legacy JSON/query
    submission terms.
    """
    for path in sorted(public_dir.rglob("*")):
        rel = path.relative_to(public_dir).as_posix().lower()
        if path.is_file() and path.suffix.lower() == ".duckdb":
            raise ValueError(
                f"public runtime contract: DuckDB artifact is forbidden: {path}"
            )
        for token in _FORBIDDEN_PUBLIC_RUNTIME_TOKENS[:2]:
            if token in rel:
                raise ValueError(
                    f"public runtime contract: forbidden legacy token {token!r} "
                    f"in path {path}"
                )
        if not path.is_file():
            continue
        content = _normalize_text(
            path.read_bytes().decode("utf-8", errors="ignore")
        )
        for token in _FORBIDDEN_PUBLIC_RUNTIME_TOKENS:
            if token in content:
                raise ValueError(
                    f"public runtime contract: forbidden legacy token {token!r} "
                    f"in file {path}"
                )
    # Task-specific answer-key/reference markers are checked after the generic
    # runtime ban so even a corrupt or non-openable file named *.duckdb is
    # rejected for crossing the public boundary, not parsed as an oracle.
    assert_public_tree_clean(task, public_dir)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _dump_yaml(data: Any) -> str:
    """Canonical YAML: sorted mapping keys, block style, stable line width."""
    return yaml.safe_dump(
        data, sort_keys=True, default_flow_style=False, allow_unicode=True, width=1000
    )


def _write_runtime_scaffold(
    public_dir: Path,
    task: TaskIR,
    destination: Destination | str = Destination.SNOWFLAKE,
) -> None:
    """Write the credential-free original ELT-Bench runtime entrypoints."""
    selected = normalize_destination(destination)
    contract = destination_contract(selected)
    documentation = public_dir / "documentation"
    documentation.mkdir()
    for name, content in sorted(_runtime_documentation(selected).items()):
        if name == "README.md":
            content = content.rstrip() + "\n\n" + public_documentation(task)
        (documentation / name).write_text(content, encoding="utf-8")
    (public_dir / "check_job_status.py").write_text(
        _CHECK_JOB_STATUS, encoding="utf-8"
    )
    (public_dir / contract.credential_filename).write_text(
        json.dumps(
            _DESTINATION_CREDENTIAL_TEMPLATES[selected], indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    elt_dir = public_dir / "elt"
    elt_dir.mkdir()
    (elt_dir / "main.tf").write_text(_TERRAFORM_MAIN, encoding="utf-8")


def _install_tree(stage_dir: Path, final_dir: Path) -> None:
    """Merge staged entries into a tree that may carry other private gold."""
    final_dir.mkdir(parents=True, exist_ok=True)
    for child in sorted(stage_dir.iterdir()):
        dest = final_dir / child.name
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        elif dest.exists() or dest.is_symlink():
            dest.unlink()
        os.replace(child, dest)


def _replace_tree(stage_dir: Path, final_dir: Path) -> None:
    """Replace one generated bundle exactly, without stale public artifacts."""
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    backup = stage_dir.parent / f".{final_dir.name}-previous"
    had_previous = final_dir.exists() or final_dir.is_symlink()
    if had_previous:
        os.replace(final_dir, backup)
    try:
        os.replace(stage_dir, final_dir)
    except BaseException:
        if had_previous and backup.exists():
            os.replace(backup, final_dir)
        raise
    if had_previous:
        if backup.is_dir() and not backup.is_symlink():
            shutil.rmtree(backup)
        elif backup.exists() or backup.is_symlink():
            backup.unlink()


def _flat_files_serving_manifest(
    task: TaskIR, db: str, base_url: str
) -> dict[str, Any] | None:
    """Serving manifest for flat-file tables (None when the task has none).

    Maps every URL emitted into config.yaml to the rendered artifact the
    harness must serve there, relative to one population's `rendered/` dir
    (render_files always writes files/<table>.csv).
    """
    file_tables = sorted(
        a.table for a in task.backends if a.backend is Backend.FILES
    )
    if not file_tables:
        return None
    tables: dict[str, Any] = {}
    for table in file_tables:
        fmt = task.backend_for(table).options.get("format", "csv")
        tables[table] = {
            "format": fmt,
            "rendered_file": f"files/{table}.csv",
            "url": flat_files_url(db, table, fmt, base_url=base_url),
        }
    return {
        "base_url": base_url,
        "base_url_env": FLAT_FILES_BASE_URL_ENV,
        "rendered_root_template": EL_SOURCES_REL_TEMPLATE,
        "note": (
            "Serve each population's rendered_file (relative to "
            "populations/<population>/rendered/; in a release that root is "
            "private/<parent_task_id>/populations/<population>/rendered/) at "
            "its url before the Airbyte Files connector runs; config.yaml "
            "flat_files paths resolve here."
        ),
        "tables": tables,
    }


def _sources_serving_manifest(
    task: TaskIR, db: str, *, flat_base: str, rest_base: str
) -> dict[str, Any]:
    """Describe the private source artifact and serving rule for each table."""
    tables: dict[str, Any] = {}
    for assignment in sorted(task.backends, key=lambda a: a.table):
        table = assignment.table
        backend = assignment.backend
        if backend is Backend.POSTGRES:
            tables[table] = {
                "backend": backend.value,
                "database": db,
                "host": "elt-postgres",
                "port": 5432,
                "schema": "public",
                "rendered_file": f"{backend.value}/{table}.sql",
                "load": (
                    "psql -f the rendered file (it DROPs, CREATEs and batch "
                    "INSERTs the table itself)"
                ),
            }
        elif backend is Backend.MONGODB:
            tables[table] = {
                "backend": backend.value,
                "collection": table,
                "connection_string": (
                    "mongodb://elt-mongodb:27017/?directConnection=true"
                ),
                "database": db,
                "rendered_file": f"{backend.value}/{table}.jsonl",
                "load": "mongoimport the rendered file (one JSON document per line)",
            }
        elif backend is Backend.S3:
            tables[table] = {
                "backend": backend.value,
                "bucket": bucket_name(db),
                "endpoint": "http://elt-localstack:4566",
                "object_key": f"{table}.jsonl",
                "rendered_parts_glob": "part-*.jsonl",
                "rendered_dir": f"{backend.value}/{table}/",
                "rule": (
                    "concatenate EVERY rendered part in lexicographic order, "
                    "then upload that concatenation as the single object "
                    "s3://<bucket>/<table>.jsonl declared by config.yaml; "
                    "uploading only part-00000 loses rows when a population "
                    "crosses a renderer chunk boundary"
                ),
            }
        elif backend is Backend.REST:
            tables[table] = {
                "backend": backend.value,
                "base_url": rest_base,
                "base_url_env": REST_BASE_URL_ENV,
                "page_files": "index.json lists the page files in order",
                "records_key": "data",
                "rendered_dir": f"{backend.value}/{table}/",
                "response_shape": "bare_array",
                "route": f"/{db}/{table}",
                "rule": (
                    "serve the concatenation of every page file's 'data' array "
                    "as ONE top-level JSON list (upstream's rest_api app "
                    "jsonify(list)s, and the Airbyte DpathExtractor for these "
                    "streams has field_path: [], so a paged wrapper would "
                    "extract nothing)"
                ),
            }
        elif backend is Backend.FILES:
            fmt = task.backend_for(table).options.get("format", "csv")
            tables[table] = {
                "backend": backend.value,
                "format": fmt,
                "rendered_file": f"{backend.value}/{table}.{fmt}",
                "url": flat_files_url(db, table, fmt, base_url=flat_base),
            }
        else:  # pragma: no cover - Backend is a closed enum
            raise ValueError(f"unknown backend {backend!r} for table {table!r}")
    return {
        "base_urls": {"flat_files": flat_base, "rest": rest_base},
        "base_url_envs": {
            "flat_files": FLAT_FILES_BASE_URL_ENV,
            "rest": REST_BASE_URL_ENV,
        },
        "database": db,
        "note": (
            "Every rendered_file/rendered_dir is relative to ONE population's "
            f"rendered/ root ({EL_SOURCES_REL_TEMPLATE} under the parent task "
            "directory). Stand the active population up per entry, then run "
            "the solver against config.yaml."
        ),
        "rendered_root_template": EL_SOURCES_REL_TEMPLATE,
        "tables": tables,
    }


def _config_tables_by_section(config: Mapping[str, Any]) -> dict[str, list[str]]:
    """Section name -> tables it declares, for every connector stanza shape."""
    found: dict[str, list[str]] = {}
    for section in ("postgres", "mongodb", "custom_api"):
        block = config.get(section)
        if isinstance(block, dict):
            tables = (block.get("config") or {}).get("tables") or []
            found[section] = [str(t) for t in tables]
    s3 = config.get("aws_s3")
    if isinstance(s3, dict):
        found["aws_s3"] = [str(e.get("table")) for e in (s3.get("data") or [])]
    files = config.get("flat_files")
    if isinstance(files, list):
        found["flat_files"] = [str(e.get("table")) for e in files]
    return found


#: config.yaml section -> the Backend it stands for.
_SECTION_BACKENDS: dict[str, Backend] = {
    "postgres": Backend.POSTGRES,
    "mongodb": Backend.MONGODB,
    "custom_api": Backend.REST,
    "aws_s3": Backend.S3,
    "flat_files": Backend.FILES,
}


def assert_serving_manifest_complete(
    task: TaskIR,
    config: Mapping[str, Any],
    manifest: Mapping[str, Any],
    rendered_dirs: Any = (),
) -> None:
    """Fail closed unless the serving manifest covers the emitted config.

    CODE certifying the serving story: config tables and manifest entries must
    correspond exactly, each entry's backend must equal the task's assignment,
    and every named rendered artifact must EXIST under each supplied root.
    """
    entries = manifest.get("tables")
    if not isinstance(entries, dict):
        raise ValueError("serving manifest has no 'tables' map (fail closed)")
    declared: dict[str, str] = {}
    for section, tables in _config_tables_by_section(config).items():
        for table in tables:
            declared[table] = section
    missing = sorted(set(declared) - set(entries))
    if missing:
        raise ValueError(
            f"serving manifest does not cover config table(s) {missing}: a "
            "connector stanza a harness cannot stand up is an unserveable "
            "source"
        )
    extra = sorted(set(entries) - set(declared))
    if extra:
        raise ValueError(
            f"serving manifest names table(s) {extra} that no connector stanza "
            "declares"
        )
    for table in sorted(entries):
        entry = entries[table]
        expected = task.backend_for(table).backend
        if str(entry.get("backend")) != expected.value:
            raise ValueError(
                f"serving manifest entry for {table!r} claims backend "
                f"{entry.get('backend')!r}, task assigns {expected.value!r}"
            )
        section_backend = _SECTION_BACKENDS.get(declared[table])
        if section_backend is not expected:
            raise ValueError(
                f"config declares {table!r} under section {declared[table]!r} "
                f"but the task assigns backend {expected.value!r}"
            )
        rel = entry.get("rendered_file") or entry.get("rendered_dir")
        if not rel:
            raise ValueError(
                f"serving manifest entry for {table!r} names no rendered artifact"
            )
        for root in rendered_dirs:
            path = Path(root) / str(rel).rstrip("/")
            if not path.exists():
                raise ValueError(
                    f"serving manifest names {rel!r} for table {table!r} but "
                    f"no such rendered artifact exists under {root}"
                )


#: Verbatim upstream (ELT-Bench/setup/elt_snowflake.yaml) DeclarativeSource
#: version and paginator page size — a taskgen REST stream must be parseable
#: by an unmodified Airbyte install of the same connector.
_AIRBYTE_MANIFEST_VERSION = CUSTOM_API_MANIFEST_VERSION
_AIRBYTE_PAGE_SIZE = 9973

#: TaskIR column type -> JSON-schema type in the emitted InlineSchemaLoader.
_AIRBYTE_JSON_TYPES: dict[str, str] = {
    "integer": "integer",
    "bigint": "integer",
    "float": "number",
    "decimal": "number",
    "boolean": "boolean",
}


def build_airbyte_custom_api_manifest(
    task: TaskIR, *, rest_base_url: str | None = None
) -> dict[str, Any]:
    """DeclarativeSource manifest for this task's REST tables.

    Shaped exactly like the pinned upstream manifest so the SAME bundle runs on
    an unmodified Airbyte install; note `field_path: []`, i.e. the response body
    IS the record list. Private/harness-side, as upstream keeps it.
    """
    db = database_name(task)
    base = resolve_rest_base_url(rest_base_url)
    rest_tables = sorted(
        a.table for a in task.backends if a.backend is Backend.REST
    )
    streams: dict[str, Any] = {}
    for table in rest_tables:
        spec = task.table(table)
        properties = {
            col.name: {
                "type": [
                    "null",
                    _AIRBYTE_JSON_TYPES.get(col.type.value, "string"),
                ]
            }
            for col in spec.columns
        }
        streams[table] = {
            # Key set held to upstream's own stanza (no `primary_key`: the
            # pinned manifest declares none, and an unmodified Airbyte install
            # of that connector version is what must parse this).
            "type": "DeclarativeStream",
            "name": table,
            "retriever": {
                "type": "SimpleRetriever",
                "requester": {
                    "$ref": "#/definitions/base_requester",
                    "http_method": "GET",
                    "path": f"/{db}/{table}",
                },
                "record_selector": {
                    "type": "RecordSelector",
                    "extractor": {"type": "DpathExtractor", "field_path": []},
                },
                "paginator": {
                    "type": "DefaultPaginator",
                    "page_token_option": {
                        "type": "RequestOption",
                        "field_name": "offset",
                        "inject_into": "request_parameter",
                    },
                    "page_size_option": {
                        "type": "RequestOption",
                        "field_name": "limit",
                        "inject_into": "request_parameter",
                    },
                    "pagination_strategy": {
                        "type": "OffsetIncrement",
                        "page_size": _AIRBYTE_PAGE_SIZE,
                    },
                },
            },
            "schema_loader": {
                "type": "InlineSchemaLoader",
                "schema": {
                    "$schema": "http://json-schema.org/schema#",
                    "additionalProperties": True,
                    "properties": properties,
                    "type": "object",
                },
            },
        }
    return {
        "version": CUSTOM_API_MANIFEST_VERSION,
        "type": "DeclarativeSource",
        "check": {"type": "CheckStream", "stream_names": rest_tables[:1]},
        "definitions": {
            "base_requester": {"type": "HttpRequester", "url_base": base},
            "streams": streams,
        },
        "streams": [
            {"$ref": f"#/definitions/streams/{table}"} for table in rest_tables
        ],
        "spec": {
            "type": "Spec",
            "connection_specification": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": True,
                "properties": {},
                "required": [],
            },
        },
    }


#: Extra destination configurations; the root retains one primary destination.
EXTRA_DESTINATIONS_DIRNAME = "destinations"


def write_extra_destinations(
    public_dir: Path,
    task: TaskIR,
    extra_destinations,
    *,
    flat_files_base_url: str | None = None,
    sync_mode: str = DEFAULT_SYNC_MODE,
) -> dict[str, dict[str, Any]]:
    """Write each extra destination configuration and credential template."""
    written: dict[str, dict[str, Any]] = {}
    for item in extra_destinations or ():
        selected = normalize_destination(item)
        contract = destination_contract(selected)
        target = public_dir / EXTRA_DESTINATIONS_DIRNAME / selected.value
        target.mkdir(parents=True, exist_ok=True)
        config = build_config(
            task,
            flat_files_base_url=flat_files_base_url,
            destination=selected,
            sync_mode=sync_mode,
        )
        (target / "config.yaml").write_text(_dump_yaml(config))
        (target / contract.credential_filename).write_text(
            json.dumps(
                _DESTINATION_CREDENTIAL_TEMPLATES[selected], indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        (target / "README.md").write_text(
            f"# {selected.value} destination\n\n"
            "This directory carries the same task with the Airbyte destination "
            f"block for {selected.value}: use this `config.yaml` and credential "
            "template in place of the ones at the bundle root. The source "
            "connectors, schemas, data model and specification are identical.\n",
            encoding="utf-8",
        )
        written[selected.value] = config
    return written


def export_task(
    task: TaskIR,
    gold: GoldLike,
    task_dir: Path,
    answer_key_dir: Path,
    *,
    flat_files_base_url: str | None = None,
    rest_base_url: str | None = None,
    destination: Destination | str = Destination.SNOWFLAKE,
    extra_destinations=(),
) -> None:
    """Write public task and private evaluation artifacts.

    Gold must bind to the exact TaskIR hash and contain stage-1 counts and
    stage-2 gold for every mart. Output directories must be disjoint. Resolve
    serving bases once so public config and serving manifests agree.
    """
    selected_destination = normalize_destination(destination)
    task_dir = task_dir.resolve()
    answer_key_dir = answer_key_dir.resolve()
    if task_dir == answer_key_dir or task_dir in answer_key_dir.parents or (
        answer_key_dir in task_dir.parents
    ):
        raise ValueError("task_dir and answer_key_dir must be disjoint trees")

    _check_gold_binding(task, gold)
    stage1 = _normalize_pops(gold.stage1)
    stage2 = _normalize_pops(gold.stage2_csv)
    if PRIMARY_POPULATION not in stage1:
        raise ValueError("gold bundle has no primary-population stage-1 counts")
    primary_counts = dict(stage1[PRIMARY_POPULATION])
    missing_counts = [t.name for t in task.tables if t.name not in primary_counts]
    if missing_counts:
        raise ValueError(f"primary stage-1 counts missing tables: {missing_counts}")
    primary_gold = dict(stage2.get(PRIMARY_POPULATION, {}))
    missing_marts = [m.name for m in task.marts if m.name not in primary_gold]
    if missing_marts:
        raise ValueError(f"primary stage-2 gold missing marts: {missing_marts}")

    db = database_name(task)
    resolved_base = resolve_flat_files_base_url(flat_files_base_url)
    resolved_rest_base = resolve_rest_base_url(rest_base_url)
    task_dir.parent.mkdir(parents=True, exist_ok=True)
    answer_key_dir.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=task_dir.parent, prefix=".export-stage-") as tmp:
        stage_public = Path(tmp) / "task"
        stage_private = Path(tmp) / "answer_key"
        stage_public.mkdir()
        stage_private.mkdir()

        # --- public tree ---------------------------------------------------
        config = build_config(
            task,
            flat_files_base_url=resolved_base,
            destination=selected_destination,
        )
        (stage_public / "config.yaml").write_text(_dump_yaml(config))
        (stage_public / "data_model.yaml").write_text(_dump_yaml(build_data_model(task)))
        _write_runtime_scaffold(stage_public, task, selected_destination)
        extra_configs = write_extra_destinations(
            stage_public,
            task,
            [d for d in extra_destinations if normalize_destination(d) is not selected_destination],
            flat_files_base_url=resolved_base,
        )
        schemas_dir = stage_public / "schemas"
        schemas_dir.mkdir()
        for table in task.tables:
            (schemas_dir / f"{table.name}.csv").write_text(schema_csv(table))
        # Source records remain private and initialize benchmark services.
        rendered_dev = task_dir.parent / "populations" / "development" / "rendered"
        assert_public_runtime_shape(stage_public)
        assert_public_runtime_tree_clean(task, stage_public)

        # --- private tree --------------------------------------------------
        (stage_private / "table.json").write_text(
            canonical_json({db: {t: primary_counts[t] for t in sorted(primary_counts)}}) + "\n"
        )
        (stage_private / "sort_key.json").write_text(
            canonical_json({db: {m.name: list(m.key_columns) for m in task.marts}}) + "\n"
        )
        private_runtime = stage_private / "runtime"
        private_runtime.mkdir()
        (stage_private / PRIVATE_AIRBYTE_CONNECTOR_CONTRACT).write_text(
            canonical_json(build_airbyte_connector_contract(config)) + "\n"
        )
        for name, extra_config in sorted(extra_configs.items()):
            stem, suffix = os.path.splitext(PRIVATE_AIRBYTE_CONNECTOR_CONTRACT)
            (stage_private / f"{stem}.{name}{suffix}").write_text(
                canonical_json(build_airbyte_connector_contract(extra_config)) + "\n"
            )
        serving = _flat_files_serving_manifest(task, db, resolved_base)
        if serving is not None:
            (stage_private / FLAT_FILES_SERVING_MANIFEST).write_text(
                canonical_json(serving) + "\n"
            )
        sources_serving = _sources_serving_manifest(
            task, db, flat_base=resolved_base, rest_base=resolved_rest_base
        )
        # Every artifact the manifest names must EXIST in the rendered root
        # this export can see: a contract naming a missing artifact is a
        # promise nobody can keep.
        assert_serving_manifest_complete(
            task,
            config,
            sources_serving,
            [rendered_dev] if rendered_dev.is_dir() else [],
        )
        (stage_private / SOURCES_SERVING_MANIFEST).write_text(
            canonical_json(sources_serving) + "\n"
        )
        if any(a.backend is Backend.REST for a in task.backends):
            (stage_private / AIRBYTE_CUSTOM_API_MANIFEST).write_text(
                _dump_yaml(
                    build_airbyte_custom_api_manifest(
                        task, rest_base_url=resolved_rest_base
                    )
                )
            )
        sql_dir = stage_private / "evaluation" / "sql"
        sql_dir.mkdir(parents=True)
        gt_dir = stage_private / "gt"
        gt_dir.mkdir()
        for mart in task.marts:
            (sql_dir / f"{mart.name}.sql").write_text(evaluation_sql(mart, database=db))
            (gt_dir / f"{mart.name}.csv").write_text(primary_gold[mart.name])

        # Preserve validate-t's canonical runtime artifacts. Their record binds
        # the connector digest, so changed contracts invalidate stale evidence.
        canonical_existing = answer_key_dir / "runtime" / "canonical"
        staged_runtime = stage_private / "runtime"
        if (
            canonical_existing.is_dir()
            and not canonical_existing.is_symlink()
            and staged_runtime.is_dir()
            and not (staged_runtime / "canonical").exists()
        ):
            shutil.copytree(canonical_existing, staged_runtime / "canonical", symlinks=False)
        _replace_tree(stage_public, task_dir)
        _install_tree(stage_private, answer_key_dir)


# Export EL and T units with private manifests over the same frozen artifacts.
# Both use the existing ``compare_stage1`` and ``compare_mart`` implementations.

def _stage1_answer_key_paths(populations: list[str]) -> dict[str, str]:
    """population -> answer-key path of its frozen stage-1 counts.

    Paths are relative to the parent task's directory (workspace layout:
    tasks/<parent_id>/answer_key/...; in a release, private/<parent_id>/
    replaces answer_key/).
    """
    return {
        pop: f"answer_key/gold/{pop}/{'stage1_counts.json'}" for pop in populations
    }


def _stage2_answer_key_paths(
    task: TaskIR, populations: list[str]
) -> dict[str, dict[str, str]]:
    """population -> mart -> answer-key path of its frozen gold CSV."""
    return {
        pop: {m.name: f"answer_key/gold/{pop}/{m.name}.csv" for m in task.marts}
        for pop in populations
    }


def _stage1_source_paths(populations: list[str]) -> dict[str, str]:
    """population -> the rendered EL source root the reward is computed over.

    Same base as the answer-key paths, so one `path_base` covers both. The
    public bundle ships only development; every graded population's root lives
    here, shipped under private/<parent>/ by export/release.py.
    """
    return {pop: EL_SOURCES_REL_TEMPLATE.format(pop=pop) for pop in populations}


def reward_manifest(
    task: TaskIR,
    gold: GoldLike,
    variant: TaskVariant,
    *,
    population_relations: Mapping[str, str] | None = None,
    memorization_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a private reward manifest for one validated task variant.

    Include the evaluator, frozen inputs, and only verified population
    relations. Paths are relative to the parent task directory.
    """
    variant = TaskVariant(variant)
    populations = sorted(_normalize_pops(gold.stage1))
    base: dict[str, Any] = {
        "variant": variant.value,
        "task_id": variant_task_id(task.task_id, variant),
        "parent_task_id": task.task_id,
        "family_id": task.family_id,
        "path_base": (
            "parent task directory: answer_key/ and populations/ resolve under "
            "tasks/<parent_task_id>/ in a workspace and under "
            "private/<parent_task_id>/ in a release; task/ resolves under "
            "variants/<variant>/ (workspace) or public/<task_id>/ (release)"
        ),
        # The reward is over ALL graded populations, all-or-nothing: a
        # submission that is right on primary and wrong on stress is wrong.
        "populations_required": "all",
    }
    if population_relations:
        base["population_relations"] = dict(sorted(population_relations.items()))
        if memorization_evidence:
            base["memorization_evidence"] = dict(memorization_evidence)
    if variant is TaskVariant.EXTRACT_LOAD:
        return base | {
            "evaluator": "elt_taskgen.verification.upstream_eval.compare_stage1",
            "mode": "strict_binary",
            "expected": _stage1_answer_key_paths(populations),
            "sources": _stage1_source_paths(populations),
            "load_plan_root": "sources[<population>]",
            "serving": f"answer_key/{SOURCES_SERVING_MANIFEST}",
            "per_table_detail": True,
        }
    if variant is TaskVariant.TRANSFORM:
        return base | {
            "evaluator": "elt_taskgen.verification.upstream_eval.compare_mart",
            "mode": "mart_fraction",
            "gold": _stage2_answer_key_paths(task, populations),
            # Relative to this UNIT's own public bundle, not the parent task
            # dir: the warehouses ship inside the T bundle, and a path under
            # neither declared base resolves nowhere.
            "warehouse": {
                pop: f"{WAREHOUSE_DIRNAME}/{pop}.duckdb" for pop in populations
            },
            "warehouse_base": (
                "public bundle directory of this task unit "
                "(variants/transform/task/ in a workspace, public/<task_id>/ "
                "in a release)"
            ),
        }
    return base | {
        "evaluator": "elt_taskgen.verification.upstream_eval.evaluate",
        "mode": "composite",
        "composite": ["stage1_gate", "mart_fraction"],
        "expected": _stage1_answer_key_paths(populations),
        "gold": _stage2_answer_key_paths(task, populations),
    }


def population_relations(task: TaskIR, populations_dir: Path) -> dict[str, str]:
    """Population relations a CODE check can PROVE from the materialized rows.

    Read from the rendered row bytes, so a task cannot buy the claim with
    prose. Empty when the relation is false OR unprovable — a manifest must
    never claim a relation on evidence it could not read.
    """
    from elt_taskgen.verification import gates as gates_mod

    populations_dir = Path(populations_dir)
    task_root = populations_dir.parent
    workspace = task_root.parent.parent
    if (
        populations_dir.name != "populations"
        or task_root.name != task.task_id
        or task_root.parent.name != "tasks"
    ):
        return {}
    names = {_pop_key(p.name) for p in task.populations}
    if not {"primary", "resampled"} <= names:
        return {}
    if not gates_mod.pair_is_rearrangement(task, workspace, "primary", "resampled"):
        return {}
    return {"resampled": gates_mod.POPULATION_RELATION_REARRANGEMENT}


def memorization_evidence(task_root: Path) -> dict[str, Any]:
    """The recorded value-bijection probe, as a reward.json annotation.

    Under a rearrangement-only `resampled` split nothing SHIPPED proves the
    reward reads source values, so the manifest points at the certification-time
    record rather than implying the split does it.
    """
    from elt_taskgen.verification import perturbation as perturbation_mod

    rel = perturbation_mod.PERTURBATION_PROBE_EVIDENCE_REL
    path = Path(task_root) / rel
    if not path.is_file():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        "kind": "value-bijection",
        "path": rel,
        "passed": bool(record.get("passed")),
    }


def el_documentation(task: TaskIR) -> str:
    """Deterministic solver-facing objective text for the EXTRACT_LOAD variant.

    The EL bundle ships NO data_model.yaml, so the objective must be stated
    explicitly. Ends with a trailing newline: it is written straight to a .md
    file, and a file without a final newline is not a POSIX text file.
    """
    by_backend: dict[str, list[str]] = {}
    for assignment in task.backends:
        by_backend.setdefault(assignment.backend.value, []).append(assignment.table)
    lines = [
        f"# Extract & Load task: {variant_task_id(task.task_id, TaskVariant.EXTRACT_LOAD)}",
        "",
        "Implement ONLY the Extract + Load stage of this ELT project.",
        "",
        "For every source system declared in config.yaml, extract each listed",
        "table and load it into the destination warehouse using exactly the",
        "table names given in schemas/ (one warehouse row per source record;",
        "column names as documented in each schemas/<table>.csv).",
        "",
        "Building marts / transformed models is NOT part of this task.",
        "",
        "## Source tables by backend",
        "",
    ]
    for backend in sorted(by_backend):
        tables = ", ".join(sorted(by_backend[backend]))
        lines.append(f"- {backend}: {tables}")
    lines += [
        "",
        "## Grading",
        "",
        "Strict binary: the submission scores 1.0 only if EVERY source table",
        "exists in the warehouse with exactly the expected row count, else 0.0.",
        "",
    ]
    # Match the specification and schema used during calibration.
    if task.solver_prompt.strip():
        lines += ["## Project specification", "", task.solver_prompt.strip(), ""]
    lines += _source_schema_markdown(task)
    return "\n".join(lines) + "\n"


#: Transform scoring text, duplicated in ``corpus/calibration.py`` and tested.
T_SCORING_CONTRACT: tuple[str, ...] = (
    "Your reward is the fraction of target marts whose output matches the",
    "frozen expected output. A mart is all-or-nothing: same number of",
    "rows, every declared column present, every value equal. Row order",
    "does not matter — both sides are sorted into a total order before",
    "comparison. Column names are matched case-insensitively, numbers",
    "compare within a small relative tolerance, text compares after",
    "trimming and case folding, and a NULL facing a value is a mismatch.",
)

#: Solver objective file for the transform variant.
T_DOCUMENTATION_FILENAME = DOCUMENTATION_FILENAME


def t_documentation(task: TaskIR, populations: Any = ()) -> str:
    """Build the transform objective used in export and calibration.

    ``populations`` names the warehouse files included in the bundle.
    """
    pops = sorted(str(getattr(p, "value", p)) for p in populations)
    marts = ", ".join(f"`{m.name}`" for m in task.marts)
    lines = [
        f"# Transform task: {variant_task_id(task.task_id, TaskVariant.TRANSFORM)}",
        "",
        "## Scope",
        "",
        "Implement ONLY the transform stage. The starting warehouse is",
        f"PROVIDED: `{WAREHOUSE_DIRNAME}/<population>.duckdb`, one DuckDB",
        "database file per population"
        + (f" ({', '.join(pops)})" if pops else "")
        + ", each already loaded with",
        "exactly the source tables listed in `schemas/` under those exact",
        "table names in the default schema `main`. Extraction and loading are",
        "done and are NOT scored.",
        "",
        f"Build every mart declared in `data_model.yaml` ({marts}) with the",
        "columns, types and grain it declares.",
        "",
        "## Submission",
        "",
        "One complete standalone DuckDB SELECT statement per mart, keyed by",
        "the exact mart name. Each statement must produce exactly that mart's",
        "declared columns under those exact column names, reading the source",
        "tables under their exact names. Do not create tables, and do not emit",
        "more than one statement per mart. The same statement is executed",
        "against EVERY population's warehouse, so it must not depend on values",
        "specific to one of them.",
        "",
        "## Grading",
        "",
        *T_SCORING_CONTRACT,
        "",
        "The reward is computed over every graded population and is",
        "all-or-nothing across them.",
        "",
    ]
    if task.solver_prompt.strip():
        lines += ["## Project specification", "", task.solver_prompt.strip(), ""]
    lines += _transformation_specification_markdown(task)
    lines += _source_schema_markdown(task)
    return "\n".join(lines) + "\n"


def materialize_warehouse(
    task: TaskIR,
    gold: GoldLike,
    population: str,
    rendered_dir: Path,
    db_path: Path,
) -> None:
    """Build a starting warehouse from frozen stage-1 gold.

    Run trusted extract/load over frozen rendered artifacts. Require matching
    row counts and exactly the task's source tables.
    """
    import duckdb  # local: keep module import light
    from elt_taskgen.reference.solution import load_sources_duckdb

    expected = dict(_normalize_pops(gold.stage1).get(population) or {})
    if not expected:
        raise ValueError(
            f"no frozen stage-1 gold counts for population {population!r}"
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    try:
        loaded = load_sources_duckdb(task, rendered_dir, con)
        if loaded.counts != expected:
            raise ValueError(
                f"population {population!r}: warehouse counts diverge from "
                f"frozen gold stage-1 (loaded {loaded.counts}, gold {expected})"
            )
        con.execute("CHECKPOINT")
    except BaseException:
        con.close()
        db_path.unlink(missing_ok=True)
        raise
    con.close()
    _assert_warehouse_clean(task, db_path)


# The content-bound warehouse census is gate evidence because export is not a
# gate and DuckDB bytes are unstable. Pin relation counts, typed row-multiset
# digests, and the catalog digest to catch SQL payload, type, and NULL changes.

def _census_cell(value: Any) -> str | None:
    """One cell as canonical text, or None for SQL NULL.

    Text is the only representation stable across two independent builds. NULL
    is JSON null, never '': starting-state identity is not reward equivalence,
    and IS NULL / COALESCE / COUNT read the two differently.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def _quote_ident(name: str) -> str:
    return quote_sql_identifier(str(name), dialect="duckdb", force=True)


def _relations(con: Any) -> list[tuple[str, str, str]]:
    """(schema, name, table_type) for every relation in THIS database.

    Schema-qualified and catalog-scoped: reading `table_name` alone leaves a
    relation hidden in another schema invisible to the leak guard.
    """
    rows = con.execute(
        "select table_schema, table_name, table_type from information_schema.tables "
        "where table_catalog = current_database() "
        "order by table_schema, table_name"
    ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]


def _census_key(schema: str, name: str) -> str:
    """Census key: the bare name in `main`, else `schema.name`."""
    return name if schema == "main" else f"{schema}.{name}"


def _relation_columns(con: Any, schema: str, name: str) -> list[list[str]]:
    """[[column_name, data_type, is_nullable], ...] in ordinal order."""
    rows = con.execute(
        "select column_name, data_type, is_nullable from information_schema.columns "
        "where table_schema = ? and table_name = ? order by ordinal_position",
        [schema, name],
    ).fetchall()
    return [[str(r[0]), str(r[1]), str(r[2])] for r in rows]


def _relation_census(con: Any, schema: str, name: str) -> dict[str, Any]:
    """{row_count, row_digest} for one relation, order-independent."""
    columns = _relation_columns(con, schema, name)
    quoted = ", ".join(_quote_ident(c[0]) for c in columns) or "*"
    rows = con.execute(
        f"SELECT {quoted} FROM {_quote_ident(schema)}.{_quote_ident(name)}"
    ).fetchall()
    row_hashes = sorted(
        sha256_hex(canonical_json([_census_cell(v) for v in row])) for row in rows
    )
    return {
        "row_count": len(rows),
        "row_digest": sha256_hex(
            canonical_json({"columns": columns, "rows": row_hashes})
        ),
    }


#: Catalog group -> the query enumerating it (this database only, internal
#: objects excluded where the view distinguishes them). Every row is a list of
#: strings so the census JSON is canonical and diffable.
_CATALOG_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "macros",
        "select schema_name, function_name, function_type, return_type, "
        "parameters::VARCHAR, parameter_types::VARCHAR, macro_definition "
        "from duckdb_functions() where not internal "
        "and database_name = current_database() order by 1, 2, 3, 5",
    ),
    (
        "views",
        "select schema_name, view_name, sql, comment from duckdb_views() "
        "where not internal and database_name = current_database() order by 1, 2",
    ),
    (
        "sequences",
        "select schema_name, sequence_name, sql from duckdb_sequences() "
        "where database_name = current_database() order by 1, 2",
    ),
    (
        "types",
        "select schema_name, type_name, logical_type, type_category "
        "from duckdb_types() where not internal "
        "and database_name = current_database() order by 1, 2",
    ),
    (
        "indexes",
        "select schema_name, index_name, table_name, is_unique, is_primary, sql "
        "from duckdb_indexes() where database_name = current_database() "
        "order by 1, 2, 3",
    ),
    (
        "schemas",
        "select schema_name, sql from duckdb_schemas() where not internal "
        "and database_name = current_database() order by 1",
    ),
    (
        "constraints",
        "select schema_name, table_name, constraint_type, constraint_text, "
        "constraint_column_names::VARCHAR from duckdb_constraints() "
        "where database_name = current_database() order by 1, 2, 3, 4",
    ),
    (
        "table_comments",
        "select schema_name, table_name, comment from duckdb_tables() "
        "where comment is not null and database_name = current_database() "
        "order by 1, 2",
    ),
    (
        "column_comments",
        "select schema_name, table_name, column_name, comment "
        "from duckdb_columns() where comment is not null "
        "and database_name = current_database() order by 1, 2, 3",
    ),
)


def _catalog_census(con: Any) -> dict[str, list[list[str]]]:
    """Every non-internal catalog object, as sorted rows of text.

    A macro, view, index or comment can carry the answer — or change what a
    correct query returns — without touching a single row.
    """
    catalog: dict[str, list[list[str]]] = {}
    for group, query in _CATALOG_QUERIES:
        rows = con.execute(query).fetchall()
        catalog[group] = sorted(
            ["" if cell is None else str(cell) for cell in row] for row in rows
        )
    return catalog


def catalog_digest(catalog: Mapping[str, Any]) -> str:
    """sha256 over the catalog census (stable key order, stable row order)."""
    return sha256_hex(
        canonical_json(
            {
                "kind": WAREHOUSE_CENSUS_KIND + ":catalog",
                "version": CENSUS_VERSION,
                "catalog": {
                    group: [list(row) for row in catalog[group]]
                    for group in sorted(catalog)
                },
            }
        )
    )


def warehouse_census(db_path: Path) -> dict[str, Any]:
    """Census one warehouse file: relations, catalog objects, and the roll-up.

    Read-only, and it never looks at a single storage byte — see the section
    comment above for why the .duckdb bytes are not a witness and for what v1
    was blind to.
    """
    import duckdb  # local import: only warehouse-bearing paths pay for it

    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"no warehouse to census at {db_path}")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = {
            _census_key(schema, name): _relation_census(con, schema, name)
            for schema, name, _type in _relations(con)
        }
        catalog = _catalog_census(con)
    finally:
        con.close()
    cat_digest = catalog_digest(catalog)
    return {
        "tables": tables,
        "catalog": catalog,
        "catalog_digest": cat_digest,
        "census_digest": census_digest(tables, cat_digest),
    }


def census_digest(
    tables: Mapping[str, Mapping[str, Any]], catalog_digest_value: str = ""
) -> str:
    """sha256 over the sorted (relation, row_count, row_digest) vector + catalog.

    `catalog_digest_value` is folded in so a warehouse carrying a macro, view
    or comment is a DIFFERENT warehouse even with identical rows. It defaults
    only so legacy records (no catalog leg) reconstruct; producers pass it.
    """
    return sha256_hex(
        canonical_json(
            {
                "kind": WAREHOUSE_CENSUS_KIND,
                "version": CENSUS_VERSION,
                "tables": [
                    [name, tables[name]["row_count"], tables[name]["row_digest"]]
                    for name in sorted(tables)
                ],
                "catalog_digest": catalog_digest_value,
            }
        )
    )


def warehouse_census_path(task_root: Path) -> Path:
    """Recorded-evidence path for one task's warehouse census."""
    return Path(task_root) / WAREHOUSE_CENSUS_EVIDENCE_REL


def record_warehouse_census(
    task: TaskIR,
    gold: GoldLike,
    *,
    populations_dir: Path,
    warehouse_dir: Path,
    task_root: Path,
) -> Path:
    """Census each shipped warehouse and an independent rebuild.

    Rebuild from the same frozen artifacts in a temporary file. Reject missing
    warehouses, differing rebuild counts, and non-source relations.
    """
    _check_gold_binding(task, gold)
    stage1 = _normalize_pops(gold.stage1)
    populations_dir = Path(populations_dir)
    warehouse_dir = Path(warehouse_dir)
    task_root = Path(task_root)

    populations: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix=".warehouse-rebuild-") as tmp:
        rebuild_root = Path(tmp)
        for pop in sorted(stage1):
            shipped = warehouse_dir / f"{pop}.duckdb"
            shipped_census = warehouse_census(shipped)
            rendered = populations_dir / pop / "rendered"
            rebuild_path = rebuild_root / pop / f"{pop}.duckdb"
            materialize_warehouse(task, gold, pop, rendered, rebuild_path)
            rebuild_census = warehouse_census(rebuild_path)
            try:
                rel = shipped.resolve().relative_to(task_root.resolve()).as_posix()
            except ValueError:
                rel = shipped.as_posix()
            populations[pop] = {
                "path": rel,
                "census_digest": shipped_census["census_digest"],
                "catalog_digest": shipped_census["catalog_digest"],
                "rebuild_census_digest": rebuild_census["census_digest"],
                "tables": shipped_census["tables"],
            }

    record = {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "kind": WAREHOUSE_CENSUS_KIND,
        "census_version": CENSUS_VERSION,
        "populations": populations,
    }
    path = warehouse_census_path(task_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(readable_json(record) + "\n", encoding="utf-8")
    return path


def emit_variant(
    task: TaskIR,
    gold: GoldLike,
    variant: TaskVariant,
    out_dir: Path,
    *,
    populations_dir: Path | None = None,
    flat_files_base_url: str | None = None,
    rest_base_url: str | None = None,
    destination: Destination | str = Destination.SNOWFLAKE,
    extra_destinations=(),
) -> None:
    """Atomically emit a public variant task and private reward manifest.

    Extract/load includes configuration, schemas, and documentation. Transform
    also includes the data model and one starting warehouse per population.
    Full is diagnostic only. Reject stale or incomplete gold, missing transform
    populations, and public-data leaks.
    """
    variant = TaskVariant(variant)
    _check_gold_binding(task, gold)
    stage1 = _normalize_pops(gold.stage1)
    stage2 = _normalize_pops(gold.stage2_csv)
    if PRIMARY_POPULATION not in stage1:
        raise ValueError("gold bundle has no primary-population stage-1 counts")
    missing_counts = [
        t.name for t in task.tables if t.name not in stage1[PRIMARY_POPULATION]
    ]
    if missing_counts:
        raise ValueError(f"primary stage-1 counts missing tables: {missing_counts}")
    if variant in (TaskVariant.TRANSFORM, TaskVariant.FULL):
        primary_gold = dict(stage2.get(PRIMARY_POPULATION, {}))
        missing_marts = [m.name for m in task.marts if m.name not in primary_gold]
        if missing_marts:
            raise ValueError(f"primary stage-2 gold missing marts: {missing_marts}")
    if variant is TaskVariant.TRANSFORM:
        if populations_dir is None:
            raise ValueError(
                "TRANSFORM variant needs populations_dir (frozen rendered "
                "artifacts) to materialize the provided starting warehouses"
            )
        for pop in sorted(stage1):
            rdir = populations_dir / pop / "rendered"
            if not rdir.is_dir():
                raise FileNotFoundError(
                    f"population {pop!r}: rendered dir missing: {rdir}"
                )

    out_dir = out_dir.resolve()
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=out_dir.parent, prefix=".variant-stage-"
    ) as tmp:
        stage_root = Path(tmp) / "bundle"
        stage_root.mkdir()
        # Population RELATIONS are proved from the materialized rows, so they
        # can only be stamped when this emission can see populations/.
        relations: dict[str, str] = {}
        probe: dict[str, Any] = {}
        if populations_dir is not None:
            relations = population_relations(task, populations_dir)
            if relations:
                probe = memorization_evidence(Path(populations_dir).parent)
        (stage_root / REWARD_MANIFEST).write_text(
            canonical_json(
                reward_manifest(
                    task,
                    gold,
                    variant,
                    population_relations=relations,
                    memorization_evidence=probe,
                )
            )
            + "\n"
        )

        if variant is not TaskVariant.FULL:
            stage_task = stage_root / "task"
            stage_task.mkdir()
            schemas_dir = stage_task / "schemas"
            schemas_dir.mkdir()
            for table in task.tables:
                (schemas_dir / f"{table.name}.csv").write_text(schema_csv(table))

            if variant is TaskVariant.EXTRACT_LOAD:
                config = build_config(
                    task,
                    flat_files_base_url=flat_files_base_url,
                    destination=destination,
                )
                (stage_task / "config.yaml").write_text(_dump_yaml(config))
                write_extra_destinations(
                    stage_task,
                    task,
                    [
                        d for d in extra_destinations
                        if normalize_destination(d) is not normalize_destination(destination)
                    ],
                    flat_files_base_url=flat_files_base_url,
                )
                (stage_task / EL_DOCUMENTATION_FILENAME).write_text(
                    el_documentation(task)
                )
                if populations_dir is not None:
                    # EL scoring covers every graded population, so validate
                    # each rendered root rather than only the development copy.
                    roots = [
                        populations_dir / pop / "rendered"
                        for pop in sorted(stage1)
                        if (populations_dir / pop / "rendered").is_dir()
                    ]
                    assert_serving_manifest_complete(
                        task,
                        config,
                        _sources_serving_manifest(
                            task,
                            database_name(task),
                            flat_base=resolve_flat_files_base_url(
                                flat_files_base_url
                            ),
                            rest_base=resolve_rest_base_url(rest_base_url),
                        ),
                        roots,
                    )
                    rendered_dev = populations_dir / "development" / "rendered"
                    if rendered_dev.is_dir():
                        shutil.copytree(rendered_dev, stage_task / "sources")
            else:  # TRANSFORM
                (stage_task / "data_model.yaml").write_text(
                    _dump_yaml(build_data_model(task))
                )
                # Include the transform unit's complete objective and scoring rule.
                (stage_task / T_DOCUMENTATION_FILENAME).write_text(
                    t_documentation(task, sorted(stage1))
                )
                warehouse_dir = stage_task / WAREHOUSE_DIRNAME
                warehouse_dir.mkdir()
                assert populations_dir is not None  # validated above
                for pop in sorted(stage1):
                    materialize_warehouse(
                        task,
                        gold,
                        pop,
                        populations_dir / pop / "rendered",
                        warehouse_dir / f"{pop}.duckdb",
                    )
            assert_public_tree_clean(task, stage_task)

        _replace_tree(stage_root, out_dir)

    if variant is TaskVariant.TRANSFORM and populations_dir is not None:
        # AFTER the install, over the bytes now at their final path: censusing
        # the staging copy would certify a file nobody ships. The evidence
        # lands under the task dir the gates know by relative path.
        record_warehouse_census(
            task,
            gold,
            populations_dir=populations_dir,
            warehouse_dir=out_dir / "task" / WAREHOUSE_DIRNAME,
            task_root=Path(populations_dir).parent,
        )

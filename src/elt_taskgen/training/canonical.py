"""Build and score the task's canonical Terraform and dbt workspace.

``main.tf`` must match private Airbyte intent, and models must fit the portable
dbt subset. Artifacts remain private under runtime/report paths and require full
reward on every graded population. Terraform is not executed; trusted sync and
the pinned dbt-DuckDB runtime perform local evaluation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp

from elt_taskgen.destinations import Destination
from elt_taskgen.export import eltbench
from elt_taskgen.export.eltbench import PRIVATE_AIRBYTE_CONNECTOR_CONTRACT
from elt_taskgen.models import PopulationName, TaskIR, canonical_json, readable_json
from elt_taskgen.training.contract import WORKSPACE_SCORER_VERSION
from elt_taskgen.training.dbt_runner import (
    DBT_COMPATIBILITY_SUBSET_VERSION,
    DBT_PROFILE_NAME,
    DbtRunnerLimits,
    DbtRuntimeConfig,
)
from elt_taskgen.training.models import WorkspaceScoreResult
from elt_taskgen.training.package import WorkspacePackage, load_workspace_package
from elt_taskgen.training.scorer import score_workspace
from elt_taskgen.training.workspace import (
    SealedWorkspace,
    install_workspace,
    seal_workspace,
)
from elt_taskgen.workspace import repo_root

CANONICAL_RECORD_SCHEMA_VERSION = "canonical-reachability-v1"
#: Relative to ``answer_key/``: ``runtime/canonical/<destination>/``.
CANONICAL_ARTIFACT_REL = "runtime/canonical"
#: Relative to ``tasks/<task_id>/``.
REACHABILITY_EVIDENCE_REL = "reports/canonical_reachability.json"
DBT_RUNTIME_ROOT = repo_root() / "runtime-images" / "dbt-duckdb"
#: The dbt source every canonical model reads its raw tables from.
CANONICAL_SOURCE_NAME = "raw"
#: Collision-resistant placeholder prefix used before Jinja source substitution.
_SOURCE_PLACEHOLDER_PREFIX = "eltsrc_"
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_]")
_SOURCE_RESOURCE_TYPES: Mapping[str, str] = {
    "postgres": "airbyte_source_postgres",
    "mongodb": "airbyte_source_mongodb_v2",
    "custom_api": "airbyte_source_custom",
    "aws_s3": "airbyte_source_s3",
}


class CanonicalArtifactError(ValueError):
    """Raised when private material cannot produce a supported canonical artifact."""


class CanonicalSubstrateError(RuntimeError):
    """Raised when the scoring workspace cannot be laid out safely."""


# --- Terraform -------------------------------------------------------------


def _hcl_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        raise CanonicalArtifactError(f"unsupported HCL literal {value!r}")
    if "${" in value or "%{" in value:
        raise CanonicalArtifactError("contract literal carries an interpolation")
    return json.dumps(value)


def _label(value: str) -> str:
    return _SAFE_LABEL_RE.sub("_", value)


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CanonicalArtifactError(f"{what} is not a mapping")
    return value


def render_canonical_main_tf(contract: Mapping[str, Any]) -> str:
    """Render ``main.tf`` that exactly matches the expected intent graph.

    Secrets and runtime values use default-free variables; labels cannot equal
    sensitive field names.
    """

    variables: list[str] = ["workspace_id"]

    def var(name: str) -> str:
        if name not in variables:
            variables.append(name)
        return f"var.{name}"

    provider = _mapping(contract.get("terraform_provider"), "terraform_provider")
    provider_config = _mapping(provider.get("configuration", {}), "provider configuration")
    destination = _mapping(contract.get("destination"), "destination")
    destination_kind = str(destination.get("key", ""))
    destination_config = _mapping(destination.get("configuration"), "destination configuration")
    destination_type = f"airbyte_destination_{destination_kind}"
    destination_label = "warehouse"
    body: list[str] = []
    sources = contract.get("sources")
    if not isinstance(sources, (list, tuple)) or not sources:
        raise CanonicalArtifactError("contract has no sources")
    for raw_source in sources:
        source = _mapping(raw_source, "source")
        key = str(source.get("key", ""))
        config = _mapping(source.get("configuration", {}), f"source {key} configuration")
        label = "src_" + _label(key)
        resource_type = _SOURCE_RESOURCE_TYPES.get(key)
        if resource_type is None and key.startswith("file_"):
            resource_type = "airbyte_source_file"
        if resource_type is None:
            raise CanonicalArtifactError(f"unknown source key {key!r}")
        if key == "custom_api":
            definition = (
                _hcl_literal(source["definition_id"])
                if source.get("definition_id")
                else var("custom_api_definition_id")
            )
            configuration = "jsonencode({})"
        else:
            definition = _hcl_literal(source.get("definition_id", ""))
            if key == "postgres":
                schemas = ", ".join(_hcl_literal(x) for x in config["schemas"])
                configuration = (
                    "{\n"
                    f"    host = {_hcl_literal(config['host'])}\n"
                    f"    port = {_hcl_literal(config['port'])}\n"
                    f"    database = {_hcl_literal(config['database'])}\n"
                    f"    username = {_hcl_literal(config['username'])}\n"
                    f"    password = {var('postgres_password')}\n"
                    f"    schemas = [{schemas}]\n"
                    "  }"
                )
            elif key == "mongodb":
                database_config = _mapping(config.get("database_config"), "mongodb database_config")
                databases = database_config.get("databases")
                if not isinstance(databases, (list, tuple)) or len(databases) != 1:
                    raise CanonicalArtifactError("mongodb contract must name one database")
                configuration = (
                    "{\n"
                    "    database_config = {\n"
                    "      self_managed_replica_set = {\n"
                    f"        connection_string = {_hcl_literal(database_config['connection_string'])}\n"
                    f"        database = {_hcl_literal(databases[0])}\n"
                    "      }\n"
                    "    }\n"
                    "  }"
                )
            elif key == "aws_s3":
                streams: list[str] = []
                for stream in config["streams"]:
                    filetype = str(_mapping(stream.get("format"), "s3 stream format")["filetype"])
                    globs = ", ".join(_hcl_literal(g) for g in stream["globs"])
                    streams.append(
                        "{\n"
                        f"      name = {_hcl_literal(stream['name'])}\n"
                        f"      format = {{ {filetype}_format = {{}} }}\n"
                        f"      globs = [{globs}]\n"
                        "    }"
                    )
                configuration = (
                    "{\n"
                    f"    aws_access_key_id = {var('s3_access_key_id')}\n"
                    f"    aws_secret_access_key = {var('s3_secret_access_key')}\n"
                    f"    bucket = {_hcl_literal(config['bucket'])}\n"
                    f"    endpoint = {_hcl_literal(config['endpoint'])}\n"
                    f"    region_name = {_hcl_literal(config['region_name'])}\n"
                    f"    streams = [{', '.join(streams)}]\n"
                    "  }"
                )
            else:  # file_<table>
                if config.get("provider") != {"storage": "HTTPS"}:
                    raise CanonicalArtifactError(f"file source {key!r} is not HTTPS-served")
                configuration = (
                    "{\n"
                    f"    dataset_name = {_hcl_literal(config['dataset_name'])}\n"
                    f"    format = {_hcl_literal(config['format'])}\n"
                    "    provider = { https_public_web = {} }\n"
                    f"    url = {_hcl_literal(config['url'])}\n"
                    "  }"
                )
        body.append(
            f'resource "{resource_type}" "{label}" {{\n'
            f"  name = {_hcl_literal(key)}\n"
            "  workspace_id = var.workspace_id\n"
            f"  definition_id = {definition}\n"
            f"  configuration = {configuration}\n"
            "}\n"
        )
        connection = _mapping(source.get("connection"), f"source {key} connection")
        if connection.get("namespace_definition") != "destination":
            raise CanonicalArtifactError(f"source {key!r} is not destination-namespaced")
        streams_doc = _mapping(connection.get("configurations"), "connection configurations")
        stream_hcl = ", ".join(
            "{ name = %s, sync_mode = %s }"
            % (_hcl_literal(stream["name"]), _hcl_literal(stream["sync_mode"]))
            for stream in streams_doc["streams"]
        )
        body.append(
            f'resource "airbyte_connection" "conn_{_label(key)}" {{\n'
            f"  source_id = {resource_type}.{label}.source_id\n"
            f"  destination_id = {destination_type}.{destination_label}.destination_id\n"
            '  namespace_definition = "destination"\n'
            f"  configurations = {{ streams = [{stream_hcl}] }}\n"
            "}\n"
        )
    if destination_kind == "snowflake":
        destination_hcl = (
            "{\n"
            f"    host = {var('destination_host')}\n"
            f"    database = {_hcl_literal(destination_config['database'])}\n"
            f"    schema = {_hcl_literal(destination_config['schema'])}\n"
            f"    warehouse = {var('destination_warehouse')}\n"
            f"    role = {var('destination_role')}\n"
            f"    number_data_type = {_hcl_literal(destination_config['number_data_type'])}\n"
            "    credentials = {\n"
            "      username_and_password = {\n"
            f"        username = {var('destination_username')}\n"
            f"        password = {var('destination_password')}\n"
            "      }\n"
            "    }\n"
            "  }"
        )
    elif destination_kind == "databricks":
        destination_hcl = (
            "{\n"
            f"    accept_terms = {_hcl_literal(destination_config['accept_terms'])}\n"
            "    authentication = {\n"
            "      oauth = {\n"
            f"        client_id = {var('databricks_client_id')}\n"
            f"        secret = {var('databricks_secret')}\n"
            "      }\n"
            "    }\n"
            f"    database = {var('destination_database')}\n"
            f"    hostname = {var('destination_hostname')}\n"
            f"    http_path = {var('destination_http_path')}\n"
            f"    port = {_hcl_literal(destination_config['port'])}\n"
            f"    purge_staging_data = {_hcl_literal(destination_config['purge_staging_data'])}\n"
            f"    schema = {_hcl_literal(destination_config['schema'])}\n"
            "  }"
        )
    elif destination_kind == "redshift":
        uploading = _mapping(destination_config.get("uploading_method"), "redshift uploading_method")
        region = (
            _hcl_literal(uploading["s3_bucket_region"])
            if uploading.get("s3_bucket_region")
            else var("redshift_s3_bucket_region")
        )
        destination_hcl = (
            "{\n"
            f"    database = {var('destination_database')}\n"
            f"    drop_cascade = {_hcl_literal(destination_config['drop_cascade'])}\n"
            f"    host = {var('destination_host')}\n"
            f"    password = {var('destination_password')}\n"
            f"    port = {_hcl_literal(destination_config['port'])}\n"
            f"    schema = {_hcl_literal(destination_config['schema'])}\n"
            f"    username = {var('destination_username')}\n"
            "    uploading_method = {\n"
            "      awss3_staging = {\n"
            f"        access_key_id = {var('redshift_access_key_id')}\n"
            f"        secret_access_key = {var('redshift_secret_access_key')}\n"
            f"        s3_bucket_name = {var('redshift_s3_bucket_name')}\n"
            f"        s3_bucket_path = {_hcl_literal(uploading['s3_bucket_path'])}\n"
            f"        s3_bucket_region = {region}\n"
            f"        purge_staging_data = {_hcl_literal(uploading['purge_staging_data'])}\n"
            "      }\n"
            "    }\n"
            "  }"
        )
    else:
        raise CanonicalArtifactError(f"unknown destination {destination_kind!r}")
    body.append(
        f'resource "{destination_type}" "{destination_label}" {{\n'
        f"  name = {_hcl_literal(destination_kind)}\n"
        "  workspace_id = var.workspace_id\n"
        f"  definition_id = {_hcl_literal(destination.get('definition_id', ''))}\n"
        f"  configuration = {destination_hcl}\n"
        "}\n"
    )
    out: list[str] = [
        "terraform {\n"
        "  required_providers {\n"
        f"    airbyte = {{ source = {_hcl_literal(provider['source'])}, "
        f"version = {_hcl_literal(provider['version'])} }}\n"
        "  }\n"
        "}\n"
    ]
    out.extend(f'variable "{name}" {{}}\n' for name in variables)
    server_url = provider_config.get("server_url", "")
    out.append(
        'provider "airbyte" {%s}\n'
        % (f"\n  server_url = {_hcl_literal(server_url)}\n" if server_url else "")
    )
    out.extend(body)
    return "".join(out)


# --- dbt -------------------------------------------------------------------


def _is_cte_reference(table: exp.Table) -> bool:
    """Return whether a table names a CTE visible at its lexical position."""
    name = table.name.casefold()
    node: exp.Expression | None = table
    while node is not None:
        parent = node.parent
        if isinstance(parent, exp.CTE):
            with_node = parent.parent
            if isinstance(with_node, exp.With):
                ctes = list(with_node.expressions)
                position = next(
                    (i for i, cte in enumerate(ctes) if cte is parent), len(ctes)
                )
                if any(cte.alias_or_name.casefold() == name for cte in ctes[:position]):
                    return True
            node = with_node
            continue
        if isinstance(parent, (exp.Select, exp.Union, exp.Subquery, exp.Query)):
            # sqlglot names the slot `with_` (older releases: `with`).
            with_node = parent.args.get("with_", parent.args.get("with"))
            if isinstance(with_node, exp.With) and node is not with_node:
                if any(cte.alias_or_name.casefold() == name for cte in with_node.expressions):
                    return True
        node = parent
    return False


def render_canonical_model(
    reference_sql: str, table_names: frozenset[str] | set[str], destination: Destination
) -> str:
    """Render a destination-dialect dbt model from trusted reference SQL.

    Physical tables become raw sources. Remove an unneeded top-level order and
    reject references outside the task.
    """

    tree = sqlglot.parse_one(reference_sql, read="duckdb")
    if (
        tree.args.get("order") is not None
        and tree.args.get("limit") is None
        and tree.args.get("offset") is None
    ):
        tree.set("order", None)
    for table in list(tree.find_all(exp.Table)):
        if _is_cte_reference(table):
            continue
        if table.name not in table_names:
            raise CanonicalArtifactError(
                f"reference SQL reads {table.name!r}, which is not a TaskIR table"
            )
        table.set(
            "this",
            exp.Identifier(this=f"{_SOURCE_PLACEHOLDER_PREFIX}{table.name}", quoted=False),
        )
    rendered = tree.sql(dialect=destination.value)
    # Token-exact and longest-name-first: table names may prefix one another
    # (employees / employees_absences_balance).
    for name in sorted(table_names, key=len, reverse=True):
        rendered = re.sub(
            r'["`]?\b' + re.escape(_SOURCE_PLACEHOLDER_PREFIX + name) + r'\b["`]?',
            "{{ source('%s', '%s') }}" % (CANONICAL_SOURCE_NAME, name),
            rendered,
        )
    if _SOURCE_PLACEHOLDER_PREFIX in rendered:
        raise CanonicalArtifactError("a source placeholder survived substitution")
    return "{{ config(materialized='table') }}\n" + rendered + "\n"


def render_sources_yml(
    table_names: list[str],
    *,
    destination: Destination,
    destination_configuration: Mapping[str, Any],
) -> str:
    """``models/sources.yml`` naming every TaskIR table under the destination
    namespace the private contract declares."""

    schema = destination_configuration.get("schema")
    if not isinstance(schema, str) or not schema:
        raise CanonicalArtifactError("destination contract declares no schema")
    lines = ["version: 2", "sources:", f"  - name: {CANONICAL_SOURCE_NAME}"]
    if destination is Destination.DATABRICKS:
        container = destination_configuration.get("catalog") or destination_configuration.get("database")
        key = "catalog"
    else:
        container = destination_configuration.get("database")
        key = "database"
    if isinstance(container, str) and container:
        lines.append(f"    {key}: {json.dumps(container)}")
    lines.append(f"    schema: {json.dumps(schema)}")
    lines.append("    tables:")
    lines.extend(f"      - name: {json.dumps(name)}" for name in sorted(table_names))
    return "\n".join(lines) + "\n"


def render_dbt_project_yml(project_name: str) -> str:
    return (
        f"name: {project_name}\n"
        "version: '1.0'\n"
        "config-version: 2\n"
        f"profile: {DBT_PROFILE_NAME}\n"
        "model-paths: ['models']\n"
        "models:\n"
        f"  {project_name}:\n"
        "    +materialized: table\n"
    )


def render_canonical_project(package: WorkspacePackage) -> dict[str, str]:
    """Every file of the canonical artifact, keyed by path under ``elt/``."""

    task = package.task
    table_names = frozenset(table.name for table in task.tables)
    contract = package.airbyte_contract
    destination_configuration = _mapping(
        _mapping(contract.get("destination"), "destination").get("configuration"),
        "destination configuration",
    )
    files: dict[str, str] = {
        "main.tf": render_canonical_main_tf(contract),
        "dbt_project.yml": render_dbt_project_yml(
            "canonical_" + eltbench.database_name(task)
        ),
        "models/sources.yml": render_sources_yml(
            sorted(table_names),
            destination=package.destination,
            destination_configuration=destination_configuration,
        ),
    }
    reference = dict(task.reference.sql_by_mart)
    for mart in task.marts:
        sql = reference.get(mart.name)
        if not sql:
            raise CanonicalArtifactError(f"mart {mart.name!r} has no reference SQL")
        files[f"models/{mart.name}.sql"] = render_canonical_model(
            sql, table_names, package.destination
        )
    return files


# --- substrate --------------------------------------------------------------


def _copy_tree(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise CanonicalSubstrateError(f"{source} is not a directory")
    shutil.copytree(source, target, symlinks=False)


def build_workspace_substrate(
    *,
    task: TaskIR,
    answer_key_dir: Path,
    public_dir: Path,
    populations_root: Path,
    oracle_dir: Path,
    scratch: Path,
    destination: Destination | None = None,
) -> WorkspacePackage:
    """Copy task inputs into a release-shaped scratch ``WorkspacePackage``.

    Files are never linked. ``verify=False`` skips only release checksums; all
    task, gold, documentation, schema, model, and contract checks still run.
    """

    from elt_taskgen.export import release as release_mod
    from elt_taskgen.semantic_contract import SEMANTIC_SCORER_VERSION
    from elt_taskgen.verification.gates import SCORER_VERSION

    tid = task.task_id
    content_hash = task.content_hash()
    scratch = Path(scratch)
    if scratch.exists():
        raise CanonicalSubstrateError(f"substrate scratch {scratch} already exists")
    public_root = scratch / "public" / tid
    private_root = scratch / "private" / tid
    _copy_tree(public_dir, public_root)
    shutil.rmtree(public_root / "sources", ignore_errors=True)
    _copy_tree(answer_key_dir, private_root / "answer_key")
    semantic_dir = private_root / release_mod.SEMANTIC_DIRNAME
    semantic_dir.mkdir(parents=True)
    (semantic_dir / release_mod.SEMANTIC_TASK_IR_FILENAME).write_text(
        readable_json(task.canonical_dump()) + "\n", encoding="utf-8"
    )
    el_sources: dict[str, str] = {}
    for population in PopulationName:
        rendered = populations_root / population.value / "rendered"
        rel = eltbench.EL_SOURCES_REL_TEMPLATE.format(pop=population.value)
        _copy_tree(rendered, private_root / rel)
        el_sources[population.value] = f"private/{tid}/{rel}"
    oracle_root = private_root / "oracle"
    oracle_root.mkdir()
    for population in PopulationName:
        oracle = oracle_dir / f"{population.value}.duckdb"
        if oracle.is_symlink() or not oracle.is_file():
            raise CanonicalSubstrateError(f"transform oracle {oracle} is missing")
        shutil.copyfile(oracle, oracle_root / oracle.name)
    resolved_destination = destination or eltbench.destination_from_config(
        __import__("yaml").safe_load((public_root / "config.yaml").read_text(encoding="utf-8"))
    )
    manifest = release_mod.ReleaseManifest(
        schema_version=release_mod.RELEASE_SCHEMA_VERSION,
        corpus_profile=release_mod.COMBINED_CORPUS_PROFILE,
        public_layout=release_mod.COMBINED_PUBLIC_LAYOUT,
        release_id=f"canonical-{content_hash[:16]}",
        tasks={tid: content_hash},
        splits={tid: "train"},
        families={tid: task.family_id},
        licenses={tid: task.license},
        checksums={},
        scorer_version=SCORER_VERSION,
        generator_version=release_mod.GENERATOR_VERSION,
        semantic_scorer_version=SEMANTIC_SCORER_VERSION,
        destinations={tid: resolved_destination.value},
        el_sources={tid: el_sources},
    )
    (scratch / "release_manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return load_workspace_package(scratch, tid, verify=False)


# --- scoring ----------------------------------------------------------------


def default_dbt_runtime_config() -> DbtRuntimeConfig:
    return DbtRuntimeConfig(
        python=DBT_RUNTIME_ROOT / ".venv" / "bin" / "python",
        manifest=DBT_RUNTIME_ROOT / "runtime.json",
    )


def dbt_runtime_available(config: DbtRuntimeConfig | None = None) -> bool:
    resolved = config or default_dbt_runtime_config()
    return resolved.python.is_file() and resolved.manifest.is_file()


def write_canonical_files(elt_dir: Path, files: Mapping[str, str]) -> None:
    for rel, text in files.items():
        target = elt_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def score_canonical_artifact(
    package: WorkspacePackage,
    files: Mapping[str, str],
    *,
    scratch: Path,
    runtime_config: DbtRuntimeConfig,
    dbt_limits: DbtRunnerLimits | None = None,
) -> tuple[SealedWorkspace, WorkspaceScoreResult]:
    """Install, seal and score the canonical files exactly as a solver's."""

    scratch = Path(scratch)
    attempt = install_workspace(package, scratch / "authoring")
    write_canonical_files(attempt.elt_dir, files)
    sealed = seal_workspace(attempt, package, scratch / "sealed")
    result = score_workspace(
        package,
        sealed,
        attempts_root=scratch / "runs",
        runtime_config=runtime_config,
        dbt_limits=dbt_limits,
    )
    return sealed, result


def remove_scratch_tree(root: Path) -> None:
    """Remove a scratch tree the sealer or scorer may have made read-only."""

    root = Path(root)
    if not root.exists():
        return
    for directory, child_dirs, files in os.walk(root, topdown=True):
        current = Path(directory)
        if not current.is_symlink():
            current.chmod(stat.S_IRWXU)
        for name in child_dirs:
            child = current / name
            if not child.is_symlink():
                child.chmod(stat.S_IRWXU)
        for name in files:
            child = current / name
            if not child.is_symlink():
                child.chmod(stat.S_IRUSR | stat.S_IWUSR)
    shutil.rmtree(root, ignore_errors=True)


# --- record -----------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CanonicalReachabilityRecord(BaseModel):
    """The gate's evidence: what was scored, under which pins, with what result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CANONICAL_RECORD_SCHEMA_VERSION
    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(min_length=64, max_length=64)
    destination: str
    airbyte_contract_sha256: str
    reference_sql_sha256: dict[str, str]
    files: dict[str, str]
    artifact_sha256: str
    seal_sha256: str
    compatibility_subset: str = DBT_COMPATIBILITY_SUBSET_VERSION
    workspace_scorer_version: str = WORKSPACE_SCORER_VERSION
    dbt_runtime_python: str
    #: None only when the artifact could not be rendered or scored at all
    #: (``render_error`` then says why); never None for a reachable record.
    result: WorkspaceScoreResult | None = None
    reachable: bool
    #: Rendering or substrate error recorded instead of a score.
    render_error: str = ""

    @model_validator(mode="after")
    def _reachable_is_earned(self) -> "CanonicalReachabilityRecord":
        if self.reachable and (self.result is None or not _result_is_full(self.result)):
            raise ValueError("reachable requires reward 1.0 on every graded population")
        if self.reachable and self.render_error:
            raise ValueError("a render error is never reachable")
        if self.result is None and not self.render_error:
            raise ValueError("a record without a result must carry a render_error")
        return self


def _result_is_full(result: WorkspaceScoreResult) -> bool:
    from elt_taskgen.verification.gates import GRADED_POPULATIONS

    graded = {population.value for population in GRADED_POPULATIONS}
    if not result.valid_submission or result.failure is not None:
        return False
    if result.reward != 1.0 or set(result.graded_populations) != graded:
        return False
    return all(result.populations[name].end_to_end_reward == 1.0 for name in graded)


#: Outcomes of one canonical score, as the validate-t runner acts on them.
OUTCOME_REACHABLE = "reachable"
#: A valid score below 1.0 is recorded and blocks the stage.
OUTCOME_SHORTFALL = "shortfall"
#: An unlabelled grader outcome is not recorded and blocks on the environment.
OUTCOME_ENVIRONMENT = "environment"
#: The grader refused the task package itself (task_package_invalid): the
#: exported evidence and the grader disagree. Not recorded; blocks for a human.
OUTCOME_TASK_PACKAGE = "task_package"
_TIMEOUT_CODE = "dbt_timeout"


def classify_score(result: WorkspaceScoreResult) -> str:
    from elt_taskgen.training.contract import WorkspaceFailureClass

    failure = result.failure
    if failure is not None and not failure.label_eligible:
        if failure.classification is WorkspaceFailureClass.TASK_DEFECT:
            return OUTCOME_TASK_PACKAGE
        return OUTCOME_ENVIRONMENT
    if any(
        _TIMEOUT_CODE in (score.error_codes or ())
        for score in result.populations.values()
    ):
        return OUTCOME_ENVIRONMENT
    return OUTCOME_REACHABLE if _result_is_full(result) else OUTCOME_SHORTFALL


def contract_digest(answer_key_dir: Path) -> str:
    """The digest ``load_workspace_package`` computes for the bundle-root
    connector contract (``canonical_json`` of the parsed document)."""
    document = json.loads(
        (Path(answer_key_dir) / PRIVATE_AIRBYTE_CONNECTOR_CONTRACT).read_text(encoding="utf-8")
    )
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def shortfall_summary(result: WorkspaceScoreResult | None) -> str:
    """One line naming every graded population below 1.0 and its error codes."""

    if result is None:
        return "no score"
    if result.failure is not None:
        return f"scorer failure {result.failure.error_code.value}"
    parts: list[str] = []
    for name in result.graded_populations:
        score = result.populations[name]
        if score.end_to_end_reward == 1.0:
            continue
        codes = ",".join(sorted(str(code) for code in getattr(score, "error_codes", ()) or ()))
        parts.append(
            f"{name}={score.end_to_end_reward}"
            + (f" [{codes}]" if codes else "")
        )
    return "; ".join(parts) if parts else f"reward={result.reward}"


def build_reachability_record(
    package: WorkspacePackage,
    *,
    files: Mapping[str, str],
    sealed: SealedWorkspace,
    result: WorkspaceScoreResult,
    runtime_config: DbtRuntimeConfig,
) -> CanonicalReachabilityRecord:
    task = package.task
    return CanonicalReachabilityRecord(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        destination=package.destination.value,
        airbyte_contract_sha256=package.airbyte_contract_sha256,
        reference_sql_sha256={
            mart: _sha256_text(sql) for mart, sql in sorted(dict(task.reference.sql_by_mart).items())
        },
        files={rel: _sha256_text(text) for rel, text in sorted(files.items())},
        artifact_sha256=sealed.submission.artifact_sha256,
        seal_sha256=sealed.seal_sha256,
        dbt_runtime_python=str(runtime_config.python),
        result=result,
        reachable=_result_is_full(result),
    )


def build_render_failure_record(
    task: TaskIR,
    *,
    destination: Destination | str,
    runtime_config: DbtRuntimeConfig,
    error: str,
    airbyte_contract_sha256: str = "",
) -> CanonicalReachabilityRecord:
    """The record for a task whose canonical artifact could not be produced."""

    value = destination.value if isinstance(destination, Destination) else str(destination)
    return CanonicalReachabilityRecord(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        destination=value,
        airbyte_contract_sha256=airbyte_contract_sha256,
        reference_sql_sha256={
            mart: _sha256_text(sql) for mart, sql in sorted(dict(task.reference.sql_by_mart).items())
        },
        files={},
        artifact_sha256="",
        seal_sha256="",
        dbt_runtime_python=str(runtime_config.python),
        result=None,
        reachable=False,
        render_error=error,
    )


def artifact_dir(answer_key_dir: Path, destination: Destination | str) -> Path:
    value = destination.value if isinstance(destination, Destination) else str(destination)
    return Path(answer_key_dir) / CANONICAL_ARTIFACT_REL / value


def record_canonical_reachability(
    *,
    task_dir: Path,
    answer_key_dir: Path,
    record: CanonicalReachabilityRecord,
    files: Mapping[str, str],
) -> Path:
    """Persist the artifact and its record privately; return the reports path."""

    target = artifact_dir(answer_key_dir, record.destination)
    if target.exists():
        remove_scratch_tree(target)
    target.mkdir(parents=True, exist_ok=True)
    if files:
        write_canonical_files(target / "elt", files)
    (target / "reachability.json").write_text(
        canonical_json(record.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    evidence = Path(task_dir) / REACHABILITY_EVIDENCE_REL
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        canonical_json(record.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    return evidence


@dataclass(frozen=True)
class ReachabilityEvidence:
    record: CanonicalReachabilityRecord | None
    problem: str | None


def load_canonical_reachability(
    task: TaskIR, task_dir: Path, answer_key_dir: Path
) -> ReachabilityEvidence:
    """Load a record bound to task, reference, contract, pins, and artifact bytes."""

    evidence = Path(task_dir) / REACHABILITY_EVIDENCE_REL
    rerun = "re-run validate-t"
    if evidence.is_symlink() or not evidence.is_file():
        return ReachabilityEvidence(None, f"{REACHABILITY_EVIDENCE_REL} is missing; {rerun}")
    try:
        evidence_text = evidence.read_text(encoding="utf-8")
        record = CanonicalReachabilityRecord.model_validate_json(evidence_text)
    except (OSError, UnicodeError, ValueError) as exc:
        return ReachabilityEvidence(
            None, f"{REACHABILITY_EVIDENCE_REL} is unreadable ({type(exc).__name__}); {rerun}"
        )
    if record.schema_version != CANONICAL_RECORD_SCHEMA_VERSION:
        return ReachabilityEvidence(record, f"record schema is not current; {rerun}")
    if record.task_id != task.task_id or record.task_content_hash != task.content_hash():
        return ReachabilityEvidence(record, f"record was produced for another task identity; {rerun}")
    if record.compatibility_subset != DBT_COMPATIBILITY_SUBSET_VERSION:
        return ReachabilityEvidence(
            record,
            f"record predates the portable dbt subset {DBT_COMPATIBILITY_SUBSET_VERSION}; {rerun}",
        )
    if record.workspace_scorer_version != WORKSPACE_SCORER_VERSION:
        return ReachabilityEvidence(record, f"record predates the current workspace scorer; {rerun}")
    expected_reference = {
        mart: _sha256_text(sql) for mart, sql in dict(task.reference.sql_by_mart).items()
    }
    if record.reference_sql_sha256 != expected_reference:
        return ReachabilityEvidence(record, f"record was scored against different reference SQL; {rerun}")
    problem = _bind_record_to_private_tree(record, Path(answer_key_dir))
    if problem is not None:
        return ReachabilityEvidence(record, f"{problem}; {rerun}")
    target = artifact_dir(answer_key_dir, record.destination)
    private_record = target / "reachability.json"
    try:
        if private_record.is_symlink() or not private_record.is_file():
            return ReachabilityEvidence(record, f"private canonical record is missing; {rerun}")
        if private_record.read_text(encoding="utf-8") != evidence_text:
            return ReachabilityEvidence(record, f"private and workspace canonical records disagree; {rerun}")
    except (OSError, UnicodeError) as exc:
        return ReachabilityEvidence(record, f"private canonical record is unreadable ({type(exc).__name__}); {rerun}")
    return ReachabilityEvidence(record, None)


def _bind_record_to_private_tree(
    record: CanonicalReachabilityRecord, answer_key_dir: Path
) -> str | None:
    """Shared by the gate and packaging: the record must describe the
    bundle-root connector contract on disk and the artifact bytes beside it."""

    contract_path = answer_key_dir / PRIVATE_AIRBYTE_CONNECTOR_CONTRACT
    try:
        document = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return f"bundle-root connector contract is unreadable ({type(exc).__name__})"
    destination = document.get("destination") if isinstance(document, dict) else None
    key = destination.get("key") if isinstance(destination, dict) else None
    if key != record.destination:
        return (
            f"record describes destination {record.destination!r} but the bundle-root "
            f"contract names {key!r}"
        )
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    if record.airbyte_contract_sha256 != digest:
        return "record was scored against a different connector contract"
    target = artifact_dir(answer_key_dir, record.destination) / "elt"
    for rel, expected in record.files.items():
        path = target / rel
        try:
            if path.is_symlink() or not path.is_file():
                return f"canonical artifact file {rel!r} is missing"
            if _sha256_text(path.read_text(encoding="utf-8")) != expected:
                return f"canonical artifact file {rel!r} changed since scoring"
        except (OSError, UnicodeError) as exc:
            return f"canonical artifact file {rel!r} is unreadable ({type(exc).__name__})"
    return None


def assert_canonical_ready(task: TaskIR, answer_key_dir: Path) -> None:
    """Require a reachable canonical record bound to current private inputs."""
    answer_key_dir = Path(answer_key_dir)
    remedy = (
        "run validate-t again for this task; a local package directory that "
        "already exists is immutable and never overwritten, so remove the stale "
        "one before freezing again"
    )
    try:
        document = json.loads(
            (answer_key_dir / PRIVATE_AIRBYTE_CONNECTOR_CONTRACT).read_text(encoding="utf-8")
        )
        destination = str(document["destination"]["key"])
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            "canonical workspace artifact cannot be checked: the bundle-root "
            f"connector contract is unreadable ({type(exc).__name__}); {remedy}"
        ) from None
    root = answer_key_dir / CANONICAL_ARTIFACT_REL
    present = (
        sorted(path.name for path in root.iterdir())
        if root.is_dir() and not root.is_symlink()
        else []
    )
    record_path = root / destination / "reachability.json"
    if record_path.is_symlink() or not record_path.is_file():
        raise ValueError(
            "canonical workspace artifact missing: no private "
            f"answer_key/{CANONICAL_ARTIFACT_REL}/{destination}/reachability.json; {remedy}"
        )
    unexpected = [name for name in present if name != destination]
    if unexpected:
        raise ValueError(
            f"canonical workspace artifact has entries for {unexpected} beside the "
            f"bundle-root destination {destination!r}; {remedy}"
        )
    try:
        record = CanonicalReachabilityRecord.model_validate_json(
            record_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(
            f"canonical reachability record is invalid ({type(exc).__name__}); {remedy}"
        ) from None
    problems: list[str] = []
    if record.task_id != task.task_id or record.task_content_hash != task.content_hash():
        problems.append("record was produced for another task identity")
    if record.schema_version != CANONICAL_RECORD_SCHEMA_VERSION:
        problems.append("record schema is not current")
    if record.compatibility_subset != DBT_COMPATIBILITY_SUBSET_VERSION:
        problems.append(f"record predates the portable dbt subset {DBT_COMPATIBILITY_SUBSET_VERSION}")
    if record.workspace_scorer_version != WORKSPACE_SCORER_VERSION:
        problems.append("record predates the current workspace scorer")
    expected_reference = {
        mart: _sha256_text(sql) for mart, sql in dict(task.reference.sql_by_mart).items()
    }
    if record.reference_sql_sha256 != expected_reference:
        problems.append("record was scored against different reference SQL")
    bound = _bind_record_to_private_tree(record, answer_key_dir)
    if bound is not None:
        problems.append(bound)
    if not record.reachable:
        problems.append(
            "the canonical solution is not reachable on the workspace channel "
            f"({record.render_error or shortfall_summary(record.result)})"
        )
    if problems:
        raise ValueError(
            "canonical workspace artifact is stale or unreachable: "
            + "; ".join(problems)
            + f"; {remedy}"
        )


__all__ = [
    "CANONICAL_ARTIFACT_REL",
    "CANONICAL_RECORD_SCHEMA_VERSION",
    "DBT_RUNTIME_ROOT",
    "REACHABILITY_EVIDENCE_REL",
    "CanonicalArtifactError",
    "CanonicalSubstrateError",
    "OUTCOME_ENVIRONMENT",
    "OUTCOME_REACHABLE",
    "OUTCOME_SHORTFALL",
    "OUTCOME_TASK_PACKAGE",
    "classify_score",
    "contract_digest",
    "CanonicalReachabilityRecord",
    "ReachabilityEvidence",
    "artifact_dir",
    "assert_canonical_ready",
    "build_reachability_record",
    "build_render_failure_record",
    "build_workspace_substrate",
    "dbt_runtime_available",
    "default_dbt_runtime_config",
    "load_canonical_reachability",
    "record_canonical_reachability",
    "remove_scratch_tree",
    "render_canonical_main_tf",
    "render_canonical_model",
    "render_canonical_project",
    "render_dbt_project_yml",
    "render_sources_yml",
    "score_canonical_artifact",
    "shortfall_summary",
    "write_canonical_files",
]

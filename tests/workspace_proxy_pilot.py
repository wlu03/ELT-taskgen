"""Generated files-plus-Postgres pilot for the workspace-v1 acceptance test.

Unlike the committed five-backend semantic-gate fixture, this helper builds a
fresh release inside a temporary directory on every use.  It deliberately
walks the production contracts in order: TaskIR -> population rendering ->
DuckDB reference execution -> frozen gold -> public export/variants -> release
freeze.  No committed fixture or precomputed oracle is copied or modified.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from elt_taskgen.destinations import (
    SOURCE_CONNECTOR_CONTRACTS,
    Destination,
    destination_contract,
)
from elt_taskgen.export import eltbench, release
from elt_taskgen.generation import source_data
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    JoinType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    PopulationName,
    PopulationSpec,
    RLVR_TASK_VARIANTS,
    ReferenceSolution,
    Relationship,
    Row,
    TableSpec,
    TaskIR,
    TaskVariant,
    derive_seed,
)
from elt_taskgen.reference.gold import freeze_gold
from elt_taskgen.reference.runner import run_reference

try:
    from release_doubles import FakeEngine, FakeReport, FakeSelection
except ImportError:  # running as tests.workspace_proxy_pilot
    from tests.release_doubles import FakeEngine, FakeReport, FakeSelection


PILOT_TASK_ID = "gate__files_postgres_pilot"
PILOT_MART = "customer_payment_rollup"


REFERENCE_SQL = """\
SELECT
    c.customer_id AS customer_id,
    c.customer_name AS customer_name,
    CAST(COUNT(p.payment_id) AS BIGINT) AS payment_count,
    CAST(COALESCE(SUM(p.amount), 0) AS DECIMAL(38, 2)) AS total_amount
FROM customers AS c
LEFT JOIN payments AS p ON p.customer_id = c.customer_id
GROUP BY c.customer_id, c.customer_name
ORDER BY c.customer_id
"""


DBT_MODEL_SQL = """\
{{ config(materialized='table') }}
SELECT
    c.customer_id AS customer_id,
    c.customer_name AS customer_name,
    CAST(COUNT(p.payment_id) AS BIGINT) AS payment_count,
    CAST(COALESCE(SUM(p.amount), 0) AS DECIMAL(38, 2)) AS total_amount
FROM {{ source('raw', 'customers') }} AS c
LEFT JOIN {{ source('raw', 'payments') }} AS p
    ON p.customer_id = c.customer_id
GROUP BY c.customer_id, c.customer_name
"""


_PAYMENT_SHAPES: dict[PopulationName, tuple[tuple[int, float], ...]] = {
    PopulationName.DEVELOPMENT: ((1, 10.25), (1, 2.75), (3, 5.00)),
    PopulationName.PRIMARY: (
        (1, 100.00),
        (2, 1.25),
        (2, 8.75),
        (3, 40.50),
    ),
    PopulationName.RESAMPLED: (
        (1, 3.50),
        (1, 7.50),
        (2, 20.00),
        (3, 0.25),
        (3, 0.75),
    ),
    PopulationName.COUNTERFACTUAL: ((2, 12.00), (2, 13.00)),
    PopulationName.STRESS: tuple(
        (1 if index < 12 else (index % 3) + 1, float((index % 7) + 1) / 4)
        for index in range(30)
    ),
}


def _literal_rows(population: PopulationName) -> dict[str, tuple[Row, ...]]:
    base = {
        PopulationName.DEVELOPMENT: 0,
        PopulationName.PRIMARY: 10_000,
        PopulationName.RESAMPLED: 20_000,
        PopulationName.COUNTERFACTUAL: 30_000,
        PopulationName.STRESS: 40_000,
    }[population]
    customers: tuple[Row, ...] = tuple(
        {
            "customer_id": base + offset,
            "customer_name": f"{population.value}-customer-{offset}",
        }
        for offset in (1, 2, 3)
    )
    payments: tuple[Row, ...] = tuple(
        {
            "payment_id": base + 100 + index,
            "customer_id": base + customer_offset,
            "amount": amount,
        }
        for index, (customer_offset, amount) in enumerate(
            _PAYMENT_SHAPES[population], start=1
        )
    )
    return {"customers": customers, "payments": payments}


def files_postgres_task() -> TaskIR:
    """Return the small two-backend TaskIR used by the acceptance test."""

    return TaskIR(
        task_id=PILOT_TASK_ID,
        family_id=PILOT_TASK_ID,
        cluster_id=PILOT_TASK_ID,
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        attribution="elt-taskgen workspace proxy acceptance pilot",
        title="Files and Postgres customer payment rollup",
        tables=(
            TableSpec(
                name="customers",
                description="One row per customer loaded from Postgres.",
                columns=(
                    ColumnSpec(
                        name="customer_id",
                        type=ColumnType.INTEGER,
                        description="Unique customer identifier.",
                    ),
                    ColumnSpec(
                        name="customer_name",
                        type=ColumnType.TEXT,
                        description="Customer display name.",
                    ),
                ),
                primary_key=("customer_id",),
            ),
            TableSpec(
                name="payments",
                description="Payment rows loaded from a CSV file source.",
                columns=(
                    ColumnSpec(
                        name="payment_id",
                        type=ColumnType.INTEGER,
                        description="Unique payment identifier.",
                    ),
                    ColumnSpec(
                        name="customer_id",
                        type=ColumnType.INTEGER,
                        description="Customer receiving the payment.",
                    ),
                    ColumnSpec(
                        name="amount",
                        type=ColumnType.DECIMAL,
                        description="Payment amount.",
                    ),
                ),
                primary_key=("payment_id",),
            ),
        ),
        relationships=(
            Relationship(
                child_table="payments",
                child_columns=("customer_id",),
                parent_table="customers",
                parent_columns=("customer_id",),
                required=True,
            ),
        ),
        backends=(
            BackendAssignment(table="customers", backend=Backend.POSTGRES),
            BackendAssignment(
                table="payments",
                backend=Backend.FILES,
                options={"format": "csv"},
            ),
        ),
        marts=(
            MartSpec(
                name=PILOT_MART,
                description="Payment count and total for every customer.",
                grain="One row per customer, including customers without payments.",
                key_columns=("customer_id",),
                columns=(
                    MartColumn(
                        name="customer_id",
                        type=ColumnType.INTEGER,
                        description="Unique customer identifier.",
                    ),
                    MartColumn(
                        name="customer_name",
                        type=ColumnType.TEXT,
                        description="Customer display name.",
                    ),
                    MartColumn(
                        name="payment_count",
                        type=ColumnType.BIGINT,
                        description="Number of payment rows for the customer.",
                    ),
                    MartColumn(
                        name="total_amount",
                        type=ColumnType.DECIMAL,
                        description="Payment total, or zero when there are no payments.",
                    ),
                ),
                plan=MartPlan(
                    mart=PILOT_MART,
                    ops=(
                        MartOp(
                            kind=MartOpKind.SOURCE,
                            description="Bring Postgres customers into scope.",
                            tables=("customers",),
                        ),
                        MartOp(
                            kind=MartOpKind.JOIN,
                            description="Left join file-backed payments to customers.",
                            tables=("customers", "payments"),
                            columns=("customer_id",),
                            join_type=JoinType.LEFT,
                            predicate="payments.customer_id = customers.customer_id",
                        ),
                        MartOp(
                            kind=MartOpKind.AGGREGATE,
                            description="Count payments and sum amount per customer.",
                            tables=("payments",),
                            columns=("payment_id", "amount"),
                            details={
                                "group_by": "customer_id, customer_name",
                                "payment_count": "COUNT(payment_id)",
                                "total_amount": "SUM(amount)",
                            },
                        ),
                        MartOp(
                            kind=MartOpKind.DERIVE,
                            description="Use zero for a customer with no payments.",
                            columns=("total_amount",),
                            predicate="COALESCE(total_amount, 0)",
                        ),
                    ),
                    notes="Both source backends are required to build the mart.",
                ),
            ),
        ),
        populations=tuple(
            PopulationSpec(
                name=population,
                seed=derive_seed(PILOT_TASK_ID, population.value),
                conditions=(
                    f"Literal {population.value} rows with population-specific ids and totals.",
                ),
                literal_rows=_literal_rows(population),
            )
            for population in PopulationName
        ),
        reference=ReferenceSolution(
            implementation_id="files_postgres_pilot_reference",
            dialect="duckdb",
            sql_by_mart={PILOT_MART: REFERENCE_SQL},
            load_notes="Load customers from Postgres SQL and payments from CSV.",
            provenance="Hand-authored acceptance pilot reference.",
            version="1",
        ),
    )


def correct_main_tf() -> str:
    """Canonical candidate HCL for the generated pilot's public contract."""

    postgres_id = SOURCE_CONNECTOR_CONTRACTS["postgres"].definition_id
    files_id = SOURCE_CONNECTOR_CONTRACTS["flat_files"].definition_id
    snowflake_id = destination_contract(Destination.SNOWFLAKE).definition_id
    return f'''\
terraform {{
  required_providers {{
    airbyte = {{ source = "airbytehq/airbyte", version = "0.6.5" }}
  }}
}}

variable "workspace_id" {{}}
variable "postgres_password" {{}}
variable "destination_host" {{}}
variable "destination_warehouse" {{}}
variable "destination_role" {{}}
variable "destination_username" {{}}
variable "destination_password" {{}}

provider "airbyte" {{}}

resource "airbyte_source_postgres" "customers" {{
  name = "postgres"
  workspace_id = var.workspace_id
  definition_id = "{postgres_id}"
  configuration = {{
    host = "elt-postgres"
    port = 5432
    database = "{PILOT_TASK_ID}"
    username = "postgres"
    password = var.postgres_password
    schemas = ["public"]
  }}
}}

resource "airbyte_source_file" "payments" {{
  name = "file_payments"
  workspace_id = var.workspace_id
  definition_id = "{files_id}"
  configuration = {{
    dataset_name = "payments"
    format = "csv"
    provider = {{ https_public_web = {{}} }}
    url = "https://elt-files:8443/{PILOT_TASK_ID}/payments.csv"
  }}
}}

resource "airbyte_destination_snowflake" "warehouse" {{
  name = "snowflake"
  workspace_id = var.workspace_id
  definition_id = "{snowflake_id}"
  configuration = {{
    host = var.destination_host
    database = "{PILOT_TASK_ID}"
    schema = "AIRBYTE_SCHEMA"
    warehouse = var.destination_warehouse
    role = var.destination_role
    number_data_type = "NUMBER(38,9)"
    credentials = {{
      username_and_password = {{
        username = var.destination_username
        password = var.destination_password
      }}
    }}
  }}
}}

resource "airbyte_connection" "customers" {{
  source_id = airbyte_source_postgres.customers.source_id
  destination_id = airbyte_destination_snowflake.warehouse.destination_id
  namespace_definition = "destination"
  configurations = {{
    streams = [{{ name = "customers", sync_mode = "full_refresh_append" }}]
  }}
}}

resource "airbyte_connection" "payments" {{
  source_id = airbyte_source_file.payments.source_id
  destination_id = airbyte_destination_snowflake.warehouse.destination_id
  namespace_definition = "destination"
  configurations = {{
    streams = [{{ name = "payments", sync_mode = "full_refresh_append" }}]
  }}
}}
'''


def write_correct_candidate(attempt) -> None:
    """Write the real solver-facing Terraform and persistent dbt artifacts."""

    attempt.elt_dir.joinpath("main.tf").write_text(
        correct_main_tf(), encoding="utf-8"
    )
    attempt.elt_dir.joinpath("dbt_project.yml").write_text(
        "name: files_postgres_pilot\n"
        "version: '1.0'\n"
        "config-version: 2\n"
        "profile: elt_taskgen\n"
        "model-paths: ['models']\n"
        "models:\n"
        "  files_postgres_pilot:\n"
        "    +materialized: table\n",
        encoding="utf-8",
    )
    models = attempt.elt_dir / "models"
    models.mkdir(exist_ok=True)
    models.joinpath("sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        f"    database: {PILOT_TASK_ID}\n"
        "    schema: AIRBYTE_SCHEMA\n"
        "    tables:\n"
        "      - name: customers\n"
        "      - name: payments\n",
        encoding="utf-8",
    )
    models.joinpath(f"{PILOT_MART}.sql").write_text(
        DBT_MODEL_SQL, encoding="utf-8"
    )


def _remove_workspace(root: Path) -> None:
    for directory, child_dirs, files in os.walk(root, topdown=True):
        current = Path(directory)
        if not current.is_symlink():
            current.chmod(0o700)
        for name in child_dirs:
            child = current / name
            if not child.is_symlink():
                child.chmod(0o700)
        for name in files:
            child = current / name
            if not child.is_symlink():
                child.chmod(0o600)
    shutil.rmtree(root, ignore_errors=True)


@contextmanager
def generated_files_postgres_release() -> Iterator[tuple[Path, TaskIR]]:
    """Yield a verified, freshly generated combined release and its TaskIR."""

    previous_flat_url = os.environ.pop(eltbench.FLAT_FILES_BASE_URL_ENV, None)
    workspace = Path(tempfile.mkdtemp(prefix="elt-files-postgres-pilot-"))
    try:
        task = files_postgres_task()
        task_root = workspace / "tasks" / task.task_id
        populations_dir = task_root / "populations"
        answer_key_dir = task_root / "answer_key"

        for population in PopulationName:
            rows = source_data.generate_rows(task, population)
            population_dir = populations_dir / population.value
            source_data.write_rows(rows, population_dir / "rows")
            source_data.render_population(
                task, population, rows, population_dir / "rendered"
            )

        reference_results = {
            population: run_reference(task, population, workspace)
            for population in PopulationName
        }
        gold = freeze_gold(task, reference_results, answer_key_dir)
        eltbench.export_task(task, gold, task_root / "task", answer_key_dir)

        variants_root = task_root / "variants"
        for variant in TaskVariant:
            eltbench.emit_variant(
                task,
                gold,
                variant,
                variants_root / variant.value,
                populations_dir=populations_dir,
            )

        engine = FakeEngine(
            workspace,
            task,
            FakeReport("pass", task.content_hash()),
        )
        selection = FakeSelection(
            train=(task.task_id,),
            variants={
                task.task_id: tuple(variant.value for variant in RLVR_TASK_VARIANTS)
            },
        )
        release_dir = workspace / "release" / "files-postgres-pilot"
        release.freeze_release(engine, selection, release_dir)
        verification = release.verify_release(release_dir)
        if not verification.ok:
            raise AssertionError(
                "generated files-plus-Postgres release failed verification: "
                + "; ".join(verification.failures[:5])
            )
        yield release_dir, task
    finally:
        _remove_workspace(workspace)
        if previous_flat_url is not None:
            os.environ[eltbench.FLAT_FILES_BASE_URL_ENV] = previous_flat_url


__all__ = [
    "PILOT_MART",
    "PILOT_TASK_ID",
    "files_postgres_task",
    "generated_files_postgres_release",
    "write_correct_candidate",
]

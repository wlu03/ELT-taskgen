"""Focused Milestone-3 tests for the cloud-free dbt artifact runner."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb

from elt_taskgen.destinations import Destination
from elt_taskgen.models import PopulationName
from elt_taskgen.training.dbt_runner import (
    DBT_COMPATIBILITY_SUBSET_VERSION,
    DBT_DUCKDB_ADAPTER_VERSION,
    DBT_DUCKDB_CORE_VERSION,
    DBT_DUCKDB_ENGINE_VERSION,
    DBT_DUCKDB_INSTALLED_DISTRIBUTIONS_SHA256,
    DBT_DUCKDB_RUNTIME_MANIFEST_SHA256,
    DBT_DUCKDB_UV_LOCK_SHA256,
    MAX_CANDIDATE_YAML_DEPTH,
    DbtCommandEvidence,
    DbtErrorCode,
    DbtPolicyFailure,
    DbtRunnerLimits,
    DbtRuntimeConfig,
    DbtTrustedFailure,
    _fingerprint_physical_raw_state,
    _load_unique_yaml,
    _prepare_execution_project,
    _validate_private_evidence,
    _validate_manifest_graph,
    rewrite_model_sql,
    run_dbt_project,
    verify_dbt_runtime,
)
from elt_taskgen.training.local_sync import run_local_sync
from elt_taskgen.training.namespace import project_namespace
from elt_taskgen.training.package import load_workspace_package
from elt_taskgen.training.terraform_intent import expected_terraform_graph

try:
    from workspace_proxy_fixture import TASK_ID, portable_five_backend_release
except ImportError:  # running as tests.test_training_dbt_runner
    from tests.workspace_proxy_fixture import TASK_ID, portable_five_backend_release


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "runtime-images" / "dbt-duckdb"
RUNTIME_CONFIG = DbtRuntimeConfig(
    python=RUNTIME_DIR / ".venv" / "bin" / "python",
    manifest=RUNTIME_DIR / "runtime.json",
)


CUSTOMER_ROLLUP = """\
WITH completed_orders AS (
    SELECT DISTINCT order_id, customer_id
    FROM {{ source('raw', 'orders') }}
    WHERE status = 'completed'
),
order_totals AS (
    SELECT order_id, SUM(quantity * unit_price) AS order_total
    FROM {{ source('raw', 'order_items') }}
    GROUP BY order_id
)
SELECT
    c.customer_id AS customer_id,
    COUNT(DISTINCT co.order_id) AS completed_orders,
    COALESCE(SUM(ot.order_total), 0) AS total_spend
FROM {{ source('raw', 'customers') }} AS c
LEFT JOIN completed_orders AS co ON co.customer_id = c.customer_id
LEFT JOIN order_totals AS ot ON ot.order_id = co.order_id
GROUP BY c.customer_id
ORDER BY c.customer_id
"""

EVENT_WIDE = """\
SELECT
    e.event_id AS event_id,
    e.big_count AS big_count,
    e.label AS label,
    e.occurred_at AS occurred_at,
    e.tz_stamp AS tz_stamp,
    m.value AS metric_value,
    m.big_note AS big_note
FROM {{ source('raw', 'events') }} AS e
LEFT JOIN {{ source('raw', 'metrics') }} AS m ON m.event_id = e.event_id
ORDER BY e.event_id
"""

DUPLICATE_GRAIN = """\
SELECT
    customer_id AS customer_id,
    0 AS completed_orders,
    0 AS total_spend
FROM {{ source('raw', 'customers') }}
UNION ALL
SELECT
    customer_id AS customer_id,
    0 AS completed_orders,
    0 AS total_spend
FROM {{ source('raw', 'customers') }}
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_candidate(
    attempt: Path,
    package,
    *,
    schema: str | None = "AIRBYTE_SCHEMA",
    database: str | None = TASK_ID,
    extra_mart_column: bool = False,
) -> None:
    models = attempt / "elt" / "models"
    models.mkdir(parents=True)
    (attempt / "elt" / "dbt_project.yml").write_text(
        "name: candidate\n"
        "version: '1.0'\n"
        "config-version: 2\n"
        "profile: elt_taskgen\n"
        "model-paths: [models]\n"
        "models:\n"
        "  candidate:\n"
        "    +materialized: table\n",
        encoding="utf-8",
    )
    source_lines = ["version: 2", "sources:", "  - name: raw"]
    if database is not None:
        source_lines.append(f"    database: {database}")
    if schema is not None:
        source_lines.append(f"    schema: {schema}")
    source_lines.append("    tables:")
    source_lines.extend(f"      - name: {table.name}" for table in package.task.tables)
    (models / "sources.yml").write_text(
        "\n".join(source_lines) + "\n", encoding="utf-8"
    )
    (models / "customer_rollup.sql").write_text(
        CUSTOMER_ROLLUP, encoding="utf-8"
    )
    event_sql = EVENT_WIDE.replace(
        "    m.big_note AS big_note\n",
        "    m.big_note AS big_note,\n    1 AS unexpected_column\n",
    ) if extra_mart_column else EVENT_WIDE
    (models / "event_wide.sql").write_text(event_sql, encoding="utf-8")


def _valid_manifest(package, namespace) -> dict:
    source_ids = {
        table.name: f"source.candidate.raw.{table.name}"
        for table in package.task.tables
    }
    sources = {
        source_id: {
            "resource_type": "source",
            "name": table,
            "identifier": table,
            "schema": namespace.raw_schema,
        }
        for table, source_id in source_ids.items()
    }
    nodes = {
        "model.candidate.customer_rollup": {
            "resource_type": "model",
            "package_name": "candidate",
            "language": "sql",
            "name": "customer_rollup",
            "alias": "customer_rollup",
            "schema": namespace.mart_schema,
            "config": {"materialized": "table"},
            "depends_on": {
                "nodes": [
                    source_ids["customers"],
                    source_ids["orders"],
                    source_ids["order_items"],
                ]
            },
        },
        "model.candidate.event_wide": {
            "resource_type": "model",
            "package_name": "candidate",
            "language": "sql",
            "name": "event_wide",
            "alias": "event_wide",
            "schema": namespace.mart_schema,
            "config": {"materialized": "table"},
            "depends_on": {
                "nodes": [source_ids["events"], source_ids["metrics"]]
            },
        },
    }
    return {
        "metadata": {"adapter_type": "duckdb"},
        "sources": sources,
        "nodes": nodes,
    }


class DbtRuntimeContractTests(unittest.TestCase):
    def test_lock_manifest_and_code_constants_are_bound(self) -> None:
        manifest = json.loads((RUNTIME_DIR / "runtime.json").read_text(encoding="utf-8"))
        distributions = json.loads(
            (RUNTIME_DIR / "installed-distributions.json").read_text(encoding="utf-8")
        )
        lock = tomllib.loads((RUNTIME_DIR / "uv.lock").read_text(encoding="utf-8"))
        locked_versions = {
            item["name"]: item["version"]
            for item in lock["package"]
            if "version" in item
        }

        self.assertEqual(_sha256(RUNTIME_DIR / "runtime.json"), DBT_DUCKDB_RUNTIME_MANIFEST_SHA256)
        self.assertEqual(_sha256(RUNTIME_DIR / "uv.lock"), DBT_DUCKDB_UV_LOCK_SHA256)
        self.assertEqual(
            _sha256(RUNTIME_DIR / "installed-distributions.json"),
            DBT_DUCKDB_INSTALLED_DISTRIBUTIONS_SHA256,
        )
        self.assertEqual(manifest["python_requires"], "==3.9.*")
        self.assertEqual(manifest["uv_lock_sha256"], DBT_DUCKDB_UV_LOCK_SHA256)
        self.assertEqual(
            manifest["installed_distributions_sha256"],
            DBT_DUCKDB_INSTALLED_DISTRIBUTIONS_SHA256,
        )
        roots = {
            "dbt-core": DBT_DUCKDB_CORE_VERSION,
            "dbt-duckdb": DBT_DUCKDB_ADAPTER_VERSION,
            "duckdb": DBT_DUCKDB_ENGINE_VERSION,
        }
        for name, version in roots.items():
            self.assertEqual(distributions[name], version)
            self.assertEqual(locked_versions[name], version)

    def test_clean_preprovisioned_runtime_matches_exact_distribution_set(self) -> None:
        if not RUNTIME_CONFIG.python.is_file():
            self.skipTest(
                "provision with: uv sync --project runtime-images/dbt-duckdb --locked"
            )
        identity = verify_dbt_runtime(RUNTIME_CONFIG)
        self.assertTrue(identity.scoring_eligible)
        self.assertEqual(identity.extra_distribution_count, 0)
        self.assertEqual(identity.packages["dbt-core"], DBT_DUCKDB_CORE_VERSION)
        self.assertEqual(identity.packages["dbt-duckdb"], DBT_DUCKDB_ADAPTER_VERSION)
        self.assertEqual(identity.packages["duckdb"], DBT_DUCKDB_ENGINE_VERSION)


class PortableSqlPolicyTests(unittest.TestCase):
    def test_canonical_json_syntax_is_rewritten_for_each_destination(self) -> None:
        cases = {
            Destination.SNOWFLAKE: "payload:country::STRING",
            Destination.DATABRICKS: "get_json_object(payload, '$.country')",
            Destination.REDSHIFT: "payload.country::VARCHAR",
        }
        for destination, expression in cases.items():
            with self.subTest(destination=destination.value):
                rewritten = rewrite_model_sql(
                    "SELECT "
                    + expression
                    + " AS country FROM {{ source('raw', 'events') }}",
                    destination,
                    json_columns=frozenset({"payload"}),
                )
                self.assertIn("source('raw', 'events')", rewritten)
                self.assertIn("country", rewritten.lower())
                executable = rewritten.replace(
                    "{{ source('raw', 'events') }}", "events"
                )
                connection = duckdb.connect(":memory:")
                try:
                    connection.execute("CREATE TABLE events(payload JSON)")
                    connection.execute(
                        "INSERT INTO events VALUES (?)", ['{"country":"US"}']
                    )
                    self.assertEqual(connection.execute(executable).fetchone(), ("US",))
                finally:
                    connection.close()

    def test_temporal_units_are_admitted_only_in_unit_slots(self) -> None:
        """portable-dbt-sql-v2: the registered temporal functions were dead
        because sqlglot parses their unit slot as an `exp.Var`; a Var is now
        admitted only in that slot and only from the closed vocabulary."""
        source = "{{ source('raw', 'events') }}"

        def run(sql: str) -> object:
            connection = duckdb.connect(":memory:")
            try:
                connection.execute(
                    "CREATE TABLE events(id INTEGER, a DATE, b DATE, created_at TIMESTAMP)"
                )
                connection.execute(
                    "INSERT INTO events VALUES (7, DATE '2024-01-01', DATE '2024-03-01', "
                    "TIMESTAMP '2024-03-05 10:00:00')"
                )
                return connection.execute(sql.replace(source, "events")).fetchone()
            finally:
                connection.close()

        for destination in Destination:
            with self.subTest(destination=destination.value, shape="date_trunc"):
                rewritten = rewrite_model_sql(
                    f"SELECT DATE_TRUNC('day', created_at) AS d FROM {source}", destination
                )
                self.assertIn("DATE_TRUNC('DAY'", rewritten)
                # DATE on the pinned grading engine (DuckDB 1.4.5), TIMESTAMP on
                # newer engines: the value is what is pinned here.
                self.assertEqual(str(run(rewritten)[0])[:10], "2024-03-05")
            with self.subTest(destination=destination.value, shape="extract"):
                rewritten = rewrite_model_sql(
                    f"SELECT EXTRACT(year FROM created_at) AS y FROM {source}", destination
                )
                self.assertIn("EXTRACT(YEAR FROM", rewritten)
                self.assertEqual(run(rewritten), (2024,))
            for sql in (
                f"SELECT DATE_TRUNC(col, created_at) AS d FROM {source}",
                f"SELECT DATE_TRUNC('epoch', created_at) AS d FROM {source}",
                f"SELECT DATE_TRUNC('week', created_at) AS d FROM {source}",
                f"SELECT EXTRACT(dow FROM created_at) AS d FROM {source}",
                f"SELECT created_at + INTERVAL '1' DAY AS d FROM {source}",
                # A quoted part is a string Literal, not a Var: same vocabulary.
                f"SELECT TRUNC(created_at, 'week') AS d FROM {source}",
                # The parser drops a third argument; the tokens do not.
                f"SELECT DATE_TRUNC('day', created_at, 'America/New_York') AS d FROM {source}",
                # A DuckDB `x + INTERVAL` turns a DATE into a TIMESTAMP.
                f"SELECT DATEADD(day, 1, created_at) AS d FROM {source}",
            ):
                with self.subTest(destination=destination.value, refused=sql):
                    with self.assertRaises(DbtPolicyFailure) as caught:
                        rewrite_model_sql(sql, destination)
                    self.assertIs(caught.exception.code, DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
        with self.subTest(destination="snowflake", shape="datediff"):
            # Snowflake counts unit boundaries, as DuckDB DATE_DIFF does.
            rewritten = rewrite_model_sql(
                f"SELECT DATEDIFF(day, a, b) AS d FROM {source}", Destination.SNOWFLAKE
            )
            self.assertIn("DATE_DIFF('DAY'", rewritten)
            self.assertEqual(run(rewritten), (60,))
        # Databricks DATEDIFF(unit, ...) counts whole elapsed units; other
        # readers produce separate node types (TimestampAdd, TsOrDsAdd,
        # TsOrDsDiff) that are separate admissions with their own fixture.
        for destination, sql in (
            (Destination.DATABRICKS, f"SELECT DATEDIFF(hour, created_at, created_at) AS d FROM {source}"),
            (Destination.DATABRICKS, f"SELECT DATEADD(day, 1, created_at) AS d FROM {source}"),
            (Destination.REDSHIFT, f"SELECT DATEADD(day, 1, created_at) AS d FROM {source}"),
            (Destination.REDSHIFT, f"SELECT DATEDIFF(day, a, b) AS d FROM {source}"),
        ):
            with self.subTest(destination=destination.value, refused=sql):
                with self.assertRaises(DbtPolicyFailure):
                    rewrite_model_sql(sql, destination)

    def test_md5_and_double_pipe_are_admitted_on_text_operands(self) -> None:
        source = "{{ source('raw', 'events') }}"
        surrogate = (
            "SELECT MD5(CAST(COALESCE(CAST(a AS VARCHAR), '_n_') || '-' || "
            f"CAST(b AS VARCHAR) AS VARCHAR)) AS k, NULL || 'b' AS n, MD5(CAST(NULL AS VARCHAR)) AS m FROM {source}"
        )
        for destination in Destination:
            with self.subTest(destination=destination.value):
                rewritten = rewrite_model_sql(surrogate, destination)
                connection = duckdb.connect(":memory:")
                try:
                    connection.execute("CREATE TABLE events(a INTEGER, b INTEGER)")
                    connection.execute("INSERT INTO events VALUES (NULL, 2)")
                    k, n, m = connection.execute(rewritten.replace(source, "events")).fetchone()
                    self.assertEqual(k, connection.execute("SELECT md5('_n_-2')").fetchone()[0])
                    self.assertRegex(k, r"^[0-9a-f]{32}$")
                    self.assertIsNone(n)
                    self.assertIsNone(m)
                finally:
                    connection.close()
            for sql in (
                f"SELECT MD5(a) AS k FROM {source}",
                f"SELECT MD5(CAST(a AS INT)) AS k FROM {source}",
                f"SELECT MD5(CAST(a AS VARCHAR) || '-' || 'x') AS k FROM {source}",
                f"SELECT MD5(TRY_CAST(a AS VARCHAR)) AS k FROM {source}",
                f"SELECT MD5(CAST(a AS VARCHAR(8))) AS k FROM {source}",
                f"SELECT MD5(CAST(payload AS VARCHAR)) AS k FROM {source}",
            ):
                with self.subTest(destination=destination.value, refused=sql):
                    with self.assertRaises(DbtPolicyFailure) as caught:
                        rewrite_model_sql(sql, destination, json_columns=frozenset({"payload"}))
                    self.assertIs(caught.exception.code, DbtErrorCode.COMPATIBILITY_UNSUPPORTED)

    def test_redshift_varchar_max_is_the_bare_text_type(self) -> None:
        source = "{{ source('raw', 'events') }}"
        rewritten = rewrite_model_sql(
            f"SELECT CAST(a AS VARCHAR(MAX)) AS s FROM {source}", Destination.REDSHIFT
        )
        self.assertIn("CAST(a AS TEXT)", rewritten)
        connection = duckdb.connect(":memory:")
        try:
            connection.execute("CREATE TABLE events(a INTEGER)")
            connection.execute("INSERT INTO events VALUES (5)")
            self.assertEqual(connection.execute(rewritten.replace(source, "events")).fetchone(), ("5",))
        finally:
            connection.close()
        # A MAX length is Redshift's VARCHAR spelling only. Databricks rejects
        # it although sqlglot's Databricks reader folds it away, so the token
        # stream is what refuses it there.
        for destination, sql in (
            (Destination.SNOWFLAKE, f"SELECT CAST(a AS VARCHAR(MAX)) AS s FROM {source}"),
            (Destination.DATABRICKS, f"SELECT CAST(a AS VARCHAR(MAX)) AS s FROM {source}"),
            (Destination.REDSHIFT, f"SELECT CAST(a AS TEXT(MAX)) AS s FROM {source}"),
            (Destination.REDSHIFT, f"SELECT CAST(a AS CHAR(MAX)) AS s FROM {source}"),
        ):
            with self.subTest(destination=destination.value, refused=sql):
                with self.assertRaises(DbtPolicyFailure):
                    rewrite_model_sql(sql, destination)

    def test_compatibility_subset_version_is_v6(self) -> None:
        # v4 is the first version whose whole admitted surface was measured
        # against live destinations; see tests/test_warehouse_differential.py.
        # v5 adds the natively measured bare-DECIMAL defaults and alias-aware
        # guards; see tests/test_warehouse_differential_compare.py. v6 stops
        # the alias-aware text guard at operands that cannot become the value.
        self.assertEqual(DBT_COMPATIBILITY_SUBSET_VERSION, "portable-dbt-sql-v6")

    def test_the_constructs_a_destination_cannot_compile_are_refused(self) -> None:
        """Each refusal below was measured: the destination rejected the SQL
        outright while the grading engine ran it."""
        source = "{{ source('raw', 'orders') }}"
        for destination, sql in (
            # Redshift: cannot cast a boolean-valued expression to text, even
            # when a parenthesis hides the comparison.
            (Destination.REDSHIFT, f"SELECT CAST((a > 1) AS TEXT) AS s FROM {source}"),
            (Destination.REDSHIFT, f"SELECT CAST(((a > 1) AND (a < 9)) AS TEXT) AS s FROM {source}"),
            (Destination.REDSHIFT, f"SELECT CAST((a IS NULL) AS TEXT) AS s FROM {source}"),
            (Destination.REDSHIFT, f"SELECT CAST((TRUE) AS TEXT) AS s FROM {source}"),
            # Redshift: no aggregate FILTER clause.
            (Destination.REDSHIFT, f"SELECT COUNT(*) FILTER (WHERE a > 1) AS s FROM {source}"),
            # Redshift: an aggregate window with ORDER BY needs an explicit frame.
            (Destination.REDSHIFT, f"SELECT SUM(a) OVER (ORDER BY a) AS s FROM {source}"),
            (Destination.REDSHIFT, f"SELECT LAST_VALUE(a) OVER (ORDER BY a) AS s FROM {source}"),
            # Redshift and Databricks: no DISTINCT window aggregate.
            (Destination.REDSHIFT, f"SELECT COUNT(DISTINCT a) OVER () AS s FROM {source}"),
            (Destination.DATABRICKS, f"SELECT COUNT(DISTINCT a) OVER () AS s FROM {source}"),
            # Databricks: TO_CHAR's datetime pattern.
            (Destination.DATABRICKS, f"SELECT TO_CHAR(CAST(a AS TIMESTAMP), 'YYYY-MM') AS s FROM {source}"),
        ):
            with self.subTest(destination=destination.value, refused=sql):
                with self.assertRaises(DbtPolicyFailure):
                    rewrite_model_sql(sql, destination)

    def test_the_window_shapes_those_destinations_do_run_stay_admitted(self) -> None:
        """The refusals are narrow: ranking and navigation windows, and any
        window carrying an explicit frame, were measured to agree."""
        source = "{{ source('raw', 'orders') }}"
        for destination, sql in (
            (Destination.REDSHIFT, f"SELECT ROW_NUMBER() OVER (ORDER BY a) AS s FROM {source}"),
            (Destination.REDSHIFT, f"SELECT RANK() OVER (ORDER BY a) AS s FROM {source}"),
            (Destination.REDSHIFT, f"SELECT LAG(a) OVER (ORDER BY a) AS s FROM {source}"),
            (
                Destination.REDSHIFT,
                f"SELECT SUM(a) OVER (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM {source}",
            ),
            (Destination.REDSHIFT, f"SELECT SUM(a) OVER (PARTITION BY a) AS s FROM {source}"),
            (Destination.DATABRICKS, f"SELECT COUNT(a) OVER (ORDER BY a) AS s FROM {source}"),
            (Destination.SNOWFLAKE, f"SELECT COUNT(DISTINCT a) OVER () AS s FROM {source}"),
            (Destination.SNOWFLAKE, f"SELECT CAST((a > 1) AS TEXT) AS s FROM {source}"),
        ):
            with self.subTest(destination=destination.value, admitted=sql):
                rewrite_model_sql(sql, destination)

    def test_direct_relations_unknown_features_and_file_readers_fail_closed(self) -> None:
        cases = (
            (
                "SELECT * FROM employees",
                DbtErrorCode.GRAPH_INVALID,
            ),
            (
                "SELECT MEDIAN(id) FROM {{ source('raw', 'events') }}",
                DbtErrorCode.COMPATIBILITY_UNSUPPORTED,
            ),
            (
                "SELECT * FROM read_csv_auto('/private/source.csv')",
                DbtErrorCode.UNSAFE_ARTIFACT,
            ),
        )
        for sql, expected in cases:
            with self.subTest(sql=sql):
                with self.assertRaises(DbtPolicyFailure) as raised:
                    rewrite_model_sql(sql, Destination.SNOWFLAKE)
                self.assertEqual(raised.exception.code, expected)

    def test_date_and_decimal_rules_are_explicit_and_bounded(self) -> None:
        date_cases = {
            Destination.SNOWFLAKE: "TO_CHAR(created_at, 'YYYY-MM')",
            Destination.DATABRICKS: "date_format(created_at, 'yyyy-MM')",
            Destination.REDSHIFT: "TO_CHAR(created_at, 'YYYY-MM')",
        }
        for destination, expression in date_cases.items():
            with self.subTest(destination=destination.value):
                rewritten = rewrite_model_sql(
                    "SELECT "
                    + expression
                    + " AS month_key, CAST(amount AS DECIMAL(38,9)) AS amount "
                    "FROM {{ source('raw', 'events') }}",
                    destination,
                )
                self.assertIn("STRFTIME", rewritten.upper())
                self.assertIn("DECIMAL(38, 9)", rewritten.upper())

        for decimal_type in ("DECIMAL(38,10)", "DECIMAL(39,9)"):
            with self.subTest(decimal_type=decimal_type):
                with self.assertRaises(DbtPolicyFailure) as raised:
                    rewrite_model_sql(
                        "SELECT CAST(amount AS "
                        + decimal_type
                        + ") FROM {{ source('raw', 'events') }}",
                        Destination.SNOWFLAKE,
                    )
                self.assertEqual(
                    raised.exception.code, DbtErrorCode.COMPATIBILITY_UNSUPPORTED
                )

    def test_nondeterministic_aggregate_and_mutating_statements_are_rejected(self) -> None:
        for sql, expected in (
            (
                "SELECT ARRAY_AGG(label) FROM {{ source('raw', 'events') }}",
                DbtErrorCode.COMPATIBILITY_UNSUPPORTED,
            ),
            ("ATTACH 'private.duckdb' AS stolen", DbtErrorCode.UNSAFE_ARTIFACT),
            (
                "COPY (SELECT 1) TO 'leak.csv'",
                DbtErrorCode.UNSAFE_ARTIFACT,
            ),
            ("EXPORT DATABASE 'leak'", DbtErrorCode.UNSAFE_ARTIFACT),
            ("LOAD httpfs", DbtErrorCode.UNSAFE_ARTIFACT),
            (
                "SELECT postgres_scan('secret', 'db', 'table')",
                DbtErrorCode.UNSAFE_ARTIFACT,
            ),
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(DbtPolicyFailure) as raised:
                    rewrite_model_sql(sql, Destination.SNOWFLAKE)
                self.assertEqual(raised.exception.code, expected)


class CandidateYamlBoundaryTests(unittest.TestCase):
    """A02: candidate YAML that PyYAML parses into an unhashable key, a cycle,
    or a tree too deep or large to walk, or that holds an unconstructable
    scalar, is the candidate's PROJECT_INVALID failure, never an unclassified
    exception that the runner turns into a no-label harness fault."""

    HEADER = "name: audit\nconfig-version: 2\n"

    def _load(self, body: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dbt_project.yml"
            path.write_text(self.HEADER + body, encoding="utf-8")
            return _load_unique_yaml(path)

    def test_malformed_shapes_are_project_invalid(self) -> None:
        bomb = "".join(
            f"l{i}: &l{i} ["
            + ", ".join([f"*l{i - 1}"] * 10 if i else ["x"] * 10)
            + "]\n"
            for i in range(9)
        )
        chained = "".join(
            f"c{i}: &c{i} " + "[" * 40 + (f"*c{i - 1}" if i else "x") + "]" * 40 + "\n"
            for i in range(4)
        )
        cases = {
            "unhashable sequence key": "? [a, b]\n: value\n",
            "unhashable mapping key": "? {a: 1}\n: value\n",
            "cyclic alias": "vars: &loop [*loop]\n",
            "nesting one level too deep": "vars: " + "[" * MAX_CANDIDATE_YAML_DEPTH
            + "]" * MAX_CANDIDATE_YAML_DEPTH + "\n",
            "nesting deep enough to exhaust the parser": "vars: " + "[" * 50_000
            + "]" * 50_000 + "\n",
            "exponential alias expansion": bomb,
            "aliases chained past the depth bound": chained,
            "impossible date": "vars: {started: 2001-13-45}\n",
        }
        for label, body in cases.items():
            with self.subTest(label):
                started = time.monotonic()
                with self.assertRaises(DbtPolicyFailure) as raised:
                    self._load(body)
                self.assertEqual(raised.exception.code, DbtErrorCode.PROJECT_INVALID)
                self.assertLess(time.monotonic() - started, 10.0)

    def test_admitted_shapes_still_load(self) -> None:
        self.assertEqual(self._load("vars: {a: 1}\n")["vars"], {"a": 1})
        deepest = "vars: " + "[" * (MAX_CANDIDATE_YAML_DEPTH - 1) + "]" * (
            MAX_CANDIDATE_YAML_DEPTH - 1
        ) + "\n"
        self.assertIn("vars", self._load(deepest))
        # A repeated alias is a shared reference, not a cycle.
        doc = self._load("shared: &a [1, 2]\nvars: [*a, *a]\n")
        self.assertEqual(doc["vars"], [[1, 2], [1, 2]])
        with self.assertRaises(DbtPolicyFailure) as raised:
            self._load("name: again\n")
        self.assertEqual(raised.exception.code, DbtErrorCode.PROJECT_INVALID)


class NativeCompatibilityRewriteTests(unittest.TestCase):
    """portable-dbt-sql-v5, from the native probes of 2026-09-18
    (tests/fixtures/warehouse_differential/native_*.json), and the v6 limit on
    which operands the alias-aware text guard follows."""

    SOURCE = "{{ source('raw', 'orders') }}"
    TEXT = {
        Destination.SNOWFLAKE: "VARCHAR",
        Destination.DATABRICKS: "STRING",
        Destination.REDSHIFT: "VARCHAR(MAX)",
    }
    DOUBLE = {
        Destination.SNOWFLAKE: "DOUBLE",
        Destination.DATABRICKS: "DOUBLE",
        Destination.REDSHIFT: "DOUBLE PRECISION",
    }

    def test_a_bare_decimal_takes_its_destinations_default(self) -> None:
        defaults = {
            Destination.SNOWFLAKE: "DECIMAL(38, 0)",
            Destination.DATABRICKS: "DECIMAL(10, 0)",
            Destination.REDSHIFT: "DECIMAL(18, 0)",
        }
        for destination, expected in defaults.items():
            for spelling in ("CAST(v AS DECIMAL)", "v::DECIMAL"):
                with self.subTest(destination=destination.value, spelling=spelling):
                    rewritten = rewrite_model_sql(
                        f"SELECT {spelling} AS d FROM {self.SOURCE}", destination
                    )
                    self.assertIn(expected, rewritten)
            explicit = rewrite_model_sql(
                f"SELECT CAST(v AS DECIMAL(12, 4)) AS d FROM {self.SOURCE}", destination
            )
            self.assertIn("DECIMAL(12, 4)", explicit)

    def test_alias_hidden_text_casts_are_refused_like_direct_ones(self) -> None:
        for destination in Destination:
            text, double = self.TEXT[destination], self.DOUBLE[destination]
            refused = {
                "direct": f"SELECT MD5(CAST(CAST(v AS {double}) AS {text})) AS h FROM {self.SOURCE}",
                "cte": (
                    f"WITH s AS (SELECT CAST(v AS {double}) AS x FROM {self.SOURCE}) "
                    f"SELECT MD5(CAST(x AS {text})) AS h FROM s"
                ),
                "chained cte": (
                    f"WITH a AS (SELECT CAST(v AS {double}) AS x FROM {self.SOURCE}), "
                    f"b AS (SELECT x AS y FROM a) SELECT CAST(y AS {text}) AS h FROM b"
                ),
                "subquery": (
                    f"SELECT CAST(x AS {text}) AS h FROM "
                    f"(SELECT CAST(v AS {double}) AS x FROM {self.SOURCE}) AS s"
                ),
                "timestamp": (
                    f"WITH s AS (SELECT CAST(v AS TIMESTAMP) AS t FROM {self.SOURCE}) "
                    f"SELECT CAST(t AS {text}) AS h FROM s"
                ),
            }
            for label, sql in refused.items():
                with self.subTest(destination=destination.value, form=label):
                    with self.assertRaises(DbtPolicyFailure) as raised:
                        rewrite_model_sql(sql, destination)
                    self.assertEqual(raised.exception.code, DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
            with self.subTest(destination=destination.value, form="integer control"):
                rewrite_model_sql(
                    f"WITH s AS (SELECT CAST(v AS INT) AS x FROM {self.SOURCE}) "
                    f"SELECT MD5(CAST(x AS {text})) AS h FROM s",
                    destination,
                )

    def test_alias_following_skips_operands_that_cannot_become_the_value(self) -> None:
        """A label, count or rank computed from a DOUBLE is not the DOUBLE's text.

        The first form is the shape of the canonical top-N marts (dlt personio
        and pipedrive, 2026-09-19): a tie count over a DOUBLE measure, a CASE
        label over the count, and a final cast of the label to text.
        """
        for destination in Destination:
            text, double = self.TEXT[destination], self.DOUBLE[destination]
            admitted = {
                "case label over a count": (
                    f"WITH a AS (SELECT CAST(v AS {double}) AS x FROM {self.SOURCE}), "
                    "b AS (SELECT COUNT(CASE WHEN x = 0 THEN 1 END) AS n FROM a), "
                    "c AS (SELECT CASE WHEN n = 0 THEN 'empty' WHEN n = 1 THEN 'unique' "
                    "ELSE 'tied' END AS state FROM b) "
                    f"SELECT CAST(state AS {text}) AS state FROM c"
                ),
                "rank ordered by a double": (
                    f"WITH a AS (SELECT ROW_NUMBER() OVER (ORDER BY CAST(v AS {double})) "
                    f"AS r FROM {self.SOURCE}) SELECT CAST(r AS {text}) AS h FROM a"
                ),
                "count of a double": (
                    f"WITH a AS (SELECT COUNT(CAST(v AS {double})) AS n FROM {self.SOURCE}) "
                    f"SELECT CAST(n AS {text}) AS h FROM a"
                ),
            }
            refused = {
                "case value": (
                    f"WITH a AS (SELECT CASE WHEN v > 0 THEN CAST(v AS {double}) ELSE 0 END "
                    f"AS x FROM {self.SOURCE}) SELECT CAST(x AS {text}) AS h FROM a"
                ),
                "window aggregate value": (
                    f"WITH a AS (SELECT SUM(CAST(v AS {double})) OVER (PARTITION BY k) "
                    f"AS x FROM {self.SOURCE}) SELECT CAST(x AS {text}) AS h FROM a"
                ),
            }
            for label, sql in admitted.items():
                with self.subTest(destination=destination.value, admitted=label):
                    rewrite_model_sql(sql, destination)
            for label, sql in refused.items():
                with self.subTest(destination=destination.value, refused=label):
                    with self.assertRaises(DbtPolicyFailure) as raised:
                        rewrite_model_sql(sql, destination)
                    self.assertEqual(raised.exception.code, DbtErrorCode.COMPATIBILITY_UNSUPPORTED)

    def test_redshift_boolean_text_casts_follow_aliases_and_declared_columns(self) -> None:
        for destination in Destination:
            text = self.TEXT[destination]
            cte = (
                f"WITH s AS (SELECT (v > 0) AS f FROM {self.SOURCE}) "
                f"SELECT CAST(f AS {text}) AS h FROM s"
            )
            declared = f"SELECT CAST(flag AS {text}) AS h FROM {self.SOURCE}"
            with self.subTest(destination=destination.value):
                if destination is Destination.REDSHIFT:
                    for sql, columns in ((cte, frozenset()), (declared, frozenset({"FLAG"}))):
                        with self.assertRaises(DbtPolicyFailure) as raised:
                            rewrite_model_sql(sql, destination, boolean_columns=columns)
                        self.assertEqual(
                            raised.exception.code, DbtErrorCode.COMPATIBILITY_UNSUPPORTED
                        )
                    # Without the declaration the column's type is unknown here.
                    rewrite_model_sql(declared, destination)
                else:
                    # Both render a boolean as 'true'/'false', as DuckDB does.
                    rewrite_model_sql(cte, destination)
                    rewrite_model_sql(
                        declared, destination, boolean_columns=frozenset({"flag"})
                    )


class DestinationNullOrderingTests(unittest.TestCase):
    """An ORDER BY without a nulls clause means different things on the
    warehouse and on DuckDB; the rewrite states the warehouse's meaning."""

    def test_rewrite_states_the_destination_null_placement(self) -> None:
        sql = "SELECT k, v, ROW_NUMBER() OVER (PARTITION BY k ORDER BY v DESC, k ASC) AS rn FROM {{ source('raw', 't') }}"
        snowflake = rewrite_model_sql(sql, Destination.SNOWFLAKE)
        self.assertIn("v DESC NULLS FIRST", snowflake)
        self.assertNotIn("k ASC NULLS FIRST", snowflake)
        databricks = rewrite_model_sql(sql, Destination.DATABRICKS)
        self.assertIn("k ASC NULLS FIRST", databricks)
        self.assertNotIn("v DESC NULLS FIRST", databricks)


class DbtRunnerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release_context = portable_five_backend_release()
        cls.release_dir = cls.release_context.__enter__()
        cls.addClassCleanup(cls.release_context.__exit__, None, None, None)
        cls.package = load_workspace_package(cls.release_dir, TASK_ID)

    def _synced_attempt(self, root: Path, name: str):
        attempt = root / name
        database = attempt / ".workspace-runtime" / "raw" / "primary.duckdb"
        database.parent.mkdir(parents=True)
        execution = run_local_sync(
            self.package,
            "primary",
            expected_terraform_graph(self.package),
            database,
        )
        self.assertTrue(execution.raw_state.strict_pass)
        return attempt, execution

    def test_private_gold_and_evaluator_are_preflighted_as_no_label_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            namespace = project_namespace(self.package, root / "attempt.duckdb")
            gold = self.package.gold.stage2_csv["primary"]
            evaluators = _validate_private_evidence(
                self.package,
                PopulationName.PRIMARY,
                gold,
                namespace=namespace,
                state_dir=root,
                limits=DbtRunnerLimits(),
            )
            self.assertEqual(set(evaluators), {"customer_rollup", "event_wide"})

            malformed = dict(gold)
            malformed["customer_rollup"] = "not,the,declared,columns\n1,2,3,4\n"
            with self.assertRaises(DbtTrustedFailure) as raised:
                _validate_private_evidence(
                    self.package,
                    PopulationName.PRIMARY,
                    malformed,
                    namespace=namespace,
                    state_dir=root,
                    limits=DbtRunnerLimits(),
                )
            self.assertEqual(raised.exception.code, DbtErrorCode.GOLD_MISSING)

            with patch(
                "elt_taskgen.training.dbt_runner._read_evaluator_sql",
                return_value="SELECT * FROM evaluator_relation_that_does_not_exist",
            ):
                with self.assertRaises(DbtTrustedFailure) as raised:
                    _validate_private_evidence(
                        self.package,
                        PopulationName.PRIMARY,
                        gold,
                        namespace=namespace,
                        state_dir=root,
                        limits=DbtRunnerLimits(),
                    )
            self.assertEqual(
                raised.exception.code,
                DbtErrorCode.EVALUATOR_INVALID,
            )

    def test_sources_require_exact_declared_cloud_schema_and_container(self) -> None:
        namespace = project_namespace(self.package, Path("candidate.duckdb"))
        cases = (
            (None, TASK_ID),
            ("wrong_schema", TASK_ID),
            ("AIRBYTE_SCHEMA", None),
            ("AIRBYTE_SCHEMA", "wrong_database"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (schema, database) in enumerate(cases):
                with self.subTest(schema=schema, database=database):
                    candidate = root / f"candidate_{index}"
                    _write_candidate(
                        candidate,
                        self.package,
                        schema=schema,
                        database=database,
                    )
                    with self.assertRaises(DbtPolicyFailure) as raised:
                        _prepare_execution_project(
                            candidate / "elt",
                            root / f"execution_{index}",
                            package=self.package,
                            namespace=namespace,
                        )
                    self.assertEqual(raised.exception.code, DbtErrorCode.GRAPH_INVALID)

    def test_malformed_yaml_in_any_candidate_file_is_a_project_failure(self) -> None:
        """A02 at the preparation boundary the scorer calls: every admitted
        YAML file, not only dbt_project.yml."""
        namespace = project_namespace(self.package, Path("candidate.duckdb"))
        bodies = {
            "unhashable key": "? [a, b]\n: value\n",
            "cyclic alias": "x-audit: &loop [*loop]\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "control"
            _write_candidate(control, self.package)
            self.assertIsInstance(
                _prepare_execution_project(
                    control / "elt",
                    root / "control_execution",
                    package=self.package,
                    namespace=namespace,
                ),
                str,
            )
            index = 0
            for relative in ("dbt_project.yml", "models/sources.yml", "models/schema.yml"):
                for label, body in bodies.items():
                    index += 1
                    with self.subTest(file=relative, shape=label):
                        candidate = root / f"candidate_{index}"
                        _write_candidate(candidate, self.package)
                        path = candidate / "elt" / relative
                        existing = path.read_text(encoding="utf-8") if path.exists() else "version: 2\n"
                        path.write_text(existing + body, encoding="utf-8")
                        with self.assertRaises(DbtPolicyFailure) as raised:
                            _prepare_execution_project(
                                candidate / "elt",
                                root / f"execution_{index}",
                                package=self.package,
                                namespace=namespace,
                            )
                        self.assertEqual(raised.exception.code, DbtErrorCode.PROJECT_INVALID)

    def test_a_ref_cannot_hide_a_double_from_the_text_cast_guard(self) -> None:
        """An upstream model's output is read by name through ref(), so the
        runner follows aliases across the whole project."""
        namespace = project_namespace(self.package, Path("candidate.duckdb"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (cast, admitted) in enumerate((("DOUBLE", False), ("INT", True))):
                with self.subTest(upstream=cast):
                    candidate = root / f"candidate_{index}"
                    _write_candidate(candidate, self.package)
                    models = candidate / "elt" / "models"
                    (models / "upstream.sql").write_text(
                        f"SELECT order_id, CAST(quantity AS {cast}) AS x "
                        "FROM {{ source('raw', 'order_items') }}\n",
                        encoding="utf-8",
                    )
                    (models / "downstream.sql").write_text(
                        "SELECT order_id, MD5(CAST(x AS VARCHAR)) AS h "
                        "FROM {{ ref('upstream') }}\n",
                        encoding="utf-8",
                    )
                    if admitted:
                        _prepare_execution_project(
                            candidate / "elt", root / f"execution_{index}",
                            package=self.package, namespace=namespace,
                        )
                        continue
                    with self.assertRaises(DbtPolicyFailure) as raised:
                        _prepare_execution_project(
                            candidate / "elt", root / f"execution_{index}",
                            package=self.package, namespace=namespace,
                        )
                    self.assertEqual(raised.exception.code, DbtErrorCode.COMPATIBILITY_UNSUPPORTED)

    def test_packages_and_hooks_are_rejected_before_dbt_executes(self) -> None:
        namespace = project_namespace(self.package, Path("candidate.duckdb"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages_candidate = root / "packages"
            _write_candidate(packages_candidate, self.package)
            (packages_candidate / "elt" / "packages.yml").write_text(
                "packages: []\n", encoding="utf-8"
            )
            hook_candidate = root / "hook"
            _write_candidate(hook_candidate, self.package)
            project = hook_candidate / "elt" / "dbt_project.yml"
            project.write_text(
                project.read_text(encoding="utf-8")
                + "on-run-start: ['select 1']\n",
                encoding="utf-8",
            )
            for index, candidate in enumerate((packages_candidate, hook_candidate)):
                with self.subTest(candidate=candidate.name):
                    with self.assertRaises(DbtPolicyFailure) as raised:
                        _prepare_execution_project(
                            candidate / "elt",
                            root / f"execution_{index}",
                            package=self.package,
                            namespace=namespace,
                        )
                    self.assertEqual(
                        raised.exception.code, DbtErrorCode.UNSAFE_ARTIFACT
                    )

    def test_candidate_cannot_redirect_dbt_discovery_or_runtime_paths(self) -> None:
        namespace = project_namespace(self.package, Path("candidate.duckdb"))
        mutations = (
            ("absolute_models", "model-paths: [models]", "model-paths: ['/tmp/models']"),
            ("traversal_models", "model-paths: [models]", "model-paths: ['../models']"),
            (
                "normalized_traversal_models",
                "model-paths: [models]",
                "model-paths: ['models/../models']",
            ),
            ("alternate_models", "model-paths: [models]", "model-paths: [other]"),
            ("underscore_alias", "model-paths: [models]", "model_paths: [models]"),
            ("target_path", "", "target-path: ../outside-target\n"),
            ("log_path", "", "log-path: /tmp/candidate-logs\n"),
            (
                "packages_install_path",
                "",
                "packages-install-path: ../candidate-packages\n",
            ),
            ("profiles_dir", "", "profiles-dir: ../candidate-profile\n"),
            ("macro_paths", "", "macro-paths: [../candidate-macros]\n"),
            ("clean_targets", "", "clean-targets: ['../../private']\n"),
            (
                "nested_external_location",
                "    +materialized: table\n",
                "    +materialized: table\n"
                "    +external_location: '../outside/table.parquet'\n",
            ),
            (
                "nested_generic_path",
                "    +materialized: table\n",
                "    +materialized: table\n    +path: '../outside'\n",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (name, old, replacement) in enumerate(mutations):
                with self.subTest(case=name):
                    candidate = root / name
                    _write_candidate(candidate, self.package)
                    project = candidate / "elt" / "dbt_project.yml"
                    text = project.read_text(encoding="utf-8")
                    project.write_text(
                        text.replace(old, replacement, 1) if old else text + replacement,
                        encoding="utf-8",
                    )
                    with self.assertRaises(DbtPolicyFailure) as raised:
                        _prepare_execution_project(
                            candidate / "elt",
                            root / f"execution_{index}",
                            package=self.package,
                            namespace=namespace,
                        )
                    self.assertEqual(
                        raised.exception.code, DbtErrorCode.UNSAFE_ARTIFACT
                    )

    def test_default_models_root_is_allowed_but_model_symlinks_are_not(self) -> None:
        namespace = project_namespace(self.package, Path("candidate.duckdb"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default_candidate = root / "default-models"
            _write_candidate(default_candidate, self.package)
            project = default_candidate / "elt" / "dbt_project.yml"
            project.write_text(
                project.read_text(encoding="utf-8").replace(
                    "model-paths: [models]\n", "", 1
                ),
                encoding="utf-8",
            )
            _prepare_execution_project(
                default_candidate / "elt",
                root / "default-execution",
                package=self.package,
                namespace=namespace,
            )

            symlink_candidate = root / "symlink-model"
            _write_candidate(symlink_candidate, self.package)
            outside = root / "outside.sql"
            outside.write_text("SELECT 1\n", encoding="utf-8")
            (symlink_candidate / "elt" / "models" / "linked.sql").symlink_to(
                outside
            )
            with self.assertRaises(DbtPolicyFailure) as raised:
                _prepare_execution_project(
                    symlink_candidate / "elt",
                    root / "symlink-execution",
                    package=self.package,
                    namespace=namespace,
                )
            self.assertEqual(raised.exception.code, DbtErrorCode.UNSAFE_ARTIFACT)

    def test_private_execution_rewrite_never_changes_candidate_bytes(self) -> None:
        namespace = project_namespace(self.package, Path("candidate.duckdb"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate"
            _write_candidate(candidate, self.package)
            before = {
                path.relative_to(candidate / "elt").as_posix(): path.read_bytes()
                for path in (candidate / "elt").rglob("*")
                if path.is_file()
            }
            execution = root / "execution"
            _prepare_execution_project(
                candidate / "elt",
                execution,
                package=self.package,
                namespace=namespace,
            )
            after = {
                path.relative_to(candidate / "elt").as_posix(): path.read_bytes()
                for path in (candidate / "elt").rglob("*")
                if path.is_file()
            }
            rewritten_sources = (execution / "models" / "sources.yml").read_bytes()
        self.assertEqual(before, after)
        self.assertNotEqual(before["models/sources.yml"], rewritten_sources)

    def test_manifest_requires_models_sources_dependencies_and_tables(self) -> None:
        namespace = project_namespace(self.package, Path("candidate.duckdb"))
        valid = _valid_manifest(self.package, namespace)
        _validate_manifest_graph(
            valid,
            package=self.package,
            namespace=namespace,
            project_name="candidate",
        )

        missing_model = copy.deepcopy(valid)
        missing_model["nodes"].pop("model.candidate.event_wide")
        bad_source_dependency = copy.deepcopy(valid)
        bad_source_dependency["nodes"]["model.candidate.event_wide"]["depends_on"] = {
            "nodes": ["source.candidate.raw.missing"]
        }
        no_source_dependency = copy.deepcopy(valid)
        no_source_dependency["nodes"]["model.candidate.event_wide"]["depends_on"] = {
            "nodes": []
        }
        wrong_materialization = copy.deepcopy(valid)
        wrong_materialization["nodes"]["model.candidate.event_wide"]["config"] = {
            "materialized": "view"
        }
        extra_model = copy.deepcopy(valid)
        extra_model["nodes"]["model.candidate.extra"] = {
            "resource_type": "model",
            "package_name": "candidate",
            "language": "sql",
            "name": "extra",
            "alias": "extra",
            "schema": namespace.mart_schema,
            "config": {"materialized": "table"},
            "depends_on": {"nodes": ["source.candidate.raw.customers"]},
        }
        colliding_alias = copy.deepcopy(valid)
        colliding_alias["nodes"]["model.candidate.extra"] = {
            "resource_type": "model",
            "package_name": "candidate",
            "language": "sql",
            "name": "extra",
            "alias": "event_wide",
            "schema": namespace.mart_schema,
            "config": {"materialized": "table"},
            "depends_on": {"nodes": ["source.candidate.raw.customers"]},
        }
        for name, manifest in (
            ("missing_model", missing_model),
            ("bad_source_dependency", bad_source_dependency),
            ("no_source_dependency", no_source_dependency),
            ("wrong_materialization", wrong_materialization),
            ("extra_model", extra_model),
            ("colliding_alias", colliding_alias),
        ):
            with self.subTest(case=name):
                with self.assertRaises(DbtPolicyFailure) as raised:
                    _validate_manifest_graph(
                        manifest,
                        package=self.package,
                        namespace=namespace,
                        project_name="candidate",
                    )
                self.assertEqual(raised.exception.code, DbtErrorCode.GRAPH_INVALID)

    def test_real_dbt_parse_compile_run_matches_both_marts(self) -> None:
        if not RUNTIME_CONFIG.python.is_file():
            self.skipTest(
                "provision with: uv sync --project runtime-images/dbt-duckdb --locked"
            )
        with tempfile.TemporaryDirectory() as directory:
            attempt, execution = self._synced_attempt(Path(directory), "correct")
            _write_candidate(attempt, self.package)
            result = run_dbt_project(
                self.package,
                "primary",
                attempt_dir=attempt,
                sync_execution=execution,
                runtime_config=RUNTIME_CONFIG,
            )

        self.assertEqual(result.dbt_project, 1.0)
        self.assertEqual(result.mart_reward, 1.0)
        self.assertTrue(result.raw_immutable)
        self.assertEqual(result.error_codes, ())
        self.assertEqual(result.mart_scores, {"customer_rollup": True, "event_wide": True})
        self.assertEqual(
            tuple(command.command for command in result.commands),
            ("parse", "compile", "run"),
        )
        self.assertTrue(all(command.succeeded for command in result.commands))

    def test_extra_physical_mart_column_is_rejected_before_gold_projection(self) -> None:
        if not RUNTIME_CONFIG.python.is_file():
            self.skipTest(
                "provision with: uv sync --project runtime-images/dbt-duckdb --locked"
            )
        with tempfile.TemporaryDirectory() as directory:
            attempt, execution = self._synced_attempt(Path(directory), "extra-column")
            _write_candidate(attempt, self.package, extra_mart_column=True)
            result = run_dbt_project(
                self.package,
                "primary",
                attempt_dir=attempt,
                sync_execution=execution,
                runtime_config=RUNTIME_CONFIG,
            )

        self.assertEqual(result.dbt_project, 1.0)
        self.assertEqual(result.mart_reward, 0.5)
        self.assertIn(DbtErrorCode.MART_SCHEMA_INVALID, result.error_codes)
        self.assertFalse(result.marts["event_wide"].exact_columns)

    def test_wrong_grain_with_duplicate_key_fails_mart(self) -> None:
        if not RUNTIME_CONFIG.python.is_file():
            self.skipTest(
                "provision with: uv sync --project runtime-images/dbt-duckdb --locked"
            )
        with tempfile.TemporaryDirectory() as directory:
            attempt, execution = self._synced_attempt(Path(directory), "wrong-grain")
            _write_candidate(attempt, self.package)
            (attempt / "elt" / "models" / "customer_rollup.sql").write_text(
                DUPLICATE_GRAIN, encoding="utf-8"
            )
            result = run_dbt_project(
                self.package,
                "primary",
                attempt_dir=attempt,
                sync_execution=execution,
                runtime_config=RUNTIME_CONFIG,
            )

        self.assertEqual(result.dbt_project, 1.0)
        self.assertEqual(result.mart_reward, 0.5)
        self.assertIn(DbtErrorCode.MART_KEY_INVALID, result.error_codes)
        self.assertFalse(result.marts["customer_rollup"].unique_key)

    def test_compile_failure_is_reported_without_reclassifying_the_harness(self) -> None:
        if not RUNTIME_CONFIG.python.is_file():
            self.skipTest(
                "provision with: uv sync --project runtime-images/dbt-duckdb --locked"
            )
        with tempfile.TemporaryDirectory() as directory:
            attempt, execution = self._synced_attempt(Path(directory), "compile-fail")
            _write_candidate(attempt, self.package)

            def command_result(*args, **kwargs):
                command = args[1]
                succeeded = command == "parse"
                return SimpleNamespace(
                    evidence=DbtCommandEvidence(
                        command=command,
                        succeeded=succeeded,
                        return_code=0 if succeeded else 1,
                        elapsed_ms=1,
                        output_sha256="0" * 64,
                        output_bytes=0,
                    ),
                    timed_out=False,
                )

            with (
                patch(
                    "elt_taskgen.training.dbt_runner._run_command",
                    side_effect=command_result,
                ),
                patch(
                    "elt_taskgen.training.dbt_runner._load_manifest",
                    return_value={},
                ),
                patch("elt_taskgen.training.dbt_runner._validate_manifest_graph"),
            ):
                result = run_dbt_project(
                    self.package,
                    "primary",
                    attempt_dir=attempt,
                    sync_execution=execution,
                    runtime_config=RUNTIME_CONFIG,
                )

        self.assertEqual(result.dbt_project, 0.0)
        self.assertTrue(result.raw_immutable)
        self.assertEqual(
            tuple(command.command for command in result.commands),
            ("parse", "compile"),
        )
        self.assertIn(DbtErrorCode.COMPILE_FAILED, result.error_codes)

    def test_successful_commands_without_persistent_marts_fail_closed(self) -> None:
        if not RUNTIME_CONFIG.python.is_file():
            self.skipTest(
                "provision with: uv sync --project runtime-images/dbt-duckdb --locked"
            )
        with tempfile.TemporaryDirectory() as directory:
            attempt, execution = self._synced_attempt(Path(directory), "missing-marts")
            _write_candidate(attempt, self.package)
            manifest = _valid_manifest(self.package, execution.namespace)

            def successful_command(*args, **kwargs):
                command = args[1]
                return SimpleNamespace(
                    evidence=DbtCommandEvidence(
                        command=command,
                        succeeded=True,
                        return_code=0,
                        elapsed_ms=1,
                        output_sha256="0" * 64,
                        output_bytes=0,
                    ),
                    timed_out=False,
                )

            with (
                patch(
                    "elt_taskgen.training.dbt_runner._run_command",
                    side_effect=successful_command,
                ),
                patch(
                    "elt_taskgen.training.dbt_runner._load_manifest",
                    return_value=manifest,
                ),
            ):
                result = run_dbt_project(
                    self.package,
                    "primary",
                    attempt_dir=attempt,
                    sync_execution=execution,
                    runtime_config=RUNTIME_CONFIG,
                )

        self.assertEqual(result.dbt_project, 1.0)
        self.assertEqual(result.mart_reward, 0.0)
        self.assertTrue(result.raw_immutable)
        self.assertIn(DbtErrorCode.MART_MISSING, result.error_codes)
        self.assertTrue(
            all(not evidence.persistent_table for evidence in result.marts.values())
        )

    def test_airbyte_metadata_mutation_changes_full_physical_raw_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, execution = self._synced_attempt(Path(directory), "metadata-mutation")
            before = _fingerprint_physical_raw_state(
                execution.database_path, execution.namespace
            )
            connection = duckdb.connect(str(execution.database_path))
            try:
                connection.execute(
                    'ALTER TABLE AIRBYTE_SCHEMA.customers '
                    'ADD COLUMN "_AIRBYTE_RAW_ID" VARCHAR'
                )
                connection.execute(
                    'UPDATE AIRBYTE_SCHEMA.customers SET "_AIRBYTE_RAW_ID" = '
                    "customer_id::VARCHAR"
                )
            finally:
                connection.close()
            after = _fingerprint_physical_raw_state(
                execution.database_path, execution.namespace
            )
        self.assertNotEqual(before, after)

    def test_early_dbt_failure_reports_raw_mutation(self) -> None:
        if not RUNTIME_CONFIG.python.is_file():
            self.skipTest(
                "provision with: uv sync --project runtime-images/dbt-duckdb --locked"
            )
        with tempfile.TemporaryDirectory() as directory:
            attempt, execution = self._synced_attempt(Path(directory), "early-mutation")
            _write_candidate(attempt, self.package)

            def mutate_then_fail(*args, **kwargs):
                connection = duckdb.connect(str(execution.database_path))
                try:
                    connection.execute(
                        'ALTER TABLE AIRBYTE_SCHEMA.customers '
                        'ADD COLUMN "_AIRBYTE_META" VARCHAR'
                    )
                finally:
                    connection.close()
                return SimpleNamespace(
                    evidence=DbtCommandEvidence(
                        command="parse",
                        succeeded=False,
                        return_code=1,
                        elapsed_ms=1,
                        output_sha256="0" * 64,
                        output_bytes=0,
                    ),
                    timed_out=False,
                )

            with patch(
                "elt_taskgen.training.dbt_runner._run_command",
                side_effect=mutate_then_fail,
            ):
                result = run_dbt_project(
                    self.package,
                    "primary",
                    attempt_dir=attempt,
                    sync_execution=execution,
                    runtime_config=RUNTIME_CONFIG,
                )

        self.assertEqual(result.dbt_project, 0.0)
        self.assertFalse(result.raw_immutable)
        self.assertIn(DbtErrorCode.PARSE_FAILED, result.error_codes)
        self.assertIn(DbtErrorCode.RAW_MUTATED, result.error_codes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

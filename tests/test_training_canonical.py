"""The canonical Terraform + dbt artifact derived from a task's private answer
key, and the transform battery gate that judges its reachability record.

The batch20 lesson (2026-09-15): a packaged, witness-agreed task
(dbt__twitter_ads) had every mart refused by the portable dbt subset, so its
transform reward on the RLVR workspace channel was unreachable and nothing
in the ladder noticed. These tests pin the mechanical derivation (contract ->
main.tf that compiles to the private intent graph; reference SQL -> dbt models
the subset admits and that reproduce gold), the record's fail-closed
semantics, and the gate's three answers (current and reachable; stale, which
is a currency refusal; present and short, which is a judgement).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import duckdb

from elt_taskgen.destinations import Destination
from elt_taskgen.models import ColumnType
from elt_taskgen.training import canonical
from elt_taskgen.training.dbt_runner import (
    DBT_COMPATIBILITY_SUBSET_VERSION,
    DbtRunnerLimits,
    _fetch_bounded_rows,
    rewrite_model_sql,
)
from elt_taskgen.training.package import load_workspace_package
from elt_taskgen.training.terraform_intent import (
    compile_terraform_intent,
    expected_terraform_graph,
)
from elt_taskgen.verification import gates, upstream_eval
from elt_taskgen.verification.strict_diagnostic import load_sources_duckdb_strict

try:
    from workspace_proxy_fixture import add_extra_destinations
except ImportError:  # running as tests.test_training_canonical
    from tests.workspace_proxy_fixture import add_extra_destinations

FIXTURE_RELEASE = Path(__file__).resolve().parent / "fixtures" / "semantic_gate" / "release"
TASK_ID = "gate__five_backend_probe"
DBT_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "runtime-images" / "dbt-duckdb"
_RUNTIME_PRESENT = (DBT_RUNTIME_ROOT / ".venv" / "bin" / "python").is_file()


class CanonicalDialectDefaultsTests(unittest.TestCase):
    """The reference SQL leans on DuckDB defaults the warehouses do not share;
    the rendered model states them."""

    def test_null_ordering_and_text_date_trunc_are_explicit(self) -> None:
        sql = (
            "SELECT t.k AS k, FIRST_VALUE(t.v) OVER (PARTITION BY t.k ORDER BY t.v DESC, t.k ASC) AS top_v, "
            "date_trunc('day', t.d) AS day FROM t ORDER BY t.k"
        )
        types = {"t": {"d": ColumnType.TIMESTAMP, "v": ColumnType.FLOAT, "k": ColumnType.TEXT}}
        rendered = canonical.render_canonical_model(
            sql, {"t"}, Destination.SNOWFLAKE, column_types=types
        )
        self.assertIn("DESC NULLS LAST", rendered)
        # Every physical read is cast to its declared type: the warehouse
        # column is whatever the connector inferred.
        self.assertIn("CAST(T.D AS TIMESTAMP)", rendered.upper())
        self.assertIn("CAST(T.V AS DOUBLE)", rendered.upper())
        rendered = canonical.render_canonical_model(
            sql, {"t"}, Destination.DATABRICKS, column_types=types
        )
        # Databricks puts NULLs first on ASC by default, so ASC keys get the clause there.
        self.assertIn("AS STRING) ASC NULLS LAST", rendered)


class RedshiftRoundingTests(unittest.TestCase):
    """Redshift's ROUND over a DOUBLE quotient returns 0.5877999999999999 where
    the other engines return 0.5878; the rendered model rounds a DECIMAL
    quotient there so the digits agree everywhere."""

    def test_redshift_rounds_a_decimal_quotient(self) -> None:
        sql = "SELECT t.k AS k, ROUND(CAST(t.top AS DOUBLE) / NULLIF(t.total, 0), 4) AS share FROM a AS t"
        types = {"a": {"k": ColumnType.TEXT, "top": ColumnType.INTEGER, "total": ColumnType.INTEGER}}
        redshift = canonical.render_canonical_model(sql, {"a"}, Destination.REDSHIFT, column_types=types)
        self.assertIn("AS DECIMAL(38, 9)) / NULLIF", redshift)
        self.assertNotIn("AS DOUBLE PRECISION) / NULLIF", redshift)
        snowflake = canonical.render_canonical_model(sql, {"a"}, Destination.SNOWFLAKE, column_types=types)
        self.assertIn("AS DOUBLE) / NULLIF", snowflake)


class CanonicalDerivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_workspace_package(FIXTURE_RELEASE, TASK_ID)

    def test_canonical_main_tf_compiles_to_the_expected_graph(self) -> None:
        """Five source kinds and the snowflake destination, from the contract
        alone: the offline compiler must accept it and the graph must EQUAL
        the private expectation (labels prefixed, secrets as variables)."""
        text = canonical.render_canonical_main_tf(self.package.airbyte_contract)
        self.assertIn('variable "workspace_id" {}', text)
        self.assertNotIn("default", text)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.tf"
            path.write_text(text, encoding="utf-8")
            graph = compile_terraform_intent(self.package, path)
        self.assertEqual(graph, expected_terraform_graph(self.package))

    def test_canonical_models_are_admitted_and_reproduce_gold(self) -> None:
        """Every mart, every destination dialect: the derived model passes the
        grader's portable-subset rewrite and, executed on the strict-loaded
        primary population, matches the frozen stage-2 gold."""
        package = self.package
        task = package.task
        tables = frozenset(table.name for table in task.tables)
        json_columns = frozenset(
            column.name for table in task.tables for column in table.columns if column.type.value == "json"
        )
        for destination in Destination:
            connection = duckdb.connect(":memory:")
            try:
                load_sources_duckdb_strict(task, package.source_root("primary"), connection)
                for mart in task.marts:
                    with self.subTest(destination=destination.value, mart=mart.name):
                        model = canonical.render_canonical_model(
                            dict(task.reference.sql_by_mart)[mart.name], tables, destination
                        )
                        self.assertTrue(model.startswith("{{ config(materialized='table') }}\n"))
                        rewritten = rewrite_model_sql(model, destination, json_columns=json_columns)
                        body = "\n".join(
                            line for line in rewritten.splitlines() if not line.startswith("{{ config")
                        )
                        for name in tables:
                            body = body.replace("{{ source('raw', '" + name + "') }}", '"' + name + '"')
                        columns, rows = _fetch_bounded_rows(connection, body, limits=DbtRunnerLimits())
                        self.assertTrue(
                            upstream_eval.compare_mart(
                                package.gold.stage2_csv["primary"][mart.name],
                                rows,
                                mart,
                                actual_columns=columns,
                            ),
                            f"{mart.name} on {destination.value} does not reproduce gold",
                        )
            finally:
                connection.close()

    def test_source_placeholders_are_token_exact_for_prefixed_table_names(self) -> None:
        sql = (
            'SELECT e."id" AS "id", COUNT(b."id") AS "n" FROM "employees" AS e '
            'LEFT JOIN "employees_absences_balance" AS b ON b."employee_id" = e."id" '
            'GROUP BY e."id" ORDER BY e."id"'
        )
        tables = {"employees", "employees_absences_balance"}
        for destination in Destination:
            with self.subTest(destination=destination.value):
                model = canonical.render_canonical_model(sql, tables, destination)
                self.assertIn("{{ source('raw', 'employees') }}", model)
                self.assertIn("{{ source('raw', 'employees_absences_balance') }}", model)
                self.assertNotIn("eltsrc_", model)
                self.assertNotIn("ORDER BY", model.upper())
        with self.assertRaises(canonical.CanonicalArtifactError):
            canonical.render_canonical_model(sql, {"employees"}, Destination.SNOWFLAKE)

    def test_cte_scoping_and_ordered_limits_are_preserved(self) -> None:
        """A CTE named after a table reads the TABLE inside its own body and is
        the CTE everywhere after it; a later CTE may read an earlier one; an
        ORDER BY a LIMIT depends on is kept (review of 2026-09-15)."""
        tables = {"orders", "customers"}
        model = canonical.render_canonical_model(
            "WITH orders AS (SELECT * FROM orders WHERE amount > 0) "
            "SELECT o.id AS id FROM orders AS o",
            tables,
            Destination.SNOWFLAKE,
        )
        self.assertEqual(model.count("{{ source('raw', 'orders') }}"), 1)
        self.assertIn("FROM orders AS o", model)
        model = canonical.render_canonical_model(
            "WITH a AS (SELECT id FROM customers), b AS (SELECT id FROM a) SELECT id FROM b",
            tables,
            Destination.SNOWFLAKE,
        )
        self.assertIn("FROM a)", model)
        self.assertIn("FROM b", model)
        model = canonical.render_canonical_model(
            "SELECT id FROM customers ORDER BY id DESC LIMIT 3", tables, Destination.SNOWFLAKE
        )
        self.assertIn("ORDER BY", model.upper())
        self.assertIn("LIMIT 3", model.upper())

    def test_score_classification_never_turns_a_harness_failure_into_a_shortfall(self) -> None:
        from tests.canonical_doubles import full_workspace_result
        from elt_taskgen.training.contract import WorkspaceErrorCode, WORKSPACE_ERROR_CLASS_BY_CODE
        from elt_taskgen.training.models import WorkspaceFailure

        full = full_workspace_result(self.package.task)
        self.assertEqual(canonical.classify_score(full), canonical.OUTCOME_REACHABLE)

        def with_failure(code):
            return full.model_copy(update={
                "reward": None,
                "valid_submission": True,
                "failure": WorkspaceFailure(classification=WORKSPACE_ERROR_CLASS_BY_CODE[code], error_code=code),
            })

        for code in (
            WorkspaceErrorCode.HARNESS_INTERNAL,
            WorkspaceErrorCode.INFRASTRUCTURE_UNAVAILABLE,
            WorkspaceErrorCode.REAL_RUNTIME_FAILED,
        ):
            with self.subTest(code=code.value):
                self.assertEqual(canonical.classify_score(with_failure(code)), canonical.OUTCOME_ENVIRONMENT)
        self.assertEqual(
            canonical.classify_score(with_failure(WorkspaceErrorCode.TASK_PACKAGE_INVALID)),
            canonical.OUTCOME_TASK_PACKAGE,
        )
        populations = dict(full.populations)
        primary = populations["primary"]
        populations["primary"] = primary.model_copy(update={"dbt_project": 0.0, "mart_reward": 0.0, "end_to_end_reward": 0.0, "error_codes": ("dbt_timeout",)})
        timed_out = full.model_copy(update={"populations": populations, "reward": 0.0})
        self.assertEqual(canonical.classify_score(timed_out), canonical.OUTCOME_ENVIRONMENT)
        populations["primary"] = primary.model_copy(update={"mart_reward": 0.5, "end_to_end_reward": 0.5, "error_codes": ("dbt_compatibility_unsupported",)})
        short = full.model_copy(update={"populations": populations, "reward": 0.5})
        self.assertEqual(canonical.classify_score(short), canonical.OUTCOME_SHORTFALL)

    def test_project_files_cover_every_mart_and_name_every_table(self) -> None:
        files = canonical.render_canonical_project(self.package)
        self.assertEqual(
            set(files),
            {"main.tf", "dbt_project.yml", "models/sources.yml"}
            | {f"models/{mart.name}.sql" for mart in self.package.task.marts},
        )
        for table in self.package.task.tables:
            self.assertIn(f"- name: {json.dumps(table.name)}", files["models/sources.yml"])
        self.assertIn("profile: elt_taskgen", files["dbt_project.yml"])

    def test_a_record_without_a_result_needs_a_render_error_and_is_never_reachable(self) -> None:
        record = canonical.build_render_failure_record(
            self.package.task,
            destination=self.package.destination,
            runtime_config=canonical.default_dbt_runtime_config(),
            error="CanonicalArtifactError: unknown source key 'ftp'",
        )
        self.assertFalse(record.reachable)
        self.assertIsNone(record.result)
        with self.assertRaises(ValueError):
            record.model_copy(update={"reachable": True}).model_validate(
                record.model_copy(update={"reachable": True}).model_dump()
            )
        with self.assertRaises(ValueError):
            canonical.CanonicalReachabilityRecord.model_validate(
                {**record.model_dump(), "render_error": ""}
            )


class MultiDestinationTests(unittest.TestCase):
    """A task ships one public bundle and a configuration per destination, and
    the portable subset's rules differ per destination, so reachability is
    proved once per destination rather than once per task (2026-09-17).
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="canonical-multi-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.release = self.root / "release"
        shutil.copytree(FIXTURE_RELEASE, self.release)
        # Read the task while the copy still matches the fixture's seal.
        self.task = load_workspace_package(self.release, TASK_ID).task
        self.destinations = add_extra_destinations(self.release, self.task)
        self.answer_key = self.release / "private" / TASK_ID / "answer_key"

    def test_the_package_lists_and_loads_every_shipped_destination(self) -> None:
        from elt_taskgen.training.package import shipped_destinations

        self.assertEqual(
            tuple(item.value for item in shipped_destinations(self.answer_key)),
            self.destinations,
        )
        namespaces = set()
        graphs: list[str] = []
        for name in self.destinations:
            with self.subTest(destination=name):
                # verify=False: the fixture's manifest seals the single-destination
                # freeze it was made from, which the next test asserts directly.
                package = load_workspace_package(
                    self.release, TASK_ID, destination=name, verify=False
                )
                self.assertEqual(package.destination.value, name)
                self.assertEqual(
                    package.airbyte_contract["destination"]["key"], name
                )
                self.assertTrue(package.logical_namespace)
                namespaces.add(package.logical_namespace)
                # Each destination compiles its own Terraform from its own contract.
                text = canonical.render_canonical_main_tf(package.airbyte_contract)
                path = self.root / f"main-{name}.tf"
                path.write_text(text, encoding="utf-8")
                graph = compile_terraform_intent(package, path)
                self.assertEqual(graph, expected_terraform_graph(package))
                graphs.append(json.dumps(graph, sort_keys=True, default=str))
        # The mart names are a property of the task, so every destination
        # shares the logical namespace; what changes is the connector contract
        # and therefore the intent graph the solver's Terraform must compile to.
        self.assertEqual(len(namespaces), 1)
        self.assertEqual(len(set(graphs)), len(self.destinations))

    def test_a_destination_added_after_the_freeze_is_refused(self) -> None:
        """The release seal covers the shipped destination set: files added to
        a frozen release are not loadable, so this test's own fixture could
        never be mistaken for a release."""
        from elt_taskgen.training.package import WorkspacePackageError

        with self.assertRaises(WorkspacePackageError):
            load_workspace_package(self.release, TASK_ID, destination="redshift")

    def test_every_shipped_destination_needs_its_own_record(self) -> None:
        from tests.canonical_doubles import write_canonical_reachability

        workspace = self.root / "workspace"
        task_dir = workspace / "tasks" / TASK_ID
        answer_key = task_dir / "answer_key"
        shutil.copytree(self.answer_key, answer_key)
        write_canonical_reachability(workspace, self.task)
        self.assertIsNone(
            canonical.load_canonical_reachability(self.task, task_dir, answer_key).problem
        )
        canonical.assert_canonical_ready(self.task, answer_key)

        canonical.remove_scratch_tree(canonical.artifact_dir(answer_key, "redshift"))
        problem = canonical.load_canonical_reachability(
            self.task, task_dir, answer_key
        ).problem
        self.assertIsNotNone(problem)
        self.assertIn("redshift", problem)
        with self.assertRaisesRegex(ValueError, "redshift"):
            canonical.assert_canonical_ready(self.task, answer_key)


class CanonicalGateTests(unittest.TestCase):
    """The gate judges the record; it never produces it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_workspace_package(FIXTURE_RELEASE, TASK_ID)

    def _workspace(self) -> tuple[Path, Path, Path]:
        root = Path(tempfile.mkdtemp(prefix="canonical-gate-"))
        self.addCleanup(shutil.rmtree, root, True)
        task_dir = root / "tasks" / TASK_ID
        answer_key = task_dir / "answer_key"
        (answer_key / "runtime").mkdir(parents=True)
        # The record is bound to the bundle-root connector contract.
        shutil.copyfile(self.package.airbyte_contract_path, answer_key / "runtime" / "airbyte_connector_contract.json")
        return root, task_dir, answer_key

    def test_missing_record_is_a_currency_refusal(self) -> None:
        root, _, _ = self._workspace()
        result = gates._gate_canonical_reachability(self.package.task, root)
        self.assertFalse(result.passed)
        self.assertIn("re-run validate-t", result.details)
        self.assertIn("canonical-reachability", gates.VARIANT_GATE_NAMES[gates.TaskVariant.TRANSFORM])

    def test_render_failure_record_is_a_judgement(self) -> None:
        root, task_dir, answer_key = self._workspace()
        record = canonical.build_render_failure_record(
            self.package.task,
            destination=self.package.destination,
            runtime_config=canonical.default_dbt_runtime_config(),
            error="CanonicalArtifactError: reference SQL reads 'ghost'",
            airbyte_contract_sha256=self.package.airbyte_contract_sha256,
        )
        canonical.record_canonical_reachability(
            task_dir=task_dir, answer_key_dir=answer_key,
            records={record.destination: record}, files={record.destination: {}},
        )
        result = gates._gate_canonical_reachability(self.package.task, root)
        self.assertFalse(result.passed)
        self.assertIn("could not be derived", result.details)
        self.assertNotIn("re-run validate-t", result.details)

    def test_a_changed_connector_contract_stales_the_record_everywhere(self) -> None:
        from tests.canonical_doubles import write_canonical_reachability

        root, task_dir, answer_key = self._workspace()
        write_canonical_reachability(root, self.package.task)
        self.assertTrue(gates._gate_canonical_reachability(self.package.task, root).passed)
        canonical.assert_canonical_ready(self.package.task, answer_key)
        contract = answer_key / "runtime" / "airbyte_connector_contract.json"
        document = json.loads(contract.read_text(encoding="utf-8"))
        document["sources"] = [{"key": "postgres"}]
        contract.write_text(json.dumps(document), encoding="utf-8")
        result = gates._gate_canonical_reachability(self.package.task, root)
        self.assertFalse(result.passed)
        self.assertIn("different connector contract", result.details)
        self.assertIn("re-run validate-t", result.details)
        with self.assertRaisesRegex(ValueError, "different connector contract"):
            canonical.assert_canonical_ready(self.package.task, answer_key)

    def test_quoted_error_text_never_reads_as_a_currency_refusal(self) -> None:
        from elt_taskgen import repair

        root, task_dir, answer_key = self._workspace()
        from tests.canonical_doubles import write_canonical_reachability

        write_canonical_reachability(root, self.package.task)
        report = canonical.CanonicalReachabilityReport.model_validate_json(
            (task_dir / canonical.REACHABILITY_EVIDENCE_REL).read_text(encoding="utf-8")
        )
        record = next(iter(report.records.values()))
        failed = record.model_copy(update={"reachable": False, "result": None, "files": {}, "render_error": "ValueError: gold drift; re-run reference-run"})
        failed = canonical.CanonicalReachabilityRecord.model_validate(failed.model_dump())
        canonical.record_canonical_reachability(
            task_dir=task_dir, answer_key_dir=answer_key,
            records={failed.destination: failed}, files={failed.destination: {}},
        )
        verdict = gates._gate_canonical_reachability(self.package.task, root)
        self.assertFalse(verdict.passed)
        self.assertFalse(repair.is_currency_refusal(verdict))

    def test_stale_subset_version_is_a_currency_refusal(self) -> None:
        root, task_dir, answer_key = self._workspace()
        record = canonical.build_render_failure_record(
            self.package.task,
            destination=self.package.destination,
            runtime_config=canonical.default_dbt_runtime_config(),
            error="x",
        )
        stale = record.model_copy(update={"compatibility_subset": "portable-dbt-sql-v1"})
        canonical.record_canonical_reachability(
            task_dir=task_dir, answer_key_dir=answer_key,
            records={stale.destination: stale}, files={stale.destination: {}},
        )
        result = gates._gate_canonical_reachability(self.package.task, root)
        self.assertFalse(result.passed)
        self.assertIn(DBT_COMPATIBILITY_SUBSET_VERSION, result.details)
        self.assertIn("re-run validate-t", result.details)


@unittest.skipUnless(
    _RUNTIME_PRESENT,
    "provision the pinned dbt runtime with: uv sync --project runtime-images/dbt-duckdb --locked",
)
class CanonicalRealScoreEveryDestinationTests(unittest.TestCase):
    """Reachability is a per-destination claim, so prove it per destination.

    This is the test the 2026-09-17 finding needed: Databricks and Redshift
    both scored 0.0 on every population because their raw landing schema is
    also the dbt target schema and the mart read as a raw mutation. One
    destination passing says nothing about the others.
    """

    def test_the_canonical_artifact_scores_one_on_every_shipped_destination(self) -> None:
        from tests.workspace_proxy_fixture import portable_five_backend_release

        with portable_five_backend_release() as release_dir:
            task = load_workspace_package(release_dir, TASK_ID).task
            destinations = add_extra_destinations(release_dir, task)
            self.assertEqual(destinations, ("snowflake", "databricks", "redshift"))
            for name in destinations:
                with self.subTest(destination=name):
                    package = load_workspace_package(
                        release_dir, TASK_ID, destination=name, verify=False
                    )
                    files = canonical.render_canonical_project(package)
                    scratch = Path(tempfile.mkdtemp(prefix=f"canonical-{name}-"))
                    try:
                        sealed, result = canonical.score_canonical_artifact(
                            package,
                            files,
                            scratch=scratch,
                            runtime_config=canonical.default_dbt_runtime_config(),
                        )
                    finally:
                        canonical.remove_scratch_tree(scratch)
                    self.assertEqual(
                        result.reward,
                        1.0,
                        canonical.shortfall_summary(result),
                    )
                    self.assertEqual(
                        set(result.graded_populations),
                        {"primary", "resampled", "counterfactual", "stress"},
                    )
                    record = canonical.build_reachability_record(
                        package,
                        files=files,
                        sealed=sealed,
                        result=result,
                        runtime_config=canonical.default_dbt_runtime_config(),
                    )
                    self.assertTrue(record.reachable)
                    self.assertEqual(record.destination, name)


@unittest.skipUnless(
    _RUNTIME_PRESENT,
    "provision the pinned dbt runtime with: uv sync --project runtime-images/dbt-duckdb --locked",
)
class CanonicalRealScoreTests(unittest.TestCase):
    def test_real_canonical_artifact_scores_one_and_the_gate_accepts_it(self) -> None:
        """No mocks: the fixture release, the real HCL compiler, the trusted
        sync and the pinned dbt Core. Then the recorded artifact is what the
        gate accepts, and a tampered byte is what it refuses."""
        from tests.workspace_proxy_fixture import portable_five_backend_release

        with portable_five_backend_release() as release_dir:
            package = load_workspace_package(release_dir, TASK_ID)
            files = canonical.render_canonical_project(package)
            scratch = Path(tempfile.mkdtemp(prefix="canonical-real-"))
            try:
                sealed, result = canonical.score_canonical_artifact(
                    package, files, scratch=scratch, runtime_config=canonical.default_dbt_runtime_config()
                )
            finally:
                canonical.remove_scratch_tree(scratch)
            self.assertEqual(result.reward, 1.0)
            self.assertEqual(
                set(result.graded_populations), {"primary", "resampled", "counterfactual", "stress"}
            )
            record = canonical.build_reachability_record(
                package, files=files, sealed=sealed, result=result,
                runtime_config=canonical.default_dbt_runtime_config(),
            )
            self.assertTrue(record.reachable)
            root = Path(tempfile.mkdtemp(prefix="canonical-gate-real-"))
            self.addCleanup(shutil.rmtree, root, True)
            task_dir = root / "tasks" / TASK_ID
            answer_key = task_dir / "answer_key"
            (answer_key / "runtime").mkdir(parents=True)
            shutil.copyfile(package.airbyte_contract_path, answer_key / "runtime" / "airbyte_connector_contract.json")
            canonical.record_canonical_reachability(
                task_dir=task_dir, answer_key_dir=answer_key,
                records={record.destination: record}, files={record.destination: files},
            )
            self.assertIsNone(canonical.load_canonical_reachability(package.task, task_dir, answer_key).problem)
            verdict = gates._gate_canonical_reachability(package.task, root)
            self.assertTrue(verdict.passed, verdict.details)
            self.assertEqual(
                verdict.evidence[f"{package.destination.value}:primary:end_to_end"], "1.0"
            )
            # A byte changed after scoring detaches the record from the artifact.
            model = canonical.artifact_dir(answer_key, package.destination) / "elt" / "models" / f"{package.task.marts[0].name}.sql"
            model.write_text(model.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
            verdict = gates._gate_canonical_reachability(package.task, root)
            self.assertFalse(verdict.passed)
            self.assertIn("changed since scoring", verdict.details)
            self.assertIn("re-run validate-t", verdict.details)


@unittest.skipUnless(
    _RUNTIME_PRESENT,
    "provision the pinned dbt runtime with: uv sync --project runtime-images/dbt-duckdb --locked",
)
class CanonicalPackagingControlTests(unittest.TestCase):
    def test_the_packaging_control_rescores_packaged_bytes_without_mocks(self) -> None:
        """The fresh-copy control once referenced two names its module never
        imported and failed every package (review of 2026-09-15). Run it for
        real on a package-shaped tree: packaged canonical bytes pass, a byte
        changed after scoring fails."""
        from elt_taskgen.export import package_verification as pv

        release = FIXTURE_RELEASE / "private" / TASK_ID
        root = Path(tempfile.mkdtemp(prefix="canonical-control-"))
        self.addCleanup(canonical.remove_scratch_tree, root)
        fresh = root / "fresh" / "package"
        private = fresh / "private" / TASK_ID
        shutil.copytree(FIXTURE_RELEASE / "public" / TASK_ID, fresh / "public" / TASK_ID)
        shutil.copytree(release / "oracle", fresh / "public" / f"{TASK_ID}__t" / "warehouse")
        shutil.copytree(release / "answer_key", private / "answer_key")
        shutil.copytree(release / "populations", private / "populations")
        shutil.copyfile(release / "semantic" / "task_ir.json", private / "task_ir.json")

        package = load_workspace_package(FIXTURE_RELEASE, TASK_ID)
        files = canonical.render_canonical_project(package)
        scratch = root / "score"
        sealed, result = canonical.score_canonical_artifact(
            package, files, scratch=scratch, runtime_config=canonical.default_dbt_runtime_config()
        )
        canonical.remove_scratch_tree(scratch)
        record = canonical.build_reachability_record(
            package, files=files, sealed=sealed, result=result,
            runtime_config=canonical.default_dbt_runtime_config(),
        )
        self.assertTrue(record.reachable)
        canonical.record_canonical_reachability(
            task_dir=root / "task", answer_key_dir=private / "answer_key",
            records={record.destination: record}, files={record.destination: files},
        )
        canonical.assert_canonical_ready(package.task, private / "answer_key")

        control = pv._canonical_reachability_control(fresh, TASK_ID)
        self.assertEqual(control.name, pv.CANONICAL_CONTROL_NAME)
        self.assertTrue(control.passed, control.observed)

        model = canonical.artifact_dir(private / "answer_key", package.destination) / "elt" / "models" / f"{package.task.marts[0].name}.sql"
        model.write_text(model.read_text(encoding="utf-8").replace("SELECT", "SELECT DISTINCT", 1), encoding="utf-8")
        control = pv._canonical_reachability_control(fresh, TASK_ID)
        self.assertFalse(control.passed)

    def test_the_control_covers_every_shipped_destination_not_the_bundle_root(self) -> None:
        """A package ships a configuration per destination. The control that
        only looked at the bundle root would have passed a package whose
        Databricks and Redshift marts were unreachable, which is what they
        were until 2026-09-17."""
        from elt_taskgen.export import package_verification as pv

        release = FIXTURE_RELEASE / "private" / TASK_ID
        root = Path(tempfile.mkdtemp(prefix="canonical-control-multi-"))
        self.addCleanup(canonical.remove_scratch_tree, root)
        fresh = root / "fresh" / "package"
        private = fresh / "private" / TASK_ID
        shutil.copytree(FIXTURE_RELEASE / "public" / TASK_ID, fresh / "public" / TASK_ID)
        shutil.copytree(release / "oracle", fresh / "public" / f"{TASK_ID}__t" / "warehouse")
        shutil.copytree(release / "answer_key", private / "answer_key")
        shutil.copytree(release / "populations", private / "populations")
        shutil.copyfile(release / "semantic" / "task_ir.json", private / "task_ir.json")
        task = load_workspace_package(FIXTURE_RELEASE, TASK_ID).task
        add_extra_destinations(fresh, task)

        # No destination carries a packaged record, so the control names all
        # three and scores none of them.
        control = pv._canonical_reachability_control(fresh, TASK_ID)
        self.assertFalse(control.passed)
        for name in ("snowflake", "databricks", "redshift"):
            self.assertIn(f"{name}: ", control.observed)


if __name__ == "__main__":
    unittest.main()

"""Tests for export/eltbench.py and export/release.py.

WHY THIS EXISTS
The exporter must emit the REAL upstream ELT-Bench contract (verified key
shapes, exact schema CSV header, every-column ORDER BY) and the release
freezer must be a pure choke point: refuse anything not currently accepted,
ship public and private trees disjointly, and never leak answer-key content,
seeds, or reference SQL into the public artifact. Sibling modules (gold,
engine, selection) are still scaffolds, so their contract shapes are stubbed
here exactly per docs/INTERFACES.md.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from pydantic import BaseModel

from elt_taskgen import demo_fixture
from elt_taskgen.engine import variant_gate_stage
from elt_taskgen.export import eltbench, release
from elt_taskgen.models import (
    Origin,
    RLVR_TASK_VARIANTS,
    TaskIR,
    TaskRevision,
    TaskVariant,
    task_to_json,
    variant_task_id,
)
from elt_taskgen.provenance import (
    IngestProvenance,
    ProvenanceArtifact,
    SourceIdentity,
    lineage_root_hash,
    publish_or_confirm,
    release_provenance_rel,
)
from elt_taskgen.verification.gates import (
    GATE_NAMES,
    ROSTER_DIGEST,
    SCORER_VERSION,
    VARIANT_GATE_NAMES,
)


class FakeGold(BaseModel):
    """Structural stand-in for reference.gold.GoldBundle (per INTERFACES.md)."""

    task_id: str
    task_content_hash: str
    stage1: dict[str, dict[str, int]]
    stage2_csv: dict[str, dict[str, str]]
    file_hashes: dict[str, str] = {}


GOLD_CSV = "customer_id,completed_order_count,total_spend\n1,1,45.0\n2,0,0.0\n"


def make_gold(task: TaskIR) -> FakeGold:
    counts = {"customers": 1000, "orders": 3000, "order_items": 9000}
    return FakeGold(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        stage1={"primary": counts, "counterfactual": {"customers": 3, "orders": 2, "order_items": 4}},
        stage2_csv={
            "primary": {demo_fixture.MART_NAME: GOLD_CSV},
            "counterfactual": {demo_fixture.MART_NAME: "customer_id,completed_order_count,total_spend\n"},
        },
    )


class FakeReport:
    def __init__(
        self,
        verdict: str,
        content_hash: str,
        gates=None,
        *,
        task_id: str | None = None,
        scorer_version: str = SCORER_VERSION,
        roster_digest: str = ROSTER_DIGEST,
    ):
        self.verdict = verdict
        self.content_hash = content_hash
        names = GATE_NAMES if gates is None else tuple(gates)
        # Mirror AcceptanceReport semantics: accepted status plus scorer and
        # roster identity; verdict text alone is insufficient.
        self.payload_json = json.dumps(
            {
                "gates": [{"gate": g, "passed": True} for g in names],
                "accepted": verdict == "pass",
                "scorer_version": scorer_version,
                "roster_digest": roster_digest,
                "roster": list(names),
                **({"task_id": task_id} if task_id is not None else {}),
            }
        )


from tests.canonical_doubles import write_canonical_reachability


class FakeEngine:
    """Structural stand-in for engine.Engine: workspace + read-only ledger."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.tasks: dict[str, TaskIR] = {}
        self.reports: dict[str, FakeReport] = {}
        #: (task_id, variant stage name) -> FakeReport
        self.variant_reports: dict[tuple[str, str], FakeReport] = {}
        self.audit_reports: dict[str, FakeReport] = {}

    def load_task(self, task_id: str) -> TaskIR:
        return self.tasks[task_id]

    def latest_report(self, task_id: str, stage: str):
        if stage == "gates":
            return self.reports.get(task_id)
        if stage == "audit":
            return self.audit_reports.get(task_id)
        # EL and T are the complete active release contract; each has its own
        # current-hash, roster-complete battery.
        return self.variant_reports.get((task_id, stage))

    def final_verdict(self, task_id: str) -> str:
        required = tuple(variant_gate_stage(v).value for v in RLVR_TASK_VARIANTS)
        rows = [self.variant_reports.get((task_id, stage)) for stage in required]
        return (
            "accepted"
            if all(row is not None and row.verdict == "pass" for row in rows)
            else "in_progress"
        )


class FakeSelection:
    def __init__(self, train=(), val=(), variants=None):
        self.train = tuple(train)
        self.val = tuple(val)
        self.rejected: dict[str, str] = {}
        self.variants = dict(variants or {})


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class ExportTaskBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # Isolate from the developer's environment: the flat-file serving
        # base must fall back to the deterministic default in these tests.
        prior = os.environ.pop(eltbench.FLAT_FILES_BASE_URL_ENV, None)
        if prior is not None:
            self.addCleanup(os.environ.__setitem__, eltbench.FLAT_FILES_BASE_URL_ENV, prior)
        self.task = demo_fixture.demo_task()
        self.gold = make_gold(self.task)
        self.task_root = self.tmp / "tasks" / self.task.task_id
        self.task_dir = self.task_root / "task"
        self.answer_key_dir = self.task_root / "answer_key"

    def export(self):
        eltbench.export_task(self.task, self.gold, self.task_dir, self.answer_key_dir)


class TestEvaluationSql(unittest.TestCase):
    def test_every_column_order_by_keys_first(self):
        mart = demo_fixture.demo_task().mart(demo_fixture.MART_NAME)
        sql = eltbench.evaluation_sql(mart)
        self.assertEqual(
            sql,
            "select * from customer_summary "
            "order by customer_id, completed_order_count, total_spend;\n",
        )

    def test_database_qualification(self):
        mart = demo_fixture.demo_task().mart(demo_fixture.MART_NAME)
        sql = eltbench.evaluation_sql(mart, database="demo__customer_summary")
        self.assertIn("from demo__customer_summary.customer_summary ", sql)


class TestExportTask(ExportTaskBase):
    def test_re_export_keeps_the_canonical_artifact_validate_t_produced(self):
        """export_task installs the staged private runtime/ wholesale; the
        canonical Terraform + dbt artifact under runtime/canonical/ is
        validate-t's evidence and must survive a same-identity re-export
        (review of 2026-09-15: it was deleted while the battery PASS stayed
        current)."""
        self.export()
        artifact = self.answer_key_dir / "runtime" / "canonical" / "snowflake"
        (artifact / "elt").mkdir(parents=True)
        (artifact / "elt" / "main.tf").write_text("# canonical\n", encoding="utf-8")
        (artifact / "reachability.json").write_text("{}\n", encoding="utf-8")
        self.export()
        self.assertEqual((artifact / "elt" / "main.tf").read_text(encoding="utf-8"), "# canonical\n")
        self.assertTrue((artifact / "reachability.json").is_file())
        self.assertTrue((self.answer_key_dir / "runtime" / "airbyte_connector_contract.json").is_file())

    def _load_status_helper(self):
        self.export()
        path = self.task_dir / "check_job_status.py"
        spec = importlib.util.spec_from_file_location(
            f"_generated_check_job_status_{id(self)}", path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _write_terraform_state(self) -> Path:
        path = self.tmp / "terraform.tfstate"
        path.write_text(
            json.dumps(
                {
                    "resources": [
                        {
                            "type": "airbyte_connection",
                            "instances": [
                                {"attributes": {"connection_id": "connection-1"}}
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_job_status_helper_normalizes_full_airbyte_urls(self):
        helper = self._load_status_helper()
        self.assertEqual(
            helper.airbyte_api_root(
                "https://airbyte.example/api/public/v1/"
            ),
            "https://airbyte.example/api/public/v1/",
        )
        self.assertEqual(
            helper.airbyte_api_root("http://airbyte.example:8000"),
            "http://airbyte.example:8000/api/public/v1/",
        )
        self.assertEqual(
            helper.airbyte_api_root("airbyte-runtime:8000"),
            "http://airbyte-runtime:8000/api/public/v1/",
        )
        with self.assertRaisesRegex(ValueError, "HTTP\\(S\\)"):
            helper.airbyte_api_root("ftp://airbyte.example/api/public/v1/")

    def test_job_status_helper_supports_basic_auth_with_full_url(self):
        helper = self._load_status_helper()
        state = self._write_terraform_state()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "data": [
                    {"connectionId": "connection-1", "status": "succeeded"}
                ]
            }
        ).encode("utf-8")
        argv = [
            "check_job_status.py",
            "--server",
            "https://airbyte.example/api/public/v1/",
            "--username",
            "airbyte-user",
            "--password",
            "airbyte-password",
            "--state",
            str(state),
            "--poll-interval",
            "0",
        ]
        with mock.patch("sys.argv", argv), mock.patch.object(
            helper, "urlopen", return_value=response
        ) as opened, mock.patch("builtins.print"):
            self.assertEqual(helper.main(), 0)
        opened.assert_called_once()
        request = opened.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://airbyte.example/api/public/v1/jobs"
            "?limit=100&orderBy=createdAt%7CDESC",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.get_header("Authorization"),
            "Basic YWlyYnl0ZS11c2VyOmFpcmJ5dGUtcGFzc3dvcmQ=",
        )
        self.assertEqual(opened.call_args.kwargs, {"timeout": 30})

    def test_job_status_helper_supports_oauth_without_printing_secret(self):
        helper = self._load_status_helper()
        state = self._write_terraform_state()
        token_response = mock.MagicMock()
        token_response.__enter__.return_value = token_response
        token_response.read.return_value = json.dumps(
            {"access_token": "short-lived-token"}
        ).encode("utf-8")
        jobs_response = mock.MagicMock()
        jobs_response.__enter__.return_value = jobs_response
        jobs_response.read.return_value = json.dumps(
            {
                "data": [
                    {"connectionId": "connection-1", "status": "succeeded"}
                ]
            }
        ).encode("utf-8")
        argv = [
            "check_job_status.py",
            "--server",
            "https://airbyte.example",
            "--client-id",
            "runtime-client",
            "--client-secret",
            "do-not-print-this",
            "--state",
            str(state),
            "--poll-interval",
            "0",
        ]
        with mock.patch("sys.argv", argv), mock.patch.object(
            helper, "urlopen", side_effect=(token_response, jobs_response)
        ) as opened, mock.patch("builtins.print") as output:
            self.assertEqual(helper.main(), 0)
        self.assertEqual(opened.call_count, 2)
        token_request = opened.call_args_list[0].args[0]
        self.assertEqual(
            token_request.full_url,
            "https://airbyte.example/api/public/v1/applications/token",
        )
        self.assertEqual(token_request.get_method(), "POST")
        self.assertEqual(
            json.loads(token_request.data.decode("utf-8")),
            {
                "client_id": "runtime-client",
                "client_secret": "do-not-print-this",
                "grant-type": "client_credentials",
            },
        )
        jobs_request = opened.call_args_list[1].args[0]
        self.assertEqual(
            jobs_request.full_url,
            "https://airbyte.example/api/public/v1/jobs"
            "?limit=100&orderBy=createdAt%7CDESC",
        )
        self.assertEqual(jobs_request.get_method(), "GET")
        self.assertEqual(
            jobs_request.get_header("Authorization"),
            "Bearer short-lived-token",
        )
        self.assertEqual(
            [call.kwargs for call in opened.call_args_list],
            [{"timeout": 30}, {"timeout": 30}],
        )
        self.assertNotIn("do-not-print-this", str(output.call_args_list))

    def test_layout_and_disjoint_trees(self):
        self.export()
        for rel in (
            "config.yaml",
            "data_model.yaml",
            "schemas/customers.csv",
            "schemas/orders.csv",
            "schemas/order_items.csv",
        ):
            self.assertTrue((self.task_dir / rel).is_file(), rel)
        for rel in (
            "table.json",
            "sort_key.json",
            "evaluation/sql/customer_summary.sql",
            "gt/customer_summary.csv",
        ):
            self.assertTrue((self.answer_key_dir / rel).is_file(), rel)
        # Public tree must not contain any private artifact names.
        public_names = {p.name for p in self.task_dir.rglob("*")}
        self.assertNotIn("table.json", public_names)
        self.assertNotIn("sort_key.json", public_names)
        self.assertNotIn("gt", public_names)

    def test_schema_csv_header_exact(self):
        self.export()
        text = (self.task_dir / "schemas" / "customers.csv").read_text()
        self.assertEqual(text.splitlines()[0], "column_name,column_description")
        self.assertIn("customer_id,Unique customer identifier.", text)

    def test_s3_config_preserves_the_original_single_object_path(self):
        """Public config mirrors upstream; private serving joins chunks."""
        from elt_taskgen.models import Backend, BackendAssignment

        task = demo_fixture.demo_task()
        task = task.model_copy(
            update={
                "backends": tuple(
                    BackendAssignment(table=a.table, backend=Backend.S3)
                    if a.table == "order_items"
                    else a
                    for a in task.backends
                )
            }
        )
        config = eltbench.build_config(task)
        (entry,) = config["aws_s3"]["data"]
        self.assertEqual(entry["table"], "order_items")
        self.assertTrue(
            entry["path"].endswith("/order_items.jsonl"), entry["path"]
        )

    def test_config_mirrors_upstream_keys(self):
        self.export()
        config = yaml.safe_load((self.task_dir / "config.yaml").read_text())
        # Demo backends: postgres, mongodb, files (+ Airbyte and snowflake always).
        self.assertEqual(
            set(config), {"Airbyte", "snowflake", "postgres", "mongodb", "flat_files"}
        )
        self.assertEqual(
            set(config["postgres"]["config"]),
            {"database", "host", "password", "port", "schema", "sync_mode", "tables", "user"},
        )
        self.assertEqual(config["postgres"]["config"]["tables"], ["customers"])
        self.assertEqual(config["postgres"]["config"]["sync_mode"], "full_refresh_append")
        self.assertEqual(config["mongodb"]["config"]["tables"], ["orders"])
        self.assertEqual(
            config["mongodb"]["config"]["connection_string"],
            "mongodb://elt-mongodb:27017/?directConnection=true",
        )
        self.assertIsInstance(config["flat_files"], list)
        self.assertEqual(config["flat_files"][0]["table"], "order_items")
        self.assertEqual(config["flat_files"][0]["format"], "csv")
        # Upstream flat_files paths are fetchable http(s) URLs (the Airbyte
        # Files connector downloads them); a bundle-relative path is a defect.
        self.assertEqual(
            config["flat_files"][0]["path"],
            "http://elt-files:8080/demo__customer_summary/order_items.csv",
        )
        # Upstream carries a definition id for exactly the source connectors
        # present in the task's config (never for absent ones, e.g. no
        # s3_definition_id when there is no aws_s3 stanza).
        self.assertEqual(
            set(config["Airbyte"]["config"]),
            {
                "files_definition_id", "mongodb_definition_id",
                "namespace_definition", "password", "postgres_definition_id",
                "server_url", "snowflake_definition_id", "username",
                "workspace_id",
            },
        )
        self.assertEqual(
            config["Airbyte"]["config"]["server_url"],
            "http://airbyte-abctl-control-plane:80/api/public/v1/",
        )
        self.assertEqual(config["snowflake"]["config"]["database"], "demo__customer_summary")
        self.assertEqual(
            {
                key: config["snowflake"]["config"][key]
                for key in ("account", "password", "role", "username", "warehouse")
            },
            {
                "account": "",
                "password": "",
                "role": "",
                "username": "",
                "warehouse": "",
            },
        )

    def test_data_model_single_models_key(self):
        self.export()
        data = yaml.safe_load((self.task_dir / "data_model.yaml").read_text())
        self.assertEqual(list(data), ["models"])
        model = data["models"][0]
        self.assertEqual(model["name"], "customer_summary")
        self.assertEqual(
            [c["name"] for c in model["columns"]],
            ["customer_id", "completed_order_count", "total_spend"],
        )
        for col in model["columns"]:
            self.assertTrue(col["description"])

    def test_data_model_yaml_quotes_colon_space_prose(self):
        """IR-010 generator pin: colon-space prose must survive a strict parse.

        The upstream world_development_indicators defect was an unquoted
        `description:` scalar whose embedded `: ` broke yaml.safe_load;
        safe_dump quotes such scalars, and this pin fails if _dump_yaml ever
        moves off that guarantee."""
        prose = (
            "The lowest value of the indicator belongs to "
            "'Health: Population: Structure' in the period 1960 to 1965."
        )
        mart = self.task.marts[0]
        column = mart.columns[0].model_copy(update={"description": prose})
        task = self.task.model_copy(
            update={
                "marts": (
                    mart.model_copy(update={"columns": (column, *mart.columns[1:])}),
                    *self.task.marts[1:],
                )
            }
        )
        parsed = yaml.safe_load(eltbench._dump_yaml(eltbench.build_data_model(task)))
        self.assertEqual(parsed["models"][0]["columns"][0]["description"], prose)

    def test_data_model_exports_ordered_plan_semantics(self):
        """Reference-stage tasks need a complete T contract even before an
        optional semantic-author pass populates solver_prompt."""
        self.assertEqual(self.task.solver_prompt, "")
        model = eltbench.build_data_model(self.task)["models"][0]
        transformation = model["transformation"]
        steps = transformation["steps"]

        self.assertEqual(
            [step["order"] for step in steps],
            list(range(1, len(self.task.marts[0].plan.ops) + 1)),
        )
        filter_step = next(step for step in steps if step["operation"] == "filter")
        self.assertEqual(filter_step["source_inputs"], ["orders"])
        self.assertNotIn("predicate", filter_step)
        self.assertEqual(
            filter_step["condition"],
            {
                "public_identifiers": ["status"],
                "literal_values": ["completed"],
            },
        )
        self.assertIn("status = 'completed'", filter_step["description"])

        item_total = next(
            step
            for step in steps
            if step["operation"] == "aggregate"
            and step.get("source_inputs") == ["order_items"]
        )
        self.assertNotIn("semantic_parameters", item_total)
        self.assertIn("SUM(quantity * unit_price)", item_total["description"])

        joins = [step for step in steps if step["operation"] == "join"]
        self.assertTrue(joins)
        self.assertTrue(all(step["join_preservation"] == "left" for step in joins))
        self.assertIn("COALESCE both measures to 0", str(transformation))
        self.assertEqual(steps[-1]["operation"], "tie_break")
        self.assertEqual(steps[-1]["carried_fields"], ["customer_id"])
        self.assertEqual(
            model["source_relationships"][0],
            {
                "child_table": "orders",
                "child_columns": ["customer_id"],
                "parent_table": "customers",
                "parent_columns": ["customer_id"],
                "required": False,
            },
        )

    def test_plan_export_withholds_compiler_only_implementation_fields(self):
        plan = self.task.marts[0].plan
        first = plan.ops[0].model_copy(
            update={
                "details": {
                    **plan.ops[0].details,
                    "name": "private_relation_binding",
                    "select": "secret_source AS secret_output",
                    "sql": "SELECT secret_output FROM private_relation_binding",
                    "public_parameter": "keep me",
                }
            }
        )
        altered = plan.model_copy(
            update={"ops": (first, *plan.ops[1:]), "notes": "private_plan_note"}
        )
        mart = self.task.marts[0].model_copy(update={"plan": altered})
        task = self.task.model_copy(update={"marts": (mart,)})
        exported = eltbench.solver_visible_plan(task, mart)
        first_exported = exported["steps"][0]

        self.assertNotIn("semantic_parameters", first_exported)
        serialized = yaml.safe_dump(exported)
        self.assertNotIn("private_relation_binding", serialized)
        self.assertNotIn("secret_source", serialized)
        self.assertNotIn("secret_output", serialized)
        self.assertNotIn("public_parameter", serialized)
        self.assertNotIn("private_plan_note", serialized)

    def test_shared_solver_projection_protects_every_public_and_author_surface(self):
        """One information barrier feeds data_model, docs, T, and author prompts."""

        from elt_taskgen.review.council import _author_view

        mart = self.task.marts[0]
        join_index = next(
            index
            for index, op in enumerate(mart.plan.ops)
            if op.join_type is not None
        )
        original = mart.plan.ops[join_index]
        private_tokens = (
            "private_join_alias",
            "secret_carried_field",
            "private_answer_key",
            "private_plan_note",
        )
        poisoned = original.model_copy(
            update={
                "tables": (*original.tables, private_tokens[0]),
                "columns": (*original.columns, private_tokens[1]),
                "predicate": (
                    "eligibility_marker = 'qualified' AND "
                    "private_join_alias.private_answer_key = orders.customer_id"
                ),
                "details": {
                    **original.details,
                    "name": private_tokens[0],
                    "select": "private_answer_key AS secret_carried_field",
                    "sql": "SELECT private_answer_key FROM private_join_alias",
                    "group_by": "private_answer_key",
                    "m_secret": "COUNT(DISTINCT private_answer_key)",
                    "tie_break": private_tokens[1],
                },
            }
        )
        ops = list(mart.plan.ops)
        ops[join_index] = poisoned
        plan = mart.plan.model_copy(
            update={"ops": tuple(ops), "notes": private_tokens[3]}
        )
        altered_mart = mart.model_copy(update={"plan": plan})
        task = self.task.model_copy(update={"marts": (altered_mart,)})

        surfaces = {
            "data_model": yaml.safe_dump(eltbench.build_data_model(task)),
            "combined_documentation": eltbench.public_documentation(task),
            "transform_documentation": eltbench.t_documentation(task),
            "author_view": _author_view(task),
        }
        for label, surface in surfaces.items():
            with self.subTest(surface=label):
                for token in private_tokens:
                    self.assertNotIn(token, surface)
                self.assertNotIn("COUNT(DISTINCT private_answer_key)", surface)
                self.assertNotIn("eligibility_marker", surface)
                self.assertNotIn(
                    "private_join_alias.private_answer_key = orders.customer_id",
                    surface,
                )
                self.assertIn("qualified", surface)
                if label != "data_model":
                    self.assertIn(
                        "literal specification values: qualified", surface
                    )
                self.assertIn(mart.grain, surface)
                self.assertIn("completed_order_count", surface)
                self.assertIn("customers without in-scope orders", surface)
                self.assertIn("orders", surface)
                self.assertIn("customers", surface)

        model = eltbench.build_data_model(task)["models"][0]
        self.assertEqual(model["source_relationships"][0]["required"], False)
        join = next(
            step
            for step in model["transformation"]["steps"]
            if step["operation"] == "join"
        )
        self.assertEqual(join["join_preservation"], "left")
        self.assertIn("customer_id", join["carried_fields"])
        self.assertEqual(
            join["condition"],
            {
                "public_identifiers": ["orders", "customer_id"],
                "literal_values": ["qualified"],
            },
        )

    def test_empty_authored_prompt_still_exports_a_complete_transform_spec(self):
        self.assertEqual(self.task.solver_prompt, "")
        text = eltbench.public_documentation(self.task)

        self.assertNotIn("## Specification\n", text)
        self.assertIn("## Transformation specification", text)
        self.assertIn("Mart 'customer_summary' has", text)
        self.assertIn("public source tables: customers, orders", text)
        self.assertNotIn("predicate: orders.customer_id = customers.customer_id", text)
        self.assertIn("COALESCE both measures to 0", text)
        self.assertNotIn("WITH completed_orders AS", text)

    def test_transform_variant_uses_the_same_plan_derived_specification(self):
        combined = eltbench.public_documentation(self.task)
        transform = eltbench.t_documentation(self.task)
        marker = "Mart 'customer_summary' has"

        self.assertIn("## Transformation specification", transform)
        self.assertEqual(combined.count(marker), 1)
        self.assertEqual(transform.count(marker), 1)

    def test_evaluation_artifacts(self):
        self.export()
        table = json.loads((self.answer_key_dir / "table.json").read_text())
        self.assertEqual(
            table,
            {"demo__customer_summary": {"customers": 1000, "order_items": 9000, "orders": 3000}},
        )
        sort_key = json.loads((self.answer_key_dir / "sort_key.json").read_text())
        self.assertEqual(sort_key, {"demo__customer_summary": {"customer_summary": ["customer_id"]}})
        sql = (self.answer_key_dir / "evaluation" / "sql" / "customer_summary.sql").read_text()
        self.assertEqual(
            sql,
            "select * from demo__customer_summary.customer_summary "
            "order by customer_id, completed_order_count, total_spend;\n",
        )
        self.assertEqual(
            (self.answer_key_dir / "gt" / "customer_summary.csv").read_text(), GOLD_CSV
        )

    def _write_dev_rendered(self) -> Path:
        """A COMPLETE development rendered root (one artifact per source table).

        Complete on purpose: export now certifies that every table the emitted
        config declares has a rendered artifact where the private serving
        manifest says it is, so a half-rendered population is an export-time
        refusal rather than a bundle nobody can stand up.
        """
        rendered = self.task_root / "populations" / "development" / "rendered"
        (rendered / "files").mkdir(parents=True)
        (rendered / "postgres").mkdir(parents=True)
        (rendered / "mongodb").mkdir(parents=True)
        (rendered / "files" / "order_items.csv").write_text(
            "order_id,quantity,unit_price\n"
        )
        (rendered / "postgres" / "customers.sql").write_text("-- customers\n")
        (rendered / "mongodb" / "orders.jsonl").write_text('{"order_id": 1}\n')
        return rendered

    def test_keeps_dev_rendered_sources_out_of_public_task(self):
        self._write_dev_rendered()
        self.export()
        self.assertFalse((self.task_dir / "sources").exists())
        self.assertTrue(
            (
                self.task_root / "populations" / "development" / "rendered"
                / "files" / "order_items.csv"
            ).is_file()
        )

    def test_combined_public_task_has_original_runtime_shape(self):
        self.export()
        for rel in (
            "config.yaml",
            "data_model.yaml",
            "check_job_status.py",
            "snowflake_credential.json",
            "elt/main.tf",
        ):
            self.assertTrue((self.task_dir / rel).is_file(), rel)
        self.assertFalse((self.task_dir / "documentation.md").exists())
        self.assertFalse(
            (
                self.task_dir / "elt"
                / eltbench.AIRBYTE_CONNECTOR_TFVARS_FILENAME
            ).exists()
        )
        self.assertEqual(
            {
                path.name
                for path in (self.task_dir / "documentation").iterdir()
                if path.is_file()
            },
            set(eltbench.RUNTIME_DOCUMENTATION_FILENAMES),
        )
        self.assertEqual(
            (self.task_dir / "elt" / "main.tf").read_text(),
            'terraform {\n'
            '  required_providers {\n'
            '    airbyte = {\n'
            '      source  = "airbytehq/airbyte"\n'
            '      version = "0.6.5"\n'
            '    }\n'
            '  }\n'
            '}\n',
        )
        readme = (self.task_dir / "documentation" / "README.md").read_text()
        self.assertIn("## Transformation specification", readme)
        self.assertIn("## Source tables", readme)
        private_contract_path = (
            self.answer_key_dir / eltbench.PRIVATE_AIRBYTE_CONNECTOR_CONTRACT
        )
        self.assertTrue(private_contract_path.is_file())
        private_contract = json.loads(private_contract_path.read_text())
        self.assertEqual(
            private_contract["terraform_provider"]["version"], "0.6.5"
        )
        self.assertEqual(
            private_contract["destination"]["connector_version"], "4.1.2"
        )
        credential = json.loads(
            (self.task_dir / "snowflake_credential.json").read_text()
        )
        self.assertEqual(
            credential, {"account": "", "password": "", "user": ""}
        )
        config = yaml.safe_load((self.task_dir / "config.yaml").read_text())
        airbyte = config["Airbyte"]["config"]
        for key in ("password", "username", "workspace_id"):
            self.assertEqual(airbyte[key], "")

    def test_databricks_and_redshift_public_runtime_shapes(self):
        cases = {
            "databricks": {
                "definition_key": "databricks_definition_id",
                "definition_id": "072d5540-f236-4294-ba7c-ade8fd918496",
                "credential": {
                    "client_id": "",
                    "hostname": "",
                    "http_path": "",
                    "secret": "",
                },
                "doc": "destination_databricks.md",
            },
            "redshift": {
                "definition_key": "redshift_definition_id",
                "definition_id": "f7a7d195-377f-cf5b-70a5-be6b819019dc",
                "credential": {
                    "database": "",
                    "host": "",
                    "password": "",
                    "port": 5439,
                    "username": "",
                },
                "doc": "destination_redshift.md",
            },
        }
        destination_keys = {
            "snowflake_definition_id",
            "databricks_definition_id",
            "redshift_definition_id",
        }
        for destination, expected in cases.items():
            with self.subTest(destination=destination):
                eltbench.export_task(
                    self.task,
                    self.gold,
                    self.task_dir,
                    self.answer_key_dir,
                    destination=destination,
                )
                eltbench.assert_public_runtime_shape(self.task_dir)
                config = yaml.safe_load((self.task_dir / "config.yaml").read_text())
                self.assertIn(destination, config)
                self.assertNotIn("snowflake", config)
                self.assertEqual(
                    destination_keys & set(config["Airbyte"]["config"]),
                    {expected["definition_key"]},
                )
                self.assertEqual(
                    config["Airbyte"]["config"][expected["definition_key"]],
                    expected["definition_id"],
                )
                self.assertEqual(
                    config[destination]["config"]["schema"],
                    "demo__customer_summary",
                )
                self.assertEqual(
                    json.loads(
                        (self.task_dir / f"{destination}_credential.json").read_text()
                    ),
                    expected["credential"],
                )
                self.assertTrue(
                    (self.task_dir / "documentation" / expected["doc"]).is_file()
                )
                if destination == "databricks":
                    self.assertEqual(
                        set(config[destination]["config"]),
                        {
                            "client_id", "database", "hostname", "http_path",
                            "schema", "secret",
                        },
                    )
                else:
                    self.assertEqual(
                        set(config[destination]["config"]),
                        {
                            "access_key_id", "database", "host", "password",
                            "port", "s3_bucket_name", "s3_bucket_region",
                            "schema", "secret_access_key", "username",
                        },
                    )

    def test_extra_destinations_are_packaged_under_destinations_dir(self):
        eltbench.export_task(
            self.task,
            self.gold,
            self.task_dir,
            self.answer_key_dir,
            destination="snowflake",
            extra_destinations=("databricks", "redshift", "snowflake"),
        )
        eltbench.assert_public_runtime_shape(self.task_dir)
        eltbench.assert_public_runtime_tree_clean(self.task, self.task_dir)
        root = yaml.safe_load((self.task_dir / "config.yaml").read_text())
        for destination in ("databricks", "redshift"):
            with self.subTest(destination=destination):
                bundle = self.task_dir / "destinations" / destination
                config = yaml.safe_load((bundle / "config.yaml").read_text())
                self.assertIn(destination, config)
                self.assertNotIn("snowflake", config)
                for source_key in set(root) - {"Airbyte", "snowflake"}:
                    self.assertEqual(config[source_key], root[source_key])
                self.assertTrue((bundle / f"{destination}_credential.json").is_file())
                self.assertFalse(
                    (self.task_dir / f"{destination}_credential.json").exists()
                )
                contract = json.loads(
                    (
                        self.answer_key_dir
                        / "runtime"
                        / f"airbyte_connector_contract.{destination}.json"
                    ).read_text()
                )
                self.assertEqual(
                    contract,
                    eltbench.build_airbyte_connector_contract(config),
                )
        # The root destination is never duplicated below destinations/.
        self.assertFalse((self.task_dir / "destinations" / "snowflake").exists())

    def test_no_extra_destinations_writes_no_destinations_dir(self):
        eltbench.export_task(
            self.task, self.gold, self.task_dir, self.answer_key_dir
        )
        self.assertFalse((self.task_dir / "destinations").exists())

    def test_runtime_shape_rejects_crossed_destination_signals(self):
        eltbench.export_task(
            self.task,
            self.gold,
            self.task_dir,
            self.answer_key_dir,
            destination="databricks",
        )
        config_path = self.task_dir / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["Airbyte"]["config"]["redshift_definition_id"] = (
            "f7a7d195-377f-cf5b-70a5-be6b819019dc"
        )
        config_path.write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(ValueError, "exactly the databricks"):
            eltbench.assert_public_runtime_shape(self.task_dir)

    def test_runtime_shape_refuses_populated_airbyte_credentials(self):
        self.export()
        config_path = self.task_dir / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["Airbyte"]["config"]["password"] = "must-not-ship"
        config_path.write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(ValueError, "must be empty before release"):
            eltbench.assert_public_runtime_shape(self.task_dir)

    def test_runtime_shape_refuses_non_upstream_public_files(self):
        self.export()
        for rel in (
            "documentation.md",
            f"elt/{eltbench.AIRBYTE_CONNECTOR_TFVARS_FILENAME}",
        ):
            with self.subTest(rel=rel):
                path = self.task_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("not part of original ELT-Bench\n")
                with self.assertRaisesRegex(ValueError, "non-upstream solver files"):
                    eltbench.assert_public_runtime_shape(self.task_dir)
                path.unlink()

    def test_public_runtime_gate_rejects_legacy_solver_artifacts(self):
        cases = (
            ("warehouse/primary.duckdb", b"not even a valid database", "DuckDB"),
            ("load_plan.json", b"{}", "load_plan"),
            ("notes.txt", b"Use sql_by_mart", "sql_by_mart"),
            ("prompt.md", b"Return a standalone DuckDB SELECT", "standalone duckdb"),
        )
        for rel, content, marker in cases:
            with self.subTest(rel=rel):
                root = self.tmp / "runtime-gate" / rel.replace("/", "_")
                root.mkdir(parents=True)
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, marker):
                    eltbench.assert_public_runtime_tree_clean(self.task, root)

    def test_export_fails_closed_when_a_rendered_artifact_is_missing(self):
        """A serving manifest that names an artifact nobody rendered is a
        promise the harness cannot keep, so the export refuses it."""
        rendered = self._write_dev_rendered()
        (rendered / "mongodb" / "orders.jsonl").unlink()
        with self.assertRaisesRegex(ValueError, "orders"):
            self.export()

    def test_deterministic_byte_identical(self):
        self.export()
        first = tree_digest(self.task_root)
        self.export()
        self.assertEqual(tree_digest(self.task_root), first)

    def test_reexport_removes_stale_public_artifacts(self):
        self.export()
        stale = self.task_dir / "sources" / "files"
        stale.mkdir(parents=True)
        (stale / "old.csv").write_text("secret fixture\n")
        warehouse = self.task_dir / "warehouse"
        warehouse.mkdir()
        (warehouse / "development.duckdb").write_bytes(b"stale")
        self.export()
        self.assertFalse((self.task_dir / "sources").exists())
        self.assertFalse((self.task_dir / "warehouse").exists())

    def test_rejects_stale_gold(self):
        stale = self.gold.model_copy(update={"task_content_hash": "0" * 64})
        with self.assertRaisesRegex(ValueError, "stale"):
            eltbench.export_task(self.task, stale, self.task_dir, self.answer_key_dir)
        self.assertFalse(self.task_dir.exists())

    def test_rejects_wrong_task_gold(self):
        wrong = self.gold.model_copy(update={"task_id": "other__task"})
        with self.assertRaises(ValueError):
            eltbench.export_task(self.task, wrong, self.task_dir, self.answer_key_dir)

    def test_rejects_missing_primary_gold(self):
        no_primary = self.gold.model_copy(update={"stage2_csv": {"primary": {}}})
        with self.assertRaisesRegex(ValueError, "missing marts"):
            eltbench.export_task(self.task, no_primary, self.task_dir, self.answer_key_dir)
        no_counts = self.gold.model_copy(update={"stage1": {"primary": {"customers": 1}}})
        with self.assertRaisesRegex(ValueError, "missing tables"):
            eltbench.export_task(self.task, no_counts, self.task_dir, self.answer_key_dir)

    def test_rejects_nested_output_dirs(self):
        with self.assertRaisesRegex(ValueError, "disjoint"):
            eltbench.export_task(self.task, self.gold, self.task_dir, self.task_dir / "inner")

    def test_leak_guard_catches_reference_sql_in_public(self):
        self.export()
        # Plant the trusted reference SQL in the public tree; the guard must fire.
        (self.task_dir / "notes.txt").write_text(demo_fixture.REFERENCE_SQL)
        with self.assertRaisesRegex(ValueError, "reference-sql"):
            eltbench.assert_public_tree_clean(self.task, self.task_dir)

    def test_leak_guard_catches_answer_key_text(self):
        self.export()
        (self.task_dir / "notes.txt").write_text("see ../answer_key/gt for answers")
        with self.assertRaises(ValueError):
            eltbench.assert_public_tree_clean(self.task, self.task_dir)


#: The two real released ids that are ALREADY within the bound, plus the two
#: that are not (schemapile 100 chars, synsql 73) — the exact strings that
#: produced un-provisionable bundles.
_SHORT_IDS = (
    "demo__customer_summary",
    "dbt__reddit_ads__reddit_ads__account_report_d7014f04",   # 52
    "dlt__workable",                                          # 13
    "wikidbs__c00012__00012_metec_solarwatt_team_members_db",  # 54
)
_LONG_IDS = (
    "schemapile__github_com_opencog_language_learning_e0eb3bdecec0__"
    "042316_poc_corpora_with_left_wall_sql",                  # 100
    "synsql__soil_composition_and_horizon_analysis__soil_profiles_horizons_top",
)

#: LocalStack's own bucket-name regex (native provider), verbatim.
_LOCALSTACK_BUCKET_RE = r"(?=^.{3,63}$)(?!^(\d+\.)+\d+$)(^(([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])\.)*([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])$)"


class TestSourceIdentifierBounds(unittest.TestCase):
    """The derived database/bucket names must be PROVISIONABLE.

    The pipeline is offline, so an over-length name is invisible until the
    bundle is stood up: LocalStack refuses a 107-character bucket outright,
    and Postgres silently TRUNCATES a 100-character database to 63 bytes —
    which, for the released SchemaPile task, is exactly `family_id + '__'`, so
    every task of that cluster would provision onto ONE database and clobber
    its siblings. Both released over-length bundles (schemapile, synsql) are
    the regression these tests pin.
    """

    @staticmethod
    def _task(task_id: str) -> TaskIR:
        return demo_fixture.demo_task().model_copy(update={"task_id": task_id})

    def test_database_name_pass_through_for_short_ids(self):
        for task_id in _SHORT_IDS:
            with self.subTest(task_id=task_id):
                self.assertLessEqual(len(task_id), eltbench.DATABASE_NAME_MAX_LEN)
                self.assertEqual(eltbench.database_name(self._task(task_id)), task_id)

    def test_database_name_bounded_for_long_ids(self):
        import re as _re

        for task_id in _LONG_IDS:
            with self.subTest(task_id=task_id):
                db = eltbench.database_name(self._task(task_id))
                self.assertLessEqual(len(db), eltbench.DATABASE_NAME_MAX_LEN)
                self.assertTrue(_re.fullmatch(r"[a-z0-9][a-z0-9_]*", db), db)
                # Deterministic: the same id always yields the same name.
                self.assertEqual(db, eltbench.database_name(self._task(task_id)))

    def test_two_ids_sharing_a_long_prefix_get_different_names(self):
        """The Postgres-aliasing regression, directly.

        Truncation alone maps every task of one SchemaPile cluster onto the
        same database; the digest is over the FULL task id, so it cannot.
        """
        prefix = "schemapile__github_com_opencog_language_learning_e0eb3bdecec0__"
        self.assertGreaterEqual(len(prefix), 63)
        a = eltbench.database_name(self._task(prefix + "042316_poc_corpora_a_sql"))
        b = eltbench.database_name(self._task(prefix + "042316_poc_corpora_b_sql"))
        self.assertNotEqual(a, b)
        self.assertLessEqual(max(len(a), len(b)), eltbench.DATABASE_NAME_MAX_LEN)

    def test_bucket_name_within_s3_limits(self):
        import re as _re

        for task_id in _LONG_IDS + _SHORT_IDS:
            with self.subTest(task_id=task_id):
                bucket = eltbench.bucket_name(
                    eltbench.database_name(self._task(task_id))
                )
                self.assertLessEqual(len(bucket), eltbench.S3_BUCKET_MAX_LEN)
                self.assertTrue(_re.match(_LOCALSTACK_BUCKET_RE, bucket), bucket)
        # A 54-char id keeps its existing 61-char bucket, byte for byte.
        self.assertEqual(
            eltbench.bucket_name(eltbench.database_name(self._task(_SHORT_IDS[3]))),
            "wikidbs--c00012--00012-metec-solarwatt-team-members-db-bucket",
        )

    def test_assert_source_identifiers_refuses_the_released_shapes(self):
        overlong = "x" * 64
        with self.assertRaisesRegex(ValueError, "bound"):
            eltbench.assert_source_identifiers(overlong)
        with self.assertRaisesRegex(ValueError, "legal database name"):
            eltbench.assert_source_identifiers("Has-Caps")

    def test_build_config_identifiers_within_limits(self):
        from elt_taskgen.models import Backend, BackendAssignment

        base = self._task(_LONG_IDS[0])
        task = base.model_copy(
            update={
                "backends": (
                    BackendAssignment(table="customers", backend=Backend.POSTGRES),
                    BackendAssignment(table="orders", backend=Backend.MONGODB),
                    BackendAssignment(table="order_items", backend=Backend.S3),
                )
            }
        )
        config = eltbench.build_config(task)
        db = eltbench.database_name(task)
        for section in ("snowflake", "postgres", "mongodb"):
            name = config[section]["config"]["database"]
            self.assertEqual(name, db)
            self.assertLessEqual(len(name), eltbench.DATABASE_NAME_MAX_LEN)
        for entry in config["aws_s3"]["data"]:
            bucket = entry["path"].split("/")[2]
            self.assertLessEqual(len(bucket), eltbench.S3_BUCKET_MAX_LEN)

    def test_export_task_answer_key_uses_the_bounded_db(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        task = self._task(_LONG_IDS[1])
        gold = make_gold(task)
        task_dir = tmp / "tasks" / task.task_id / "task"
        key_dir = tmp / "tasks" / task.task_id / "answer_key"
        eltbench.export_task(task, gold, task_dir, key_dir)
        db = eltbench.database_name(task)
        self.assertNotEqual(db, task.task_id)  # bounded: the two differ
        self.assertEqual(list(json.loads((key_dir / "table.json").read_text())), [db])
        self.assertEqual(
            list(json.loads((key_dir / "sort_key.json").read_text())), [db]
        )
        sql = (
            key_dir / "evaluation" / "sql" / f"{demo_fixture.MART_NAME}.sql"
        ).read_text()
        self.assertIn(f"from {db}.{demo_fixture.MART_NAME} ", sql)
        config = yaml.safe_load((task_dir / "config.yaml").read_text())
        self.assertEqual(config["snowflake"]["config"]["database"], db)


class TestSourcesServing(ExportTaskBase):
    """The private serving contract: how each backend is stood up.

    config.yaml is upstream-shaped and deliberately says nothing about where
    the rendered bytes live, so without this manifest a harness has to guess —
    and the two guesses that lose rows silently (which S3 objects to upload,
    what shape a REST route returns) are exactly the two this pins.
    """

    def manifest(self) -> dict:
        self.export()
        return json.loads(
            (self.answer_key_dir / eltbench.SOURCES_SERVING_MANIFEST).read_text()
        )

    def test_manifest_covers_every_config_table(self):
        manifest = self.manifest()
        config = yaml.safe_load((self.task_dir / "config.yaml").read_text())
        declared = set()
        for tables in eltbench._config_tables_by_section(config).values():
            declared.update(tables)
        self.assertEqual(set(manifest["tables"]), declared)
        for table, entry in manifest["tables"].items():
            self.assertEqual(
                entry["backend"], self.task.backend_for(table).backend.value
            )

    def test_s3_entry_names_one_object_and_chunk_concatenation_rule(self):
        from elt_taskgen.models import Backend, BackendAssignment

        task = self.task.model_copy(
            update={
                "backends": tuple(
                    BackendAssignment(table=a.table, backend=Backend.S3)
                    if a.table == "order_items"
                    else a
                    for a in self.task.backends
                )
            }
        )
        db = eltbench.database_name(task)
        manifest = eltbench._sources_serving_manifest(
            task, db, flat_base="http://elt-files:8080", rest_base="http://elt-api:5005"
        )
        entry = manifest["tables"]["order_items"]
        config = eltbench.build_config(task)
        (data,) = config["aws_s3"]["data"]
        self.assertEqual(entry["bucket"], eltbench.bucket_name(db))
        self.assertEqual(entry["object_key"], "order_items.jsonl")
        self.assertEqual(entry["rendered_dir"], "s3/order_items/")
        self.assertEqual(entry["rendered_parts_glob"], "part-*.jsonl")
        # The declared config path and the serving rule agree about the layout.
        self.assertTrue(data["path"].endswith("/order_items.jsonl"))
        self.assertIn("single object", entry["rule"])
        self.assertIn("concatenate EVERY rendered part", entry["rule"])

    def test_rest_entry_route_is_a_bare_array(self):
        from elt_taskgen.models import Backend, BackendAssignment

        task = self.task.model_copy(
            update={
                "backends": tuple(
                    BackendAssignment(table=a.table, backend=Backend.REST)
                    if a.table == "orders"
                    else a
                    for a in self.task.backends
                )
            }
        )
        db = eltbench.database_name(task)
        entry = eltbench._sources_serving_manifest(
            task, db, flat_base="http://elt-files:8080", rest_base="http://elt-api:5005"
        )["tables"]["orders"]
        self.assertEqual(entry["route"], f"/{db}/orders")
        self.assertEqual(entry["records_key"], "data")
        self.assertEqual(entry["response_shape"], "bare_array")
        self.assertEqual(entry["base_url"], "http://elt-api:5005")

    def test_rest_base_url_resolution(self):
        self.assertEqual(
            eltbench.resolve_rest_base_url(), eltbench.DEFAULT_REST_BASE_URL
        )
        os.environ[eltbench.REST_BASE_URL_ENV] = "https://api.example.org/v1/"
        self.addCleanup(os.environ.pop, eltbench.REST_BASE_URL_ENV, None)
        self.assertEqual(
            eltbench.resolve_rest_base_url(), "https://api.example.org/v1"
        )
        self.assertEqual(
            eltbench.resolve_rest_base_url("http://other:5005"), "http://other:5005"
        )
        for bad in ("elt-api:5005", "ftp://x.example", "", "http://"):
            with self.assertRaises(ValueError):
                eltbench.resolve_rest_base_url(bad)

    def test_serving_manifest_is_private_only(self):
        self.export()
        self.assertFalse(
            (self.task_dir / eltbench.SOURCES_SERVING_MANIFEST).exists()
        )
        eltbench.assert_public_tree_clean(self.task, self.task_dir)

    def test_incomplete_manifest_is_refused(self):
        self.export()
        config = yaml.safe_load((self.task_dir / "config.yaml").read_text())
        manifest = self.manifest()
        manifest["tables"].pop("customers")
        with self.assertRaisesRegex(ValueError, "does not cover"):
            eltbench.assert_serving_manifest_complete(self.task, config, manifest)

    def test_airbyte_manifest_shape(self):
        from elt_taskgen.models import Backend, BackendAssignment

        task = self.task.model_copy(
            update={
                "backends": tuple(
                    BackendAssignment(table=a.table, backend=Backend.REST)
                    if a.table in ("orders", "customers")
                    else a
                    for a in self.task.backends
                )
            }
        )
        db = eltbench.database_name(task)
        manifest = eltbench.build_airbyte_custom_api_manifest(task)
        streams = manifest["definitions"]["streams"]
        self.assertEqual(sorted(streams), ["customers", "orders"])
        self.assertEqual(
            manifest["definitions"]["base_requester"]["url_base"],
            eltbench.DEFAULT_REST_BASE_URL,
        )
        for name, stream in streams.items():
            retriever = stream["retriever"]
            self.assertEqual(
                retriever["record_selector"]["extractor"]["field_path"], []
            )
            self.assertEqual(retriever["requester"]["path"], f"/{db}/{name}")
        self.assertEqual(len(manifest["streams"]), 2)
        # Emitted next to the answer key ONLY when the task uses REST.
        self.export()
        self.assertFalse(
            (self.answer_key_dir / eltbench.AIRBYTE_CUSTOM_API_MANIFEST).exists()
        )

    def test_airbyte_manifest_matches_the_upstream_stanza_keys(self):
        upstream = Path("../ELT-Bench/setup/elt_snowflake.yaml").resolve()
        if not upstream.is_file():
            self.skipTest("pinned upstream checkout not present")
        from elt_taskgen.models import Backend, BackendAssignment

        task = self.task.model_copy(
            update={
                "backends": tuple(
                    BackendAssignment(table=a.table, backend=Backend.REST)
                    if a.table == "orders"
                    else a
                    for a in self.task.backends
                )
            }
        )
        pinned = yaml.safe_load(upstream.read_text())
        their_streams = pinned["definitions"]["streams"]
        theirs = their_streams[sorted(their_streams)[0]]
        ours = eltbench.build_airbyte_custom_api_manifest(task)[
            "definitions"
        ]["streams"]["orders"]
        self.assertEqual(pinned["version"], eltbench._AIRBYTE_MANIFEST_VERSION)
        self.assertLessEqual(set(ours), set(theirs))
        self.assertEqual(
            theirs["retriever"]["record_selector"]["extractor"]["field_path"], []
        )


class TestFlatFilesHosting(ExportTaskBase):
    """flat_files[].path must be a fetchable http(s) URL (upstream contract:
    all 100 pinned configs use https URLs the Airbyte Files connector
    downloads), backed by a serving manifest in the private answer key."""

    def test_default_base_url_is_absolute_http(self):
        url = eltbench.flat_files_url("demo__customer_summary", "order_items", "csv")
        self.assertEqual(
            url, "http://elt-files:8080/demo__customer_summary/order_items.csv"
        )

    def test_env_var_overrides_base(self):
        os.environ[eltbench.FLAT_FILES_BASE_URL_ENV] = "https://files.example.org/elt/"
        self.addCleanup(os.environ.pop, eltbench.FLAT_FILES_BASE_URL_ENV, None)
        config = eltbench.build_config(self.task)
        self.assertEqual(
            config["flat_files"][0]["path"],
            "https://files.example.org/elt/demo__customer_summary/order_items.csv",
        )

    def test_explicit_argument_beats_env(self):
        os.environ[eltbench.FLAT_FILES_BASE_URL_ENV] = "https://env.example.org"
        self.addCleanup(os.environ.pop, eltbench.FLAT_FILES_BASE_URL_ENV, None)
        config = eltbench.build_config(
            self.task, flat_files_base_url="https://arg.example.org"
        )
        self.assertTrue(
            config["flat_files"][0]["path"].startswith("https://arg.example.org/")
        )

    def test_non_http_base_fails_closed(self):
        for bad in ("sources/files", "ftp://x.example", "file:///tmp", "", "http://"):
            with self.assertRaises(ValueError):
                eltbench.resolve_flat_files_base_url(bad)

    def test_serving_manifest_matches_config(self):
        self.export()
        manifest = json.loads(
            (self.answer_key_dir / "flat_files_serving.json").read_text()
        )
        config = yaml.safe_load((self.task_dir / "config.yaml").read_text())
        self.assertEqual(manifest["base_url"], "http://elt-files:8080")
        self.assertEqual(
            manifest["base_url_env"], "ELT_TASKGEN_FLAT_FILES_BASE_URL"
        )
        entry = manifest["tables"]["order_items"]
        self.assertEqual(entry["url"], config["flat_files"][0]["path"])
        self.assertEqual(entry["rendered_file"], "files/order_items.csv")
        self.assertEqual(entry["format"], "csv")
        # WHICH rendered root the file is relative to is now stated, because
        # the harness must serve the ACTIVE population's copy and the release
        # ships one root per graded population.
        self.assertEqual(
            manifest["rendered_root_template"], eltbench.EL_SOURCES_REL_TEMPLATE
        )
        self.assertIn("populations/<population>/rendered", manifest["note"])
        self.assertIn("private/<parent_task_id>/populations", manifest["note"])
        # The manifest is private: it must never appear in the public tree.
        self.assertFalse((self.task_dir / "flat_files_serving.json").exists())

    def test_export_respects_explicit_base(self):
        eltbench.export_task(
            self.task,
            self.gold,
            self.task_dir,
            self.answer_key_dir,
            flat_files_base_url="https://mirror.example.org/base",
        )
        config = yaml.safe_load((self.task_dir / "config.yaml").read_text())
        manifest = json.loads(
            (self.answer_key_dir / "flat_files_serving.json").read_text()
        )
        expected = (
            "https://mirror.example.org/base/demo__customer_summary/order_items.csv"
        )
        self.assertEqual(config["flat_files"][0]["path"], expected)
        self.assertEqual(manifest["tables"]["order_items"]["url"], expected)


class _FreezeReleaseHarness(ExportTaskBase):
    """Freeze-release fixture surface: setUp plus helpers, no test methods.

    TestFreezeRelease and TestLayeredIdentities both inherit this so neither
    re-collects the other's tests (the composed-harness counterpart lives in
    tests/test_certification.py, which instantiates TestFreezeRelease("freeze")).
    """

    def write_population_sources(self, populations=None):
        """Strict-loadable rendered source roots, one per GRADED population.

        The EL reward is `compare_stage1` over every population it names, so a
        release must carry each of those rendered roots (private/<parent>/
        populations/<pop>/rendered/) or the __el unit is scorable on the one
        population whose sources happen to sit in the public bundle. Freeze
        refuses a missing one, so the fixture writes them — one artifact per
        source table, in the layout `find_rendered_artifact` resolves.
        """
        pops = populations if populations is not None else sorted(self.gold.stage1)
        for pop in pops:
            counts = self.gold.stage1[pop]
            root = self.task_root / "populations" / pop / "rendered"
            (root / "postgres").mkdir(parents=True, exist_ok=True)
            (root / "mongodb").mkdir(parents=True, exist_ok=True)
            (root / "files").mkdir(parents=True, exist_ok=True)
            (root / "postgres" / "customers.sql").write_text(
                f'-- {pop}\nINSERT INTO "customers" '
                f"SELECT i, 'Customer ' || i FROM "
                f"generate_series(1, {counts['customers']}) AS rows(i);\n",
                encoding="utf-8",
            )
            (root / "mongodb" / "orders.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            "order_id": index,
                            "customer_id": ((index - 1) % counts["customers"]) + 1,
                            "status": "completed",
                        },
                        sort_keys=True,
                    )
                    + "\n"
                    for index in range(1, counts["orders"] + 1)
                ),
                encoding="utf-8",
            )
            (root / "files" / "order_items.csv").write_text(
                "order_id,quantity,unit_price\n"
                + "".join(
                    f"{((index - 1) % counts['orders']) + 1},1,1.000000000\n"
                    for index in range(1, counts["order_items"] + 1)
                ),
                encoding="utf-8",
            )

    def write_frozen_gold(self):
        """The frozen per-population gold the EL/T reward manifests name.

        `reference.gold.freeze_gold` writes this tree in a real workspace; the
        stand-in writes just the files reward.json points at, because release
        now verifies that every declared reward input RESOLVES in the shipped
        release (a reward naming a path that is not there is unscorable).
        """
        for pop, counts in sorted(self.gold.stage1.items()):
            gold_dir = self.answer_key_dir / "gold" / pop
            gold_dir.mkdir(parents=True, exist_ok=True)
            (gold_dir / "stage1_counts.json").write_text(
                json.dumps(counts, sort_keys=True) + "\n", encoding="utf-8"
            )
            for mart, csv_text in sorted(
                self.gold.stage2_csv.get(pop, {}).items()
            ):
                (gold_dir / f"{mart}.csv").write_text(csv_text, encoding="utf-8")
        files = {
            path.relative_to(self.answer_key_dir).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted((self.answer_key_dir / "gold").rglob("*"))
            if path.is_file()
        }
        (self.answer_key_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "task_id": self.task.task_id,
                    "task_content_hash": self.task.content_hash(),
                    "files": files,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def setUp(self):
        super().setUp()
        self.export()
        self.write_population_sources()
        self.write_frozen_gold()
        # validate-t's canonical-reachability evidence, which release re-reads.
        write_canonical_reachability(self.tmp, self.task)
        # The real variant-gate stages emit these trees before measuring them.
        # Keep this release test focused on the freeze contract with minimal,
        # leak-clean stand-ins for those already-certified bundles.
        variants_root = self.task_root / "variants"
        for variant in RLVR_TASK_VARIANTS:
            root = variants_root / variant.value
            task_dir = root / "task"
            task_dir.mkdir(parents=True)
            public_name = (
                "config.yaml"
                if variant is TaskVariant.EXTRACT_LOAD
                else "data_model.yaml"
            )
            (task_dir / public_name).write_text(
                f"unit: {variant.value}\n", encoding="utf-8"
            )
            (root / eltbench.REWARD_MANIFEST).write_text(
                json.dumps(eltbench.reward_manifest(self.task, self.gold, variant)),
                encoding="utf-8",
            )
        self.engine = FakeEngine(self.tmp)
        self.engine.tasks[self.task.task_id] = self.task
        for variant in RLVR_TASK_VARIANTS:
            stage = variant_gate_stage(variant).value
            self.engine.variant_reports[(self.task.task_id, stage)] = FakeReport(
                "pass",
                self.task.content_hash(),
                gates=VARIANT_GATE_NAMES[variant],
                task_id=variant_task_id(self.task.task_id, variant),
            )
        self.engine.audit_reports[self.task.task_id] = FakeReport(
            "pass", self.task.content_hash()
        )
        self.selection = FakeSelection(
            train=(self.task.task_id,),
            variants={
                self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)
            },
        )
        self.out_dir = self.tmp / "release" / "r1"

    def freeze(self):
        return release.freeze_release(self.engine, self.selection, self.out_dir)

    def publish_source_provenance(self) -> IngestProvenance:
        """Give release fixtures the same immutable evidence as typed ingest."""

        self.task_root.mkdir(parents=True, exist_ok=True)
        # The engine's task, which a test may have given an intake lineage:
        # intake evidence binds to the lineage ROOT, not to the current hash.
        task = self.engine.tasks.get(self.task.task_id, self.task)
        (self.task_root / "task_ir.json").write_text(
            task_to_json(task), encoding="utf-8"
        )
        record = IngestProvenance(
            task_id=self.task.task_id,
            task_content_hash=lineage_root_hash(task),
            source=SourceIdentity(
                pool=self.task.origin.value,
                origin=self.task.origin,
                selector="fixture/demo-v1",
                upstream_url="https://example.invalid/elt-taskgen-demo",
                upstream_revision="demo-v1",
                source_digest=hashlib.sha256(b"demo source fixture").hexdigest(),
                source_digest_kind="sha256-file",
                adapter_name="elt_taskgen.demo_fixture",
                adapter_version="test-v1",
                adapter_digest=hashlib.sha256(b"demo adapter fixture").hexdigest(),
                license=self.task.license,
                license_evidence="repository:LICENSE",
                selection_inputs={
                    "ingest_manifest": ProvenanceArtifact(
                        locator="manifest-schema:five-source-ingest-v1",
                        digest="3" * 64,
                        digest_kind="sha256-canonical-json-v1",
                    ),
                    "source_catalog": ProvenanceArtifact(
                        locator="manifest:catalog",
                        digest="4" * 64,
                        digest_kind="sha256-file",
                    ),
                },
            ),
        )
        publish_or_confirm(self.task_root, record)
        return record


class TestFreezeRelease(_FreezeReleaseHarness):
    def test_happy_path_layout_and_manifest(self):
        manifest = self.freeze()
        tid = self.task.task_id
        el_id = variant_task_id(tid, TaskVariant.EXTRACT_LOAD)
        t_id = variant_task_id(tid, TaskVariant.TRANSFORM)
        public_task = self.out_dir / "public" / tid
        self.assertTrue((public_task / "config.yaml").is_file())
        self.assertTrue((public_task / "data_model.yaml").is_file())
        self.assertTrue((public_task / "elt" / "main.tf").is_file())
        self.assertFalse((self.out_dir / "public" / el_id).exists())
        self.assertFalse((self.out_dir / "public" / t_id).exists())
        self.assertFalse((public_task / "sources").exists())
        self.assertTrue(
            (self.out_dir / "private" / tid / "answer_key" / "table.json").is_file()
        )
        semantic_task_ir = (
            self.out_dir / "private" / tid / release.SEMANTIC_TASK_IR_REL
        )
        self.assertTrue(semantic_task_ir.is_file())
        self.assertFalse(
            (self.out_dir / "public" / tid / release.SEMANTIC_TASK_IR_REL).exists()
        )
        self.assertTrue(
            (self.out_dir / "private" / el_id / eltbench.REWARD_MANIFEST).is_file()
        )
        self.assertTrue(
            (self.out_dir / "private" / t_id / eltbench.REWARD_MANIFEST).is_file()
        )
        self.assertFalse(
            (self.out_dir / "private" / tid / eltbench.REWARD_MANIFEST).exists()
        )
        self.assertTrue((self.out_dir / "release_manifest.json").is_file())
        self.assertTrue((self.out_dir / "checksums.sha256").is_file())
        self.assertEqual(manifest.schema_version, release.RELEASE_SCHEMA_VERSION)
        self.assertEqual(manifest.corpus_profile, release.COMBINED_CORPUS_PROFILE)
        self.assertEqual(manifest.public_layout, release.COMBINED_PUBLIC_LAYOUT)
        self.assertEqual(manifest.destinations, {tid: "snowflake"})
        self.assertEqual(
            manifest.destination_connector_versions,
            {tid: "4.1.2"},
        )
        self.assertEqual(manifest.tasks, {tid: self.task.content_hash()})
        self.assertEqual(manifest.splits, {tid: "train"})
        self.assertEqual(manifest.families, {tid: self.task.family_id})
        self.assertEqual(manifest.licenses, {tid: "CC0-1.0"})
        self.assertEqual(manifest.variants, {tid: ("extract_load", "transform")})
        self.assertEqual(
            [r.variant for r in manifest.variant_acceptance[tid]],
            ["extract_load", "transform"],
        )
        self.assertTrue(all(r.shipped for r in manifest.variant_acceptance[tid]))
        self.assertTrue(manifest.scorer_version)
        self.assertEqual(
            manifest.semantic_scorer_version, release.SEMANTIC_SCORER_VERSION
        )
        self.assertEqual(manifest.generator_version, release.GENERATOR_VERSION)
        # On-disk manifest round-trips to the returned one.
        on_disk = json.loads((self.out_dir / "release_manifest.json").read_text())
        self.assertEqual(on_disk, manifest.model_dump(mode="json"))

    def test_checksums_cover_every_file_and_verify(self):
        # This fixture has no DuckDB warehouse, so every non-comment checksum
        # line is a byte hash; skip the explanatory header.
        manifest = self.freeze()
        lines = (self.out_dir / "checksums.sha256").read_text().splitlines()
        self.assertTrue(lines[0].startswith("#"), lines[0])
        listed = {}
        for line in lines:
            if line.lstrip().startswith("#") or not line.strip():
                continue
            digest, rel = line.split("  ", 1)
            listed[rel] = digest
        actual_files = {
            p.relative_to(self.out_dir).as_posix()
            for p in self.out_dir.rglob("*")
            if p.is_file() and p.name != "checksums.sha256"
        }
        self.assertEqual(set(listed), actual_files)
        for rel, digest in listed.items():
            self.assertEqual(
                hashlib.sha256((self.out_dir / rel).read_bytes()).hexdigest(), digest, rel
            )
        # Manifest checksums cover public/ + private/ (everything but the manifest).
        self.assertEqual(
            set(manifest.checksums), actual_files - {"release_manifest.json"}
        )
        # No warehouses -> no exemptions: the kinds map stays empty, i.e. the
        # byte rule is still the default and the exemption is opt-in per file.
        self.assertEqual(manifest.checksum_kinds, {})
        self.assertEqual(manifest.warehouse_census, {})

    def test_verify_release_accepts_a_freshly_frozen_release(self):
        """The verifier agrees with the freezer on a release it just cut."""
        manifest = self.freeze()
        result = release.verify_release(self.out_dir)
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(result.release_id, manifest.release_id)
        self.assertEqual(result.schema_version, release.RELEASE_SCHEMA_VERSION)
        self.assertEqual(result.corpus_profile, release.COMBINED_CORPUS_PROFILE)
        self.assertEqual(result.files_checked, len(manifest.checksums) + 1)
        self.assertEqual(result.byte_pinned, result.files_checked)
        self.assertEqual(result.census_pinned, 0)
        self.assertEqual(result.unpinned_files, ())

    def test_verify_release_refuses_unsafe_manifest_paths_before_joining(self):
        """Manifest-controlled paths cannot traverse or change path dialect."""
        self.freeze()
        manifest_path = self.out_dir / "release_manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        task_id = self.task.task_id
        payload["tasks"]["../../outside"] = "0" * 64
        payload["checksums"]["../outside"] = "0" * 64
        payload["el_sources"][task_id]["primary"] = r"private\outside"
        manifest_path.chmod(0o644)
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        detail = "\n".join(result.failures)
        self.assertIn("tasks key '../../outside'", detail)
        self.assertIn("checksums path '../outside'", detail)
        self.assertIn("backslashes are forbidden", detail)

    def test_verify_release_refuses_a_pinned_file_symlink(self):
        """A matching target outside the tree is not a released regular file."""
        manifest = self.freeze()
        rel = next(
            path for path in sorted(manifest.checksums) if path.endswith(".yaml")
        )
        victim = self.out_dir / rel
        outside = self.tmp / "outside-target.yaml"
        outside.write_bytes(victim.read_bytes())
        victim.unlink()
        victim.symlink_to(outside)

        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any(rel in failure and "symbolic link" in failure for failure in result.failures),
            result.failures,
        )

    def test_freeze_refuses_symlinked_workspace_input_before_copying(self):
        outside = self.tmp / "simulated-host-secret.txt"
        outside.write_text("simulated host secret\n", encoding="utf-8")
        link = self.answer_key_dir / "unrelated-host-secret.txt"
        link.symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "symbolic links are forbidden"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_verify_release_refuses_duplicate_flat_checksum_entries(self):
        self.freeze()
        path = self.out_dir / "checksums.sha256"
        lines = path.read_text(encoding="utf-8").splitlines()
        entry = next(line for line in lines if line and not line.startswith("#"))
        path.chmod(0o644)
        path.write_text("\n".join((*lines, entry)) + "\n", encoding="utf-8")

        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("duplicate checksum entry" in failure for failure in result.failures),
            result.failures,
        )

    def test_verify_release_reports_a_malformed_flat_checksum_index(self):
        self.freeze()
        path = self.out_dir / "checksums.sha256"
        path.chmod(0o644)
        path.write_text("not a checksum line\n", encoding="utf-8")

        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("checksum index is invalid" in failure for failure in result.failures),
            result.failures,
        )

    def test_serving_verifier_refuses_reward_paths_outside_private_parent(self):
        manifest = self.freeze()
        el_id = variant_task_id(self.task.task_id, TaskVariant.EXTRACT_LOAD)
        reward_rel = f"private/{el_id}/{eltbench.REWARD_MANIFEST}"
        reward_path = self.out_dir / reward_rel
        payload = json.loads(reward_path.read_text(encoding="utf-8"))
        payload["expected"]["primary"] = "../../../../outside.json"
        reward_path.chmod(0o644)
        reward_path.write_text(json.dumps(payload), encoding="utf-8")
        (self.tmp / "outside.json").write_text("exists outside the release\n")

        on_disk = {
            path.relative_to(self.out_dir).as_posix()
            for path in self.out_dir.rglob("*")
            if path.is_file()
        } - {"checksums.sha256"}
        failures: list[str] = []

        def fail(rel: str, kind: str, detail: str) -> None:
            failures.append(f"{rel}: {kind}: {detail}")

        release._verify_serving_surface(self.out_dir, manifest, on_disk, fail)

        self.assertTrue(
            any("expected['primary'] names unsafe path" in item for item in failures),
            failures,
        )

    def test_verify_release_detects_tampering_and_extra_files(self):
        """Byte-pinned files still detect an edit; extras are not ignored."""
        self.freeze()
        target = self.out_dir / "public"
        victim = next(p for p in sorted(target.rglob("*")) if p.is_file())
        victim.chmod(0o644)
        victim.write_text(victim.read_text(encoding="utf-8") + "\n# tampered\n")
        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("sha256 mismatch" in f for f in result.failures), result.failures
        )

        planted = self.out_dir / "public" / "PLANTED.txt"
        planted.write_text("not covered by any pin\n")
        result = release.verify_release(self.out_dir)
        self.assertIn("public/PLANTED.txt", result.unpinned_files)
        self.assertFalse(result.ok)

    def test_schema_32_verify_requires_the_private_semantic_task_ir(self):
        self.freeze()
        semantic = (
            self.out_dir
            / "private"
            / self.task.task_id
            / release.SEMANTIC_TASK_IR_REL
        )
        semantic.chmod(0o644)
        semantic.unlink()
        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any(
                "private semantic TaskIR is missing" in failure
                for failure in result.failures
            ),
            result.failures,
        )

    def test_verify_release_refuses_a_widened_census_exemption(self):
        """The byte exemption covers DuckDB warehouses and nothing else.

        A manifest that claims the census rule for an ordinary file would let
        that file's bytes drift unchecked, so the verifier refuses the claim
        outright rather than falling back to bytes.
        """
        manifest = self.freeze()
        rel = next(r for r in sorted(manifest.checksums) if r.endswith(".yaml"))
        forged = manifest.model_dump(mode="json")
        forged["checksum_kinds"] = {rel: release.WAREHOUSE_CHECKSUM_KIND}
        forged["warehouse_census"] = {
            rel: {
                "census_version": eltbench.CENSUS_VERSION,
                "census_digest": manifest.checksums[rel],
                "row_counts": {},
                "row_digests": {},
            }
        }
        path = self.out_dir / "release_manifest.json"
        path.chmod(0o644)
        path.write_text(json.dumps(forged))
        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("DuckDB warehouses only" in f for f in result.failures),
            result.failures,
        )

    def test_released_files_read_only(self):
        self.freeze()
        mode = (self.out_dir / "release_manifest.json").stat().st_mode
        self.assertEqual(mode & 0o222, 0)

    def test_public_tree_has_no_private_content(self):
        self.freeze()
        public = self.out_dir / "public"
        blob = "\n".join(
            p.read_bytes().decode("utf-8", errors="ignore")
            for p in sorted(public.rglob("*")) if p.is_file()
        ).lower()
        normalized = " ".join(blob.split())
        self.assertNotIn("answer_key", normalized)
        self.assertNotIn(" ".join(demo_fixture.REFERENCE_SQL.split()).lower(), normalized)
        for pop in self.task.populations:
            self.assertNotIn(str(pop.seed), normalized)
        names = {p.name for p in public.rglob("*")}
        for private_name in ("table.json", "sort_key.json", "gt", "gold"):
            self.assertNotIn(private_name, names)
        self.assertFalse(any(p.suffix == ".duckdb" for p in public.rglob("*")))
        for token in ("load_plan", "sql_by_mart", "standalone duckdb"):
            self.assertNotIn(token, normalized)

    def test_refuses_existing_out_dir(self):
        self.out_dir.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.freeze()

    def test_refuses_missing_el_battery(self):
        stage = variant_gate_stage(TaskVariant.EXTRACT_LOAD).value
        self.engine.variant_reports.pop((self.task.task_id, stage))
        with self.assertRaisesRegex(ValueError, "certification phases are required"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_refuses_failed_t_battery(self):
        variant = TaskVariant.TRANSFORM
        stage = variant_gate_stage(variant).value
        self.engine.variant_reports[(self.task.task_id, stage)] = FakeReport(
            "fail",
            self.task.content_hash(),
            gates=VARIANT_GATE_NAMES[variant],
            task_id=variant_task_id(self.task.task_id, variant),
        )
        with self.assertRaisesRegex(ValueError, "certification phases are required"):
            self.freeze()

    def test_refuses_missing_final_audit(self):
        self.engine.audit_reports.pop(self.task.task_id)
        with self.assertRaisesRegex(ValueError, "no final audit report"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_refuses_stale_final_audit(self):
        self.engine.audit_reports[self.task.task_id] = FakeReport(
            "pass", "0" * 64
        )
        with self.assertRaisesRegex(ValueError, "audit is bound to stale"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_refuses_fatal_shadowed_final_verdict(self):
        with mock.patch.object(
            self.engine, "final_verdict", return_value="rejected"
        ):
            with self.assertRaisesRegex(ValueError, "final verdict is 'rejected'"):
                self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_refuses_unbound_current_selection_record(self):
        from elt_taskgen.corpus.selection import SelectionResult

        self.selection = SelectionResult(
            train=(self.task.task_id,),
            val=(),
            rejected={},
            variants={
                self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)
            },
        )
        with self.assertRaisesRegex(ValueError, "not bound.*task content"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_refuses_selection_bound_to_another_task_identity(self):
        from elt_taskgen.corpus.selection import SelectionResult

        self.selection = SelectionResult(
            train=(self.task.task_id,),
            val=(),
            rejected={},
            variants={
                self.task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)
            },
            task_content_hashes={self.task.task_id: "0" * 64},
            difficulty_measurements={self.task.task_id: "1" * 64},
        )
        with self.assertRaisesRegex(ValueError, "selection binds content hash"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_refuses_stale_el_acceptance_hash(self):
        variant = TaskVariant.EXTRACT_LOAD
        stage = variant_gate_stage(variant).value
        self.engine.variant_reports[(self.task.task_id, stage)] = FakeReport(
            "pass",
            "0" * 64,
            gates=VARIANT_GATE_NAMES[variant],
            task_id=variant_task_id(self.task.task_id, variant),
        )
        with self.assertRaisesRegex(ValueError, "stale content hash"):
            self.freeze()

    def test_refuses_anchor_origin(self):
        anchor = self.task.model_copy(update={"origin": Origin.ELTBENCH_ANCHOR})
        self.engine.tasks[self.task.task_id] = anchor
        with self.assertRaisesRegex(ValueError, "anchor"):
            self.freeze()

    def test_refuses_train_val_overlap(self):
        self.selection = FakeSelection(
            train=(self.task.task_id,), val=(self.task.task_id,)
        )
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.freeze()

    def test_refuses_family_straddling_split(self):
        sibling = self.task.model_copy(update={"task_id": "demo__customer_summary_b"})
        self.engine.tasks[sibling.task_id] = sibling
        for variant in RLVR_TASK_VARIANTS:
            stage = variant_gate_stage(variant).value
            self.engine.variant_reports[(sibling.task_id, stage)] = FakeReport(
                "pass",
                sibling.content_hash(),
                gates=VARIANT_GATE_NAMES[variant],
                task_id=variant_task_id(sibling.task_id, variant),
            )
        self.engine.audit_reports[sibling.task_id] = FakeReport(
            "pass", sibling.content_hash()
        )
        both = tuple(v.value for v in RLVR_TASK_VARIANTS)
        self.selection = FakeSelection(
            train=(self.task.task_id,),
            val=(sibling.task_id,),
            variants={self.task.task_id: both, sibling.task_id: both},
        )
        with self.assertRaisesRegex(ValueError, "straddle"):
            self.freeze()

    def test_refuses_empty_selection(self):
        self.selection = FakeSelection()
        with self.assertRaisesRegex(ValueError, "empty"):
            self.freeze()

    def test_refuses_unsafe_selection_task_id_before_workspace_access(self):
        task_id = "../../outside"
        self.selection = FakeSelection(
            train=(task_id,),
            variants={task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)},
        )
        with self.assertRaisesRegex(ValueError, "unsafe task_id"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_refuses_selection_without_explicit_pair(self):
        self.selection = FakeSelection(train=(self.task.task_id,))
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.freeze()

    def test_refuses_selection_with_only_el(self):
        self.selection = FakeSelection(
            train=(self.task.task_id,),
            variants={self.task.task_id: (TaskVariant.EXTRACT_LOAD.value,)},
        )
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.freeze()

    def test_refuses_missing_workspace_trees(self):
        shutil.rmtree(self.answer_key_dir)
        with self.assertRaisesRegex(ValueError, "answer key"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_refuses_leaked_public_bundle(self):
        planted = self.task_root / "task" / "notes.txt"
        planted.write_text(demo_fixture.REFERENCE_SQL)
        with self.assertRaisesRegex(ValueError, "leak"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_release_never_mutates_workspace(self):
        before = tree_digest(self.task_root)
        self.freeze()
        self.assertEqual(tree_digest(self.task_root), before)

    # -- the GRADED EL source roots (X1) -----------------------------------

    def test_release_ships_every_el_population_source(self):
        """The __el reward is graded on every population it NAMES.

        The release used to ship one rendered tree — development, inside the
        public bundle — so a perfect load of everything the release contained
        scored 1.0 on development and 0.0 on every graded population, whose
        sources existed only in the workspace.
        """
        manifest = self.freeze()
        tid = self.task.task_id
        el_id = variant_task_id(tid, TaskVariant.EXTRACT_LOAD)
        reward = json.loads(
            (self.out_dir / "private" / el_id / eltbench.REWARD_MANIFEST).read_text()
        )
        self.assertTrue(reward["expected"])
        for pop in reward["expected"]:
            rel = f"private/{tid}/populations/{pop}/rendered"
            self.assertEqual(manifest.el_sources[tid][pop], rel)
            self.assertEqual(reward["sources"][pop], f"populations/{pop}/rendered")
            csv_rel = f"{rel}/files/order_items.csv"
            self.assertTrue((self.out_dir / csv_rel).is_file(), csv_rel)
            # Byte-pinned like any other file: no census exemption widening.
            self.assertIn(csv_rel, manifest.checksums)
            self.assertNotIn(csv_rel, manifest.checksum_kinds)
        self.assertTrue(release.verify_release(self.out_dir).ok)

    def test_freeze_refuses_a_missing_population_source(self):
        shutil.rmtree(self.task_root / "populations" / "counterfactual" / "rendered")
        with self.assertRaisesRegex(ValueError, "counterfactual"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())  # staging cleaned up

    def test_freeze_refuses_a_population_source_missing_a_table(self):
        """The shipped tree must BE the graded surface, not a lookalike."""
        (
            self.task_root / "populations" / "primary" / "rendered"
            / "mongodb" / "orders.jsonl"
        ).unlink()
        with self.assertRaisesRegex(FileNotFoundError, "orders"):
            self.freeze()

    def test_freeze_refuses_source_value_outside_strict_decimal_contract(self):
        source = (
            self.task_root
            / "populations"
            / "primary"
            / "rendered"
            / "files"
            / "order_items.csv"
        )
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "1.000000000", "1.0000000001", 1
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "strict source load failed"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_freeze_refuses_source_counts_that_disagree_with_gold(self):
        source = (
            self.task_root
            / "populations"
            / "counterfactual"
            / "rendered"
            / "files"
            / "order_items.csv"
        )
        lines = source.read_text(encoding="utf-8").splitlines()
        source.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "counts disagree"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_freeze_refuses_gold_outside_canonical_decimal_contract(self):
        gold_path = (
            self.answer_key_dir
            / "gold"
            / "primary"
            / f"{demo_fixture.MART_NAME}.csv"
        )
        gold_path.write_text(
            gold_path.read_text(encoding="utf-8").replace(
                "45.0", "45.0000000001", 1
            ),
            encoding="utf-8",
        )
        manifest_path = self.answer_key_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rel = gold_path.relative_to(self.answer_key_dir).as_posix()
        manifest["files"][rel] = hashlib.sha256(gold_path.read_bytes()).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "gold is not canonically"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_verify_refuses_pinned_but_strict_unscorable_release(self):
        """Byte integrity cannot substitute for semantic loadability."""
        source = (
            self.task_root
            / "populations"
            / "primary"
            / "rendered"
            / "files"
            / "order_items.csv"
        )
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "1.000000000", "1.0000000001", 1
            ),
            encoding="utf-8",
        )
        # Simulate a release created by the old freezer while retaining the
        # current manifest/checksums.  The verifier must independently run
        # the new semantic check and catch what byte pins cannot express.
        with mock.patch(
            "elt_taskgen.verification.release_portability."
            "validate_release_portability"
        ):
            self.freeze()
        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("strict source load failed" in failure for failure in result.failures),
            result.failures,
        )

    def test_tampered_el_source_fails_verify(self):
        self.freeze()
        tid = self.task.task_id
        rel = f"private/{tid}/populations/primary/rendered/files/order_items.csv"
        victim = self.out_dir / rel
        victim.chmod(0o644)
        victim.write_text(victim.read_text() + "2\n")
        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(any(rel in f for f in result.failures), result.failures)

    def test_legacy_reward_json_without_sources_still_freezes(self):
        """A workspace whose EL battery predates the `sources` key re-releases.

        The manifest is authoritative for where the roots landed, so an old
        reward.json is not a reason to re-run the EL gate stage.
        """
        reward_path = (
            self.task_root / "variants" / TaskVariant.EXTRACT_LOAD.value
            / eltbench.REWARD_MANIFEST
        )
        payload = json.loads(reward_path.read_text())
        payload.pop("sources")
        reward_path.write_text(json.dumps(payload))
        manifest = self.freeze()
        tid = self.task.task_id
        self.assertEqual(
            sorted(manifest.el_sources[tid]), sorted(self.gold.stage1)
        )

    def test_freeze_refuses_a_reward_declaring_another_source_layout(self):
        reward_path = (
            self.task_root / "variants" / TaskVariant.EXTRACT_LOAD.value
            / eltbench.REWARD_MANIFEST
        )
        payload = json.loads(reward_path.read_text())
        payload["sources"]["primary"] = "sources/primary"
        reward_path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "source root"):
            self.freeze()

    # -- identifiers, provenance, roster identity ---------------------------

    def test_release_refuses_overlong_source_identifiers(self):
        """A bundle emitted by pre-bound code must never be frozen silently."""
        overlong = "s" * 100
        overlong_bucket = f"{overlong.replace('_', '-')}-bucket"
        config = self.task_root / "task" / "config.yaml"
        payload = yaml.safe_load(config.read_text())
        payload["snowflake"]["config"]["database"] = overlong
        payload["postgres"]["config"]["database"] = overlong
        payload["aws_s3"] = {
            "data": [
                {
                    "path": f"s3://{overlong_bucket}/t.jsonl",
                    "sync_mode": "full_refresh_append",
                    "table": "t",
                }
            ]
        }
        payload["Airbyte"]["config"]["s3_definition_id"] = (
            "69589781-7828-43c5-9f63-8925b1c1ccc2"
        )
        config.write_text(yaml.safe_dump(payload))
        with self.assertRaisesRegex(ValueError, "source identifier"):
            self.freeze()
        self.assertFalse(self.out_dir.exists())

    def test_manifest_records_the_runtime_environment(self):
        manifest = self.freeze()
        for key in (
            "python",
            "duckdb",
            "sqlglot",
            "pydantic",
            "pyyaml",
            "python-hcl2",
            "lark",
        ):
            self.assertIn(key, manifest.environment)
        # The suite runs under the locked environment, so there is no drift.
        self.assertEqual(manifest.environment_drift, {})

    def test_release_refuses_environment_drift_unless_allowed(self):
        original = release.environment_drift
        release.environment_drift = lambda *a, **k: {"duckdb": ("9.9.9", "1.5.5")}
        self.addCleanup(setattr, release, "environment_drift", original)
        with self.assertRaisesRegex(ValueError, "uv.lock"):
            self.freeze()
        manifest = release.freeze_release(
            self.engine, self.selection, self.out_dir, allow_unlocked_env=True
        )
        self.assertEqual(
            manifest.environment_drift,
            {"duckdb": "installed 9.9.9, locked 1.5.5"},
        )

    def test_the_operator_flag_also_arrives_on_the_engine(self):
        """The CLI stage runner calls a stage with no argv, so it parks the
        operator's `--allow-unlocked-env` on the engine; either channel is
        enough, and neither one HIDES the drift (it is recorded)."""
        original = release.environment_drift
        release.environment_drift = lambda *a, **k: {"sqlglot": ("1.0", "30.16.0")}
        self.addCleanup(setattr, release, "environment_drift", original)
        self.engine.allow_unlocked_env = True
        manifest = self.freeze()
        self.assertEqual(
            manifest.environment_drift,
            {"sqlglot": "installed 1.0, locked 30.16.0"},
        )

    def test_manifest_carries_scorer_rosters_and_release_id_depends_on_them(self):
        from elt_taskgen.verification import gates as gates_mod

        manifest = self.freeze()
        self.assertEqual(manifest.scorer_version, gates_mod.SCORER_VERSION)
        self.assertEqual(manifest.roster_digest, gates_mod.ROSTER_DIGEST)
        for variant in RLVR_TASK_VARIANTS:
            self.assertEqual(
                manifest.gate_rosters[variant.value],
                tuple(VARIANT_GATE_NAMES[variant]),
            )
        original = release._scorer_version
        release._scorer_version = lambda: "9.9.9"
        self.addCleanup(setattr, release, "_scorer_version", original)
        other = release.freeze_release(
            self.engine, self.selection, self.tmp / "release" / "r2"
        )
        self.assertNotEqual(other.release_id, manifest.release_id)

        original_semantic = release.SEMANTIC_SCORER_VERSION
        release.SEMANTIC_SCORER_VERSION = "9.9.9"
        self.addCleanup(
            setattr,
            release,
            "SEMANTIC_SCORER_VERSION",
            original_semantic,
        )
        semantic_other = release.freeze_release(
            self.engine, self.selection, self.tmp / "release" / "r3"
        )
        self.assertEqual(semantic_other.semantic_scorer_version, "9.9.9")
        self.assertNotEqual(semantic_other.release_id, other.release_id)

    def test_legacy_manifest_without_the_new_fields_still_loads(self):
        """Every 2.0 manifest must keep PARSING (defaults identify it)."""
        manifest = self.freeze()
        raw = manifest.model_dump(mode="json")
        for key in (
            "el_sources",
            "population_relations",
            "gate_rosters",
            "roster_digest",
            "environment",
            "environment_drift",
            "semantic_release_id",
            "runtime_bundle_ids",
            "certification_ids",
            "certification_matrix",
        ):
            raw.pop(key)
        raw["schema_version"] = "2.0"
        legacy = release.ReleaseManifest.model_validate(raw)
        self.assertEqual(legacy.el_sources, {})
        self.assertEqual(legacy.gate_rosters, {})
        self.assertEqual(legacy.environment, {})
        # The empty defaults are what IDENTIFY a pre-3.3 manifest: no layered
        # identities, so the layered checks never judge it.
        self.assertEqual(legacy.semantic_release_id, "")
        self.assertEqual(legacy.runtime_bundle_ids, {})
        self.assertEqual(legacy.certification_ids, {})
        self.assertEqual(legacy.certification_matrix, {})

    def test_stale_scorer_battery_is_a_refusal_not_a_crash(self):
        """The real ledger shape after a roster grew: refuse with 're-run'."""
        variant = TaskVariant.EXTRACT_LOAD
        stage = variant_gate_stage(variant).value
        self.engine.variant_reports[(self.task.task_id, stage)] = FakeReport(
            "pass",
            self.task.content_hash(),
            gates=VARIANT_GATE_NAMES[variant],
            task_id=variant_task_id(self.task.task_id, variant),
            scorer_version="1.0.0",
            roster_digest="",
        )
        records = release.variant_acceptance(self.engine, self.task)
        record = records[variant.value]
        self.assertFalse(record.accepted)
        self.assertIn("scorer", record.refusal_reason)
        self.assertIn("re-run", record.refusal_reason)
        with self.assertRaisesRegex(ValueError, "certification phases are required"):
            self.freeze()

    def test_no_warehouse_means_no_census_reconciliation(self):
        """A stand-in bundle with NO census certificate is not reconciled.

        The check is driven by the certificate, not by what happens to be on
        disk (otherwise deleting every warehouse would skip it — the drift it
        exists to catch). These synthetic trees carry no
        reports/warehouse_census.json, so there is nothing they can
        contradict, and freeze proceeds with an empty census map.
        """
        certificate = self.task_root / eltbench.WAREHOUSE_CENSUS_EVIDENCE_REL
        self.assertFalse(certificate.exists(), "fixture must carry no certificate")
        manifest = self.freeze()
        self.assertEqual(manifest.warehouse_census, {})
        self.assertEqual(manifest.checksum_kinds, {})

    def test_verify_refuses_an_el_bundle_without_serving_coverage(self):
        """A config stanza no serving entry covers is an unserveable source.

        Byte pins cannot see this: both files are exactly as frozen. What is
        wrong is the RELATION between them — the public bundle declares a
        table the private serving contract never says how to stand up.
        """
        config = self.task_root / "task" / "config.yaml"
        payload = yaml.safe_load(config.read_text())
        payload["postgres"]["config"]["tables"].append("smuggled_table")
        config.write_text(yaml.safe_dump(payload))
        self.freeze()
        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("does not cover" in f and "smuggled_table" in f
                for f in result.failures),
            result.failures,
        )

    # -- population relations (G4) -----------------------------------------

    def _make_rearrangement(self):
        """primary and resampled holding the SAME rows in a different order.

        The provided-rows shape (WikiDBs): the resampled split perturbs no
        value, so it carries no memorization signal of its own — which the
        manifest must SAY, or a trainer aggregating per-population rewards
        silently double-counts primary.
        """
        rows = {
            "customers": [{"customer_id": i} for i in range(4)],
            "orders": [{"order_id": i} for i in range(3)],
            "order_items": [{"order_item_id": i} for i in range(2)],
        }
        for pop, order in (("primary", 1), ("resampled", -1)):
            out = self.task_root / "populations" / pop / "rows"
            out.mkdir(parents=True, exist_ok=True)
            for table, table_rows in rows.items():
                (out / f"{table}.jsonl").write_text(
                    "".join(
                        json.dumps(row, sort_keys=True) + "\n"
                        for row in table_rows[::order]
                    ),
                    encoding="utf-8",
                )

    def test_reward_manifest_records_a_proven_rearrangement(self):
        from elt_taskgen.verification import gates as gates_mod

        self._make_rearrangement()
        relations = eltbench.population_relations(
            self.task, self.task_root / "populations"
        )
        self.assertEqual(
            relations,
            {"resampled": gates_mod.POPULATION_RELATION_REARRANGEMENT},
        )
        manifest = eltbench.reward_manifest(
            self.task,
            self.gold,
            TaskVariant.EXTRACT_LOAD,
            population_relations=relations,
        )
        self.assertEqual(manifest["population_relations"], relations)
        # A constructed pool claims NOTHING, so its manifest is byte-unchanged.
        plain = eltbench.reward_manifest(
            self.task, self.gold, TaskVariant.EXTRACT_LOAD
        )
        self.assertNotIn("population_relations", plain)
        self.assertNotIn("memorization_evidence", plain)

    def test_a_moved_population_is_not_claimed_as_a_rearrangement(self):
        self._make_rearrangement()
        moved = self.task_root / "populations" / "resampled" / "rows" / "orders.jsonl"
        moved.write_text('{"order_id": 99}\n', encoding="utf-8")
        self.assertEqual(
            eltbench.population_relations(self.task, self.task_root / "populations"),
            {},
        )

    def test_release_manifest_carries_population_relations(self):
        from elt_taskgen.verification import gates as gates_mod

        self._make_rearrangement()
        reward_path = (
            self.task_root / "variants" / TaskVariant.EXTRACT_LOAD.value
            / eltbench.REWARD_MANIFEST
        )
        payload = json.loads(reward_path.read_text())
        payload["population_relations"] = {
            "resampled": gates_mod.POPULATION_RELATION_REARRANGEMENT
        }
        reward_path.write_text(json.dumps(payload))
        manifest = self.freeze()
        self.assertEqual(
            manifest.population_relations[self.task.task_id],
            {"resampled": gates_mod.POPULATION_RELATION_REARRANGEMENT},
        )
        self.assertTrue(release.verify_release(self.out_dir).ok)


class TestReleaseSourceProvenance(_FreezeReleaseHarness):
    """Schema-3.5 provenance is explicit, pinned, and independently checked."""

    @staticmethod
    def _repin_manifest(out_dir: Path, payload: dict) -> None:
        manifest_path = out_dir / "release_manifest.json"
        manifest_path.chmod(0o644)
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        flat_path = out_dir / "checksums.sha256"
        flat_path.chmod(0o644)
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        lines = [
            f"{digest}  release_manifest.json"
            if line.endswith("  release_manifest.json")
            and not line.lstrip().startswith("#")
            else line
            for line in flat_path.read_text(encoding="utf-8").splitlines()
        ]
        flat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_freeze_copies_and_binds_the_exact_typed_record(self) -> None:
        record = self.publish_source_provenance()
        manifest = self.freeze()
        task_id = self.task.task_id
        rel = release_provenance_rel(task_id)

        self.assertEqual(manifest.source_provenance[task_id], record)
        self.assertEqual(
            manifest.source_provenance_digests[task_id],
            record.evidence_digest(),
        )
        self.assertIn(rel, manifest.checksums)
        self.assertEqual(
            (self.out_dir / rel).read_bytes(), record.deterministic_bytes()
        )
        result = release.verify_release(self.out_dir)
        self.assertTrue(result.ok, result.failures)

    def test_a_task_whose_hash_moved_after_intake_still_verifies(self) -> None:
        """Intake evidence binds to the lineage ROOT, and authoring moves the
        content hash away from it. The released private TaskIR therefore has to
        carry its revisions: without them it read as its own lineage root and
        every authored task failed its own freeze (batch50, 2026-09-19)."""
        intake_hash = "a" * 64
        moved = self.task.model_copy(
            update={
                "revisions": (
                    TaskRevision(
                        revision=1,
                        reason="initial intake revision",
                        content_hash=intake_hash,
                    ),
                )
            }
        )
        # Revisions are not hashed, so only the lineage moves here.
        self.assertNotEqual(intake_hash, moved.content_hash())
        self.assertEqual(moved.content_hash(), self.task.content_hash())
        self.engine.tasks[self.task.task_id] = moved
        record = self.publish_source_provenance()
        self.assertEqual(record.task_content_hash, intake_hash)

        self.freeze()
        released = TaskIR.model_validate_json(
            (self.out_dir / "private" / self.task.task_id / release.SEMANTIC_TASK_IR_REL)
            .read_text(encoding="utf-8")
        )
        self.assertEqual(lineage_root_hash(released), intake_hash)
        self.assertEqual(released.content_hash(), moved.content_hash())
        result = release.verify_release(self.out_dir)
        self.assertTrue(result.ok, result.failures)

    def test_provenance_rotates_release_identity_without_moving_task_hash(self) -> None:
        without = self.freeze()
        record = self.publish_source_provenance()
        with_provenance = release.freeze_release(
            self.engine,
            self.selection,
            self.tmp / "release" / "r2",
        )

        self.assertEqual(without.tasks, with_provenance.tasks)
        self.assertNotEqual(without.release_id, with_provenance.release_id)
        self.assertNotEqual(
            without.semantic_release_id,
            with_provenance.semantic_release_id,
        )
        self.assertEqual(
            with_provenance.source_provenance_digests[self.task.task_id],
            record.evidence_digest(),
        )

    def test_verifier_rejects_manifest_sidecar_disagreement(self) -> None:
        self.publish_source_provenance()
        self.freeze()
        manifest_path = self.out_dir / "release_manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["source_provenance"][self.task.task_id]["source"][
            "selector"
        ] = "forged-selector"
        self._repin_manifest(self.out_dir, payload)

        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        detail = "\n".join(result.failures)
        self.assertIn("provenance/ingest_provenance.json", detail)
        self.assertIn("sidecar record disagrees", detail)

    def test_verifier_independently_rejects_a_resealed_wrong_lineage(self) -> None:
        self.publish_source_provenance()
        self.freeze()
        manifest_path = self.out_dir / "release_manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        task_id = self.task.task_id
        rel = release_provenance_rel(task_id)
        bad_record = IngestProvenance.model_validate(
            payload["source_provenance"][task_id]
            | {"task_content_hash": "f" * 64}
        )

        sidecar = self.out_dir / rel
        sidecar.chmod(0o644)
        sidecar.write_bytes(bad_record.deterministic_bytes())
        sidecar.chmod(0o444)
        payload["source_provenance"][task_id] = bad_record.model_dump(mode="json")
        payload["source_provenance_digests"][task_id] = bad_record.evidence_digest()
        payload["checksums"][rel] = hashlib.sha256(sidecar.read_bytes()).hexdigest()

        payload["release_id"] = release._combined_release_id(
            destinations=payload["destinations"],
            destination_connector_versions=payload[
                "destination_connector_versions"
            ],
            splits=payload["splits"],
            tasks=payload["tasks"],
            public_runtime_checksums={
                path: digest
                for path, digest in payload["checksums"].items()
                if path.startswith("public/")
            },
            variants=payload["variants"],
            rejected_variants=payload["rejected_variants"],
            el_sources=payload["el_sources"],
            scorer_version=payload["scorer_version"],
            roster_digest=payload["roster_digest"],
            semantic_scorer_version=payload["semantic_scorer_version"],
            source_provenance_digests=payload["source_provenance_digests"],
        )
        payload["semantic_release_id"] = release._semantic_release_id(
            tasks=payload["tasks"],
            el_sources=payload["el_sources"],
            private_checksums=release._private_semantic_checksums(
                payload["checksums"], payload["tasks"]
            ),
            scorer_version=payload["scorer_version"],
            roster_digest=payload["roster_digest"],
            semantic_scorer_version=payload["semantic_scorer_version"],
        )
        payload["runtime_bundle_ids"] = {
            tid: release._runtime_bundle_id(
                semantic_release_id=payload["semantic_release_id"],
                task_id=tid,
                destination=payload["destinations"][tid],
                public_checksums=release._public_task_checksums(
                    payload["checksums"], tid
                ),
                private_runtime_checksums=release._private_runtime_checksums(
                    payload["checksums"], tid
                ),
            )
            for tid in payload["tasks"]
        }
        payload["certification_ids"] = {
            tid: release._certification_id(
                runtime_bundle_id=payload["runtime_bundle_ids"][tid],
                matrix=payload["certification_matrix"][
                    payload["destinations"][tid]
                ],
                validate_matrix=False,
            )
            for tid in payload["tasks"]
        }

        flat_path = self.out_dir / "checksums.sha256"
        flat_path.chmod(0o644)
        lines = [
            f"{payload['checksums'][rel]}  {rel}" if line.endswith(f"  {rel}") else line
            for line in flat_path.read_text(encoding="utf-8").splitlines()
        ]
        flat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._repin_manifest(self.out_dir, payload)

        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        detail = "\n".join(result.failures)
        self.assertIn("not bound to released TaskIR", detail)
        self.assertIn("lineage root", detail)

    def test_schema_34_release_without_provenance_keeps_verifying(self) -> None:
        self.freeze()
        manifest_path = self.out_dir / "release_manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["schema_version"] = "3.4"
        payload.pop("source_provenance")
        payload.pop("source_provenance_digests")
        self._repin_manifest(self.out_dir, payload)

        result = release.verify_release(self.out_dir)
        self.assertTrue(result.ok, result.failures)


class TestLayeredIdentities(_FreezeReleaseHarness):
    """Schema-3.4 chained layered identities (IR-006).

    Each test freezes twice and asserts where a change originates and how it
    propagates down the semantic -> runtime -> certification chain. The legacy
    release_id's FROZEN behavior — including its blindness to private bytes —
    is asserted, never altered.
    """

    def freeze_again(self, name: str = "r2"):
        return release.freeze_release(
            self.engine, self.selection, self.tmp / "release" / name
        )

    def _forge_manifest(self, out_dir: Path, mutate) -> None:
        """Rewrite the frozen manifest and re-pin its OWN byte hash in
        checksums.sha256, so only the identity checks can fire."""
        manifest_path = out_dir / "release_manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        mutate(payload)
        manifest_path.chmod(0o644)
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        flat_path = out_dir / "checksums.sha256"
        flat_path.chmod(0o644)
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        lines = [
            f"{digest}  release_manifest.json"
            if (
                line.endswith("  release_manifest.json")
                and not line.lstrip().startswith("#")
            )
            else line
            for line in flat_path.read_text(encoding="utf-8").splitlines()
        ]
        flat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_private_population_bytes_rotate_the_full_identity_chain(self):
        first = self.freeze()
        self.assertTrue(
            first.semantic_release_id.startswith(release.SEMANTIC_ID_PREFIX)
        )
        source_sql = (
            self.task_root / "populations" / "counterfactual" / "rendered"
            / "postgres" / "customers.sql"
        )
        source_sql.write_text(
            source_sql.read_text(encoding="utf-8").replace(
                "'Customer '", "'Buyer '"
            ),
            encoding="utf-8",
        )
        second = self.freeze_again()
        self.assertNotEqual(second.semantic_release_id, first.semantic_release_id)
        self.assertNotEqual(second.runtime_bundle_ids, first.runtime_bundle_ids)
        self.assertNotEqual(second.certification_ids, first.certification_ids)
        # The exact IR-006 gap, kept frozen-compatible: the legacy release_id
        # is blind to private bytes (it binds el_sources path STRINGS and
        # public bytes only) and does not move.
        self.assertEqual(second.release_id, first.release_id)

    def test_gold_bytes_rotate_the_full_identity_chain(self):
        first = self.freeze()
        gold_csv = (
            self.answer_key_dir / "gold" / "counterfactual"
            / f"{demo_fixture.MART_NAME}.csv"
        )
        gold_csv.write_text(
            gold_csv.read_text(encoding="utf-8") + "3,1,9.0\n", encoding="utf-8"
        )
        gold_manifest = self.answer_key_dir / "manifest.json"
        payload = json.loads(gold_manifest.read_text(encoding="utf-8"))
        rel = gold_csv.relative_to(self.answer_key_dir).as_posix()
        payload["files"][rel] = hashlib.sha256(gold_csv.read_bytes()).hexdigest()
        gold_manifest.write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        second = self.freeze_again()
        self.assertNotEqual(second.semantic_release_id, first.semantic_release_id)
        self.assertNotEqual(second.runtime_bundle_ids, first.runtime_bundle_ids)
        self.assertNotEqual(second.certification_ids, first.certification_ids)
        self.assertEqual(second.certification_matrix, first.certification_matrix)

    def test_public_runtime_bytes_move_runtime_and_certification_not_semantic(self):
        first = self.freeze()
        tid = self.task.task_id
        doc = self.task_root / "task" / "documentation" / "README.md"
        doc.write_text(
            doc.read_text(encoding="utf-8") + "\nOne more overview line.\n",
            encoding="utf-8",
        )
        second = self.freeze_again()
        self.assertNotEqual(
            second.runtime_bundle_ids[tid], first.runtime_bundle_ids[tid]
        )
        self.assertNotEqual(
            second.certification_ids[tid], first.certification_ids[tid]
        )
        self.assertEqual(second.semantic_release_id, first.semantic_release_id)
        # Frozen behavior asserted, not altered: the legacy release_id binds
        # the public runtime bytes, so it moves too.
        self.assertNotEqual(second.release_id, first.release_id)

    def test_destination_runtime_contract_invalidates_runtime_and_certification(self):
        import dataclasses

        from elt_taskgen import destinations as destinations_mod

        first = self.freeze()
        tid = self.task.task_id
        contract = destinations_mod.DESTINATION_CONTRACTS[
            destinations_mod.Destination.SNOWFLAKE
        ]
        bumped = dataclasses.replace(contract, connector_version="9.9.9")
        with mock.patch.dict(
            destinations_mod.DESTINATION_CONTRACTS,
            {destinations_mod.Destination.SNOWFLAKE: bumped},
        ):
            # Public config deliberately carries no connector-version keys.
            # Rebuild only the private exact connector contract under the new
            # pin, then freeze the unchanged original-shaped solver bundle.
            config_payload = yaml.safe_load(
                (self.task_root / "task" / "config.yaml").read_text()
            )
            (self.answer_key_dir / eltbench.PRIVATE_AIRBYTE_CONNECTOR_CONTRACT).write_text(
                json.dumps(
                    eltbench.build_airbyte_connector_contract(config_payload),
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            # The changed contract stales validate-t's record; re-record it.
            write_canonical_reachability(self.tmp, self.task)
            second = self.freeze_again()
        self.assertNotEqual(
            second.certification_ids[tid], first.certification_ids[tid]
        )
        self.assertEqual(
            second.certification_matrix["snowflake"]["destination_connector"],
            "9.9.9",
        )
        # The exact connector contract is private runtime metadata: it must not
        # contaminate semantic identity, but it must rotate the runtime bundle.
        self.assertEqual(second.semantic_release_id, first.semantic_release_id)
        self.assertEqual(
            release._runtime_bundle_id(
                semantic_release_id=first.semantic_release_id,
                task_id=tid,
                destination="snowflake",
                public_checksums=release._public_task_checksums(
                    first.checksums, tid
                ),
                private_runtime_checksums=release._private_runtime_checksums(
                    first.checksums, tid
                ),
            ),
            first.runtime_bundle_ids[tid],
        )
        self.assertNotEqual(
            second.runtime_bundle_ids[tid], first.runtime_bundle_ids[tid]
        )
        # Frozen behavior asserted: destination_connector_versions feeds the
        # legacy release_id, so it moves.
        self.assertNotEqual(second.release_id, first.release_id)

    def test_dbt_image_or_driver_change_invalidates_certification_only(self):
        from elt_taskgen import runtime_matrix

        first = self.freeze()
        tid = self.task.task_id
        with mock.patch.object(
            runtime_matrix, "runner_images_digest", return_value="0" * 64
        ):
            second = self.freeze_again()
        self.assertNotEqual(
            second.certification_ids[tid], first.certification_ids[tid]
        )
        self.assertEqual(
            second.certification_matrix["snowflake"]["runner_images_digest"],
            "0" * 64,
        )
        self.assertEqual(second.semantic_release_id, first.semantic_release_id)
        self.assertEqual(second.runtime_bundle_ids, first.runtime_bundle_ids)
        self.assertEqual(second.release_id, first.release_id)

    def test_warehouse_settings_change_invalidates_certification_only(self):
        import dataclasses

        from elt_taskgen import destinations as destinations_mod

        first = self.freeze()
        tid = self.task.task_id
        contract = destinations_mod.DESTINATION_CONTRACTS[
            destinations_mod.Destination.SNOWFLAKE
        ]
        bumped = dataclasses.replace(contract, fixed_schema="OTHER_SCHEMA")
        with mock.patch.dict(
            destinations_mod.DESTINATION_CONTRACTS,
            {destinations_mod.Destination.SNOWFLAKE: bumped},
        ):
            second = self.freeze_again()
        self.assertNotEqual(
            second.certification_ids[tid], first.certification_ids[tid]
        )
        self.assertEqual(second.semantic_release_id, first.semantic_release_id)
        self.assertEqual(second.runtime_bundle_ids, first.runtime_bundle_ids)
        self.assertEqual(second.release_id, first.release_id)

    def test_one_semantic_task_yields_three_distinct_runtime_bundle_ids(self):
        import inspect

        from elt_taskgen import destinations as destinations_mod

        checks = {"public/x/config.yaml": "0" * 64}
        private_checks = {
            "private/x/answer_key/runtime/airbyte_connector_contract.json": (
                "1" * 64
            )
        }
        ids = {
            dest.value: release._runtime_bundle_id(
                semantic_release_id="semantic-example",
                task_id="x",
                destination=dest.value,
                public_checksums=checks,
                private_runtime_checksums=private_checks,
            )
            for dest in destinations_mod.Destination
        }
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids.values())), 3)
        for value in ids.values():
            self.assertTrue(value.startswith(release.RUNTIME_BUNDLE_ID_PREFIX))
        # API-level Phase-2 guarantee: the semantic identity accepts no
        # destination-shaped input at all.
        params = inspect.signature(release._semantic_release_id).parameters
        self.assertFalse(
            any("destination" in name for name in params), sorted(params)
        )

    def test_three_destination_freezes_share_semantics_but_not_runtime(self):
        """Real projections, not synthetic hash arguments, obey the layers."""
        from elt_taskgen import destinations as destinations_mod

        manifests = {}
        for destination in destinations_mod.Destination:
            eltbench.export_task(
                self.task,
                self.gold,
                self.task_dir,
                self.answer_key_dir,
                destination=destination,
            )
            # A new bundle-root contract needs validate-t's record for it.
            write_canonical_reachability(self.tmp, self.task)
            out_dir = self.tmp / "release" / destination.value
            manifests[destination.value] = release.freeze_release(
                self.engine, self.selection, out_dir
            )

        semantic_ids = {m.semantic_release_id for m in manifests.values()}
        runtime_ids = {
            m.runtime_bundle_ids[self.task.task_id] for m in manifests.values()
        }
        certification_ids = {
            m.certification_ids[self.task.task_id] for m in manifests.values()
        }
        self.assertEqual(len(semantic_ids), 1)
        self.assertEqual(len(runtime_ids), len(destinations_mod.Destination))
        self.assertEqual(
            len(certification_ids), len(destinations_mod.Destination)
        )

        # Gold is byte-identical across the projections; only runtime contract
        # material is allowed to vary beneath the shared semantic identity.
        gold_pins = []
        prefix = f"private/{self.task.task_id}/answer_key/gold/"
        for manifest in manifests.values():
            gold_pins.append(
                {
                    rel: digest
                    for rel, digest in manifest.checksums.items()
                    if rel.startswith(prefix)
                }
            )
        self.assertTrue(all(pins == gold_pins[0] for pins in gold_pins[1:]))

    def test_schema_33_identity_algorithm_remains_verifiable(self):
        """A 3.3 manifest keeps its historical, unchained hash meaning."""
        self.freeze()

        def convert_to_schema_33(payload):
            payload["schema_version"] = "3.3"
            semantic_id = release._semantic_release_id(
                tasks=payload["tasks"],
                el_sources=payload["el_sources"],
                private_checksums=(
                    release._schema_33_private_semantic_checksums(
                        payload["checksums"], payload["tasks"]
                    )
                ),
                scorer_version=payload["scorer_version"],
                roster_digest=payload["roster_digest"],
                semantic_scorer_version=payload["semantic_scorer_version"],
            )
            payload["semantic_release_id"] = semantic_id
            bundles = {}
            certifications = {}
            for tid in payload["tasks"]:
                destination = payload["destinations"][tid]
                bundle = release._schema_33_runtime_bundle_id(
                    task_id=tid,
                    destination=destination,
                    public_checksums=release._public_task_checksums(
                        payload["checksums"], tid
                    ),
                )
                bundles[tid] = bundle
                certifications[tid] = release._certification_id(
                    runtime_bundle_id=bundle,
                    matrix=payload["certification_matrix"][destination],
                )
            payload["runtime_bundle_ids"] = bundles
            payload["certification_ids"] = certifications

        self._forge_manifest(self.out_dir, convert_to_schema_33)
        result = release.verify_release(self.out_dir)
        self.assertTrue(result.ok, result.failures)

    def test_schema_33_matrix_keeps_its_recorded_legacy_shape(self):
        """Closed matrix validation starts at release schema 3.4."""

        self.freeze()

        def convert_to_legacy_matrix(payload):
            payload["schema_version"] = "3.3"
            semantic_id = release._semantic_release_id(
                tasks=payload["tasks"],
                el_sources=payload["el_sources"],
                private_checksums=release._schema_33_private_semantic_checksums(
                    payload["checksums"], payload["tasks"]
                ),
                scorer_version=payload["scorer_version"],
                roster_digest=payload["roster_digest"],
                semantic_scorer_version=payload["semantic_scorer_version"],
            )
            payload["semantic_release_id"] = semantic_id
            payload["certification_matrix"]["snowflake"]["matrix_version"] = "8"
            payload["certification_matrix"]["snowflake"].pop("runner_image:dbt")
            bundles = {}
            certifications = {}
            for tid in payload["tasks"]:
                destination = payload["destinations"][tid]
                bundle = release._schema_33_runtime_bundle_id(
                    task_id=tid,
                    destination=destination,
                    public_checksums=release._public_task_checksums(
                        payload["checksums"], tid
                    ),
                )
                bundles[tid] = bundle
                certifications[tid] = release._certification_id(
                    runtime_bundle_id=bundle,
                    matrix=payload["certification_matrix"][destination],
                    validate_matrix=False,
                )
            payload["runtime_bundle_ids"] = bundles
            payload["certification_ids"] = certifications

        self._forge_manifest(self.out_dir, convert_to_legacy_matrix)
        result = release.verify_release(self.out_dir)
        self.assertTrue(result.ok, result.failures)

    def test_schema_34_release_refuses_open_or_incomplete_matrix(self):
        from elt_taskgen import runtime_matrix

        self.freeze()
        self._forge_manifest(
            self.out_dir,
            lambda payload: payload["certification_matrix"]["snowflake"].__setitem__(
                "unexpected", "value"
            ),
        )
        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any(
                f"closed v{runtime_matrix.CERTIFICATION_MATRIX_VERSION} schema"
                in failure
                for failure in result.failures
            ),
            result.failures,
        )

        second_dir = self.tmp / "release" / "r2"
        self.freeze_again()
        self._forge_manifest(
            second_dir,
            lambda payload: payload["certification_matrix"]["snowflake"].pop(
                "dbt_core_version"
            ),
        )
        result = release.verify_release(second_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("dbt_core_version" in failure for failure in result.failures),
            result.failures,
        )

    def test_schema_34_matrix_destination_is_bound_to_outer_task(self):
        """A self-consistent matrix cannot retarget the outer task."""
        from elt_taskgen import runtime_matrix

        fresh = self.freeze()
        self.assertEqual(
            fresh.certification_matrix["snowflake"]["matrix_version"],
            runtime_matrix.CERTIFICATION_MATRIX_VERSION,
        )

        def forge_cross_destination_matrix(payload):
            tid = self.task.task_id
            # Keep the outer matrix key and task destination as Snowflake but
            # replace the value with a complete, valid Redshift matrix. Also
            # recompute its digest, so only the destination binding can fail.
            matrix = runtime_matrix.certification_matrix("redshift")
            payload["certification_matrix"]["snowflake"] = matrix
            payload["certification_ids"][tid] = release._certification_id(
                runtime_bundle_id=payload["runtime_bundle_ids"][tid],
                matrix=matrix,
                validate_matrix=False,
            )

        self._forge_manifest(self.out_dir, forge_cross_destination_matrix)
        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any(
                "destination" in failure and "verifier contract" in failure
                for failure in result.failures
            ),
            result.failures,
        )

    def test_schema_34_closed_v10_matrix_remains_verifiable(self):
        """A frozen v10 recipe retains its historical hash semantics."""
        from elt_taskgen import runtime_matrix

        self.freeze()

        def downgrade_matrix_to_v10(payload):
            tid = self.task.task_id
            destination = payload["destinations"][tid]
            matrix = payload["certification_matrix"][destination]
            matrix["matrix_version"] = "10"
            matrix["certification_stage_evidence_schema_version"] = "1.1"
            matrix["certification_attestation_schema_version"] = "1.2"
            matrix["certification_pending_schema_version"] = "1.0"
            # v10 matrices were recorded under canonical fingerprint version 1.
            matrix["canonical_fingerprint_version"] = "1"
            # Validate before deliberately taking the hash-only compatibility
            # path. This mirrors verify_release and prevents a malformed
            # synthetic historical record from becoming a test fixture.
            validated = runtime_matrix.validate_recorded_certification_matrix(
                matrix,
                destination,
            )
            payload["certification_matrix"][destination] = validated
            payload["certification_ids"][tid] = release._certification_id(
                runtime_bundle_id=payload["runtime_bundle_ids"][tid],
                matrix=validated,
                validate_matrix=False,
            )

        self._forge_manifest(self.out_dir, downgrade_matrix_to_v10)
        result = release.verify_release(self.out_dir)
        self.assertTrue(result.ok, result.failures)

    def test_verify_recomputes_layered_identities_and_detects_tampering(self):
        self.freeze()
        self._forge_manifest(
            self.out_dir,
            lambda payload: payload.__setitem__(
                "semantic_release_id", release.SEMANTIC_ID_PREFIX + "0" * 16
            ),
        )
        result = release.verify_release(self.out_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("semantic_release_id" in f for f in result.failures),
            result.failures,
        )
        self.assertTrue(
            any(v.kind == "identity" and not v.ok for v in result.files)
        )

        tid = self.task.task_id
        second_dir = self.tmp / "release" / "r2"
        self.freeze_again()
        self._forge_manifest(
            second_dir,
            lambda payload: payload["runtime_bundle_ids"].__setitem__(
                tid, release.RUNTIME_BUNDLE_ID_PREFIX + "0" * 16
            ),
        )
        result = release.verify_release(second_dir)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("runtime_bundle_id" in f and tid in f for f in result.failures),
            result.failures,
        )

    def test_pre_33_manifest_is_never_judged_by_layered_identities(self):
        new_fields = (
            "semantic_release_id",
            "runtime_bundle_ids",
            "certification_ids",
            "certification_matrix",
        )
        self.freeze()

        def strip_to_32(payload):
            for key in new_fields:
                payload.pop(key)
            payload["schema_version"] = "3.2"

        self._forge_manifest(self.out_dir, strip_to_32)
        result = release.verify_release(self.out_dir)
        # The legacy release_id still recomputes (its inputs are untouched)
        # and no layered-identity check judges a pre-3.3 manifest.
        self.assertTrue(result.ok, result.failures)
        self.assertFalse(
            any(field in failure for failure in result.failures
                for field in new_fields)
        )

        # A runs-style 2.0 relabel is equally untouched by the new checks.
        second_dir = self.tmp / "release" / "r2"
        self.freeze_again()

        def strip_to_20(payload):
            for key in new_fields:
                payload.pop(key)
            payload["schema_version"] = "2.0"

        self._forge_manifest(second_dir, strip_to_20)
        result = release.verify_release(second_dir)
        self.assertTrue(result.ok, result.failures)
        self.assertFalse(
            any(v.kind == "identity" and not v.ok for v in result.files)
        )


class TestSyncModeAndDestinationVersionContract(ExportTaskBase):
    """IR-009: the sync-mode contract is declared and validated, and the
    exported config binds the destination connector version pin."""

    def _with_backend(self, backend, table="order_items"):
        from elt_taskgen.models import BackendAssignment

        task = demo_fixture.demo_task()
        return task.model_copy(
            update={
                "backends": tuple(
                    BackendAssignment(table=a.table, backend=backend)
                    if a.table == table
                    else a
                    for a in task.backends
                )
            }
        )

    def _tamper(self, mutate):
        config_path = self.task_dir / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        mutate(config)
        config_path.write_text(yaml.safe_dump(config))

    def test_build_config_rejects_uncertified_sync_modes(self):
        for mode in ("incremental_append", "overwrite"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(
                    ValueError, r"certified modes: full_refresh_append"
                ):
                    eltbench.build_config(self.task, sync_mode=mode)

    def test_default_sync_mode_output_is_unchanged(self):
        from elt_taskgen.destinations import (
            DEFAULT_SYNC_MODE,
            config_sync_mode_declarations,
        )

        default = eltbench.build_config(self.task)
        self.assertEqual(
            default, eltbench.build_config(self.task, sync_mode=DEFAULT_SYNC_MODE)
        )
        declarations = config_sync_mode_declarations(default)
        self.assertTrue(declarations)
        for path, declared in declarations:
            self.assertEqual(declared, "full_refresh_append", path)

    def test_connector_versions_are_private_not_public(self):
        from elt_taskgen.destinations import DESTINATION_CONTRACTS

        for destination, contract in DESTINATION_CONTRACTS.items():
            with self.subTest(destination=destination.value):
                config = eltbench.build_config(self.task, destination=destination)
                airbyte = config["Airbyte"]["config"]
                self.assertFalse(
                    [key for key in airbyte if key.endswith("_connector_version")]
                )
                private = eltbench.build_airbyte_connector_contract(config)
                self.assertEqual(
                    private["destination"]["connector_version"],
                    contract.connector_version,
                )

    def test_runtime_shape_rejects_connector_versions_and_client_oauth(self):
        self.export()
        config_path = self.task_dir / "config.yaml"
        original = config_path.read_text()
        for key, value in (
            ("snowflake_connector_version", "4.1.2"),
            ("client_id", ""),
            ("client_secret", ""),
        ):
            with self.subTest(key=key):
                config = yaml.safe_load(original)
                config["Airbyte"]["config"][key] = value
                config_path.write_text(yaml.safe_dump(config))
                with self.assertRaisesRegex(ValueError, "original ELT-Bench shape"):
                    eltbench.assert_public_runtime_shape(self.task_dir)

    def test_runtime_shape_rejects_tampered_sync_modes_in_demo_stanzas(self):
        self.export()
        config_path = self.task_dir / "config.yaml"
        original = config_path.read_text()
        cases = (
            (
                "postgres",
                lambda c: c["postgres"]["config"].__setitem__(
                    "sync_mode", "incremental_append"
                ),
            ),
            (
                "mongodb",
                lambda c: c["mongodb"]["config"].__setitem__(
                    "sync_mode", "overwrite"
                ),
            ),
            (
                "flat_files",
                lambda c: c["flat_files"][0].__setitem__(
                    "sync_mode", "incremental_dedupe"
                ),
            ),
            ("missing", lambda c: c["postgres"]["config"].pop("sync_mode")),
        )
        for label, mutate in cases:
            with self.subTest(section=label):
                config = yaml.safe_load(original)
                mutate(config)
                config_path.write_text(yaml.safe_dump(config))
                with self.assertRaisesRegex(ValueError, "unsupported sync mode"):
                    eltbench.assert_public_runtime_shape(self.task_dir)

    def test_runtime_shape_rejects_tampered_sync_modes_for_s3_and_rest(self):
        from elt_taskgen.models import Backend

        cases = (
            (
                Backend.S3,
                "aws_s3",
                lambda c: c["aws_s3"]["data"][0].__setitem__(
                    "sync_mode", "overwrite"
                ),
            ),
            (
                Backend.REST,
                "custom_api",
                lambda c: c["custom_api"]["config"].__setitem__(
                    "sync_mode", "overwrite"
                ),
            ),
        )
        for backend, section, mutate in cases:
            with self.subTest(section=section):
                task = self._with_backend(backend)
                gold = make_gold(task)
                task_dir = self.tmp / section / "task"
                answer_dir = self.tmp / section / "answer_key"
                eltbench.export_task(task, gold, task_dir, answer_dir)
                eltbench.assert_public_runtime_shape(task_dir)
                config_path = task_dir / "config.yaml"
                config = yaml.safe_load(config_path.read_text())
                mutate(config)
                config_path.write_text(yaml.safe_dump(config))
                with self.assertRaisesRegex(ValueError, "unsupported sync mode"):
                    eltbench.assert_public_runtime_shape(task_dir)


if __name__ == "__main__":
    unittest.main()

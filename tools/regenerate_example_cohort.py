"""Build or freeze the fixed 15-task ELT-Bench example cohort.

The default ``reference`` mode deliberately stops at the deterministic DuckDB
reference stage.  It exports a destination-bound combined Airbyte + dbt task
for inspection, but never calls that tree released or runtime-certified.

The ``release`` mode does not regenerate tasks.  It takes the catalog from a
reference cohort plus a workspace in which the same tasks have subsequently
passed both EL and T batteries, re-projects the public bundles for one selected
warehouse, and delegates the freeze to ``freeze_release``.  The result is the
official schema-3 combined public/private release layout.  Release mode never
starts Airbyte, dbt, or a cloud warehouse; live runtime certification remains a
separate sparse operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Callable

import yaml

from elt_taskgen.adapters import dbt, dlt, schemapile, synsql, wikidbs
from elt_taskgen.cli import _make_engine, cmd_ingest_anchor
from elt_taskgen.corpus.selection import SelectionResult
from elt_taskgen.corpus.difficulty import structural_features
from elt_taskgen.destinations import (
    Destination,
    destination_contract,
    destination_from_config,
    normalize_destination,
)
from elt_taskgen.export.eltbench import (
    assert_public_runtime_shape,
    database_name,
    export_task,
    materialize_warehouse,
)
from elt_taskgen.export.release import freeze_release, verify_release
from elt_taskgen.generation.challenging_policy import (
    CHALLENGING_TEMPLATE_SHARE_DENOMINATOR,
    CHALLENGING_TEMPLATE_SHARE_NUMERATOR,
    ChallengingCohortPolicyReport,
    validate_challenging_cohort,
)
from elt_taskgen.generation.difficulty_profiles import (
    CHALLENGING_DIFFICULTY_PROFILE,
    DIFFICULTY_PROFILES,
    GenerationDifficultyProfile,
    STANDARD_DIFFICULTY_PROFILE,
    difficulty_profile as resolve_difficulty_profile,
)
from elt_taskgen.generation.lineage import prune_to_effective_lineage
from elt_taskgen.generation.mart_plan import MIN_MART_COLUMNS
from elt_taskgen.generation.populations import apply_data_scale_profile
from elt_taskgen.models import (
    AcceptanceReport,
    PopulationName,
    RLVR_TASK_VARIANTS,
    TaskIR,
    task_from_json,
)
from elt_taskgen.reference.gold import GoldBundle, load_gold
from elt_taskgen.verification.reference_readiness import (
    CHALLENGING_REFERENCE_READINESS_ROSTER,
    CHALLENGING_REFERENCE_READINESS_ROSTER_DIGEST,
    REFERENCE_READINESS_EVIDENCE_REL,
    REFERENCE_READINESS_ROSTER,
    REFERENCE_READINESS_ROSTER_DIGEST,
    ensure_reference_readiness_evidence,
    run_reference_readiness,
)


REFERENCE_SCHEMA_VERSION = "1.1"
EXPECTED_TASKS = 15
POPULATIONS = tuple(population.value for population in PopulationName)
EXPECTED_ALIASES = frozenset(
    f"{source}_{number:02d}"
    for source in ("dbt", "dlt", "schemapile", "synsql", "wikidbs")
    for number in range(1, 4)
)
EXPECTED_SOURCE_COUNTS = {
    source: 3 for source in ("dbt", "dlt", "schemapile", "synsql", "wikidbs")
}


@dataclass(frozen=True)
class ExampleSpec:
    alias: str
    source: str
    number: int
    name: str
    build: Callable[[], TaskIR]


def _engine(workspace: Path, *, allow_unlocked_env: bool = False):
    """Build the same offline engine used by the command-line stages."""
    args = argparse.Namespace(
        workspace=workspace,
        max_repair_rounds=3,
        provider=None,
        model=None,
        offline=True,
        empirical=False,
        variants=None,
        recalibrate=False,
        allow_unlocked_env=allow_unlocked_env,
    )
    return _make_engine(args, echo=print)


def _one_dbt_task(manifest: Path) -> TaskIR:
    if not manifest.is_file():
        raise FileNotFoundError(
            f"compiled dbt manifest is missing: {manifest}; build it with "
            "tools/build_dbt_manifest.py before regenerating examples"
        )
    extracted = dbt.extract_candidates(dbt.load_manifest(manifest), pool="dbt")
    if len(extracted.tasks) != 1:
        raise RuntimeError(
            f"expected one task from {manifest}, found {len(extracted.tasks)}"
        )
    return extracted.tasks[0]


def _find_wikidbs(root: Path, directory: str) -> Path:
    matches = sorted(root.glob(f"part-*/{directory}"))
    if len(matches) != 1:
        raise FileNotFoundError(
            f"expected one WikiDBs directory {directory!r}, found {matches}"
        )
    return matches[0]


def build_specs(
    repo: Path,
    data_root: Path,
    *,
    difficulty_profile: GenerationDifficultyProfile = CHALLENGING_DIFFICULTY_PROFILE,
) -> tuple[ExampleSpec, ...]:
    """Construct the fixed 5-pool x 3-task cohort."""
    dbt_builds = repo / "runs" / "dbt_elt" / "dbt_builds"
    synsql_tables = data_root / "SynSQL-2.5M" / "tables.json"
    schemapile_source = data_root / "schemapile" / "schemapile-perm.json"
    wikidbs_root = data_root / "WikiDBs"

    # The SchemaPile corpus is one large JSON object. Load it once, not once per
    # selected record. These keys are the exact 40/39/31-table examples from the
    # original test_1 cohort; cluster IDs are their independence units.
    schema_records = json.loads(schemapile_source.read_text(encoding="utf-8"))

    def dbt_task(package: str) -> TaskIR:
        return _one_dbt_task(
            dbt_builds
            / package
            / "package"
            / "integration_tests"
            / "target"
            / "manifest.json"
        )

    def dlt_task(name: str) -> TaskIR:
        connector = repo / "config" / "dlt_connectors" / f"{name}.yaml"
        return dlt.to_task_ir(
            dlt.load_connector(connector), difficulty_profile=difficulty_profile
        )

    def schema_task(key: str, cluster: str) -> TaskIR:
        try:
            record = schema_records[key]
        except KeyError as exc:
            raise KeyError(f"SchemaPile source has no record {key!r}") from exc
        return schemapile.to_task_ir(
            record,
            key=key,
            cluster=cluster,
            difficulty_profile=difficulty_profile,
        )

    def synsql_task(database: str) -> TaskIR:
        return synsql.to_task_ir(
            database, synsql_tables, difficulty_profile=difficulty_profile
        )

    def wiki_task(directory: str) -> TaskIR:
        return wikidbs.to_task_ir(
            _find_wikidbs(wikidbs_root, directory),
            wikidbs_root=wikidbs_root,
            difficulty_profile=difficulty_profile,
        )

    return (
        ExampleSpec(
            "dbt_01",
            "dbt",
            1,
            "Apple Search Ads",
            lambda: dbt_task("dbt_apple_search_ads"),
        ),
        ExampleSpec("dbt_02", "dbt", 2, "Reddit Ads", lambda: dbt_task("dbt_reddit_ads")),
        ExampleSpec("dbt_03", "dbt", 3, "Twitter Ads", lambda: dbt_task("dbt_twitter")),
        ExampleSpec("dlt_01", "dlt", 1, "Personio", lambda: dlt_task("personio")),
        ExampleSpec("dlt_02", "dlt", 2, "Pipedrive", lambda: dlt_task("pipedrive")),
        ExampleSpec("dlt_03", "dlt", 3, "Workable", lambda: dlt_task("workable")),
        ExampleSpec(
            "schemapile_01",
            "schemapile",
            1,
            "ARFC Pride",
            lambda: schema_task(
                "082965_03_uiuc_mga.sql", "github_com_arfc_pride_a189059a602a"
            ),
        ),
        ExampleSpec(
            "schemapile_02",
            "schemapile",
            2,
            "Apache Ranger",
            lambda: schema_task(
                "167589_xa_core_db_postgres.sql",
                "github_com_common_coolteam_ranger_93308f5c1f72",
            ),
        ),
        ExampleSpec(
            "schemapile_03",
            "schemapile",
            3,
            "Mojito",
            lambda: schema_task(
                "230521_V1__Initial_Setup.sql",
                "github_com_sachin_awati_mojito_d536723c35a9",
            ),
        ),
        ExampleSpec(
            "synsql_01",
            "synsql",
            1,
            "Employee Information and Salary",
            lambda: synsql_task("employee_information_and_salary_data"),
        ),
        ExampleSpec(
            "synsql_02",
            "synsql",
            2,
            "Genetic Sequencing and Analysis",
            lambda: synsql_task("genetic_sequencing_and_analysis"),
        ),
        ExampleSpec(
            "synsql_03",
            "synsql",
            3,
            "Web Application Testing",
            lambda: synsql_task("web_application_testing_and_automation"),
        ),
        ExampleSpec(
            "wikidbs_01",
            "wikidbs",
            1,
            "Semiconductor Science and Technology Publications",
            lambda: wiki_task("00074 SemiconductorScienceTechnologyPublications"),
        ),
        ExampleSpec(
            "wikidbs_02",
            "wikidbs",
            2,
            "Marine Science and Engineering Publications",
            lambda: wiki_task("20531 MARINE_SCIENCE_ENGINEERING_PUBLICATIONS_DB"),
        ),
        ExampleSpec(
            "wikidbs_03",
            "wikidbs",
            3,
            "Cryptococcus Neoformans Genomic Data",
            lambda: wiki_task(
                "60782 cryptococcus_neoformans_chromosome_ae0173491_genomic_data"
            ),
        ),
    )


def _copy_private_material(task_root: Path, destination: Path) -> None:
    private = destination / "private"
    private.mkdir(parents=True)
    shutil.copy2(task_root / "task_ir.json", private / "task_ir.json")
    shutil.copytree(task_root / "answer_key", private / "answer_key")
    shutil.copytree(task_root / "oracle", private / "oracle")
    readiness = task_root / REFERENCE_READINESS_EVIDENCE_REL
    if not readiness.is_file():
        raise FileNotFoundError(f"reference-readiness evidence is missing: {readiness}")
    reports = private / "reports"
    reports.mkdir()
    shutil.copy2(readiness, reports / readiness.name)
    perturbation = task_root / "reports" / "perturbation_probe.json"
    if perturbation.is_file():
        shutil.copy2(perturbation, reports / perturbation.name)
    for population in POPULATIONS:
        source = task_root / "populations" / population / "rendered"
        shutil.copytree(
            source, private / "populations" / population / "rendered"
        )


def _sha256_text(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _make_json_readable(root: Path) -> None:
    """Pretty-print JSON while preserving each frozen gold manifest."""
    manifests = sorted(root.glob("tasks/*/private/answer_key/manifest.json"))
    manifest_paths = {path.resolve() for path in manifests}
    for path in sorted(root.rglob("*.json")):
        if path.resolve() in manifest_paths:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    # Gold manifests pin text bytes. Recompute only their already-declared
    # entries after formatting; export-only table/sort/evaluation files remain
    # outside the frozen-gold file set, exactly as in the normal workspace.
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        answer_key = manifest_path.parent
        for rel in sorted(manifest["files"]):
            manifest["files"][rel] = _sha256_text(answer_key / rel)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _readme(catalog: dict) -> str:
    destination = str(catalog["destination"])
    profile = str(catalog.get("difficulty_profile", "standard"))
    profile_contract = catalog.get("difficulty_profile_contract", {})
    primary_floor = int(profile_contract.get("synthetic_primary_row_floor", 0))
    challenge_summary = catalog.get("challenge_summary", {})
    scale_summary_lines = []
    if challenge_summary:
        scale_summary_lines = [
            "Frozen Stage-1 measurement for this build: "
            f"{int(challenge_summary['primary_rows_total']):,} PRIMARY rows total, "
            f"{float(challenge_summary['primary_rows_median']):,.1f} median per task; "
            f"{int(challenge_summary['stress_rows_total']):,} STRESS rows total.",
            "",
        ]
    if primary_floor:
        difficulty_lines = [
            "The profile raises evidence-backed mart budgets and spreads marts across",
            "different grains where each source schema supports that work. After unused",
            "source lineage is pruned, synthetic tasks are proportionally retargeted to",
            f"at least {primary_floor:,} declared PRIMARY rows; DEVELOPMENT stays small and",
            "COUNTERFACTUAL witnesses stay literal. WikiDBs PRIMARY rows remain the",
            "vendor-provided real rows and are never padded with synthetic data.",
        ]
    else:
        difficulty_lines = [
            "This profile preserves each adapter's historical mart budgets and synthetic",
            "population sizes. It is provided as a reproducible comparison baseline.",
        ]
    rows = [
        "| Alias | Source | Example | Tables | Marts | Primary rows | Stress rows | Data | Canonical task ID |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for task in catalog["tasks"]:
        rows.append(
            f"| `{task['alias']}` | {task['source']} | {task['name']} | "
            f"{task['source_table_count']} | {task['mart_count']} | "
            f"{task['primary_rows']:,} | {task['stress_rows']:,} | "
            f"{task['data_provenance']} | "
            f"`{task['canonical_id']}` |"
        )
    return "\n".join(
        [
            "# ELT-taskgen test_1: 15 combined example tasks",
            "",
            "This directory is a deterministic, local reference-stage example set.",
            "It contains three examples from each of five source pools. Every alias",
            "represents one combined EL+T task; `_el` and `_t` are stage aliases, not",
            "separate public tasks.",
            "",
            "These examples are **not release-certified and not cloud-certified**.",
            "Generation stopped after the local DuckDB reference stage, then emitted",
            f"the current combined {destination} runtime scaffold. No council model, audit",
            "approval, Airbyte instance, dbt runner, Snowflake, Databricks, or Redshift",
            "was invoked.",
            "",
            "## Generation difficulty profile",
            "",
            f"This cohort was generated with the deterministic `{profile}` profile.",
            *difficulty_lines,
            "",
            "The profile describes generated structure and scale, not empirical solver",
            "difficulty. A task is not called hard until a pinned solver campaign measures",
            "it, and it is not runtime-certified until the real Airbyte + warehouse + dbt",
            "path passes separately.",
            "",
            "## Per-task layout",
            "",
            "```text",
            "tasks/<alias>/",
            "├── public/                 # solver-facing combined Airbyte + dbt task",
            "│   ├── config.yaml         # EL instructions",
            "│   ├── data_model.yaml     # T mart contract",
            "│   ├── schemas/",
            "│   ├── documentation/",
            "│   ├── check_job_status.py",
            f"│   ├── {destination}_credential.json",
            "│   └── elt/",
            "│       └── main.tf       # original provider-only seed",
            "└── private/                # never expose to the solver",
            "    ├── task_ir.json",
            "    ├── answer_key/",
            "    ├── reports/reference_readiness.json",
            "    ├── reports/perturbation_probe.json   # when required by the roster",
            "    ├── populations/<population>/rendered/",
            "    └── oracle/<population>.duckdb",
            "```",
            "",
            "Start with `public/config.yaml` and `public/schemas/` for EL. Then read",
            "`public/data_model.yaml` for the mart names, grain, keys, columns, and",
            "business logic for T. Use `private/answer_key/` and `private/oracle/`",
            "only while inspecting or evaluating the generator; they are hidden from",
            "an agent in a real task.",
            "The solver-facing tree mirrors the original ELT-Bench input shape.",
            "Exact connector versions and the compiled modern Airbyte payload remain",
            "under `private/answer_key/runtime/`; they are harness evidence and are",
            "never auto-loaded into the solver's Terraform project.",
            "",
            "All `.json` files are indented for inspection. JSONL source files remain",
            "one record per line because line boundaries are part of that format. There",
            "is no top-level checksum file.",
            "",
            "## Cohort",
            "",
            *scale_summary_lines,
            *rows,
            "",
            "## Regeneration",
            "",
            "From the `ELT-taskgen` repository:",
            "",
            "```bash",
            "uv run --frozen python tools/regenerate_example_cohort.py \\",
            f"  --destination {destination} \\",
            f"  --difficulty-profile {profile} \\",
            "  --workspace /private/tmp/elt-taskgen-test1-workspace \\",
            "  --output ../ELT-taskgen-test_1.new",
            "```",
            "",
            "The command refuses a non-empty output directory. Generate into a new",
            "directory, inspect `validation_report.json`, and replace the old example",
            "tree only after validation succeeds.",
            "",
            "## Freeze an accepted cohort",
            "",
            "Reference output is not an official release. After these exact task IDs",
            "have passed both EL and T batteries in a curation workspace, freeze one",
            "destination-bound schema-3 release with:",
            "",
            "```bash",
            "uv run --frozen python tools/regenerate_example_cohort.py \\",
            "  --mode release \\",
            f"  --destination {destination} \\",
            "  --workspace /path/to/accepted-workspace \\",
            "  --catalog ../ELT-taskgen-test_1/catalog.json \\",
            "  --output /path/to/immutable-release",
            "```",
            "",
            "Release mode does not rerun generation and does not use cloud compute. It",
            "fails closed unless the workspace already contains current passing EL and",
            "T batteries. The release builder writes `release_manifest.json`, the",
            "combined `public/<canonical_task_id>/` tree, and the private evaluator tree.",
            "Run live Airbyte + dbt warehouse certification separately.",
            "",
        ]
    )


def _catalog_entry(
    spec: ExampleSpec,
    task: TaskIR,
    destination: Destination | str,
    *,
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
    gold: GoldBundle | None = None,
) -> dict:
    selected = normalize_destination(destination)
    contract = destination_contract(selected)
    by_backend: dict[str, int] = {}
    for assignment in task.backends:
        key = assignment.backend.value
        by_backend[key] = by_backend.get(key, 0) + 1
    populations = {population.name: population for population in task.populations}

    def population_rows(name: PopulationName) -> int:
        if gold is not None:
            return sum(gold.stage1.get(name.value, {}).values())
        population = populations.get(name)
        if population is None:
            return 0
        if population.literal_rows:
            return sum(len(rows) for rows in population.literal_rows.values())
        return sum(population.scale.values())

    primary = populations.get(PopulationName.PRIMARY)
    data_provenance = (
        "provided_real"
        if primary is not None and bool(primary.literal_rows)
        else "generated_synthetic"
    )
    primary_rows = population_rows(PopulationName.PRIMARY)
    stress_rows = population_rows(PopulationName.STRESS)
    features = structural_features(task)
    challenge_axes = {
        "semantic_structure": {
            "active_source_tables": int(features["active_source_table_count"]),
            "active_lineage_ratio": features["active_lineage_ratio"],
            "marts": len(task.marts),
            "mart_columns": sum(len(mart.columns) for mart in task.marts),
            "computed_mart_columns": sum(
                int(column.computed) for mart in task.marts for column in mart.columns
            ),
            "semantic_interactions": int(features["semantic_interaction_count"]),
            "plan_signatures": int(features["plan_signature_count"]),
        },
        "data_scale": {
            "primary_rows": primary_rows,
            "stress_rows": stress_rows,
        },
        "data_realism": {"provenance": data_provenance},
        "empirical_solver_difficulty": "unmeasured",
        "runtime_certification": "not_run",
    }
    return {
        "alias": spec.alias,
        "stage_aliases": {"el": f"{spec.alias}_el", "t": f"{spec.alias}_t"},
        "source": spec.source,
        "number": spec.number,
        "name": spec.name,
        "canonical_id": task.task_id,
        "family_id": task.family_id,
        "task_content_hash": task.content_hash(),
        "source_table_count": len(task.tables),
        "mart_count": len(task.marts),
        "difficulty_profile": difficulty_profile.name,
        "data_provenance": data_provenance,
        "primary_rows": primary_rows,
        "stress_rows": stress_rows,
        "challenge_axes": challenge_axes,
        "backends": dict(sorted(by_backend.items())),
        "populations": list(POPULATIONS),
        "destination": selected.value,
        "destination_connector_version": contract.connector_version,
        # The logical namespace is deterministic and destination-neutral. For
        # Snowflake it is the database (with AIRBYTE_SCHEMA beneath it); for
        # Databricks and Redshift it is the task schema inside the separately
        # provisioned catalog/database.
        "runtime_namespace": database_name(task),
        "status": "reference_ready",
        "release_certified": False,
        "runtime_certified": False,
    }


def _validate_fixed_cohort_catalog(tasks: object) -> list[dict]:
    """Return typed rows only for the exact fixed 5-pool x 3-task cohort."""

    if not isinstance(tasks, list):
        raise RuntimeError("catalog tasks must be a list")
    if len(tasks) != EXPECTED_TASKS:
        raise RuntimeError(f"expected {EXPECTED_TASKS} tasks, found {len(tasks)}")
    if not all(isinstance(row, dict) for row in tasks):
        raise RuntimeError("catalog task entries must be objects")

    rows: list[dict] = tasks
    aliases = [str(row.get("alias") or "") for row in rows]
    actual_aliases = set(aliases)
    if len(actual_aliases) != EXPECTED_TASKS:
        raise RuntimeError("task aliases are not unique")
    if actual_aliases != EXPECTED_ALIASES:
        missing = sorted(EXPECTED_ALIASES - actual_aliases)
        unexpected = sorted(actual_aliases - EXPECTED_ALIASES)
        raise RuntimeError(
            "catalog does not contain the exact fixed cohort aliases "
            f"(missing={missing}, unexpected={unexpected})"
        )

    source_counts = {
        source: sum(1 for row in rows if row.get("source") == source)
        for source in EXPECTED_SOURCE_COUNTS
    }
    unexpected_sources = sorted(
        {
            str(row.get("source"))
            for row in rows
            if row.get("source") not in EXPECTED_SOURCE_COUNTS
        }
    )
    if source_counts != EXPECTED_SOURCE_COUNTS or unexpected_sources:
        raise RuntimeError(
            "catalog does not contain exactly three tasks from each source pool "
            f"(counts={source_counts}, unexpected={unexpected_sources})"
        )
    return rows


def _validate_reference_readiness_payload(
    payload: object,
    task: TaskIR,
    *,
    alias: str,
    challenging: bool = False,
) -> AcceptanceReport:
    """Require current, structurally valid readiness evidence for one task."""

    try:
        report = AcceptanceReport.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"malformed reference readiness report for {alias}"
        ) from exc
    if not report.accepted:
        raise RuntimeError(f"reference readiness did not accept {alias}")
    if report.task_id != task.task_id:
        raise RuntimeError(f"reference readiness task id is stale for {alias}")
    if report.task_content_hash != task.content_hash():
        raise RuntimeError(f"stale reference readiness for {alias}")
    expected_roster = (
        CHALLENGING_REFERENCE_READINESS_ROSTER
        if challenging
        else REFERENCE_READINESS_ROSTER
    )
    expected_digest = (
        CHALLENGING_REFERENCE_READINESS_ROSTER_DIGEST
        if challenging
        else REFERENCE_READINESS_ROSTER_DIGEST
    )
    if report.roster != expected_roster:
        raise RuntimeError(f"reference readiness gate roster is stale for {alias}")
    if report.roster_digest != expected_digest:
        raise RuntimeError(f"reference readiness roster digest is stale for {alias}")
    measured_gates = tuple(gate.gate for gate in report.gates)
    if measured_gates != expected_roster:
        raise RuntimeError(
            f"reference readiness measured gate roster is stale for {alias}"
        )
    return report


def validate_output(output: Path) -> dict:
    """Validate structure, gold, public/private boundary, and DuckDB counts."""
    import duckdb

    catalog = json.loads((output / "catalog.json").read_text(encoding="utf-8"))
    tasks = _validate_fixed_cohort_catalog(catalog.get("tasks"))
    profile_name = str(catalog.get("difficulty_profile", "standard"))
    try:
        profile = resolve_difficulty_profile(profile_name)
    except ValueError as exc:
        raise RuntimeError(
            f"catalog has unknown difficulty profile {profile_name!r}"
        ) from exc
    challenging = profile.name == CHALLENGING_DIFFICULTY_PROFILE.name
    selected = normalize_destination(catalog.get("destination"))
    contract = destination_contract(selected)
    if list(output.rglob("checksums.sha256")):
        raise RuntimeError("example output unexpectedly contains checksums.sha256")

    public_files = 0
    private_files = 0
    oracle_counts = 0
    readiness_reports = 0
    perturbation_reports = 0
    policy_tasks: list[tuple[str, TaskIR]] = []
    for row in tasks:
        root = output / "tasks" / row["alias"]
        public = root / "public"
        private = root / "private"
        for required in (
            public / "config.yaml",
            public / "data_model.yaml",
            public / "documentation" / "README.md",
            public / "elt" / "main.tf",
            private / "task_ir.json",
            private / "answer_key" / "manifest.json",
            private / "reports" / "reference_readiness.json",
        ):
            if not required.is_file():
                raise RuntimeError(f"missing required example file: {required}")
        assert_public_runtime_shape(public)
        config = yaml.safe_load((public / "config.yaml").read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise RuntimeError(f"public config is not an object for {row['alias']}")
        actual_destination = destination_from_config(config)
        if actual_destination is not selected or row.get("destination") != selected.value:
            raise RuntimeError(f"destination mismatch for {row['alias']}")
        destination_config = config[contract.config_section]["config"]
        actual_namespace = destination_config[contract.logical_namespace_field]
        if actual_namespace != row.get("runtime_namespace"):
            raise RuntimeError(f"runtime namespace mismatch for {row['alias']}")
        if row.get("destination_connector_version") != contract.connector_version:
            raise RuntimeError(f"destination connector version mismatch for {row['alias']}")
        if (public / "sources").exists() or list(public.rglob("*.duckdb")):
            raise RuntimeError(f"private source/oracle material leaked into {public}")
        if any("__el" in part or "__t" in part for path in root.rglob("*") for part in path.parts):
            raise RuntimeError(f"legacy split task artifact found under {root}")

        task = task_from_json((private / "task_ir.json").read_text(encoding="utf-8"))
        if task.task_id != row["canonical_id"] or task.content_hash() != row["task_content_hash"]:
            raise RuntimeError(f"catalog identity mismatch for {row['alias']}")
        readiness_payload = json.loads(
            (private / "reports" / "reference_readiness.json").read_text(
                encoding="utf-8"
            )
        )
        _validate_reference_readiness_payload(
            readiness_payload,
            task,
            alias=str(row["alias"]),
            challenging=challenging,
        )
        policy_tasks.append((str(row["alias"]), task))
        readiness_reports += 1
        if (private / "reports" / "perturbation_probe.json").is_file():
            perturbation_reports += 1
        gold = load_gold(private / "answer_key")
        if gold.task_id != task.task_id or gold.task_content_hash != task.content_hash():
            raise RuntimeError(f"gold identity mismatch for {row['alias']}")
        schema_names = {path.stem for path in (public / "schemas").glob("*.csv")}
        if schema_names != {table.name for table in task.tables}:
            raise RuntimeError(f"schema coverage mismatch for {row['alias']}")
        model = yaml.safe_load((public / "data_model.yaml").read_text(encoding="utf-8")) or {}
        model_names = {str(item.get("name")) for item in model.get("models", [])}
        if model_names != {mart.name for mart in task.marts}:
            raise RuntimeError(f"data_model mart coverage mismatch for {row['alias']}")

        for population in POPULATIONS:
            rendered = private / "populations" / population / "rendered"
            if not rendered.is_dir():
                raise RuntimeError(f"missing rendered population: {rendered}")
            database = private / "oracle" / f"{population}.duckdb"
            if not database.is_file():
                raise RuntimeError(f"missing private DuckDB oracle: {database}")
            con = duckdb.connect(str(database), read_only=True)
            try:
                actual = {
                    table.name: int(
                        con.execute(
                            f'SELECT COUNT(*) FROM "{table.name.replace(chr(34), chr(34) * 2)}"'
                        ).fetchone()[0]
                    )
                    for table in task.tables
                }
            finally:
                con.close()
            if actual != gold.stage1[population]:
                raise RuntimeError(
                    f"DuckDB/source count mismatch for {row['alias']} {population}"
                )
            oracle_counts += 1
        public_files += sum(1 for path in public.rglob("*") if path.is_file())
        private_files += sum(1 for path in private.rglob("*") if path.is_file())

    if challenging:
        validate_challenging_cohort(policy_tasks)

    return {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "ok": True,
        "destination": selected.value,
        "destination_connector_version": contract.connector_version,
        "task_count": len(tasks),
        "source_counts": dict(EXPECTED_SOURCE_COUNTS),
        "public_files_checked": public_files,
        "private_files_checked": private_files,
        "duckdb_oracles_checked": oracle_counts,
        "gold_manifests_verified": len(tasks),
        "reference_readiness_reports": readiness_reports,
        "perturbation_probe_reports": perturbation_reports,
        "legacy_split_tasks": 0,
        "top_level_checksum_files": 0,
        "cloud_runs": 0,
    }


def generate(
    repo: Path,
    data_root: Path,
    bench_root: Path,
    workspace: Path,
    output: Path,
    *,
    destination: Destination | str = Destination.SNOWFLAKE,
    difficulty_profile: GenerationDifficultyProfile = CHALLENGING_DIFFICULTY_PROFILE,
) -> dict:
    selected = normalize_destination(destination)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output}")
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"workspace directory is not empty: {workspace}")
    output.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)

    code = cmd_ingest_anchor(
        argparse.Namespace(
            workspace=workspace,
            bench_root=bench_root,
            db=None,
            command="measure-target",
        )
    )
    if code != 0:
        raise RuntimeError("failed to arm the ELT-Bench contamination index")

    specs = build_specs(repo, data_root, difficulty_profile=difficulty_profile)
    if len(specs) != EXPECTED_TASKS:
        raise RuntimeError(f"cohort definition has {len(specs)} tasks, expected 15")
    challenging = difficulty_profile.name == CHALLENGING_DIFFICULTY_PROFILE.name
    prepared: list[tuple[ExampleSpec, TaskIR]] = []
    for position, spec in enumerate(specs, 1):
        print(f"[{position:02d}/{EXPECTED_TASKS}] build {spec.alias}: {spec.name}")
        task = prune_to_effective_lineage(spec.build(), minimum_source_tables=2)
        task = apply_data_scale_profile(task, difficulty_profile)
        prepared.append((spec, task))

    # Challenging generation is fail-closed before the engine can begin the
    # reference stage.  In particular, a large cohort cannot hide one shallow
    # task or buy a high mart count by repeating one plan template.
    policy_report: ChallengingCohortPolicyReport | None = None
    if challenging:
        policy_report = validate_challenging_cohort(
            (spec.alias, task) for spec, task in prepared
        )

    catalog_rows: list[dict] = []
    engine = _engine(workspace)
    try:
        for spec, task in prepared:
            engine.register(task)
            engine.run(task.task_id, until="reference")
            report = engine.latest_report(task.task_id, "reference")
            if report is None or report.verdict != "pass":
                verdict = None if report is None else report.verdict
                raise RuntimeError(
                    f"{spec.alias} did not reach reference successfully: {verdict}"
                )
            task = engine.load_task(task.task_id)
            task_root = workspace / "tasks" / task.task_id
            gold = load_gold(task_root / "answer_key")
            ensure_reference_readiness_evidence(task, workspace, gold)
            readiness = run_reference_readiness(
                task,
                workspace,
                gold,
                challenging=challenging,
            )
            if not readiness.accepted:
                failures = [gate.gate for gate in readiness.gates if not gate.passed]
                raise RuntimeError(
                    f"{spec.alias} failed local reference readiness: {failures}"
                )
            export_task(
                task,
                gold,
                task_root / "task",
                task_root / "answer_key",
                destination=selected,
            )
            for population in POPULATIONS:
                materialize_warehouse(
                    task,
                    gold,
                    population,
                    task_root / "populations" / population / "rendered",
                    task_root / "oracle" / f"{population}.duckdb",
                )

            destination = output / "tasks" / spec.alias
            shutil.copytree(task_root / "task", destination / "public")
            _copy_private_material(task_root, destination)
            catalog_rows.append(
                _catalog_entry(
                    spec,
                    task,
                    selected,
                    difficulty_profile=difficulty_profile,
                    gold=gold,
                )
            )
    finally:
        engine.close()

    primary_rows = [int(row["primary_rows"]) for row in catalog_rows]
    stress_rows = [int(row["stress_rows"]) for row in catalog_rows]
    synthetic_rows = [
        int(row["primary_rows"])
        for row in catalog_rows
        if row["data_provenance"] == "generated_synthetic"
    ]
    provided_real_rows = [
        int(row["primary_rows"])
        for row in catalog_rows
        if row["data_provenance"] == "provided_real"
    ]
    profile_contract = difficulty_profile.catalog_contract()
    if challenging:
        profile_contract.update(
            {
                "minimum_mart_columns": MIN_MART_COLUMNS,
                "require_classified_mart_columns": True,
                "require_multi_mart_template_diversity": True,
                "max_corpus_template_share": (
                    CHALLENGING_TEMPLATE_SHARE_NUMERATOR
                    / CHALLENGING_TEMPLATE_SHARE_DENOMINATOR
                ),
            }
        )
    catalog = {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "description": "Fixed 15-task combined ELT reference-stage example cohort.",
        "task_boundary": "combined_el_t",
        "destination": selected.value,
        "destination_connector_version": destination_contract(selected).connector_version,
        "status": "reference_ready",
        "release_certified": False,
        "runtime_certified": False,
        "difficulty_profile": difficulty_profile.name,
        "difficulty_profile_contract": profile_contract,
        "challenge_summary": {
            "primary_rows_total": sum(primary_rows),
            "primary_rows_median": median(primary_rows),
            "stress_rows_total": sum(stress_rows),
            "stress_rows_median": median(stress_rows),
            "synthetic_task_count": len(synthetic_rows),
            "synthetic_primary_rows_min": min(synthetic_rows, default=0),
            "provided_real_task_count": sum(
                row["data_provenance"] == "provided_real" for row in catalog_rows
            ),
            "provided_real_primary_rows_total": sum(provided_real_rows),
            "mart_count": (
                policy_report.mart_count
                if policy_report is not None
                else sum(int(row["challenge_axes"]["semantic_structure"]["marts"]) for row in catalog_rows)
            ),
            "distinct_plan_template_count": (
                policy_report.distinct_template_count
                if policy_report is not None
                else None
            ),
            "largest_plan_template_count": (
                policy_report.largest_template_count
                if policy_report is not None
                else None
            ),
            "largest_plan_template_share": (
                policy_report.largest_template_share
                if policy_report is not None
                else None
            ),
            "unclassified_mart_columns": 0 if policy_report is not None else None,
        },
        "tasks": catalog_rows,
    }
    (output / "catalog.json").write_text(
        json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    aliases = {
        "schema_version": 2,
        "description": (
            "Simple aliases for 15 combined EL+T examples. Stage aliases label "
            "the two reward phases; they are not separate public tasks."
        ),
        "tasks": catalog_rows,
    }
    (output / "task_aliases.yaml").write_text(
        yaml.safe_dump(aliases, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
    )
    (output / "README.md").write_text(_readme(catalog), encoding="utf-8")
    _make_json_readable(output)
    validation = validate_output(output)
    (output / "validation_report.json").write_text(
        json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return validation


def _selection_from_catalog(catalog_path: Path) -> tuple[SelectionResult, dict]:
    """Load the fixed cohort identity without rebuilding any source task.

    A reference catalog predates corpus splitting, so an omitted ``split`` is
    intentionally interpreted as ``train``.  Supplying ``split: val`` on a row
    preserves that explicit choice in the schema-3 release manifest.
    """

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    rows = catalog.get("tasks")
    if not isinstance(rows, list) or len(rows) != EXPECTED_TASKS:
        found = 0 if not isinstance(rows, list) else len(rows)
        raise ValueError(
            f"example catalog must contain {EXPECTED_TASKS} tasks, found {found}"
        )
    aliases = [str(row.get("alias")) for row in rows if isinstance(row, dict)]
    if len(aliases) != EXPECTED_TASKS or set(aliases) != EXPECTED_ALIASES:
        raise ValueError("catalog does not describe the fixed 5-pool x 3-task cohort")

    train: list[str] = []
    val: list[str] = []
    content_hashes: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("catalog task entries must be objects")
        task_id = str(row.get("canonical_id") or "")
        content_hash = str(row.get("task_content_hash") or "")
        if not task_id or not content_hash:
            raise ValueError(
                f"catalog entry {row.get('alias')!r} has no canonical identity"
            )
        if task_id in content_hashes:
            raise ValueError(f"catalog repeats canonical task id {task_id!r}")
        content_hashes[task_id] = content_hash
        split = str(row.get("split", "train"))
        if split == "train":
            train.append(task_id)
        elif split == "val":
            val.append(task_id)
        else:
            raise ValueError(
                f"catalog entry {row.get('alias')!r} has invalid split {split!r}"
            )

    required_variants = tuple(variant.value for variant in RLVR_TASK_VARIANTS)
    selection = SelectionResult(
        train=tuple(train),
        val=tuple(val),
        rejected={},
        variants={task_id: required_variants for task_id in content_hashes},
        rejected_variants={},
    )
    return selection, catalog


def _copy_or_link_workspace_file(source: str, destination: str) -> str:
    """Hard-link immutable evidence; privately copy the mutable SQLite ledger."""

    source_path = Path(source)
    if source_path.name.startswith("taskgen.sqlite"):
        return shutil.copy2(source, destination)
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def _clone_workspace_for_release(source: Path, destination: Path) -> None:
    """Create a cheap isolated release view without mutating accepted evidence."""

    shutil.copytree(
        source,
        destination,
        symlinks=True,
        copy_function=_copy_or_link_workspace_file,
    )


def freeze_certified_cohort(
    workspace: Path,
    catalog_path: Path,
    output: Path,
    *,
    destination: Destination | str,
    allow_unlocked_env: bool = False,
) -> dict:
    """Freeze an accepted fixed cohort through the official schema-3 builder.

    Destination projection happens in a hard-linked temporary workspace. This
    leaves the accepted workspace byte-for-byte unchanged and ensures that one
    Snowflake freeze cannot silently retarget a later Databricks or Redshift
    freeze. ``freeze_release`` remains the only code allowed to publish the
    official combined public/private layout and its manifest.
    """

    selected = normalize_destination(destination)
    workspace = workspace.resolve()
    catalog_path = catalog_path.resolve()
    output = output.resolve()
    if not workspace.is_dir():
        raise FileNotFoundError(f"accepted workspace does not exist: {workspace}")
    if not catalog_path.is_file():
        raise FileNotFoundError(f"reference catalog does not exist: {catalog_path}")
    if output.exists():
        raise FileExistsError(f"release output already exists: {output}")
    if workspace == output or workspace in output.parents:
        raise ValueError("release output must not be inside the accepted workspace")

    selection, catalog = _selection_from_catalog(catalog_path)
    expected_hashes = {
        str(row["canonical_id"]): str(row["task_content_hash"])
        for row in catalog["tasks"]
    }
    selected_ids = tuple(selection.train) + tuple(selection.val)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        dir=output.parent, prefix=".example-release-workspace-"
    ) as tmp:
        overlay = Path(tmp) / "workspace"
        _clone_workspace_for_release(workspace, overlay)
        engine = _engine(overlay, allow_unlocked_env=allow_unlocked_env)
        try:
            runtime_namespaces: dict[str, str] = {}
            for task_id in selected_ids:
                task = engine.load_task(task_id)
                expected_hash = expected_hashes[task_id]
                if task.content_hash() != expected_hash:
                    raise ValueError(
                        f"task {task_id!r} content hash does not match the cohort catalog"
                    )
                task_root = overlay / "tasks" / task_id
                gold = load_gold(task_root / "answer_key")
                export_task(
                    task,
                    gold,
                    task_root / "task",
                    task_root / "answer_key",
                    destination=selected,
                )
                runtime_namespaces[task_id] = database_name(task)

            manifest = freeze_release(
                engine,
                selection,
                output,
                allow_unlocked_env=allow_unlocked_env,
            )
        finally:
            engine.close()

    verification = verify_release(output)
    if not verification.ok:
        detail = "; ".join(verification.failures[:5])
        raise RuntimeError(
            f"schema-3 release failed its post-freeze verification: {detail}"
        )
    unexpected_destinations = {
        task_id: value
        for task_id, value in manifest.destinations.items()
        if value != selected.value
    }
    if unexpected_destinations:
        raise RuntimeError(
            "release manifest contains unexpected destinations: "
            f"{unexpected_destinations}"
        )
    return {
        "ok": True,
        "mode": "release",
        "schema_version": manifest.schema_version,
        "release_id": manifest.release_id,
        "task_count": len(manifest.tasks),
        "destination": selected.value,
        "destination_connector_version": destination_contract(selected).connector_version,
        "runtime_namespaces": dict(sorted(runtime_namespaces.items())),
        "files_checked": verification.files_checked,
        "cloud_runs": 0,
    }


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("reference", "release"),
        default="reference",
        help=(
            "reference builds local DuckDB examples; release freezes an already "
            "accepted workspace through the official schema-3 builder"
        ),
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--destination",
        choices=tuple(destination.value for destination in Destination),
        default=Destination.SNOWFLAKE.value,
        help="single real warehouse bound into every public task in this output",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        help=(
            "reference cohort catalog whose exact 15 task identities are frozen; "
            "required in release mode"
        ),
    )
    parser.add_argument(
        "--allow-unlocked-env",
        action="store_true",
        help=(
            "release despite uv.lock drift and record that drift in the schema-3 "
            "manifest; ignored in reference mode"
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=repo.parent / "ELT-training-data" / "curation" / "packages_raw",
    )
    parser.add_argument(
        "--bench-root", type=Path, default=repo.parent / "ELT-Bench"
    )
    parser.add_argument(
        "--difficulty-profile",
        choices=tuple(sorted(DIFFICULTY_PROFILES)),
        help=(
            "reference generation profile (default: challenging); standard "
            "preserves the historical mart and synthetic-scale budgets"
        ),
    )
    args = parser.parse_args()
    if args.mode == "release":
        if args.catalog is None:
            parser.error("--catalog is required in release mode")
        if args.difficulty_profile is not None:
            parser.error("--difficulty-profile is only valid in reference mode")
        validation = freeze_certified_cohort(
            args.workspace.resolve(),
            args.catalog.resolve(),
            args.output.resolve(),
            destination=args.destination,
            allow_unlocked_env=args.allow_unlocked_env,
        )
    else:
        if args.catalog is not None:
            parser.error("--catalog is only valid in release mode")
        if args.allow_unlocked_env:
            parser.error("--allow-unlocked-env is only valid in release mode")
        validation = generate(
            repo,
            args.data_root.resolve(),
            args.bench_root.resolve(),
            args.workspace.resolve(),
            args.output.resolve(),
            destination=args.destination,
            difficulty_profile=resolve_difficulty_profile(
                args.difficulty_profile or CHALLENGING_DIFFICULTY_PROFILE.name
            ),
        )
    print(json.dumps(validation, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

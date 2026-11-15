def main():
    import contextlib
    import io
    import json
    import tempfile
    from pathlib import Path
    from unittest import mock

    from elt_taskgen import cli
    from elt_taskgen.adapters import synsql
    from elt_taskgen.generation.config import DEFAULT_GENERATION_CONFIG
    from elt_taskgen.generation.difficulty_profiles import DEFAULT_GENERATION_PROFILE
    from elt_taskgen.generation.mart_plan import fan_out_rollup
    from elt_taskgen import ingest_manifest as ingest
    from elt_taskgen.pipeline_readiness import GenerationRunSpec, PipelineReadinessReport
    from tests.test_adapters_dbt_synsql import _synsql_tables_entry
    from tests.test_configured_pipeline_cli import ConfiguredPipelineCliTests, _SelectedRoster
    from tests.test_five_source_ingest import FiveSourceManifestTests
    from tests.test_dataset_column_names import EVIDENCE, build_task

    print("\nSTEP 1: Check automatic defaults and stable fingerprints")
    parser = cli.build_parser()
    standard = GenerationRunSpec(candidate_count=1, profile="draft")
    assert standard.generation == DEFAULT_GENERATION_CONFIG
    assert standard.fingerprint() == GenerationRunSpec.model_validate_json(standard.model_dump_json()).fingerprint()
    assert standard.fingerprint() != standard.model_copy(update={"seed": 7}).fingerprint()
    with tempfile.TemporaryDirectory(prefix="relationship-config-") as directory:
        config = Path(directory) / "run.json"
        config.write_text(json.dumps({"candidate_count": 1, "profile": "draft"}))
        args = parser.parse_args(["pipeline", "--run-config", str(config)])
        assert cli._configured_run_spec(args).generation == DEFAULT_GENERATION_CONFIG
        args = parser.parse_args(["pipeline", "--run-config", str(config), "--seed", "7"])
        assert cli._configured_run_spec(args).generation.recipe_selection(7).seed == 7
    print("  PASS: all recipes are automatic; the existing run seed controls reproducibility.")

    print("\nSTEP 2: Run the configured coordinator and refuse a changed seed on resume")
    fixture = ConfiguredPipelineCliTests()
    fixture.setUp()
    try:
        workspace = fixture.root / "profile-run"
        selected = _SelectedRoster(("proof__profile",))
        tasks = fixture._tasks(workspace, ("proof__profile",))
        ingested = fixture._ingested(selected, tasks)
        args = fixture._args(workspace, 1)
        patches = fixture._patch_selection(selected, ingested)
        with patches[0], patches[1], patches[2] as call, mock.patch.object(cli, "_ensure_firewall_armed"), contextlib.redirect_stdout(io.StringIO()):
            assert cli.cmd_pipeline(args) == 0
            assert call.call_args.kwargs["recipe_selection"] == DEFAULT_GENERATION_CONFIG.recipe_selection()
            assert cli.cmd_pipeline(args) == 0
            report = PipelineReadinessReport.model_validate_json(
                (workspace / "state/pipeline_runs/configured-test/readiness.json").read_text()
            )
            assert report.config.generation == DEFAULT_GENERATION_CONFIG
            from elt_taskgen.pipeline_readiness import render_run_report_markdown

            assert "Mart generation: automatic" in render_run_report_markdown(report)
            call.reset_mock()
            args.selection_seed = 7
            try:
                cli.cmd_pipeline(args)
            except cli.CliUsageError:
                pass
            else:
                raise AssertionError("changed seed was silently resumed")
            call.assert_not_called()
    finally:
        fixture.doCleanups()
    print("  PASS: the coordinator forwards and saves generation settings; changing the seed cannot reuse a run id.")

    print("\nSTEP 3: Exercise pinned ingest, generation provenance, and identical replay")
    fixture = FiveSourceManifestTests()
    fixture.setUp()
    try:
        forwarded = {}
        source = build_task(fan_out_rollup(EVIDENCE, mart="studio_activity"), "proof__automatic_ingest")

        def build(entry, paths, catalog, **options):
            forwarded[entry.pool] = options.get("difficulty_profile", "standard")
            identity = fixture._task(entry, paths, catalog)
            return source.model_copy(update={key: getattr(identity, key) for key in
                ("task_id", "family_id", "cluster_id", "origin", "license")})

        with contextlib.ExitStack() as stack:
            for pool in ("dbt", "dlt", "synsql", "schemapile", "wikidbs"):
                stack.enter_context(mock.patch.object(ingest, f"_build_{pool}_task", side_effect=build))
            first = ingest.ingest_five_sources(fixture.manifest, workspace=fixture.workspace,
                difficulty_profile=DEFAULT_GENERATION_PROFILE.name, recipe_selection=DEFAULT_GENERATION_CONFIG.recipe_selection())
            second = ingest.ingest_five_sources(fixture.manifest, workspace=fixture.workspace,
                difficulty_profile=DEFAULT_GENERATION_PROFILE.name, recipe_selection=DEFAULT_GENERATION_CONFIG.recipe_selection())
            assert [task.content_hash() for task in first.tasks] == [task.content_hash() for task in second.tasks]
            assert forwarded == {"dbt": "standard", "dlt": "standard", "wikidbs": "standard",
                                 "synsql": DEFAULT_GENERATION_PROFILE.name, "schemapile": DEFAULT_GENERATION_PROFILE.name}
            prepared = ingest.prepare_five_sources(
                ingest.load_five_source_manifest(fixture.manifest), manifest_path=fixture.manifest,
                workspace=fixture.workspace, difficulty_profile=DEFAULT_GENERATION_PROFILE.name,
                recipe_selection=DEFAULT_GENERATION_CONFIG.recipe_selection(),
            )
            assert all(item.provenance.source.selection_inputs["generation_config"].digest == DEFAULT_GENERATION_CONFIG.digest() for item in prepared)
            try:
                ingest.ingest_five_sources(fixture.manifest, workspace=fixture.workspace,
                    difficulty_profile=DEFAULT_GENERATION_PROFILE.name, recipe_selection=DEFAULT_GENERATION_CONFIG.recipe_selection(7))
            except ingest.FiveSourceIngestError:
                pass
            else:
                raise AssertionError("ingest reused generation-bound provenance with another seed")
    finally:
        fixture.doCleanups()
    print("  PASS: adapters receive supported options; provenance prevents mixing generation settings.")

    print("\nSTEP 4: Compare a real SynSQL task with an internal baseline and preserve the pinned task id")
    entry = _synsql_tables_entry()
    for field in ("table_names", "table_names_original"):
        entry[field].append("payments")
    for field in ("column_names", "column_names_original"):
        entry[field].extend([[2, "payment_id"], [2, "customer_id"], [2, "amount"]])
    entry["column_types"].extend(["number", "number", "number"])
    entry["foreign_keys"].append([8, 1])
    entry["primary_keys"].append(7)
    entry["ddls"].append("CREATE TABLE payments (payment_id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL)")
    with tempfile.TemporaryDirectory(prefix="relationship-adapter-") as directory:
        source = Path(directory) / "tables.json"
        source.write_text(json.dumps([entry]))
        baseline = synsql.to_task_ir(entry["db_id"], source)
        branched = synsql.to_task_ir(entry["db_id"], source, difficulty_profile=DEFAULT_GENERATION_PROFILE)
        assert baseline.task_id == branched.task_id
        assert baseline.content_hash() != branched.content_hash()
        assert branched.marts[0].plan.template_id == "branched_fact_rollup"
        assert all(mart.plan.template_id != "branched_fact_rollup" for mart in baseline.marts)
    print("  PASS: real schema ingestion selects the branched shape and retains its source identity.")


if __name__ == "__main__":
    main()

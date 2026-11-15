def build_fixture(arity=2, origin="synthetic"):
    from elt_taskgen.adapters.evidence import build_branched_marts
    from elt_taskgen.generation.populations import derive_populations_and_attacks
    from elt_taskgen.models import (
        Backend, BackendAssignment, ColumnSpec, ColumnType, Origin, Relationship, TableSpec, TaskIR,
    )
    from elt_taskgen.reference.solution import attach_reference

    keys = ("tenant_id", "account_id", "region_id")[:arity]
    key_columns = tuple(ColumnSpec(name=key, type=ColumnType.INTEGER) for key in keys)
    tables = (
        TableSpec(name="accounts", primary_key=keys, columns=key_columns + (ColumnSpec(name="name", type=ColumnType.TEXT),)),
        TableSpec(name="events", primary_key=("event_id",), columns=(ColumnSpec(name="event_id", type=ColumnType.INTEGER),)
                  + key_columns + (ColumnSpec(name="category_id", type=ColumnType.INTEGER, nullable=True),
                                  ColumnSpec(name="amount", type=ColumnType.INTEGER, nullable=True, description="Recorded transaction amount in source currency."))),
        TableSpec(name="reviews", primary_key=("review_id",), columns=(ColumnSpec(name="review_id", type=ColumnType.INTEGER),)
                  + key_columns + (ColumnSpec(name="score", type=ColumnType.INTEGER, nullable=True),)),
        TableSpec(name="categories", primary_key=("category_id",), columns=(ColumnSpec(name="category_id", type=ColumnType.INTEGER),
                  ColumnSpec(name="label", type=ColumnType.TEXT))),
    )
    relationships = tuple(Relationship(child_table=name, child_columns=keys, parent_table="accounts", parent_columns=keys)
                          for name in ("events", "reviews")) + (Relationship(child_table="events", child_columns=("category_id",),
                          parent_table="categories", parent_columns=("category_id",), required=False),)
    marts, shapes = build_branched_marts(tables, relationships)
    assert marts
    task_id = f"proof__semantics_{arity}_{origin}"
    assignments = tuple(BackendAssignment(table=t.name, backend=Backend.POSTGRES) for t in tables)
    populations, attacks = derive_populations_and_attacks(task_id=task_id, tables=tables, relationships=relationships,
        shapes=shapes, scale_hint={t.name: 25 for t in tables}, backends=1, backend_assignments=assignments)
    return attach_reference(TaskIR(task_id=task_id, family_id=f"{origin}__semantics", cluster_id=task_id,
        origin=Origin(origin), license="CC0-1.0", tables=tables, relationships=relationships, backends=assignments,
        marts=marts, populations=populations, attack_cases=attacks))


def independent_rows(task, mart, evidence, rows):
    """Python business-rule oracle; does not inspect or reuse generated SQL."""
    from collections import defaultdict

    e = evidence
    dimension = {r[e.dimension_key]: r for r in rows[e.dimension]} if e.dimension else {}
    output = []
    for parent in rows[e.parent]:
        key = tuple(parent[k] for k in e.keys)
        records = [r for r in rows[e.fact] if tuple(r[c] for c in e.links) == key
                   and all(r[c] is not None for c in e.links)]
        values = [r[e.measure] for r in records if r[e.measure] is not None]
        threshold = sum(values) / len(values) if values else None
        qualifying = sum(v >= threshold for v in values)
        groups = defaultdict(list)
        for record in records:
            group = record[e.group]
            if group is not None and (not e.dimension or group in dimension):
                groups[group].append(record[e.measure])
        totals = [sum(v for v in values if v is not None) for values in groups.values()
                  if any(v is not None for v in values)]
        winner = min(records, key=lambda r: (r[e.measure] is None, -(r[e.measure] or 0),
                     *((r[k] is None, r[k]) for k in e.row_key))) if records else None
        selected_group = None if winner is None else winner[e.group]
        if e.dimension:
            selected_group = dimension.get(selected_group, {}).get(e.dimension_label)
        result = {**dict(zip(e.keys, key)), "record_count": len(records), "group_count": len(groups),
                  "measure_total": sum(values), "qualifying_count": qualifying,
                  "null_relationship_count": sum(r[e.group] is None for r in records),
                  "unmatched_relationship_count": sum(r[e.group] is not None and r[e.group] not in dimension for r in records),
                  "mean_group_total": sum(totals) / len(totals) if totals else None,
                  **{f"selected_key_{i}": winner[k] if winner else None for i, k in enumerate(e.row_key)},
                  "selected_measure": winner[e.measure] if winner else None, "selected_group": selected_group,
                  "qualifying_fraction": round(qualifying / len(records), 4) if records else 0}
        if e.second_fact:
            result["other_record_count"] = sum(tuple(r[c] for c in e.second_links) == key
                and all(r[c] is not None for c in e.second_links) for r in rows[e.second_fact])
        output.append({c.name: result[c.name] for c in mart.columns})
    return output


def check_oracle(task):
    from elt_taskgen.generation.semantic_difficulty import TEMPLATE, bound_evidence
    from elt_taskgen.generation.source_data import generate_rows
    from elt_taskgen.models import PopulationName
    from elt_taskgen.reference.solution import compile_plan_sql
    from elt_taskgen.verification.upstream_eval import compare_mart, rows_to_canonical_csv
    from tests.proofs.prove_branched_fact_rollup import load_and_fetch

    mart, = (m for m in task.marts if m.plan.template_id == TEMPLATE)
    e = bound_evidence(task, mart)
    for population in PopulationName:
        rows = generate_rows(task, population)
        expected = independent_rows(task, mart, e, rows)
        observed = load_and_fetch(task, rows, compile_plan_sql(task, mart))
        assert len(observed) == len(rows[e.parent])
        assert compare_mart(rows_to_canonical_csv(expected, tuple(c.name for c in mart.columns)), observed, mart), population
    return mart, e


def main():
    import contextlib
    import json
    import tempfile
    from pathlib import Path
    from unittest import mock

    from elt_taskgen.generation.difficulty_profiles import apply_generation_profile
    from elt_taskgen.generation.semantic_difficulty import (
        apply_semantic_difficulty, bound_evidence, certify_semantics, mutation_cases,
        SemanticDifficultyError, TEMPLATE, semantic_candidates, variants,
    )
    from elt_taskgen.generation.source_data import generate_rows, materialize_population
    from elt_taskgen.models import PopulationName, TaskVariant, canonical_json
    from elt_taskgen.reference.gold import freeze_gold
    from elt_taskgen.reference.runner import population_dir, run_reference
    from elt_taskgen.reference.solution import compile_plan_sql
    from elt_taskgen.verification.attacks import materialize_mutation, run_attack
    from elt_taskgen.verification.gates import _gate_variant_required_mutants, TRANSFORM_MUTANT_KINDS
    from elt_taskgen.verification.upstream_eval import compare_mart, evaluate_variant, rows_to_canonical_csv
    from tests.proofs.prove_branched_fact_rollup import load_and_fetch

    print("\nSTEP 1: Compose harder semantics from declared schema evidence")
    original = build_fixture()
    task = apply_semantic_difficulty(original)
    added = [m for m in task.marts if m.plan.template_id == TEMPLATE]
    assert len(added) == 1
    assert task.marts[:-1] == original.marts
    for pop in original.populations:
        if pop.name is not PopulationName.COUNTERFACTUAL:
            assert task.population(pop.name) == pop
    print(f"  Added {added[0].name}: {len(added[0].columns)} columns")
    assert apply_semantic_difficulty(task) == task
    from elt_taskgen.generation.mart_plan import solver_safe_mart_requirements
    from elt_taskgen.review.prose_fidelity import check_prose_fidelity
    from tests.test_plan_descriptions import compliant_prose
    public = json.dumps(solver_safe_mart_requirements(task, added[0]))
    assert added[0].plan.notes not in public
    assert "ignore_filter" not in public and "counterfactual" not in public
    assert not check_prose_fidelity(task.model_copy(update={"solver_prompt": compliant_prose(task)}))
    print("  PASS: deterministic, idempotent composition preserves existing marts and ordinary populations.")

    print("\nSTEP 2: Compare SQL with independent Python arithmetic on all five populations")
    for arity in (1, 2, 3):
        checked = task if arity == 2 else apply_semantic_difficulty(build_fixture(arity))
        mart, evidence = check_oracle(checked)
        cases = mutation_cases(evidence, mart.name)
        rows = generate_rows(checked, PopulationName.COUNTERFACTUAL)
        expected = rows_to_canonical_csv(independent_rows(checked, mart, evidence, rows), tuple(c.name for c in mart.columns))
        for case in cases:
            sql = materialize_mutation(checked, case, object())
            for other in checked.marts:
                if other.name != mart.name:
                    assert sql[other.name] == compile_plan_sql(checked, other)
            wrong = load_and_fetch(checked, rows, sql[mart.name])
            assert not compare_mart(expected, wrong, mart), case.name
        print(f"  PASS: {arity}-column grain, 5 populations, {len(cases)} successfully executed mutants rejected.")

    print("\nSTEP 3: Check schema-driven selection and optional features")
    # A second branch and a dimension are optional; the core composition survives without either.
    raw = original.model_dump(mode="json")
    raw["relationships"] = raw["relationships"][:1]
    for table in raw["tables"]:
        if table["name"] == "events":
            for column in table["columns"]:
                if column["name"] == "category_id":
                    column.update(name="category", type="text", enum_values=["open", "closed"])
    # Only schema evidence and fresh populations are used; the original branch mart is retained as-is.
    from elt_taskgen.models import TaskIR
    text_task = TaskIR.model_validate(raw)
    # Its old fixture refers to category_id, so regenerate rows through the source generator.
    text_task = text_task.model_copy(update={"populations": tuple(p.model_copy(update={"literal_rows": {}})
        if p.literal_rows else p for p in text_task.populations)})
    from elt_taskgen.generation.semantic_difficulty import semantic_witness_rows
    e = semantic_candidates(text_task)[0]
    text_rows = semantic_witness_rows(text_task, e)
    text_task = text_task.model_copy(update={"populations": tuple(p.model_copy(update={"literal_rows": text_rows})
        if p.name is PopulationName.COUNTERFACTUAL else p for p in text_task.populations)})
    text_task = apply_semantic_difficulty(text_task)
    _, e = check_oracle(text_task)
    assert not e.dimension and not e.second_fact
    assert len(variants(e)) == 11  # Nine core rules plus two composite components.
    unkeyed = original.model_copy(update={"tables": tuple(t.model_copy(update={"primary_key": ()})
        if t.name == "events" else t for t in original.tables)})
    _, e = check_oracle(apply_semantic_difficulty(unkeyed))
    assert e.row_key == (e.group,)
    from elt_taskgen.demo_fixture import demo_task
    try:
        apply_semantic_difficulty(demo_task())
    except SemanticDifficultyError as exc:
        assert "No semantic candidate" in str(exc)
    else:
        raise AssertionError("unsupported schema silently received an easy task")
    assert apply_generation_profile(original, "standard") == original
    print("  PASS: direct categories and unkeyed facts work; no source keys are invented. Unsupported schemas refuse explicitly.")

    print("\nSTEP 4: Exercise shared enrichment for every source pool through pinned ingest")
    from elt_taskgen import cli, ingest_manifest as ingest
    from elt_taskgen.pipeline_readiness import GenerationRunSpec
    from elt_taskgen.generation.recipe_registry import RecipeSelection
    from tests.test_five_source_ingest import FiveSourceManifestTests
    assert TEMPLATE in GenerationRunSpec(candidate_count=1, profile="draft").generation.recipe_ids
    try:
        cli._apply_generation_semantics(demo_task().model_copy(update={"relationships": ()}), object())
    except cli.CliUsageError as exc:
        assert "semantic generation refused" in str(exc)
    else:
        raise AssertionError("CLI silently accepted an unsupported schema")
    fixture = FiveSourceManifestTests()
    fixture.setUp()
    try:
        def build(entry, paths, catalog, **options):
            identity = fixture._task(entry, paths, catalog)
            return original.model_copy(update={"task_id": identity.task_id, "family_id": identity.family_id,
                "cluster_id": identity.cluster_id, "origin": identity.origin, "license": identity.license})
        with contextlib.ExitStack() as stack:
            for pool in ("dbt", "synsql", "schemapile", "dlt", "wikidbs"):
                stack.enter_context(mock.patch.object(ingest, f"_build_{pool}_task", side_effect=build))
            prepared = ingest.prepare_five_sources(ingest.load_five_source_manifest(fixture.manifest),
                manifest_path=fixture.manifest, workspace=fixture.workspace, difficulty_profile="semantic", recipe_selection=RecipeSelection(enabled_recipes=(TEMPLATE,)))
        assert len(prepared) == 5
        for item in prepared:
            assert any(m.plan.template_id == TEMPLATE for m in item.task.marts)
            assert item.provenance.source.selection_inputs["generation_config"].locator == "mart-generation:automatic"
            assert cli._apply_generation_semantics(item.task, object()) == item.task
    finally:
        fixture.doCleanups()
    print("  PASS: all five pools receive identical semantic policy and pinned generation provenance.")

    # A native adapter also emits older shape witnesses. In particular, a field
    # used both as a filter and an argmax label must remain inside its enum.
    from elt_taskgen.adapters import synsql
    from elt_taskgen.generation.difficulty_profiles import SEMANTIC_DIFFICULTY_PROFILE
    from elt_taskgen.generation.populations import _literal_row_problems
    from tests.test_adapters_dbt_synsql import _synsql_tables_entry
    with tempfile.TemporaryDirectory(prefix="semantic-native-") as temporary:
        source = Path(temporary) / "tables.json"
        entry = _synsql_tables_entry()
        source.write_text(json.dumps([entry]))
        native = synsql.to_task_ir(entry["db_id"], source, difficulty_profile=SEMANTIC_DIFFICULTY_PROFILE)
    assert not _literal_row_problems(native, native.population(PopulationName.COUNTERFACTUAL))
    native = apply_semantic_difficulty(native)
    check_oracle(native)
    print("  PASS: native enum-constrained adapter output has legal witnesses and correct composed results.")

    print("\nSTEP 5: Run reference, attack fidelity, and the required-mutant gate")
    mart, = (m for m in task.marts if m.plan.template_id == TEMPLATE)
    evidence = bound_evidence(task, mart)
    semantic_cases = mutation_cases(evidence, mart.name)
    # Keep every required legacy transform case. Extraction deliberately drops
    # source tables; those failures cannot count as semantic evidence.
    for gate_task in (task, native):
        cases = tuple(c for c in gate_task.attack_cases if c.required and c.kind in TRANSFORM_MUTANT_KINDS)
        with tempfile.TemporaryDirectory(prefix="semantic-proof-") as temporary:
            workspace = Path(temporary)
            results = {}
            for population in PopulationName:
                materialize_population(gate_task, population, population_dir(workspace, gate_task.task_id, population))
                results[population] = run_reference(gate_task, population, workspace)
            task_root = workspace / "tasks" / gate_task.task_id
            gold = freeze_gold(gate_task, results, task_root / "answer_key")
            for population, result in results.items():
                assert evaluate_variant(TaskVariant.TRANSFORM, gate_task, gold, population,
                    result.stage1_counts, result.mart_rows).reward == 1.0
            rewards = {}
            for case in cases:
                run_attack(gate_task, case, gold, workspace)
                record = json.loads((task_root / "attacks" / case.name / "rewards.json").read_text())
                assert not record["errors"], record["errors"]
                rewards[case.name] = {PopulationName(p): r for p, r in record["rewards_by_variant"]["transform"].items()}
            gate = _gate_variant_required_mutants(gate_task, rewards, TaskVariant.TRANSFORM, workspace)
            assert gate.passed, gate.details
            from elt_taskgen.verification.attack_matrix import check_declared_matrix
            assert check_declared_matrix(gate_task, gold, workspace) is None
            print(f"  PASS: {gate_task.origin.value}: reference 5/5; {len(cases)} legacy/new mutants evaluated without SQL crashes.")

    print("\nSTEP 6: Refuse missing cases, unbound plans, and inert witnesses")
    broken = task.model_copy(update={"attack_cases": tuple(c for c in task.attack_cases if c != semantic_cases[0])})
    try:
        apply_semantic_difficulty(broken)
    except SemanticDifficultyError:
        pass
    else:
        raise AssertionError("missing case was accepted")
    changed_plan = mart.plan.model_copy(update={"notes": canonical_json({})})
    try:
        bound_evidence(task, mart.model_copy(update={"plan": changed_plan}))
    except (SemanticDifficultyError, KeyError, TypeError):
        pass
    else:
        raise AssertionError("unbound plan was accepted")
    rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
    rows[evidence.fact] = ()
    try:
        certify_semantics(task, mart, evidence, rows)
    except SemanticDifficultyError as exc:
        assert "No executable witness" in str(exc)
    else:
        raise AssertionError("inert witnesses were accepted")
    print("  PASS: admission requires executable evidence, not template labels.")


if __name__ == "__main__":
    main()

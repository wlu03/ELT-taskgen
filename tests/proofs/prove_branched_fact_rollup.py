def build_fixture(arity=2):
    from elt_taskgen.adapters.evidence import branched_fact_candidates
    from elt_taskgen.generation.populations import derive_populations_and_attacks
    from elt_taskgen.generation.shapes.branched_fact_rollup import branched_fact_rollup
    from elt_taskgen.models import (
        Backend, BackendAssignment, ColumnSpec, ColumnType, MartSpec,
        Origin, Relationship, TableSpec, TaskIR,
    )
    from elt_taskgen.reference.solution import attach_reference

    keys = ("race_id", "driver_id", "season_id")[:arity]
    key_columns = tuple(ColumnSpec(name=key, type=ColumnType.INTEGER) for key in keys)
    tables = (TableSpec(name="results", columns=key_columns + (ColumnSpec(name="result_name", type=ColumnType.TEXT),), primary_key=keys),) + tuple(
        TableSpec(
            name=name,
            columns=(ColumnSpec(name="event_id", type=ColumnType.INTEGER),) + key_columns
                    + (ColumnSpec(name="duration", type=ColumnType.INTEGER, nullable=True),),
            primary_key=("event_id",),
        )
        for name in ("lap_times", "pit_stops")
    )
    relationships = tuple(Relationship(
        child_table=table.name, child_columns=keys,
        parent_table="results", parent_columns=keys,
    ) for table in tables[1:])
    candidate, = branched_fact_candidates(tables, relationships)
    built = branched_fact_rollup(candidate, mart="driver_activity")
    mart = MartSpec(
        name=built.plan.mart, description="Activity for every result row.",
        grain="One row per complete result key, including results with no activity.",
        key_columns=keys, columns=built.columns, plan=built.plan,
    )
    assignments = tuple(BackendAssignment(table=table.name, backend=backend) for table, backend in zip(
        tables, (Backend.POSTGRES, Backend.MONGODB, Backend.S3), strict=True,
    ))
    task_id = f"proof__branched_{arity}"
    populations, cases = derive_populations_and_attacks(
        task_id=task_id, tables=tables, relationships=relationships,
        shapes=(built.shape,), scale_hint={"results": 8, "lap_times": 30, "pit_stops": 25},
        backends=3, backend_assignments=assignments,
    )
    task = TaskIR(
        task_id=task_id, family_id="proof__branched", cluster_id=task_id,
        origin=Origin.SYNTHETIC, license="CC0-1.0", tables=tables,
        relationships=relationships, backends=assignments, marts=(mart,),
        populations=populations, attack_cases=cases,
    )
    return attach_reference(task), built, candidate


def load_and_fetch(task, rows, sql):
    import duckdb
    from elt_taskgen.reference.solution import create_table, _insert_rows

    with duckdb.connect(":memory:") as connection:
        for table in task.tables:
            create_table(connection, table)
            _insert_rows(connection, table, rows[table.name])
        cursor = connection.execute(sql)
        return [dict(zip((column[0] for column in cursor.description), row, strict=True)) for row in cursor.fetchall()]


def main():
    import copy
    import json
    import tempfile
    from dataclasses import replace
    from pathlib import Path


    from elt_taskgen.adapters.evidence import branched_fact_candidates
    from elt_taskgen.adapters.synsql import _build_marts
    from elt_taskgen.generation.difficulty_profiles import RELATIONSHIP_DIFFICULTY_PROFILE
    from elt_taskgen.generation.mart_plan import validate_plan, semantic_pattern_problems
    from elt_taskgen.generation.populations import counterfactual_rows_for_shapes
    from elt_taskgen.generation.shapes.branched_fact_rollup import branch_measures
    from elt_taskgen.generation.source_data import generate_rows, materialize_population
    from elt_taskgen.generation.witness.branched import InertBranchedWitnessError, verify_branched_witnesses
    from elt_taskgen.models import MartOpKind, PopulationName, TaskVariant
    from elt_taskgen.reference.gold import freeze_gold
    from elt_taskgen.reference.runner import population_dir, run_reference
    from elt_taskgen.reference.solution import compile_plan_sql
    from elt_taskgen.verification.attacks import materialize_mutation, run_attack
    from elt_taskgen.verification.branched import branched_attack_cases
    from elt_taskgen.verification.relationship_coverage import REPORT_REL, relationship_coverage, write_relationship_report
    from elt_taskgen.verification.gates import _gate_variant_required_mutants
    from elt_taskgen.verification.upstream_eval import compare_mart, evaluate_variant, rows_to_canonical_csv

    print("\nSTEP 1: Select two facts sharing a complete composite key")
    task, built, candidate = build_fixture()
    mart = task.marts[0]
    assert not validate_plan(task, mart.plan)
    assert not semantic_pattern_problems(mart.plan)
    assert task.content_hash() == build_fixture()[0].content_hash()
    print(f"  Grain:    {candidate.grain_table} {candidate.grain_key}")
    print(f"  Branches: {[branch.table for branch in candidate.branches]}")
    print(f"  Witnesses: {built.shape.witnesses}")

    print("\nSTEP 2: Inspect the cross-product and key-collision rows")
    rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
    verify_branched_witnesses(task.tables, task.relationships, built.shape, rows)
    key = tuple(rows["results"][0][column] for column in candidate.grain_key)
    for branch in candidate.branches:
        matching = [row for row in rows[branch.table] if tuple(row[c] for c in branch.child_columns) == key]
        print(f"  {branch.table:<14} key={key}: {[row[branch.measure] for row in matching]}")
        assert len(matching) >= 2
    print(f"  Parent keys: {[tuple(row[c] for c in candidate.grain_key) for row in rows['results']]}")

    print("\nSTEP 3: Compare the reference to direct per-parent arithmetic")
    reference_sql = compile_plan_sql(task, mart)
    reference_rows = load_and_fetch(task, rows, reference_sql)
    groups = branch_measures(candidate)
    expected = []
    for parent in rows[candidate.grain_table]:
        result = {key: parent[key] for key in candidate.grain_key + tuple(name for name, _ in candidate.attributes)}
        for branch, measures in zip(candidate.branches, groups, strict=True):
            facts = [row for row in rows[branch.table] if all(row[c] == parent[p] for c, p in
                     zip(branch.child_columns, branch.parent_columns, strict=True))]
            values = [row[branch.measure] for row in facts if branch.measure and row[branch.measure] is not None]
            for column, function, _ in measures:
                result[column] = {"COUNT": len(facts), "SUM": sum(values),
                                  "MIN": min(values, default=0), "MAX": max(values, default=0)}[function]
        expected.append(result)
    columns = tuple(column.name for column in mart.columns)
    gold_csv = rows_to_canonical_csv(expected, columns)
    assert compare_mart(gold_csv, reference_rows, mart)
    # A different correct SQL strategy must also pass the result comparator.
    selections = [f'p."{name}"' for name in candidate.grain_key]
    for branch, measures in zip(candidate.branches, groups, strict=True):
        predicate = " AND ".join(f'f."{c}" = p."{p}"' for c, p in
                                 zip(branch.child_columns, branch.parent_columns, strict=True))
        for name, function, _ in measures:
            argument = "*" if function == "COUNT" else f'f."{branch.measure}"'
            selections.append(f'COALESCE((SELECT {function}({argument}) FROM "{branch.table}" f '
                              f'WHERE {predicate}), 0) AS "{name}"')
    alternative_sql = f'SELECT {", ".join(selections)} FROM "{candidate.grain_table}" p'
    assert compare_mart(gold_csv, load_and_fetch(task, rows, alternative_sql), mart)
    print("  PASS: SQL agrees with independent Python arithmetic, including empty branches.")
    print("  PASS: equivalent correlated subqueries are accepted too.")

    print("\nSTEP 4: Execute every targeted mutant on those same rows")
    mutants = branched_attack_cases(built.shape)
    fingerprints = set()
    for case in mutants:
        mutant_sql = materialize_mutation(task, case, object())[mart.name]
        assert mutant_sql not in fingerprints
        fingerprints.add(mutant_sql)
        mutant_rows = load_and_fetch(task, rows, mutant_sql)
        assert not compare_mart(gold_csv, mutant_rows, mart), case.name
        print(f"  KILLED: {case.name}")
    print(f"  All {len(mutants)} mutants executed successfully and returned wrong results.")

    print("\nSTEP 5: Check branch independence and witness removal")
    changed = copy.deepcopy(rows)
    new_row = dict(changed["lap_times"][0])
    new_row.update(event_id=999999, duration=9)
    changed["lap_times"].append(new_row)
    after = load_and_fetch(task, changed, reference_sql)
    before_by_key = {tuple(row[k] for k in candidate.grain_key): row for row in reference_rows}
    for row in after:
        before = before_by_key[tuple(row[k] for k in candidate.grain_key)]
        assert all(row[name] == before[name] for name, _, _ in groups[1])
    weakened = copy.deepcopy(rows)
    weakened["lap_times"] = [row for row in weakened["lap_times"] if row["event_id"] != rows["lap_times"][0]["event_id"]]
    try:
        verify_branched_witnesses(task.tables, task.relationships, built.shape, weakened)
    except InertBranchedWitnessError:
        print("  PASS: changing A leaves B unchanged; removing the fanout witness is refused.")
    else:
        raise AssertionError("a lost witness was accepted")
    weakened = copy.deepcopy(rows)
    collision_key = tuple(rows["results"][4][column] for column in candidate.grain_key)
    weakened["lap_times"] = [row for row in weakened["lap_times"]
                             if tuple(row[c] for c in candidate.branches[0].child_columns) != collision_key]
    try:
        verify_branched_witnesses(task.tables, task.relationships, built.shape, weakened)
    except InertBranchedWitnessError:
        print("  PASS: removing one branch's component-collision witness is refused.")
    else:
        raise AssertionError("a missing composite collision was accepted")

    print("\nSTEP 6: Exercise key arities, reordered FKs, selection, and population merging")
    for arity in (1, 3):
        other, other_built, _ = build_fixture(arity)
        other_rows = generate_rows(other, PopulationName.COUNTERFACTUAL)
        expected_sql = compile_plan_sql(other, other.marts[0])
        expected_rows = load_and_fetch(other, other_rows, expected_sql)
        expected_csv = rows_to_canonical_csv(expected_rows, tuple(c.name for c in other.marts[0].columns))
        for case in branched_attack_cases(other_built.shape):
            wrong = load_and_fetch(other, other_rows, materialize_mutation(other, case, object())[other.marts[0].name])
            assert not compare_mart(expected_csv, wrong, other.marts[0]), case.name
        print(f"  Arity {arity}: all targeted mutants killed.")
    reversed_rels = tuple(rel.model_copy(update={
        "parent_columns": tuple(reversed(rel.parent_columns)),
        "child_columns": tuple(reversed(rel.child_columns)),
    }) for rel in task.relationships)
    assert branched_fact_candidates(task.tables, reversed_rels) == (candidate,)
    one_to_one = (task.tables[0],) + tuple(table.model_copy(update={"primary_key": candidate.grain_key}) for table in task.tables[1:])
    assert not branched_fact_candidates(one_to_one, task.relationships)
    marts, shapes, names = _build_marts(task.tables, task.relationships, "fixture", difficulty_profile=RELATIONSHIP_DIFFICULTY_PROFILE)
    assert names[0] == "branched_fact_rollup" and len(marts) == len(shapes)
    merged = counterfactual_rows_for_shapes(task.tables, task.relationships, (built.shape, replace(built.shape, mart="second")))
    verify_branched_witnesses(task.tables, task.relationships, built.shape, merged)
    joins = [i for i, op in enumerate(mart.plan.ops) if op.kind is MartOpKind.JOIN]
    broken_ops = list(mart.plan.ops)
    first = joins[0]
    broken_ops[first] = broken_ops[first].model_copy(update={"predicate": broken_ops[first].predicate.split(" AND ")[0]})
    assert semantic_pattern_problems(mart.plan.model_copy(update={"ops": tuple(broken_ops)}))
    broken_ops[first] = broken_ops[first].model_copy(update={"tables": ()})
    assert semantic_pattern_problems(mart.plan.model_copy(update={"ops": tuple(broken_ops)}))
    print("  PASS: selection rejects one-to-one branches; full-key certification rejects a missing predicate.")

    print("\nSTEP 7: Run all five populations through rendering, gold, and the transform gate")
    with tempfile.TemporaryDirectory(prefix="branched-proof-") as temporary:
        workspace = Path(temporary)
        results = {}
        cases = tuple(case for case in task.attack_cases if case in mutants or case.name in ("inner_join", "no_null_default"))
        gate_task = task.model_copy(update={"attack_cases": cases})
        for population in PopulationName:
            directory = population_dir(workspace, task.task_id, population)
            materialize_population(gate_task, population, directory)
            results[population] = run_reference(gate_task, population, workspace)
        task_root = workspace / "tasks" / task.task_id
        gold = freeze_gold(gate_task, results, task_root / "answer_key")
        for population, result in results.items():
            measured = evaluate_variant(TaskVariant.TRANSFORM, gate_task, gold, population, result.stage1_counts, result.mart_rows)
            assert measured.reward == 1.0
        rewards = {}
        for case in cases:
            run_attack(gate_task, case, gold, workspace)
            record = json.loads((workspace / "tasks" / task.task_id / "attacks" / case.name / "rewards.json").read_text())
            assert not record["errors"], record["errors"]
            rewards[case.name] = {PopulationName(pop): reward for pop, reward in record["rewards_by_variant"]["transform"].items()}
        gate = _gate_variant_required_mutants(gate_task, rewards, TaskVariant.TRANSFORM, workspace)
        assert gate.passed, gate.details
        print(f"  Reference: 5/5 populations passed. Transform gate: {len(cases)}/{len(cases)} cases passed.")
        print("  PASS: all kills are wrong outputs, with no SQL crashes.")

        print("\nSTEP 8: Save measured coverage and reject stale, missing, or crashed evidence")
        report = json.loads((task_root / REPORT_REL).read_text())
        assert report == relationship_coverage(gate_task, workspace)
        reasoning = report["relationship_reasoning"]
        assert reasoning["complete"] and reasoning["killed_mutant_cases"] == len(mutants)
        assert len(reasoning["killed_mutant_families"]) == 3
        assert report["integration_backends"]["backend_count"] == 3
        case = mutants[0]
        record_path = task_root / "attacks" / case.name / "rewards.json"
        original = record_path.read_text()
        for state in ("stale", "execution_error", "survived", "incomplete", "not_run"):
            altered = json.loads(original)
            if state == "stale":
                altered["task_content_hash"] = "0" * 64
            elif state == "execution_error":
                altered["errors"] = {"counterfactual": "simulated SQL error"}
            elif state == "survived":
                altered["rewards_by_variant"]["transform"]["counterfactual"] = 1.0
            elif state == "incomplete":
                altered["rewards_by_variant"]["transform"].pop("primary")
            record_path.write_text(json.dumps(altered))
            if state == "not_run":
                record_path.unlink()
            observed = relationship_coverage(gate_task, workspace)["relationship_reasoning"]
            assert not observed["complete"] and observed["killed_mutant_cases"] == len(mutants) - 1
            assert observed["marts"][0]["mutants"][0]["status"] == state
            record_path.write_text(original)
        sql_path = task_root / "attacks" / case.name / f"mutation_{mart.name}.sql"
        original_sql = sql_path.read_text()
        sql_path.write_text("SELECT 1")
        assert relationship_coverage(gate_task, workspace)["relationship_reasoning"]["marts"][0]["mutants"][0]["status"] == "invalid"
        sql_path.write_text(original_sql)
        assert write_relationship_report(gate_task, workspace) == report
        print("  PASS: seven cases count as three families; absent, stale, erroneous, or altered evidence earns no credit.")

        print("\nSTEP 9: Freeze private reports in real package/release layouts using fixture acceptance records")
        from elt_taskgen.export import eltbench, local_package, release
        from elt_taskgen.models import RLVR_TASK_VARIANTS, sha256_hex
        from tests.release_doubles import FakeEngine, FakeReport, FakeSelection

        eltbench.export_task(gate_task, gold, task_root / "task", task_root / "answer_key")
        for variant in RLVR_TASK_VARIANTS:
            eltbench.emit_variant(gate_task, gold, variant, task_root / "variants" / variant.value,
                                  populations_dir=task_root / "populations")
        engine = FakeEngine(workspace, gate_task, FakeReport("pass", gate_task.content_hash()))
        engine.task_dir = lambda task_id: workspace / "tasks" / task_id
        package_path = workspace / "package"
        package = local_package.freeze_local_package(engine, gate_task, package_path)
        relative = f"private/{task.task_id}/{REPORT_REL.as_posix()}"
        assert json.loads((package_path / relative).read_text()) == report
        assert local_package.verify_local_package(package_path).ok
        selection = FakeSelection(train=(task.task_id,), variants={task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)})
        release_path = workspace / "release"
        manifest = release.freeze_release(engine, selection, release_path)
        assert json.loads((release_path / relative).read_text()) == report
        assert manifest.checksums[relative] == sha256_hex((release_path / relative).read_text())
        assert not list((release_path / "public").rglob(REPORT_REL.name))
        print("  PASS: package and release contain checksum-bound private coverage; public bundles omit it.")


if __name__ == "__main__":
    main()

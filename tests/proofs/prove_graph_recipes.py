"""Run: .venv/bin/python -m tests.proofs.prove_graph_recipes (no provider calls)."""
from pathlib import Path


def main():
    import json
    import random
    import tempfile
    from dataclasses import asdict
    from tests.graph_fixtures import graph_source
    from tests.graph_oracle import graph_expected
    from tests.proofs.prove_branched_fact_rollup import load_and_fetch
    from elt_taskgen.generation.graph_evidence import GRAPH_RECIPES, NORMALIZED, COMPOSITE, ROLES, graph_candidates
    from elt_taskgen.generation.graph_recipes import bound_graph
    from elt_taskgen.generation.graph_plan import PATTERNS
    from elt_taskgen.generation.relationship_graph import relationship_graph, normalized_integer
    from elt_taskgen.generation.recipe_registry import apply_semantic_recipes, RecipeSelection
    from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
    from elt_taskgen.generation.source_data import generate_rows
    from elt_taskgen.generation.populations import _literal_row_problems
    from elt_taskgen.models import PopulationName, TaskIR, canonical_json
    from elt_taskgen.reference.solution import compile_plan_sql
    from elt_taskgen.verification.attacks import materialize_mutation, _validate_realized_fidelity
    from elt_taskgen.verification.relationship_coverage import relationship_coverage
    from elt_taskgen.verification.upstream_eval import compare_mart, rows_to_canonical_csv
    checks=mutants=0
    samples=[]
    gate_tasks=[]
    print("STEP 1: Generate six graph recipes over scalar, composite, three-part, and text keys",flush=True)
    for recipe in GRAPH_RECIPES:
        for arity in (1,2,3):
            if recipe==COMPOSITE and arity==1:
                assert not graph_candidates(graph_source(recipe,arity),recipe)
                checks+=1
                continue
            for text in (False,True) if recipe!=NORMALIZED else (False,):
                source=graph_source(recipe,arity,text)
                selection=RecipeSelection(enabled_recipes=(recipe,))
                task=apply_semantic_recipes(source,selection)
                assert task.marts[:-1]==source.marts
                assert apply_semantic_recipes(task,selection)==task
                assert TaskIR.model_validate_json(task.model_dump_json())==task
                mart=task.marts[-1]
                e=bound_graph(task,mart)
                assert PATTERNS[recipe] in mart.plan.semantic_patterns
                assert not _literal_row_problems(task,task.population(PopulationName.COUNTERFACTUAL))
                checks+=5
                cf=task.population(PopulationName.COUNTERFACTUAL).literal_rows
                # Each population, reversed source order, all-empty inputs,
                # and arbitrary independently retained subsets get a separate
                # Python business-rule result; no generated SQL in the oracle.
                inputs=[(p.value,generate_rows(task,p)) for p in PopulationName]
                inputs.extend((("reversed",{n:tuple(reversed(v)) for n,v in cf.items()}),
                               ("empty",{n:() for n in cf})))
                for seed in range(3):
                    rng=random.Random(seed)
                    inputs.append((f"subset_{seed}",{n:tuple(r for r in v if rng.random()<0.7) for n,v in cf.items()}))
                for label,rows in inputs:
                    expected=graph_expected(e,recipe,rows)
                    observed=load_and_fetch(task,rows,compile_plan_sql(task,mart))
                    assert compare_mart(rows_to_canonical_csv(expected,tuple(c.name for c in mart.columns)),observed,mart),(recipe,arity,text,label,expected,observed)
                    assert len(observed)==len(rows[e.grain_table])
                    checks+=2
                gold=rows_to_canonical_csv(graph_expected(e,recipe,cf),tuple(c.name for c in mart.columns))
                for case in task.attack_cases:
                    sql=materialize_mutation(task,case,object())
                    _validate_realized_fidelity(task,case,sql,None)
                    assert all(sql[m.name]==compile_plan_sql(task,m) for m in source.marts)
                    wrong=load_and_fetch(task,cf,sql[mart.name])
                    assert not compare_mart(gold,wrong,mart),(recipe,case.name)
                    mutants+=1
                    checks+=2
                with tempfile.TemporaryDirectory() as directory:
                    report=relationship_coverage(task,Path(directory))
                    assert report['relationship_reasoning_difficulty']['witness_backed_mutant_family_count']==0
                    assert not report['relationship_reasoning']['complete']
                    assert report['integration_backend_difficulty']['calibrated_score'] is None
                    checks+=3
                if arity==2 and not text:
                    gate_tasks.append(task)
                    samples.append({"recipe":recipe,"input":cf,"output":graph_expected(e,recipe,cf),
                        "mutants":[c.name for c in task.attack_cases],"graph":asdict(relationship_graph(task,grain_table=e.grain_table,rows=cf,population="counterfactual"))})
                print(f"  PASS {recipe}, key arity={arity}, text={text}",flush=True)
    print("STEP 2: Reject unsupported bindings, tampered contracts, and missing mandatory mutants",flush=True)
    task=apply_semantic_recipes(graph_source(ROLES),RecipeSelection(enabled_recipes=(ROLES,)))
    for changed in (task.model_copy(update={"attack_cases":task.attack_cases[1:]}),
                    task.model_copy(update={"marts":(*task.marts[:-1],task.marts[-1].model_copy(update={"grain":"changed"}))})):
        try:
            apply_semantic_recipes(changed,RecipeSelection(enabled_recipes=(ROLES,)))
        except SemanticDifficultyError:
            checks+=1
        else:
            raise AssertionError("tampered graph admitted")
    for value,expected in ((" +00042 ",42),("42.0",None),("42e0",None),("\t42",None),("４２",None),
                           ("9223372036854775808",None),("-9223372036854775808",-(2**63)),("-0",0),(True,None)):
        assert normalized_integer(value)==expected
        checks+=1
    unknown=relationship_graph(task)
    assert all(e.observed_orphan_count is None and e.maximum_fanout is None for e in unknown.edges)
    checks+=1
    observed=relationship_graph(task,rows=task.population(PopulationName.COUNTERFACTUAL).literal_rows,population="counterfactual")
    assert all(e.observed_null_count==1 and e.observed_orphan_count==1 and e.maximum_fanout==5
               and e.observed_match_rate==5/6 for e in observed.edges)
    reordered=task.model_copy(update={"relationships":tuple(r.model_copy(update={
        "child_columns":tuple(reversed(r.child_columns)),"parent_columns":tuple(reversed(r.parent_columns))}) for r in reversed(task.relationships))})
    assert graph_candidates(task,ROLES)==graph_candidates(reordered,ROLES)
    assert all(e.parent_key_proven and len(e.child_columns)==len(e.parent_columns)==2 for e in unknown.edges)
    from elt_taskgen.models import Backend
    diverse=task.model_copy(update={"backends":tuple(a.model_copy(update={"backend":Backend.S3}) if a.table=='entities' else a for a in task.backends)})
    assert all(e.cross_backend for e in relationship_graph(diverse).edges)
    try:
        relationship_graph(task,rows={})
    except ValueError:
        pass
    else:
        raise AssertionError("unscoped observations accepted")
    # A malformed saved graph is not a certified pattern.
    from elt_taskgen.generation.graph_plan import certifies_graph
    mart=task.marts[-1];notes=json.loads(mart.plan.notes);notes['evidence']['edges']=[]
    assert not certifies_graph(mart.plan.model_copy(update={"notes":json.dumps(notes)}),ROLES)
    checks+=6
    print("STEP 3: Run rendered populations, frozen gold, required-mutant gates, and measured coverage",flush=True)
    from elt_taskgen.generation.source_data import materialize_population
    from elt_taskgen.reference.runner import population_dir,run_reference
    from elt_taskgen.reference.gold import freeze_gold
    from elt_taskgen.models import TaskVariant
    from elt_taskgen.verification.attacks import run_attack
    from elt_taskgen.verification.gates import _gate_variant_required_mutants
    for task in gate_tasks:
        with tempfile.TemporaryDirectory(prefix="graph-gate-") as directory:
            workspace=Path(directory)
            results={}
            for population in PopulationName:
                materialize_population(task,population,population_dir(workspace,task.task_id,population))
                results[population]=run_reference(task,population,workspace)
            root=workspace/'tasks'/task.task_id
            gold=freeze_gold(task,results,root/'answer_key')
            rewards={}
            for case in task.attack_cases:
                run_attack(task,case,gold,workspace)
                record=json.loads((root/'attacks'/case.name/'rewards.json').read_text())
                assert record['errors']=={},record
                rewards[case.name]={PopulationName(p):r for p,r in record['rewards_by_variant']['transform'].items()}
                checks+=1
            gate=_gate_variant_required_mutants(task,rewards,TaskVariant.TRANSFORM,workspace)
            assert gate.passed,gate.details
            report=relationship_coverage(task,workspace)
            assert report['relationship_reasoning']['complete']
            assert report['relationship_reasoning']['killed_mutant_cases']==len(task.attack_cases)
            # Components of one composite key count as one failure family.
            assert report['relationship_reasoning_difficulty']['witness_backed_mutant_family_count']<len(task.attack_cases)
            checks+=4
            path=root/'attacks'/task.attack_cases[0].name/'rewards.json'
            saved=path.read_text()
            for state in ('stale','execution_error','survived','incomplete','not_run'):
                record=json.loads(saved)
                if state=='stale':record['task_content_hash']='0'*64
                elif state=='execution_error':record['errors']={'counterfactual':'SQL error'}
                elif state=='survived':record['rewards_by_variant']['transform']['counterfactual']=1
                elif state=='incomplete':record['rewards_by_variant']['transform'].pop('primary')
                path.write_text(json.dumps(record))
                if state=='not_run':path.unlink()
                observed=relationship_coverage(task,workspace)['relationship_reasoning']
                assert not observed['complete']
                assert observed['marts'][0]['mutants'][0]['status']==state
                path.write_text(saved)
                checks+=2
            print(f"  PASS {task.marts[-1].plan.template_id}: gate and evidence freshness",flush=True)
    print("STEP 4: Save exact counterexample inputs, correct outputs, and mutation inventory",flush=True)
    destination=Path(__file__).resolve().parents[2] / 'runs' / 'relationship-proofs'/'graph-recipes-proof.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(canonical_json({"checks":checks,"mutants_killed":mutants,"samples":samples,
        "scope":"Local DuckDB business-rule and mutation proof; no cloud or model benchmark."})+'\n')
    print(f"PASS: {checks} checks; {mutants} successfully executed wrong implementations rejected. {destination}")


if __name__=='__main__':
    main()

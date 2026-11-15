"""Native validity intervals: boundaries, gaps, overlap and version tie rules."""
from pathlib import Path


def expected(e,rows):
    from datetime import datetime
    stamp=lambda value:datetime.fromisoformat(str(value)) if value is not None else None
    output=[]
    for event in rows[e.fact]:
        eligible=[]
        time=stamp(event[e.event_time])
        event_key=tuple(event[c] for c in e.links)
        for h in rows[e.history]:
            start,end=stamp(h[e.valid_from]),stamp(h[e.valid_to])
            if (time is not None and start is not None and None not in event_key
                and event_key==tuple(h[c] for c in e.history_links) and start<=time
                and (end is None or start<end and time<end)):
                eligible.append(h)
        winner=max(eligible,key=lambda h:(stamp(h[e.valid_from]),tuple(h[k] for k in e.history_keys))) if eligible else None
        output.append({**{k:event[k] for k in e.row_keys},"historical_value":winner[e.attribute] if winner else None,
                       "matched_from":stamp(winner[e.valid_from]) if winner else None})
    return output


def main():
    import json
    import random
    from tests.advanced_fixtures import advanced_source
    from tests.proofs.prove_branched_fact_rollup import load_and_fetch
    from elt_taskgen.generation.advanced_recipes import apply_advanced_difficulty,bound_advanced
    from elt_taskgen.generation.source_data import generate_rows
    from elt_taskgen.models import ColumnSpec,ColumnType,PopulationName,canonical_json
    from elt_taskgen.reference.solution import compile_plan_sql
    from elt_taskgen.verification.attacks import materialize_mutation
    from elt_taskgen.verification.upstream_eval import compare_mart,rows_to_canonical_csv
    checks=mutants=0
    samples=[]
    print("STEP 1: Admit native as-of recipes for scalar/composite, numeric/text, date/timestamp keys",flush=True)
    for composite in (False,True):
        for text in (False,True):
            for temporal in (ColumnType.DATE,ColumnType.TIMESTAMP):
                source=advanced_source(composite=composite,text_keys=text)
                source=source.model_copy(update={"tables":tuple(t.model_copy(update={"columns":(
                    *(c.model_copy(update={"type":temporal}) if c.type is ColumnType.TIMESTAMP else c for c in t.columns),
                    *((ColumnSpec(name="valid_to",type=temporal,nullable=True),) if t.name=="customer_history" else ()))}) for t in source.tables)})
                task=apply_advanced_difficulty(source,recipe="historical_asof_v1")
                mart=task.marts[-1];e=bound_advanced(task,mart)
                assert apply_advanced_difficulty(task,recipe="historical_asof_v1")==task
                checks+=1
                cf=task.population(PopulationName.COUNTERFACTUAL).literal_rows
                inputs=[(p.value,generate_rows(task,p)) for p in PopulationName]
                inputs.extend((("reversed",{n:tuple(reversed(v)) for n,v in cf.items()}),("empty",{n:() for n in cf})))
                for seed in range(5):
                    rng=random.Random(seed)
                    inputs.append((f"subset_{seed}",{n:tuple(r for r in v if rng.random()<0.7) for n,v in cf.items()}))
                for label,rows in inputs:
                    gold=rows_to_canonical_csv(expected(e,rows),tuple(c.name for c in mart.columns))
                    actual=load_and_fetch(task,rows,compile_plan_sql(task,mart))
                    assert compare_mart(gold,actual,mart),(composite,text,temporal,label,expected(e,rows),actual)
                    assert len(actual)==len(rows[e.fact])
                    checks+=2
                gold=rows_to_canonical_csv(expected(e,cf),tuple(c.name for c in mart.columns))
                for case in task.attack_cases:
                    sql=materialize_mutation(task,case,object())[mart.name]
                    assert not compare_mart(gold,load_and_fetch(task,cf,sql),mart),case.name
                    mutants+=1;checks+=1
                samples.append({"composite":composite,"text":text,"type":temporal.value,
                    "input":cf,"output":expected(e,cf),"mutants":[c.name for c in task.attack_cases]})
                print(f"  PASS composite={composite}, text={text}, time={temporal.value}",flush=True)
    print("STEP 2: Save independent answers and successfully executed mutation failures",flush=True)
    destination=Path(__file__).resolve().parents[2] / 'runs' / 'relationship-proofs'/'native-asof-proof.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps({"checks":checks,"mutants_killed":mutants,"samples":samples},default=str,sort_keys=True)+'\n')
    print(f"PASS: {checks} checks; {mutants} mutants rejected. {destination}")


if __name__=='__main__':
    main()

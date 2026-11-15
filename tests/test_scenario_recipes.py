import copy
from datetime import datetime,timedelta
import random
import unittest
from unittest.mock import patch

from elt_taskgen.generation.recipe_assignment import assign_recipe_batch
from elt_taskgen.generation.recipe_registry import RecipeSelection,apply_semantic_recipes
from elt_taskgen.generation.scenario_evidence import SCENARIO_RECIPES,INTERVAL,SESSION,LIFECYCLE,DOCUMENT,LEDGER,HIERARCHY,scenario_candidates
from elt_taskgen.generation.scenario_plan import PATTERNS,scenario_plan
from elt_taskgen.generation.scenario_recipes import apply_scenario_difficulty,bound_scenario
from elt_taskgen.generation.mart_plan import detect_semantic_patterns,op_problems
from elt_taskgen.generation.source_data import generate_rows
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
from elt_taskgen.models import ColumnType,MartOp,MartOpKind,PopulationName
from elt_taskgen.verification.attacks import materialize_mutation,_validate_realized_fidelity
from elt_taskgen.reference.solution import compile_plan_sql
from tests.scenario_fixtures import scenario_source
from tests.scenario_oracle import assert_oracle


class ScenarioRecipeTests(unittest.TestCase):
    def test_all_recipes_keys_populations_and_mutations(self):
        for composite,text,self_graph in ((False,False,False),(True,False,False),(True,True,True)):
            for recipe in SCENARIO_RECIPES:
                with self.subTest(recipe=recipe,composite=composite,text=text,self_graph=self_graph):
                    original=scenario_source(composite=composite,text_keys=text,self_graph=self_graph)
                    task=apply_scenario_difficulty(original,recipe=recipe)
                    mart=task.marts[-1]
                    e=bound_scenario(task,mart)
                    self.assertEqual(task.marts[:-1],original.marts)
                    self.assertEqual(apply_scenario_difficulty(task,recipe=recipe),task)
                    self.assertIn(PATTERNS[recipe],detect_semantic_patterns(mart.plan))
                    for population in task.populations:
                        rows=generate_rows(task,population.name)
                        assert_oracle(task,mart,e,rows)
                    for case in task.attack_cases[len(original.attack_cases):]:
                        sqls=materialize_mutation(task,case,object())
                        _validate_realized_fidelity(task,case,sqls,None)
                        self.assertEqual(sqls[original.marts[0].name],compile_plan_sql(task,original.marts[0]))

    def test_nulls_orphans_empty_inputs_and_shuffled_order(self):
        for recipe in SCENARIO_RECIPES:
            task=apply_scenario_difficulty(scenario_source(),recipe=recipe)
            mart=task.marts[-1]
            e=bound_scenario(task,mart)
            rows={k:list(v) for k,v in task.population(PopulationName.COUNTERFACTUAL).literal_rows.items()}
            for mode in ('empty_facts','empty_parents','shuffle','null_fields','orphan_links'):
                with self.subTest(recipe=recipe,mode=mode):
                    changed=copy.deepcopy(rows)
                    if mode=='empty_facts': changed[e.fact]=[]
                    elif mode=='empty_parents': changed[e.parent]=[]
                    elif mode=='shuffle':
                        for values in changed.values():random.Random(7).shuffle(values)
                    elif mode=='null_fields':
                        for r in changed[e.fact]:
                            for c in (e.start,e.end,e.debit,e.credit):
                                if c:r[c]=None
                    else:
                        for r in changed[e.fact][::2]:r[e.links[-1]]=-999999
                    assert_oracle(task,mart,e,changed)

    def test_random_intervals_bursts_and_cycles_against_independent_algorithms(self):
        for recipe in (INTERVAL,SESSION,HIERARCHY):
            task=apply_scenario_difficulty(scenario_source(composite=True),recipe=recipe)
            mart=task.marts[-1]
            e=bound_scenario(task,mart)
            baseline={k:list(v) for k,v in task.population(PopulationName.COUNTERFACTUAL).literal_rows.items()}
            for seed in range(12):
                rng=random.Random(seed)
                rows=copy.deepcopy(baseline)
                rows[e.fact]=[]
                parents=rows[e.parent]
                sample=baseline[e.fact][0]
                for i in range(45):
                    row=copy.deepcopy(sample)
                    row[e.row_keys[-1]]=200_000+i
                    owner=rng.choice(parents)
                    row.update({c:owner[k] for c,k in zip(e.links,e.keys)})
                    if recipe==HIERARCHY:
                        target=rng.choice(parents)
                        row.update({c:target[k] for c,k in zip(e.targets,e.keys)})
                    else:
                        moment=datetime(2024,1,10)+timedelta(seconds=rng.randrange(-10_000,120_000),microseconds=rng.randrange(1000000))
                        row[e.start]=moment.isoformat()
                        if recipe==INTERVAL:
                            row[e.end]=(moment+timedelta(seconds=rng.randrange(-1000,50000))).isoformat()
                    rows[e.fact].append(row)
                assert_oracle(task,mart,e,rows)

    def test_source_capabilities_and_certificates_fail_closed(self):
        source=scenario_source()
        text_times=source.model_copy(update={'tables':tuple(t.model_copy(update={'columns':tuple(
            c.model_copy(update={'type':ColumnType.TEXT}) if c.type in (ColumnType.TIMESTAMP,ColumnType.DATE) else c
            for c in t.columns)}) for t in source.tables)})
        for recipe in (INTERVAL,SESSION,LIFECYCLE):
            self.assertFalse(scenario_candidates(text_times,recipe))
        self.assertTrue(all(e.parent=='journal_entries' for e in scenario_candidates(source,LEDGER)))
        for recipe in SCENARIO_RECIPES:
            task=apply_scenario_difficulty(source,recipe=recipe)
            mart=task.marts[-1]
            with self.assertRaises(SemanticDifficultyError):
                bound_scenario(task,mart.model_copy(update={'description':'unrelated rules'}))
            with self.assertRaisesRegex(SemanticDifficultyError,'mandatory'):
                apply_scenario_difficulty(task.model_copy(update={'attack_cases':()}),recipe=recipe)
            ops=list(mart.plan.ops)
            ops[-1]=ops[-1].model_copy(update={'details':{**ops[-1].details,'select':'1 AS fabricated'}})
            self.assertNotIn(PATTERNS[recipe],detect_semantic_patterns(mart.plan.model_copy(update={'ops':tuple(ops)})))
        malformed=MartOp(kind=MartOpKind.TRANSITIVE_CLOSURE,description='bad',tables=('x',),columns=('a','b'),
            details={'name':'bad','from_columns':'["a"]','to_columns':'["a"]'})
        self.assertTrue(op_problems(malformed))

    def test_deep_composite_hierarchy_and_cycle_membership(self):
        task=apply_scenario_difficulty(scenario_source(composite=True),recipe=HIERARCHY)
        mart=task.marts[-1]
        e=bound_scenario(task,mart)
        rows=copy.deepcopy(dict(task.population(PopulationName.COUNTERFACTUAL).literal_rows))
        parent,fact=rows[e.parent][0],rows[e.fact][0]
        rows[e.parent]=[{**parent, e.keys[0]:realm,e.keys[1]:i} for realm in (1,2) for i in range(40)]
        rows[e.fact]=[{**fact,e.row_keys[-1]:i, e.links[0]:1,e.links[1]:i,
            e.targets[0]:1,e.targets[1]:i+1} for i in range(39)]
        rows[e.fact].append({**fact,e.row_keys[-1]:39,e.links[0]:1,e.links[1]:39,
            e.targets[0]:1,e.targets[1]:20})
        result=assert_oracle(task,mart,e,rows)
        by_key={(r[e.keys[0]],r[e.keys[1]]):r for r in result}
        self.assertEqual(by_key[1,0]['reachable_count'],39)
        self.assertFalse(by_key[1,0]['is_in_cycle'])
        self.assertTrue(by_key[1,20]['is_in_cycle'])
        self.assertEqual(by_key[2,0]['reachable_count'],0)

    def test_permuted_keys_date_intervals_and_integer_ledgers(self):
        source=scenario_source(composite=True)
        source=source.model_copy(update={'relationships':tuple(r.model_copy(update={
            'child_columns':tuple(reversed(r.child_columns)),
            'parent_columns':tuple(reversed(r.parent_columns))}) for r in source.relationships),
            'tables':tuple(t.model_copy(update={'columns':tuple(c.model_copy(update={
                'type':ColumnType.DATE if c.type is ColumnType.TIMESTAMP else ColumnType.BIGINT})
                if c.type in (ColumnType.TIMESTAMP,ColumnType.DECIMAL) else c for c in t.columns)}) for t in source.tables)})
        for recipe in (INTERVAL,LIFECYCLE,LEDGER):
            with self.subTest(recipe=recipe):
                task=apply_scenario_difficulty(source,recipe=recipe)
                e=bound_scenario(task,task.marts[-1])
                assert_oracle(task,task.marts[-1],e,generate_rows(task,PopulationName.COUNTERFACTUAL))

    def test_batch_matching_retries_executable_refusals(self):
        tasks=tuple(scenario_source().model_copy(update={'task_id':f'proof__fallback_{i}',
            'family_id':f'proof__fallback_{i}'}) for i in range(2))
        def admit(task,*,recipe):
            if recipe==INTERVAL:
                raise SemanticDifficultyError('schema matched but witness did not discriminate')
            return apply_scenario_difficulty(task,recipe=recipe)
        with patch('elt_taskgen.generation.scenario_recipes.apply_scenario_difficulty',side_effect=admit):
            result=assign_recipe_batch(tasks,RecipeSelection(enabled_recipes=(INTERVAL,LEDGER)))
        self.assertEqual([t.marts[-1].plan.template_id for t in result],[LEDGER,LEDGER])

    def test_batch_assignment_covers_rare_capabilities_and_is_order_independent(self):
        tasks=[scenario_source().model_copy(update={'task_id':f'proof__batch_{i}','family_id':f'proof__family_{i}','cluster_id':f'proof__cluster_{i}'}) for i in range(6)]
        selection=RecipeSelection(enabled_recipes=SCENARIO_RECIPES,seed=19)
        result=assign_recipe_batch(tasks,selection)
        self.assertEqual({t.marts[-1].plan.template_id for t in result},set(SCENARIO_RECIPES))
        reversed_result=assign_recipe_batch(reversed(tasks),selection)
        self.assertEqual({t.task_id:t.content_hash() for t in result},{t.task_id:t.content_hash() for t in reversed_result})
        self.assertEqual(assign_recipe_batch(result,selection),result)
        # A source with a rare temporal capability is allocated to that recipe;
        # a source with text dates can still supply the ledger recipe.
        limited=tasks[1].model_copy(update={'tables':tuple(t.model_copy(update={'columns':tuple(
            c.model_copy(update={'type':ColumnType.TEXT}) if c.type is ColumnType.TIMESTAMP else c for c in t.columns)}) for t in tasks[1].tables)})
        pair=assign_recipe_batch((tasks[0],limited),RecipeSelection(enabled_recipes=(INTERVAL,LEDGER)))
        self.assertEqual([t.marts[-1].plan.template_id for t in pair],[INTERVAL,LEDGER])


if __name__=='__main__':unittest.main()

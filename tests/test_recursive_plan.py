"""Compiler contracts and cycle termination for the closed closure operation."""
import unittest

from elt_taskgen.generation.mart_plan import detect_semantic_patterns, op_problems, validate_plan
from elt_taskgen.generation.recursive import closure_sql
from elt_taskgen.models import JoinType, MartOp, MartOpKind, canonical_json
from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection
from elt_taskgen.reference.solution import compile_plan_sql, plan_compilation_problems, PlanCompilationError


class RecursivePlanTests(unittest.TestCase):
    def operation(self, relation):
        return MartOp(kind=MartOpKind.TRANSITIVE_CLOSURE, description='Reachable pairs',
            tables=(relation,), columns=('a','b'), details={'name':'reachable',
                'from_columns':canonical_json(['a']), 'to_columns':canonical_json(['b'])})

    def test_internal_ctes_do_not_shadow_source_names(self):
        for name in ('edges','closure_edges','closure_pairs','CLOSURE_EDGES'):
            with self.subTest(name=name), sandboxed_memory_connection() as con:
                con.execute(f'CREATE TABLE "{name}" (a INTEGER,b INTEGER)')
                con.execute(f'INSERT INTO "{name}" VALUES (1,2),(2,1),(2,3),(1,2),(NULL,1)')
                result=set(con.execute(closure_sql(self.operation(name), f'"{name}"')).fetchall())
                self.assertEqual(result,{(1,1),(1,2),(1,3),(2,1),(2,2),(2,3)})

    def test_malformed_closure_is_refused_by_both_validator_and_compiler(self):
        from tests.scenario_fixtures import scenario_source
        from elt_taskgen.generation.scenario_evidence import HIERARCHY, scenario_candidates
        from elt_taskgen.generation.scenario_plan import scenario_plan
        task=scenario_source()
        mart=scenario_plan(task,scenario_candidates(task,HIERARCHY)[0],'hierarchy',HIERARCHY)
        position=next(i for i,o in enumerate(mart.plan.ops) if o.kind is MartOpKind.TRANSITIVE_CLOSURE)
        original=mart.plan.ops[position]
        variants=[original.model_copy(update={field:value}) for field,value in (
            ('tables',()),('columns',()),('predicate','a=b'),('join_type',JoinType.INNER))]
        variants.extend(original.model_copy(update={'details':{k:v for k,v in original.details.items() if k!=omit}})
            for omit in original.details)
        variants.append(original.model_copy(update={'details':{**original.details,'from_columns':'not json'}}))
        variants.append(original.model_copy(update={'details':{**original.details,'to_columns':original.details['from_columns']}}))
        self.assertFalse(plan_compilation_problems(task,mart))
        for op in variants:
            with self.subTest(op=op):
                self.assertTrue(op_problems(op))
                ops=tuple(op if i==position else other for i,other in enumerate(mart.plan.ops))
                changed=mart.model_copy(update={'plan':mart.plan.model_copy(update={'ops':ops})})
                changed_task=task.model_copy(update={'marts':(changed,)})
                self.assertTrue(validate_plan(changed_task,changed.plan))
                with self.assertRaises(PlanCompilationError):
                    compile_plan_sql(changed_task,changed)

    def test_pattern_detectors_refuse_missing_dependency_relations(self):
        from tests.test_recipe_registry import recipe_source
        from elt_taskgen.generation.recipe_registry import RECIPE_REGISTRY, RecipeSelection, apply_semantic_recipes
        for recipe in RECIPE_REGISTRY:
            task=apply_semantic_recipes(recipe_source(recipe),RecipeSelection(enabled_recipes=(recipe,)))
            plan=task.marts[-1].plan
            for index,op in enumerate(plan.ops):
                changed=plan.model_copy(update={'ops':tuple(o.model_copy(update={'tables':()}) if i==index else o
                    for i,o in enumerate(plan.ops))})
                with self.subTest(recipe=recipe,operation=op.kind):
                    # A malformed operation cannot crash validation before the
                    # compiler can report the missing relation.
                    detect_semantic_patterns(changed)


if __name__=='__main__':
    unittest.main()

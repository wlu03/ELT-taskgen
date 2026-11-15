import copy
import random
import unittest

from elt_taskgen.generation.advanced_evidence import ADVANCED_RECIPES, ASOF, COHORT, ALLOCATION, advanced_candidates
from elt_taskgen.generation.advanced_plan import advanced_plan, PATTERNS
from elt_taskgen.generation.advanced_recipes import apply_advanced_difficulty, bound_advanced
from elt_taskgen.generation.mart_plan import detect_semantic_patterns
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
from elt_taskgen.generation.source_data import generate_rows
from elt_taskgen.models import ColumnType, PopulationName
from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection
from elt_taskgen.reference.solution import create_table, _insert_rows, compile_plan_sql, execute_mart
from elt_taskgen.verification.attacks import materialize_mutation, _validate_realized_fidelity
from elt_taskgen.verification.upstream_eval import compare_mart, rows_to_canonical_csv
from tests.advanced_fixtures import advanced_source
from tests.advanced_oracle import oracle


def query(task, mart, rows, sql=None):
    with sandboxed_memory_connection(memory_limit_mb=512, threads=1, disable_temp_spill=True) as con:
        for table in task.tables:
            create_table(con, table)
            _insert_rows(con, table, rows[table.name])
        return execute_mart(con, mart, sql or compile_plan_sql(task, mart))


def assert_oracle(task, mart, e, rows):
    actual, expected = query(task, mart, rows), oracle(e, mart.plan.template_id, rows)
    key = lambda row: tuple(row[k] for k in mart.key_columns)
    assert len(actual) == len(expected), (len(actual), len(expected))
    for want, got in zip(sorted(expected, key=key), sorted(actual, key=key), strict=True):
        for c in mart.columns:
            if c.type is ColumnType.FLOAT:
                assert abs(want[c.name] - got[c.name]) < 0.00011, (c.name, want, got)
            elif c.type in (ColumnType.DATE, ColumnType.TIMESTAMP):
                assert str(want[c.name]) == str(got[c.name]), (c.name, want, got)
            else:
                assert want[c.name] == got[c.name], (c.name, want, got)
    return actual


class AdvancedRecipeTests(unittest.TestCase):
    def test_date_and_timestamp_boundaries_are_interchangeable(self):
        for event_type, history_type in ((ColumnType.DATE, ColumnType.DATE),
                                         (ColumnType.DATE, ColumnType.TIMESTAMP),
                                         (ColumnType.TIMESTAMP, ColumnType.DATE)):
            source = advanced_source()
            source = source.model_copy(update={"tables": tuple(t.model_copy(update={"columns": tuple(
                c.model_copy(update={"type": event_type}) if c.name == "ordered_at" else
                c.model_copy(update={"type": history_type}) if c.name == "valid_from" else c
                for c in t.columns)}) for t in source.tables)})
            task = apply_advanced_difficulty(source, recipe=ASOF)
            mart = task.marts[-1]
            assert_oracle(task, mart, bound_advanced(task, mart), generate_rows(task, PopulationName.COUNTERFACTUAL))

    def test_null_history_attributes_and_orphan_keys_follow_the_contract(self):
        for recipe in ADVANCED_RECIPES:
            task = apply_advanced_difficulty(advanced_source(), recipe=recipe)
            mart, e = task.marts[-1], bound_advanced(task, task.marts[-1])
            rows = copy.deepcopy(generate_rows(task, PopulationName.COUNTERFACTUAL))
            # An orphan is tested with an optional relationship, not asserted
            # to be a valid instance of the original required FK contract.
            task = task.model_copy(update={"relationships": tuple(r.model_copy(update={"required": False})
                if r.child_table == e.fact and r.child_columns == e.links else r for r in task.relationships)})
            rows[e.fact][0][e.links[0]] = 99999999
            if recipe == ASOF:
                rows[e.history][2][e.attribute] = None  # winning version remains NULL, not the previous value
                rows[e.history][0][e.valid_from] = None
            assert_oracle(task, mart, e, rows)

    def test_all_populations_keys_mutations_and_row_order(self):
        for composite, text_keys in ((False, False), (True, False), (True, True)):
            for recipe in ADVANCED_RECIPES:
                with self.subTest(composite=composite, text_keys=text_keys, recipe=recipe):
                    original = advanced_source(composite=composite, text_keys=text_keys)
                    task = apply_advanced_difficulty(original, recipe=recipe)
                    mart = task.marts[-1]
                    e = bound_advanced(task, mart)
                    self.assertEqual(apply_advanced_difficulty(task, recipe=recipe), task)
                    self.assertEqual(task.marts[:-1], original.marts)
                    for population in PopulationName:
                        rows = generate_rows(task, population)
                        assert_oracle(task, mart, e, rows)
                        if population is not PopulationName.COUNTERFACTUAL:
                            self.assertEqual(task.population(population), original.population(population))
                    rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
                    gold = rows_to_canonical_csv(oracle(e, recipe, rows), tuple(c.name for c in mart.columns))
                    for case in task.attack_cases:
                        sql = materialize_mutation(task, case, object())
                        _validate_realized_fidelity(task, case, sql, None)
                        self.assertFalse(compare_mart(gold, query(task, mart, rows, sql[mart.name]), mart), case.name)
                        for old in original.marts:
                            self.assertEqual(sql[old.name], compile_plan_sql(task, old))
                    for mode in ("shuffle", "empty_facts", "empty_parents", "all_nulls"):
                        changed = copy.deepcopy(rows)
                        if mode == "shuffle":
                            rng = random.Random(42)
                            for values in changed.values():
                                rng.shuffle(values)
                        elif mode == "empty_facts":
                            changed[e.fact] = []
                        elif mode == "empty_parents":
                            changed[e.parent] = []
                        else:
                            column = e.event_time or e.weight
                            for row in changed[e.fact]:
                                row[column] = None
                        assert_oracle(task, mart, e, changed)

    def test_binding_attacks_and_structural_certificates_fail_closed(self):
        original = advanced_source()
        for recipe in ADVANCED_RECIPES:
            task = apply_advanced_difficulty(original, recipe=recipe)
            mart, e = task.marts[-1], bound_advanced(task, task.marts[-1])
            broken = mart.model_copy(update={"description": "An unrelated contract."})
            with self.assertRaises(SemanticDifficultyError):
                bound_advanced(task, broken)
            with self.assertRaisesRegex(SemanticDifficultyError, "mandatory"):
                apply_advanced_difficulty(task.model_copy(update={"attack_cases": ()}), recipe=recipe)
            for i, op in enumerate(mart.plan.ops):
                if op.predicate:
                    ops = list(mart.plan.ops)
                    ops[i] = op.model_copy(update={"predicate": "TRUE"})
                    self.assertNotIn(PATTERNS[recipe], detect_semantic_patterns(mart.plan.model_copy(update={"ops": tuple(ops)})))
                    break

    def test_unsupported_dates_history_and_weights_are_not_invented(self):
        original = advanced_source()
        text_dates = original.model_copy(update={"tables": tuple(t.model_copy(update={"columns": tuple(
            c.model_copy(update={"type": ColumnType.TEXT}) if c.type in (ColumnType.TIMESTAMP, ColumnType.DATE) else c
            for c in t.columns)}) for t in original.tables)})
        self.assertFalse(advanced_candidates(text_dates, ASOF))
        self.assertFalse(advanced_candidates(text_dates, COHORT))
        unique_entity_history = original.model_copy(update={"tables": tuple(t.model_copy(update={"business_key": ("customer_id",)})
            if t.name == "customer_history" else t for t in original.tables)})
        self.assertFalse(advanced_candidates(unique_entity_history, ASOF))
        only_updates = original.model_copy(update={"tables": tuple(t.model_copy(update={"columns": tuple(
            c.model_copy(update={"name": "updated_at"}) if c.name == "created_at" else c for c in t.columns)})
            for t in original.tables)})
        self.assertFalse(advanced_candidates(only_updates, COHORT))
        no_weight = original.model_copy(update={"tables": tuple(t.model_copy(update={"columns": tuple(
            c.model_copy(update={"name": "rank"}) if c.name == "quantity" else c for c in t.columns)}) for t in original.tables)})
        self.assertFalse(advanced_candidates(no_weight, ALLOCATION))

"""Shared admission and mutation execution for schema-bound recipe families.

Each family supplies its semantic builders and binding validator. Admission
preserves their population wording and explicitly declared retry policy.
"""
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError, _merged_counterfactual
from elt_taskgen.models import AttackCase, AttackKind, MartSpec, PopulationName, Row, TaskIR, canonical_json

Evidence = TypeVar("Evidence")
WitnessRows = Mapping[str, Sequence[Row]]


@dataclass(frozen=True)
class RecipeFamily(Generic[Evidence]):
    """Concrete recipe functions sharing one executable admission contract."""

    name: str
    recipes: tuple[str, ...]
    candidates: Callable[[TaskIR, str], tuple[Evidence, ...]]
    plan: Callable[..., MartSpec]
    witness_rows: Callable[[TaskIR, Evidence, str], WitnessRows]
    variants: Callable[[Evidence, str], tuple[str, ...]]
    bind: Callable[[TaskIR, MartSpec], Evidence]
    witness_description: str = "executable witnesses"
    retryable_errors: tuple[type[Exception], ...] = (SemanticDifficultyError,)

    def cases(self, evidence: Evidence, mart: str, recipe: str) -> tuple[AttackCase, ...]:
        return tuple(AttackCase(
            name=f"{mart}__{variant}", kind=AttackKind.CUSTOM,
            description=f"Violate {variant.replace('_', ' ')} in {mart}.",
            mutation="directive:structured:" + canonical_json(
                {"operation": recipe, "mart": mart, "variant": variant}),
            expected_pass={PopulationName.COUNTERFACTUAL: False},
        ) for variant in self.variants(evidence, recipe))

    def materialize_mutation(self, task: TaskIR, payload: dict) -> dict[str, str]:
        from elt_taskgen.reference.solution import build_reference, compile_plan_sql

        if set(payload) != {"operation", "mart", "variant"} or payload["operation"] not in self.recipes:
            raise SemanticDifficultyError(f"Invalid {self.name} mutation")
        mart = task.mart(payload["mart"])
        if payload["operation"] != mart.plan.template_id:
            raise SemanticDifficultyError(f"{self.name.capitalize()} mutation targets a different recipe")
        evidence = self.bind(task, mart)
        result = dict(build_reference(task).sql_by_mart)
        mutant = self.plan(task, evidence, mart.name, mart.plan.template_id, payload["variant"])
        result[mart.name] = compile_plan_sql(task, mutant)
        return result

    def certify(self, task: TaskIR, mart: MartSpec, evidence: Evidence, rows: WitnessRows) -> tuple[str, ...]:
        from elt_taskgen.generation.mart_plan import validate_plan
        from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection
        from elt_taskgen.reference.solution import create_table, _insert_rows, compile_plan_sql, execute_mart
        from elt_taskgen.verification.upstream_eval import compare_mart, rows_to_canonical_csv

        problems = validate_plan(task, mart.plan)
        if problems:
            raise SemanticDifficultyError("; ".join(problems))
        variants = self.variants(evidence, mart.plan.template_id)
        with sandboxed_memory_connection(memory_limit_mb=512, threads=1, disable_temp_spill=True) as con:
            for table in task.tables:
                create_table(con, table)
                _insert_rows(con, table, rows.get(table.name, ()))
            correct = execute_mart(con, mart, compile_plan_sql(task, mart))
            gold = rows_to_canonical_csv(correct, tuple(c.name for c in mart.columns))
            for variant in variants:
                mutant = self.plan(task, evidence, mart.name, mart.plan.template_id, variant)
                wrong = execute_mart(con, mutant, compile_plan_sql(task, mutant))
                if compare_mart(gold, wrong, mart):
                    raise SemanticDifficultyError(f"No executable witness for {variant}")
        return variants

    def apply(self, task: TaskIR, recipes: tuple[str, ...]) -> TaskIR:
        """Admit the first witnessed candidate in the caller's stable order.

        Reuse requires revalidating binding, mandatory mutations, and witnesses.
        Only candidate construction retries; a corrupt existing binding or a
        reference-attachment failure propagates to the caller.
        """
        from elt_taskgen.generation.populations import _literal_row_problems
        from elt_taskgen.reference.solution import attach_reference

        if not recipes:
            raise ValueError("At least one recipe is required")
        for recipe in recipes:
            if recipe not in self.recipes:
                raise ValueError(recipe)
        existing = [m for m in task.marts if m.plan.template_id in self.recipes]
        if existing:
            for mart in existing:
                if mart.plan.template_id not in recipes:
                    raise SemanticDifficultyError(f"Task already contains a different {self.name} recipe")
                evidence = self.bind(task, mart)
                if any(c not in task.attack_cases for c in self.cases(evidence, mart.name, mart.plan.template_id)):
                    raise SemanticDifficultyError(f"{self.name.capitalize()} recipe is missing mandatory mutations")
                self.certify(task, mart, evidence, task.population(PopulationName.COUNTERFACTUAL).literal_rows)
            return task

        refusals = []
        for recipe in recipes:
            candidates = self.candidates(task, recipe)
            if not candidates:
                refusals.append(f"{recipe}: no typed schema evidence")
            for evidence in candidates[:32]:
                try:
                    rows = _merged_counterfactual(task, self.witness_rows(task, evidence, recipe))
                    name = recipe.removesuffix("_v1")
                    occupied = {m.name for m in task.marts} | {t.name for t in task.tables}
                    while name in occupied:
                        name += "_"
                    mart = self.plan(task, evidence, name, recipe)
                    populations = tuple(p.model_copy(update={
                        "literal_rows": rows,
                        "scale": {n: max(1, len(v)) for n, v in rows.items()},
                        "conditions": (*p.conditions, f"{recipe}: {self.witness_description} for {name}"),
                    }) if p.name is PopulationName.COUNTERFACTUAL else p for p in task.populations)
                    changed = TaskIR.model_validate_json(task.model_copy(update={
                        "marts": (*task.marts, mart), "populations": populations,
                        "attack_cases": (*task.attack_cases, *self.cases(evidence, name, recipe)),
                        "reference": None,
                    }).model_dump_json())
                    problems = _literal_row_problems(changed, changed.population(PopulationName.COUNTERFACTUAL))
                    if problems:
                        raise SemanticDifficultyError("; ".join(problems))
                    self.certify(changed, mart, evidence, rows)
                except self.retryable_errors as exc:
                    refusals.append(f"{recipe}: {exc}")
                    continue
                return attach_reference(changed)
        raise SemanticDifficultyError(f"No witnessed {self.name} recipe: " + " | ".join(refusals[:8]))

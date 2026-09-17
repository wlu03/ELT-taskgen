"""Derive effective source lineage and remove unused source tables.

Compiled queries are authoritative; ``source`` plan hints alone do not prove
lineage.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from elt_taskgen.models import (
    AttackKind,
    MartOpKind,
    PopulationName,
    PopulationSpec,
    TaskIR,
    canonical_json,
)
from elt_taskgen.reference.solution import compile_plan_sql


class EffectiveLineageError(ValueError):
    """The task's executable transformation lineage cannot be established."""


@dataclass(frozen=True)
class EffectiveLineage:
    """Declared source tables read by each mart's executable SQL."""

    by_mart: dict[str, tuple[str, ...]]
    used_tables: tuple[str, ...]
    unused_tables: tuple[str, ...]


def effective_lineage(task: TaskIR) -> EffectiveLineage:
    """Return declared source tables referenced by each compiled mart query.

    Reject marts with no executable source.
    """

    declared = {table.name for table in task.tables}
    by_mart: dict[str, tuple[str, ...]] = {}
    used: set[str] = set()
    reference_sql = task.reference.sql_by_mart if task.reference is not None else {}

    for mart in task.marts:
        sql = reference_sql.get(mart.name)
        if not sql:
            sql = compile_plan_sql(task, mart)
        try:
            tree = parse_one(sql, read="duckdb")
        except (ParseError, ValueError) as exc:
            raise EffectiveLineageError(
                f"mart {mart.name!r}: compiled SQL cannot be parsed for lineage"
            ) from exc
        names = tuple(
            sorted({node.name for node in tree.find_all(exp.Table)} & declared)
        )
        if not names:
            raise EffectiveLineageError(
                f"mart {mart.name!r}: compiled transformation reads no declared "
                "source table"
            )
        by_mart[mart.name] = names
        used.update(names)

    return EffectiveLineage(
        by_mart=by_mart,
        used_tables=tuple(sorted(used)),
        unused_tables=tuple(sorted(declared - used)),
    )


def _compact_population(population: PopulationSpec, used: set[str]) -> PopulationSpec:
    return population.model_copy(
        update={
            "scale": {
                table: count
                for table, count in population.scale.items()
                if table in used
            },
            "literal_rows": {
                table: rows
                for table, rows in population.literal_rows.items()
                if table in used
            },
        }
    )


_DUPLICATE_CONDITION_PREFIXES = (
    "Exact-duplicate rows are injected",
    "The marts deduplicate",
    "No mart deduplicates",
    "Every table declares a primary key, so no exact-duplicate rows are injected",
    "Every real row, plus exact duplicates",
)


def _condition_survives_compaction(condition: str, removed: set[str]) -> bool:
    """Keep conditions unless a recognized structural subject was removed."""

    for table in removed:
        if condition.startswith(f"COVERAGE SHORTFALL: {table} "):
            return False
        if condition.startswith(f"{table} row "):
            return False
        if f"DANGLING LINK: {table}." in condition:
            return False
        if f" references {table}." in condition:
            return False
        if f"Anchor rows live in {table};" in condition:
            return False
        if f"linked rows live in {table}." in condition:
            return False
        if any(
            token in condition
            for token in (
                f"anchor={table};",
                f"bridge={table};",
                f"child={table}]",
            )
        ):
            return False
    return True


def _literal_duplicate_tables(task: TaskIR) -> set[str] | None:
    """Tables whose stress literal rows add copies over primary, if literal."""

    by_name = {population.name: population for population in task.populations}
    primary = by_name.get(PopulationName.PRIMARY)
    stress = by_name.get(PopulationName.STRESS)
    if primary is None or stress is None or not stress.literal_rows:
        return None
    duplicated: set[str] = set()
    for table in task.tables:
        base = Counter(
            canonical_json(row)
            for row in primary.literal_rows.get(table.name, ())
        )
        stressed = Counter(
            canonical_json(row)
            for row in stress.literal_rows.get(table.name, ())
        )
        if any(count > base.get(row, 0) for row, count in stressed.items()):
            duplicated.add(table.name)
    return duplicated


def _refresh_population_conditions(task: TaskIR, removed: set[str]) -> TaskIR:
    """Make private population claims match the compacted executable task."""

    from elt_taskgen.generation.populations import (  # noqa: PLC0415
        stress_duplicate_conditions,
    )

    read_tables = set(effective_lineage(task).used_tables)
    deduped_tables = {
        table
        for mart in task.marts
        for op in mart.plan.ops
        if op.kind is MartOpKind.DEDUPE
        for table in op.tables
        if table in read_tables
    }
    duplicate_conditions = stress_duplicate_conditions(
        task.tables,
        deduped_tables=deduped_tables,
        read_tables=read_tables,
        duplicated_tables=_literal_duplicate_tables(task),
    )
    populations: list[PopulationSpec] = []
    for population in task.populations:
        conditions = tuple(
            condition
            for condition in population.conditions
            if _condition_survives_compaction(condition, removed)
            and not (
                population.name is PopulationName.STRESS
                and condition.startswith(_DUPLICATE_CONDITION_PREFIXES)
            )
        )
        if population.name is PopulationName.STRESS:
            conditions += duplicate_conditions
        populations.append(population.model_copy(update={"conditions": conditions}))
    return task.model_copy(update={"populations": tuple(populations)})


_EXTRACT_LOAD_ATTACK_KINDS = frozenset(
    {
        AttackKind.PARTIAL_BACKEND,
        AttackKind.DUPLICATE_ON_LOAD,
        AttackKind.TRUNCATE_TABLE,
        AttackKind.WRONG_SOURCE_FILE,
        AttackKind.STALE_SNAPSHOT,
        AttackKind.NULL_ROW_DROP,
        AttackKind.HEADER_AS_ROW,
        AttackKind.FABRICATE_COUNTS,
        AttackKind.SKIP_EXTRACTION,
    }
)


def _refresh_extract_load_attacks(task: TaskIR) -> TaskIR:
    """Rebuild source-dependent EL probes after compaction."""

    # Local import avoids making the population builder depend on lineage.
    from elt_taskgen.generation.populations import (  # noqa: PLC0415
        POLICY_CONSTRUCTED,
        POLICY_PROVIDED_ROWS,
        _skip_extraction_case,
        el_attack_cases,
    )

    policy = (
        POLICY_PROVIDED_ROWS
        if any(
            "provided-rows" in condition
            for population in task.populations
            for condition in population.conditions
        )
        else POLICY_CONSTRUCTED
    )
    backend_count = len({assignment.backend for assignment in task.backends})
    transform_cases = tuple(
        case
        for case in task.attack_cases
        if case.kind not in _EXTRACT_LOAD_ATTACK_KINDS
    )
    refreshed = list(transform_cases)
    if backend_count > 1:
        refreshed.append(_skip_extraction_case(backend_count, policy))
    refreshed.extend(
        el_attack_cases(
            task.tables,
            backend_assignments=task.backends,
            backends=backend_count,
            policy=policy,
            populations=task.populations,
        )
    )
    return task.model_copy(update={"attack_cases": tuple(refreshed)})


def prune_to_effective_lineage(
    task: TaskIR, *, minimum_source_tables: int = 1
) -> TaskIR:
    """Remove unused tables and stale ``SOURCE`` hints before task registration."""

    if minimum_source_tables < 1:
        raise ValueError("minimum_source_tables must be at least 1")
    lineage = effective_lineage(task)
    used = set(lineage.used_tables)
    if len(used) < minimum_source_tables:
        raise EffectiveLineageError(
            f"task has only {len(used)} effective source table(s), below required "
            f"minimum {minimum_source_tables}"
        )
    if not lineage.unused_tables:
        return task

    marts = []
    for mart in task.marts:
        ops = []
        for op in mart.plan.ops:
            if op.kind is not MartOpKind.SOURCE:
                ops.append(op)
                continue
            kept = tuple(table for table in op.tables if table in used)
            if not kept:
                continue
            if kept == op.tables:
                ops.append(op)
                continue
            ops.append(
                op.model_copy(
                    update={
                        "tables": kept,
                        "description": "Read source table"
                        + ("s " if len(kept) > 1 else " ")
                        + ", ".join(kept)
                        + ".",
                    }
                )
            )
        if not ops:
            raise EffectiveLineageError(
                f"mart {mart.name!r}: compaction removed every plan operation"
            )
        marts.append(
            mart.model_copy(update={"plan": mart.plan.model_copy(update={"ops": tuple(ops)})})
        )

    compacted = task.model_copy(
        update={
            "tables": tuple(table for table in task.tables if table.name in used),
            "relationships": tuple(
                rel
                for rel in task.relationships
                if rel.child_table in used and rel.parent_table in used
            ),
            "backends": tuple(
                assignment for assignment in task.backends if assignment.table in used
            ),
            "marts": tuple(marts),
            "populations": tuple(
                _compact_population(population, used)
                for population in task.populations
            ),
        }
    )
    compacted = _refresh_population_conditions(compacted, set(lineage.unused_tables))
    compacted = _refresh_extract_load_attacks(compacted)

    # Recompile after compaction.  This catches any plan whose apparently dead
    # SOURCE op was actually required through an implicit compiler dependency.
    after = effective_lineage(compacted)
    if set(after.used_tables) != used or after.unused_tables:
        raise EffectiveLineageError(
            "effective lineage changed while compacting the task; refusing an "
            "unstable source boundary"
        )
    return compacted


__all__ = [
    "EffectiveLineage",
    "EffectiveLineageError",
    "effective_lineage",
    "prune_to_effective_lineage",
]

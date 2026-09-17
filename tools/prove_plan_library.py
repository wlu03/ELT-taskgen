"""Execute every plan-library shape and its named wrong implementation.

WHY THIS EXISTS
A shape whose wrong implementation scores 1.0 on every population is width
without discrimination. This script is the EXECUTED proof that each shape's
counterfactual witness rows actually separate the reference from the mutant:
it builds a task per shape, compiles the reference through the ONE compiler
(`reference.solution.compile_plan_sql`), mutates it through the ONE mutation
engine (`verification.attacks._apply_kind`), executes both on every population
and scores both with the ONE reward (`verification.upstream_eval.compare_mart`).

Run: .venv/bin/python tools/prove_plan_library.py
"""

from __future__ import annotations

import json
import sys

import duckdb

from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.generation import populations as pops
from elt_taskgen.generation.source_data import generate_rows
from elt_taskgen.models import (
    AttackKind,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartSpec,
    Origin,
    PopulationName,
    TableSpec,
    Relationship,
    TaskIR,
)
from elt_taskgen.reference import solution as ref
from elt_taskgen.verification import attacks, upstream_eval

DOMAIN = ("active", "trial", "churned", "archived")
SUB_STATUS = ("paid", "pending", "failed")

TABLES = (
    TableSpec(
        name="accounts",
        description="Customer accounts.",
        columns=(
            ColumnSpec(name="account_id", type=ColumnType.BIGINT),
            ColumnSpec(name="account_name", type=ColumnType.TEXT),
            ColumnSpec(name="account_status", type=ColumnType.TEXT, enum_values=DOMAIN),
        ),
        primary_key=("account_id",),
    ),
    TableSpec(
        name="subscriptions",
        description="One row per subscription of an account to a plan.",
        columns=(
            ColumnSpec(name="subscription_id", type=ColumnType.BIGINT),
            ColumnSpec(name="account_id", type=ColumnType.BIGINT, nullable=True),
            ColumnSpec(name="plan_id", type=ColumnType.BIGINT, nullable=True),
            ColumnSpec(name="sub_status", type=ColumnType.TEXT, enum_values=SUB_STATUS),
            ColumnSpec(name="sub_label", type=ColumnType.TEXT),
            ColumnSpec(name="amount", type=ColumnType.INTEGER),
            ColumnSpec(name="started_at", type=ColumnType.DATE),
        ),
        primary_key=("subscription_id",),
    ),
    TableSpec(
        name="plans",
        description="Billing plans.",
        columns=(
            ColumnSpec(name="plan_id", type=ColumnType.BIGINT),
            ColumnSpec(name="plan_name", type=ColumnType.TEXT),
        ),
        primary_key=("plan_id",),
    ),
)

RELATIONSHIPS = (
    Relationship(
        child_table="subscriptions",
        child_columns=("account_id",),
        parent_table="accounts",
        parent_columns=("account_id",),
        required=False,
    ),
    Relationship(
        child_table="subscriptions",
        child_columns=("plan_id",),
        parent_table="plans",
        parent_columns=("plan_id",),
        required=False,
    ),
)

BACKENDS = (
    BackendAssignment(table="accounts", backend=Backend.POSTGRES),
    BackendAssignment(table="subscriptions", backend=Backend.POSTGRES),
    BackendAssignment(table="plans", backend=Backend.FILES),
)

EVIDENCE = mp.ChainEvidence(
    parent="accounts",
    parent_key="account_id",
    parent_attr="account_name",
    parent_domain_column="account_status",
    domain=DOMAIN,
    out_of_domain="archived",
    bridge="subscriptions",
    bridge_key="subscription_id",
    bridge_key_is_unique=True,
    bridge_parent_fk="account_id",
    bridge_child_fk="plan_id",
    bridge_status="sub_status",
    bridge_status_pass=("paid",),
    bridge_status_fail=("pending", "failed"),
    bridge_amount="amount",
    bridge_label="sub_label",
    bridge_timestamp="started_at",
    child="plans",
    child_key="plan_id",
    child_label="plan_name",
    child_link_optional=True,
    owner_link_optional=True,
)

SCALE = {"accounts": 12, "subscriptions": 40, "plans": 4}


def build_task(built: mp.BuiltPlan, task_id: str) -> TaskIR:
    mart = MartSpec(
        name=built.plan.mart,
        description=f"{built.shape.shape_name} mart.",
        grain=built.plan.ops[len(built.shape.key_columns)].description or "one row per key",
        key_columns=built.shape.key_columns,
        columns=built.columns,
        plan=built.plan,
    )
    populations, cases = pops.derive_populations_and_attacks(
        task_id=task_id,
        tables=TABLES,
        relationships=RELATIONSHIPS,
        shapes=(built.shape,),
        scale_hint=SCALE,
        backends=2,
    )
    return TaskIR(
        task_id=task_id,
        family_id=f"proof__{built.shape.shape_name}",
        cluster_id=task_id,
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        tables=TABLES,
        relationships=RELATIONSHIPS,
        backends=BACKENDS,
        marts=(mart,),
        populations=populations,
        attack_cases=cases,
    )


def load(con: duckdb.DuckDBPyConnection, task: TaskIR, rows: dict) -> None:
    for table in task.tables:
        con.execute(f'DROP TABLE IF EXISTS "{table.name}"')
        ref.create_table(con, table)
        payload = rows.get(table.name) or []
        if payload:
            ref._insert_rows(con, table, payload)


def fetch(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> int:
    report: dict[str, dict] = {}
    shapes = mp.registered_shape_builders()
    ok = True
    for name, builder in shapes:
        built = builder(EVIDENCE, mart=f"{name}_mart")
        task_id = f"proof__{name}"
        task = build_task(built, task_id)
        problems = mp.validate_plan(task, built.plan)
        mart = task.marts[0]
        sql = ref.compile_plan_sql(task, mart)
        cols = tuple(c.name for c in mart.columns)

        entry: dict = {
            "shape": name,
            "template_id": built.plan.template_id or None,
            "semantic_patterns": [
                pattern.value for pattern in built.plan.semantic_patterns
            ],
            "detected_semantic_patterns": [
                pattern.value for pattern in mp.detect_semantic_patterns(built.plan)
            ],
            "semantic_pattern_problems": mp.semantic_pattern_problems(built.plan),
            "target_columns": len(mart.columns),
            "computed_columns": built.computed_count,
            "passthrough_columns": built.passthrough_count,
            "computed_ratio": round(built.computed_count / len(mart.columns), 3),
            "op_kinds": [op.kind.value for op in built.plan.ops],
            "template_signature": mp.plan_template_signature(built.plan),
            "witnesses": list(built.shape.witnesses),
            "validate_plan_problems": problems,
            "attack_cases": [c.name for c in task.attack_cases],
            "rewards": {},
        }

        skip = {
            AttackKind.CONSTANTS, AttackKind.KEYS_ONLY, AttackKind.NO_OP,
            AttackKind.SKIP_EXTRACTION, AttackKind.CUSTOM,
        }
        pairs: list[tuple[AttackKind, str]] = []
        for kind, variants in attacks.KIND_VARIANTS.items():
            if kind in skip and kind is not AttackKind.CUSTOM:
                continue
            for variant in sorted(variants):
                pairs.append((kind, variant))
        pairs.sort(key=lambda p: (p[0].value, p[1]))
        mutants: dict[str, str | None] = {}
        for kind, variant in pairs:
            label = kind.value + (f"@{variant}" if variant else "")
            try:
                mutants[label] = attacks._apply_kind(kind, sql, mart, variant)
            except ValueError as e:
                mutants[label] = None
                entry.setdefault("mutation_errors", {})[label] = str(e)

        for pop in PopulationName:
            rows = generate_rows(task, pop)
            con = duckdb.connect(":memory:")
            try:
                load(con, task, rows)
                gold_rows = fetch(con, sql)
                gold_csv = upstream_eval.rows_to_canonical_csv(gold_rows, cols)
                per_pop: dict[str, object] = {
                    "gold_rows": len(gold_rows),
                    "reference_reward": 1.0
                    if upstream_eval.compare_mart(gold_csv, gold_rows, mart)
                    else 0.0,
                }
                for kind_value, mutant_sql in mutants.items():
                    if mutant_sql is None:
                        per_pop[kind_value] = "NOT_APPLICABLE"
                        continue
                    try:
                        actual = fetch(con, mutant_sql)
                    except duckdb.Error as e:
                        per_pop[kind_value] = f"SQL_ERROR: {e}"
                        continue
                    per_pop[kind_value] = (
                        1.0 if upstream_eval.compare_mart(gold_csv, actual, mart) else 0.0
                    )
                entry["rewards"][pop.value] = per_pop
            finally:
                con.close()

        cf = entry["rewards"][PopulationName.COUNTERFACTUAL.value]
        killers = [k for k, v in cf.items() if v == 0.0 and k != "reference_reward"]
        entry["killed_on_counterfactual"] = sorted(killers)
        entry["claims"] = list(built.shape.attack_claims)
        entry["unsupported_claims"] = sorted(
            set(built.shape.attack_claims) - set(killers)
        )
        entry["budget_problems"] = mp.budget_problems(built)
        entry["column_kind_problems"] = mp.column_kind_problems(mart)
        entry["unclassified_columns"] = mp.unclassified_columns(mart)
        entry["population_problems"] = pops.validate_population_coverage(task)
        entry["mutation_score"] = round(
            len(killers) / max(1, len([v for v in cf.values() if v in (0.0, 1.0)]) - 1), 3
        )
        if (
            problems
            or cf["reference_reward"] != 1.0
            or entry["unsupported_claims"]
            or entry["semantic_pattern_problems"]
            or entry["budget_problems"]
            or entry["column_kind_problems"]
            or entry["unclassified_columns"]
            or entry["population_problems"]
        ):
            ok = False
        report[name] = entry

    print(json.dumps(report, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

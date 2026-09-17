"""Per-variant acceptance batteries (verification/gates.py, Phase B).

WHY THIS EXISTS
The parent battery scores every gate with the FULL reward, so it structurally
cannot certify either exported subtask. These tests assert the three claims
that makes true:

  * an EL battery may NEVER read a transform mutant as evidence — the demo
    task's four required cases are all transform kinds, and its EL
    required-mutants gate must go RED saying so, not green;
  * the T battery removes a MASK rather than relabelling one — with an empty
    gold mart on a graded population, `no_op` collects credit under the T
    reward that it could never collect under the parent reward, and
    degenerate-zero(t) must catch it;
  * a gate that does not apply to a variant is RECORDED as not-applicable with
    its reason and its named replacement, never counted as a pass, and the
    applicable-gate count is on the report.

The fixture deliberately honours `primary.scale == resampled.scale` in ROW
COUNTS (which populations.validate_population_coverage enforces on every real
task and which the older hand-built fixture in test_gates_eval.py does not), so
the EL cardinality gates are exercised against the shape the corpus actually
has: an EL gold that is bit-identical on the memorization pair.

Run `python -m tests.test_variant_gates --report` to print the two active unit
batteries on the demo task, per gate, with the not-applicable reasons.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen import demo_fixture
from elt_taskgen.demo_fixture import (
    COUNTERFACTUAL_LITERAL_ROWS,
    HARDCODE_PRIMARY_DIRECTIVE,
    MART_NAME,
    REFERENCE_SQL,
)
from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    AcceptanceReport,
    AttackCase,
    AttackKind,
    ColumnType,
    GateResult,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    PopulationName,
    Row,
    TaskIR,
    TaskVariant,
)
from elt_taskgen.export import eltbench
from elt_taskgen.reference.gold import gold_digests
from elt_taskgen.verification import el_probes, gates, upstream_eval
from elt_taskgen.verification.upstream_eval import (
    evaluate_variant,
    rows_to_canonical_csv,
    sort_rows,
)
from tests.canonical_doubles import write_canonical_reachability

P = PopulationName
V = TaskVariant
MART_COLS = ("customer_id", "completed_order_count", "total_spend")

#: The task-relative path the T warehouse builder records per population.
WAREHOUSE_REL = "variants/transform/task/warehouse/{pop}.duckdb"

#: Coverage the contamination-clean gate demands (see tests/test_gates_eval.py).
ARMED_COVERAGE = {
    "level": "armed",
    "index_dir": "state/contamination",
    "benchmark_fingerprints": 1453,
    "benchmark_by_kind": {"family": 321, "schema": 200, "schema-table": 932},
    "shape_fingerprints": 200,
    "admitted_tasks": 0,
    "admitted_fingerprints": 0,
    "admitted_by_kind": {},
    "stores": [],
}


class GoldStub(BaseModel):
    """Contract shape of reference.gold.GoldBundle (docs/INTERFACES.md)."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    task_content_hash: str
    stage1: dict[str, dict[str, int]]
    stage2_csv: dict[str, dict[str, str]]
    file_hashes: dict[str, str] = Field(default_factory=dict)


# Resampled matches primary row counts but not values, preventing extract/load
# signal from population memorization.

DEMO_DATA: dict[P, dict[str, list[tuple]]] = {
    P.DEVELOPMENT: {
        "customers": [(1, "C1"), (2, "C2")],
        "orders": [(101, 1, "completed"), (102, 2, "completed")],
        "order_items": [(101, 1, 10.0), (102, 2, 5.0)],
    },
    P.PRIMARY: {
        "customers": [(1, "P1"), (2, "P2"), (3, "P3"), (4, "P4"), (5, "P5")],
        "orders": [
            (201, 1, "completed"), (202, 1, "completed"),
            (203, 3, "cancelled"), (204, 4, "completed"),
            (205, 5, "completed"), (206, None, "completed"),
        ],
        "order_items": [
            (201, 1, 10.0), (202, 2, 5.0), (204, 1, 20.0),
            (204, 3, 1.0), (205, 2, 7.5), (206, 1, 9.0),
        ],
    },
    P.RESAMPLED: {
        "customers": [(11, "R1"), (12, "R2"), (13, "R3"), (14, "R4"), (15, "R5")],
        "orders": [
            (301, 11, "completed"), (302, 11, "completed"),
            (303, 13, "cancelled"), (304, 14, "completed"),
            (305, 15, "completed"), (306, None, "completed"),
        ],
        "order_items": [
            (301, 1, 12.0), (302, 2, 3.0), (304, 1, 4.0),
            (304, 3, 2.0), (305, 2, 6.0), (306, 1, 9.0),
        ],
    },
    P.COUNTERFACTUAL: {
        table: [tuple(row.values()) for row in rows]
        for table, rows in COUNTERFACTUAL_LITERAL_ROWS.items()
    },
    P.STRESS: {
        "customers": [(21, "S1"), (22, "S2")],
        "orders": [(401, 21, "completed"), (401, 21, "completed"), (402, 22, "completed")],
        "order_items": [(401, 1, 10.0), (402, 1, 10.0)],
    },
}


def run_demo_sql(sql: str, data: dict[str, list[tuple]]) -> list[Row]:
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE customers (customer_id INTEGER, customer_name VARCHAR)")
        con.execute("CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, status VARCHAR)")
        con.execute("CREATE TABLE order_items (order_id INTEGER, quantity INTEGER, unit_price DOUBLE)")
        for table, rows in data.items():
            if rows:
                ph = ",".join("?" for _ in rows[0])
                con.executemany(f"INSERT INTO {table} VALUES ({ph})", rows)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def resolve_load_plans(
    task: TaskIR, gold: "GoldStub"
) -> dict[str, "object | str"]:
    """case name -> LoadMutationPlan, or the no-surface REASON string.

    Planning is delegated to the REAL planner
    (verification/attacks.resolve_load_mutation) so the tests exercise the
    production choice logic (largest table, readable swap, differing-vector
    stale source, fabricated hints); only the count ARITHMETIC below is a
    test-local port, exactly as `_fake_evaluate`-style stubs elsewhere port
    the documented reward semantics.
    """
    from elt_taskgen.verification import attacks

    plans: dict[str, object | str] = {}
    for case in task.attack_cases:
        mutation = case.mutation.strip()
        if not mutation.startswith(attacks.LOAD_DIRECTIVE_PREFIX):
            continue
        name, arg = attacks.split_load_directive(
            mutation[len(attacks.LOAD_DIRECTIVE_PREFIX):]
        )
        try:
            plans[case.name] = attacks.resolve_load_mutation(task, name, arg, gold)
        except attacks.InapplicableLoadMutationError as exc:
            # For a REQUIRED case the real runner re-raises; the fixture
            # records the reason instead so tests can hand DELIBERATELY
            # pathological golds (identical count vectors) to the gate under
            # test and assert THAT gate goes red.
            plans[case.name] = str(exc)
    return plans


def simulated_load_stage1(
    task: TaskIR,
    gold: "GoldStub",
    plan: object,
    pop: P,
    data: dict[P, dict[str, list[tuple]]],
) -> dict[str, int]:
    """The stage-1 count vector one resolved load mutation lands for `pop`."""
    from elt_taskgen.verification import attacks

    if plan.fabricated_counts is not None:
        return dict(plan.fabricated_counts[pop])
    source = plan.source_population.get(pop, pop)
    counts = dict(gold.stage1[source.value])
    for table in plan.omit_tables:
        counts.pop(table, None)
    for a, b in plan.swaps:
        counts[a], counts[b] = counts[b], counts[a]
    for table, op in plan.ops.items():
        if op == attacks.OP_DUPLICATE:
            counts[table] *= 2
        elif op == attacks.OP_EMPTY:
            counts[table] = 0
        elif op == attacks.OP_HEADER_AS_ROW:
            counts[table] += 1
        elif op == attacks.OP_TRUNCATE_FIRST_UNIT:
            counts[table] = min(counts[table], attacks._unit_rows(task, table))
        elif op == attacks.OP_DROP_NULL_ROWS:
            counts[table] = sum(
                1 for row in data[source][table] if None not in row
            )
        else:  # pragma: no cover - closed vocabulary
            raise AssertionError(f"unknown load op {op!r}")
    return counts


def build_gold(task: TaskIR, data: dict[P, dict[str, list[tuple]]] | None = None) -> GoldStub:
    data = data or DEMO_DATA
    stage1: dict[str, dict[str, int]] = {}
    stage2: dict[str, dict[str, str]] = {}
    for pop, rows_by_table in data.items():
        stage1[pop.value] = {t: len(rows) for t, rows in rows_by_table.items()}
        mart_rows = run_demo_sql(REFERENCE_SQL, rows_by_table)
        ordered = sort_rows(mart_rows, task.mart(MART_NAME).key_columns, MART_COLS)
        stage2[pop.value] = {MART_NAME: rows_to_canonical_csv(ordered, MART_COLS)}
    return GoldStub(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        stage1=stage1,
        stage2_csv=stage2,
    )


def compute_rewards_by_variant(
    task: TaskIR, gold: GoldStub
) -> dict[TaskVariant, dict[str, dict[P, float]]]:
    """Both active rewards plus the legacy FULL reward from one execution.

    This is the `rewards_by_variant` artifact verification/attacks.py records.
    The T battery reads THIS map, never the legacy `rewards` field: for a
    faithful transform mutant the two are provably equal (stage 1 passes, so
    evaluate() == _mart_fraction), and that is exactly what would make copying
    a relabelling of the parent battery rather than a measurement.
    """
    primary_rows = upstream_eval.parse_canonical_csv(
        gold.stage2_csv[P.PRIMARY.value][MART_NAME]
    )[1]
    load_plans = resolve_load_plans(task, gold)
    out: dict[TaskVariant, dict[str, dict[P, float]]] = {v: {} for v in TaskVariant}
    for case in task.attack_cases:
        if isinstance(load_plans.get(case.name), str):
            # No surface on this data (e.g. truncate_table where every table
            # fits in one unit): the runner records inapplicable.json and the
            # case has NO entry in the measured payload — mirrored here.
            continue
        for variant in TaskVariant:
            out[variant][case.name] = {}
        for pop in P:
            if case.name in load_plans:
                # A load mutant: the trusted transform runs over a MUTATED
                # warehouse. Only the count vector matters to EL; the FULL/T
                # rewards see an empty mart submission, exactly what
                # run_attack records for a fabricated (nothing-was-loaded)
                # submission and a strictly-losing outcome for the rest.
                stage1 = simulated_load_stage1(
                    task, gold, load_plans[case.name], pop, DEMO_DATA
                )
                marts = {MART_NAME: []}
            elif case.kind is AttackKind.SKIP_EXTRACTION:
                # The loader creates the skipped backend's tables EMPTY.
                skipped = {
                    b.table
                    for b in task.backends
                    if b.backend is task.backends[0].backend
                }
                stage1 = {
                    t: (0 if t in skipped else n)
                    for t, n in gold.stage1[pop.value].items()
                }
                marts: dict[str, list[Row]] = {MART_NAME: []}
            elif case.mutation == HARDCODE_PRIMARY_DIRECTIVE:
                stage1 = dict(gold.stage1[pop.value])
                marts = {MART_NAME: [dict(r) for r in primary_rows]}
            else:
                stage1 = dict(gold.stage1[pop.value])
                marts = {MART_NAME: run_demo_sql(case.mutation, DEMO_DATA[pop])}
            for variant in TaskVariant:
                out[variant][case.name][pop] = evaluate_variant(
                    variant, task, gold, pop,
                    actual_stage1=stage1, actual_marts=marts,
                ).reward
    return out


# ---------------------------------------------------------------------------
# Workspace: real rendered artifacts (find_rendered_artifact must RESOLVE) and
# every recorded-evidence file the variant batteries consume.
# ---------------------------------------------------------------------------

def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def write_workspace(
    workspace: Path,
    task: TaskIR,
    gold: GoldStub,
    rewards_by_variant: dict[TaskVariant, dict[str, dict[P, float]]] | None = None,
) -> None:
    tdir = workspace / "tasks" / task.task_id
    for pop, data in DEMO_DATA.items():
        pdir = tdir / "populations" / pop.value
        rows_dir = pdir / "rows"
        rows_dir.mkdir(parents=True, exist_ok=True)
        for table in task.tables:
            cols = [c.name for c in table.columns]
            lines = [
                json.dumps(dict(zip(cols, row)), sort_keys=True)
                for row in data[table.name]
            ]
            (rows_dir / f"{table.name}.jsonl").write_text(
                "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
            )
        rendered = pdir / "rendered"
        (rendered / "postgres").mkdir(parents=True, exist_ok=True)
        (rendered / "mongodb").mkdir(parents=True, exist_ok=True)
        (rendered / "files").mkdir(parents=True, exist_ok=True)
        (rendered / "postgres" / "customers.sql").write_text(
            "".join(
                f"INSERT INTO customers VALUES ({r[0]}, '{r[1]}');\n"
                for r in data["customers"]
            ),
            encoding="utf-8",
        )
        (rendered / "mongodb" / "orders.jsonl").write_text(
            "".join(
                json.dumps(
                    dict(zip(("order_id", "customer_id", "status"), r)), sort_keys=True
                )
                + "\n"
                for r in data["orders"]
            ),
            encoding="utf-8",
        )
        (rendered / "files" / "order_items.csv").write_text(
            "order_id,quantity,unit_price\n"
            + "".join(f"{r[0]},{r[1]},{r[2]}\n" for r in data["order_items"]),
            encoding="utf-8",
        )

    # Inapplicable load probes record a reason; measured probes record
    # rewards/errors so the gate distinguishes a true kill from a crash.
    load_plans = resolve_load_plans(task, gold)
    measured = rewards_by_variant or compute_rewards_by_variant(task, gold)
    for case in task.attack_cases:
        plan_or_reason = load_plans.get(case.name)
        d = tdir / "attacks" / case.name
        if isinstance(plan_or_reason, str):
            d.mkdir(parents=True, exist_ok=True)
            (d / "inapplicable.json").write_text(
                json.dumps(
                    {
                        "case": case.name,
                        "kind": case.kind.value,
                        "required": case.required,
                        "inapplicable": plan_or_reason,
                        "task_content_hash": task.content_hash(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            continue
        write_attack_record(workspace, task, case, measured)

    reports = tdir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    write_determinism(workspace, task, gold)
    (reports / "contamination_post.json").write_text(
        json.dumps(
            {
                "task_id": task.task_id,
                "task_content_hash": task.content_hash(),
                "collisions": [],
                "coverage": dict(ARMED_COVERAGE),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    write_dual_build(workspace, task)
    write_census(workspace, task, gold)
    write_independent_load(workspace, task)
    write_warehouse_census(workspace, task, gold)
    write_canonical_reachability(workspace, task)


def write_attack_record(
    workspace: Path,
    task: TaskIR,
    case: AttackCase,
    rewards_by_variant: dict[TaskVariant, dict[str, dict[P, float]]],
    *,
    errors: dict[str, str] | None = None,
    content_hash: str | None = None,
) -> Path:
    """Fixture mirror of attacks._record_attack's rewards.json."""
    d = workspace / "tasks" / task.task_id / "attacks" / case.name
    d.mkdir(parents=True, exist_ok=True)
    per_variant = {
        v.value: {
            p.value: r for p, r in rewards_by_variant.get(v, {}).get(case.name, {}).items()
        }
        for v in TaskVariant
    }
    record = {
        "case": case.name,
        "kind": case.kind.value,
        "required": case.required,
        "source_finding": case.source_finding,
        "rewards": per_variant.get(V.FULL.value, {}),
        "rewards_by_variant": {
            v.value: per_variant[v.value] for v in RLVR_TASK_VARIANTS
        },
        "expected_pass": {p.value: v for p, v in case.expected_pass.items()},
        "errors": dict(errors or {}),
        "task_content_hash": content_hash or task.content_hash(),
    }
    (d / "rewards.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return d / "rewards.json"


def determinism_evidence_for(task: TaskIR, gold: GoldStub) -> dict[str, str]:
    """What cli.run_reference_stage binds the record with: task_id +
    task_content_hash, and split digests that ARE the frozen gold's."""
    gd = gold_digests(gold, P.PRIMARY)
    return {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "population": P.PRIMARY.value,
        "runs": "3",
        "run_digest": gd.joint,
        gates.DETERMINISM_STAGE1_DIGEST_KEY: gd.stage1,
        gates.DETERMINISM_STAGE2_DIGEST_KEY: gd.stage2,
    }


def write_determinism(
    workspace: Path,
    task: TaskIR,
    gold: GoldStub | None = None,
    *,
    evidence: dict[str, str] | None = None,
) -> Path:
    path = workspace / "tasks" / task.task_id / gates.DETERMINISM_EVIDENCE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    if evidence is not None:
        ev = evidence
    else:
        assert gold is not None, "gold is required to derive the split digests"
        ev = determinism_evidence_for(task, gold)
    path.write_text(
        GateResult(
            gate="determinism",
            passed=True,
            details="3/3 rebuilds byte-identical",
            evidence=ev,
        ).model_dump_json(),
        encoding="utf-8",
    )
    return path


def _agreement_record(task: TaskIR, role: str, extra_sample: dict) -> dict:
    agreement = {p.value: 1.0 for p in P}
    sample = {"sample_index": 0, "prompt_sha256": "0" * 64, "rewards": dict(agreement)}
    sample.update(extra_sample)
    return {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "role": role,
        "status": "agreed",
        "agreement": agreement,
        "samples": [sample],
        "detail": f"fixture {role} record",
    }


def write_dual_build(workspace: Path, task: TaskIR) -> Path:
    path = workspace / "tasks" / task.task_id / gates.DUAL_BUILD_EVIDENCE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    record = _agreement_record(
        task,
        "independent_implementer",
        {"sql_by_mart": {m.name: "select 1" for m in task.marts}, "dev_pass": True},
    )
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return path


def write_independent_load(workspace: Path, task: TaskIR, **overrides) -> Path:
    path = workspace / "tasks" / task.task_id / gates.EL_INDEPENDENT_LOAD_EVIDENCE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    record = _agreement_record(
        task,
        gates.EL_INDEPENDENT_LOAD_ROLE,
        {
            "load_plan": {
                t.name: {
                    "path": f"rendered/{task.backend_for(t.name).backend.value}/{t.name}",
                    "format": task.backend_for(t.name).backend.value,
                }
                for t in task.tables
            }
        },
    )
    record.update(overrides)
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return path


def write_census(
    workspace: Path,
    task: TaskIR,
    gold: GoldStub,
    *,
    counts: dict[str, dict[str, int]] | None = None,
    **overrides,
) -> Path:
    """The independent per-format record counter's output (track 1's producer).

    Counts default to the frozen gold, i.e. the agreeing case; a test that
    wants a reader bug passes `counts` that disagree. The recorded artifact
    path is the one find_rendered_artifact actually resolves on disk (the
    populations-load gate now enforces census/resolver PATH agreement, not
    just count agreement); a stub path stands in only when nothing resolves.
    """
    from elt_taskgen.reference.solution import find_rendered_artifact

    path = workspace / "tasks" / task.task_id / gates.EL_CENSUS_EVIDENCE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    tdir = workspace / "tasks" / task.task_id

    def _artifact_rel(pop: PopulationName, table_name: str) -> str:
        rendered_dir = tdir / "populations" / pop.value / "rendered"
        try:
            resolved = find_rendered_artifact(task, rendered_dir, table_name)
            return resolved.relative_to(tdir).as_posix()
        except (FileNotFoundError, ValueError):
            return (
                f"populations/{pop.value}/rendered/"
                f"{task.backend_for(table_name).backend.value}/{table_name}"
            )

    record = {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "kind": gates.EL_CENSUS_KIND,
        # The gate accepts only THIS counter's records: its version and its
        # independence claim, exactly (a foreign version certifies nothing).
        "counter_version": el_probes.COUNTER_VERSION,
        "independent_of": list(el_probes.INDEPENDENT_OF),
        "populations": {
            pop.value: {
                t.name: {
                    "artifact": _artifact_rel(pop, t.name),
                    "format": task.backend_for(t.name).backend.value,
                    "records": (counts or gold.stage1)[pop.value][t.name],
                }
                for t in task.tables
            }
            for pop in P
        },
    }
    record.update(overrides)
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return path


def build_warehouse(path: Path, data: dict[str, list[tuple]]) -> None:
    """A real DuckDB warehouse of the demo's three source tables for one
    population — the artifact the T determinism gate RE-CENSUSES live."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE customers (customer_id INTEGER, customer_name VARCHAR)")
        con.execute("CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, status VARCHAR)")
        con.execute("CREATE TABLE order_items (order_id INTEGER, quantity INTEGER, unit_price DOUBLE)")
        for table, rows in data.items():
            if rows:
                ph = ",".join("?" for _ in rows[0])
                con.executemany(f"INSERT INTO {table} VALUES ({ph})", rows)
    finally:
        con.close()


def write_warehouse_census(
    workspace: Path,
    task: TaskIR,
    gold: GoldStub,
    *,
    counts: dict[str, dict[str, int]] | None = None,
    extra_relation: str | None = None,
    shared_digest: bool = False,
    rebuild_moves: bool = False,
    census_version: str | None = None,
    build_files: bool = True,
) -> Path:
    """The T warehouse builder's record — over REAL shipped warehouses.

    By default the five .duckdb files are built from DEMO_DATA at the recorded
    task-relative path and censused with the production census
    (export.eltbench.warehouse_census), so the recorded digest IS what the
    file on disk censuses to. The knobs fake one leg each for the red tests.
    """
    path = workspace / "tasks" / task.task_id / gates.WAREHOUSE_CENSUS_EVIDENCE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    tdir = workspace / "tasks" / task.task_id
    populations: dict[str, dict] = {}
    for pop in P:
        rel = WAREHOUSE_REL.format(pop=pop.value)
        if build_files:
            build_warehouse(tdir / rel, DEMO_DATA[pop])
        live = eltbench.warehouse_census(tdir / rel) if (tdir / rel).is_file() else None
        if live is not None and counts is None and not shared_digest:
            tables = {name: dict(entry) for name, entry in live["tables"].items()}
            digest = live["census_digest"]
        else:
            tables = {
                t.name: {
                    "row_count": (counts or gold.stage1)[pop.value][t.name],
                    "row_digest": _digest("rows", pop.value, t.name),
                }
                for t in task.tables
            }
            digest = _digest("census", "shared" if shared_digest else pop.value)
        if extra_relation:
            tables[extra_relation] = {
                "row_count": 1,
                "row_digest": _digest("extra", pop.value),
            }
        populations[pop.value] = {
            "path": rel,
            "census_digest": digest,
            "rebuild_census_digest": (
                _digest("census", "rebuild", pop.value) if rebuild_moves else digest
            ),
            "tables": tables,
        }
    record = {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "kind": gates.WAREHOUSE_CENSUS_KIND,
        "census_version": (
            str(eltbench.CENSUS_VERSION) if census_version is None else census_version
        ),
        "populations": populations,
    }
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return path


def gate_by_name(report: AcceptanceReport, name: str) -> GateResult:
    for g in report.gates:
        if g.gate == name:
            return g
    raise AssertionError(f"no gate named {name!r} in {report.task_id}")


def transform_only(task: TaskIR) -> TaskIR:
    """The demo WITHOUT its extract-load catalogue — the pre-EL-battery shape.

    Several tests pin the crux ("a task whose required cases are all transform
    kinds must fail the EL battery closed"), which stopped being the demo's
    own shape the moment demo_fixture gained the load-directive cases; this
    reconstructs it instead of assuming it."""
    from elt_taskgen.verification.attacks import LOAD_DIRECTIVE_PREFIX

    return task.model_copy(
        update={
            "attack_cases": tuple(
                c
                for c in task.attack_cases
                if not c.mutation.strip().startswith(LOAD_DIRECTIVE_PREFIX)
            )
        }
    )


def _battery_scales(task: TaskIR) -> TaskIR:
    """Re-scale the demo populations for this module's HAND-BUILT row counts.

    WHY. DEMO_DATA realizes a handful of rows per table, while the pristine
    fixture declares scaled populations whose realized counts
    generation/source_data.realized_row_count guarantees to land 2-7% off the
    declared scale — a guarantee the declared-scale-reconciliation gate
    asserts against the frozen stage-1 counts, and one a 5-row stand-in can
    never satisfy. Below REALIZED_DIVERGENCE_MIN_SCALE the generator realizes
    counts exactly and the gate RECORDS the surface instead of asserting it —
    the honest description of this fixture (the asserted path is proven by
    construction in tests/test_gates_integrity.py and end-to-end by the demo
    replay). Scales stay UNEQUAL to the hand-built counts so every
    fabricate_counts mutant still loses, and primary/resampled keep a SHARED
    scale (the memorization-pair invariant every real task carries).
    """
    scaled = {
        P.PRIMARY: {"customers": 40, "orders": 45, "order_items": 45},
        P.RESAMPLED: {"customers": 40, "orders": 45, "order_items": 45},
        P.STRESS: {"customers": 30, "orders": 35, "order_items": 35},
    }
    populations = tuple(
        p.model_copy(update={"scale": scaled[p.name]}) if p.name in scaled else p
        for p in task.populations
    )
    return task.model_copy(update={"populations": populations})


def task_with_el_mutant() -> TaskIR:
    """The demo task plus ONE required extraction-side mutant.

    Without it the demo has no EL discrimination at all — which is the point of
    `test_el_required_mutants_refuses_transform_mutants`. skip_extraction is the
    only extraction kind models.AttackKind currently defines; the rest of the
    catalogue (duplicate_on_load, partial_backend, ...) lands in track 1 and is
    recorded as undeclared in the gate evidence until it does.
    """
    task = _battery_scales(demo_fixture.demo_task())
    case = AttackCase(
        name="skip_postgres_backend",
        kind=AttackKind.SKIP_EXTRACTION,
        description="Loads the whole postgres backend as empty tables.",
        mutation="",
        expected_pass={p: False for p in P},
    )
    return task.model_copy(update={"attack_cases": task.attack_cases + (case,)})


# ---------------------------------------------------------------------------
# The applicability matrix itself
# ---------------------------------------------------------------------------

class TestApplicabilityMatrix(unittest.TestCase):
    def test_active_roster_helper_returns_exactly_el_and_t(self) -> None:
        from elt_taskgen.verification import variant_battery

        rosters = variant_battery.variant_gate_names()
        self.assertEqual(tuple(rosters), RLVR_TASK_VARIANTS)
        self.assertNotIn(V.FULL, rosters)

    def test_not_applicable_always_names_a_replacement_in_the_roster(self) -> None:
        for variant in V:
            roster = set(gates.VARIANT_GATE_NAMES[variant])
            for cell in gates.VARIANT_GATE_MATRIX[variant]:
                if cell.verdict is gates.VariantGateVerdict.NOT_APPLICABLE:
                    self.assertNotIn(cell.gate, roster)
                    self.assertTrue(cell.replaced_by)
                    for repl in cell.replaced_by:
                        self.assertIn(repl, roster)

    def test_el_replaces_dual_build_t_replaces_populations_load(self) -> None:
        el = gates.VARIANT_GATE_NAMES[V.EXTRACT_LOAD]
        t = gates.VARIANT_GATE_NAMES[V.TRANSFORM]
        self.assertNotIn("dual-build-agreement", el)
        self.assertIn("el-artifact-census", el)
        self.assertIn("el-independent-load", el)
        self.assertNotIn("populations-load", t)
        self.assertIn("warehouses-load", t)

    def test_el_admissible_kinds_are_disjoint_from_transform_mutants(self) -> None:
        self.assertFalse(
            gates.EXTRACTION_MUTANT_KINDS & gates.TRANSFORM_MUTANT_KINDS,
            "an extraction mutant set overlapping the transform mutants IS the "
            "fiction being removed",
        )

    def test_weak_variant_is_rejected_not_quiet(self) -> None:
        """A battery certified mostly by waiver must FAIL, not report accepted.

        No shipped variant trips this today (both are 11/12), so it is proven
        by constructing the situation the rule exists to forbid: waive one more
        parent gate and the roster gate must go red.
        """
        weak = tuple(
            cell.model_copy(
                update={
                    "verdict": gates.VariantGateVerdict.NOT_APPLICABLE,
                    "replaced_by": ("el-artifact-census",),
                }
            )
            if cell.gate == "info-content"
            else cell
            for cell in gates.VARIANT_GATE_MATRIX[V.EXTRACT_LOAD]
        )
        roster = tuple(
            c.gate
            for c in weak
            if c.verdict is not gates.VariantGateVerdict.NOT_APPLICABLE
        )
        with unittest.mock.patch.dict(
            gates.VARIANT_GATE_MATRIX, {V.EXTRACT_LOAD: weak}
        ), unittest.mock.patch.dict(
            gates.VARIANT_GATE_NAMES, {V.EXTRACT_LOAD: roster}
        ):
            produced = tuple(
                GateResult(gate=g, passed=True) for g in roster if g != "variant-roster"
            )
            result = gates._gate_variant_roster(V.EXTRACT_LOAD, produced)
        self.assertFalse(result.passed)
        self.assertIn("WEAK variant", result.details)
        # Denominator moved 12 -> 13 when B2 added mart-key-unique.
        self.assertEqual(result.evidence["applicable_parent_gates"], "11/13")

    def test_failure_partition_defaults_to_task_level(self) -> None:
        self.assertEqual(
            gates.classify_variant_failure(V.TRANSFORM, "degenerate-zero"),
            gates.FAILURE_TASK_LEVEL,
        )
        self.assertEqual(
            gates.classify_variant_failure(V.EXTRACT_LOAD, "el-artifact-census"),
            gates.FAILURE_TASK_LEVEL,
        )
        self.assertEqual(
            gates.classify_variant_failure(V.EXTRACT_LOAD, "required-mutants"),
            gates.FAILURE_VARIANT_LOCAL,
        )
        # An unknown gate must NOT be quietly variant-local.
        self.assertEqual(
            gates.classify_variant_failure(V.TRANSFORM, "brand-new-gate"),
            gates.FAILURE_TASK_LEVEL,
        )


# ---------------------------------------------------------------------------
# The two active unit batteries end to end; FULL is tested explicitly as legacy
# ---------------------------------------------------------------------------

class VariantBatteryCase(unittest.TestCase):
    task: TaskIR
    gold: GoldStub
    rewards: dict[TaskVariant, dict[str, dict[P, float]]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.task = task_with_el_mutant()
        cls.gold = build_gold(cls.task)
        cls.rewards = compute_rewards_by_variant(cls.task, cls.gold)
        cls._tmp = Path(tempfile.mkdtemp(prefix="variant-gates-"))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def fresh_workspace(self, task: TaskIR | None = None, gold: GoldStub | None = None) -> Path:
        ws = Path(tempfile.mkdtemp(dir=self._tmp))
        # The standing rewards only feed the recorded rewards.json mirrors
        # (the crash check reads their `errors`); a test's OWN reward map is
        # what the battery is handed in run_variant.
        write_workspace(ws, task or self.task, gold or self.gold, self.rewards)
        return ws

    def run_variant(
        self,
        variant: TaskVariant,
        *,
        workspace: Path | None = None,
        task: TaskIR | None = None,
        gold: GoldStub | None = None,
        rewards=None,
    ) -> AcceptanceReport:
        task = task or self.task
        gold = gold or self.gold
        return gates.run_variant_gates(
            variant,
            task,
            workspace or self.fresh_workspace(task, gold),
            gold,
            self.rewards if rewards is None else rewards,
        )


class TestActiveBatteries(VariantBatteryCase):
    def test_two_reports_one_parent_hash_two_unit_ids(self) -> None:
        ws = self.fresh_workspace()
        reports = gates.run_all_variant_gates(self.task, ws, self.gold, self.rewards)
        self.assertEqual(tuple(reports), RLVR_TASK_VARIANTS)
        self.assertNotIn(V.FULL, reports)
        self.assertEqual(
            {r.task_id for r in reports.values()},
            {
                self.task.task_id + "__el",
                self.task.task_id + "__t",
            },
        )
        for report in reports.values():
            self.assertEqual(report.task_content_hash, self.task.content_hash())

    def test_both_active_units_accept_on_a_fully_evidenced_workspace(self) -> None:
        ws = self.fresh_workspace()
        reports = gates.run_all_variant_gates(self.task, ws, self.gold, self.rewards)
        for variant, report in reports.items():
            failed = [g.gate for g in report.gates if not g.passed]
            self.assertTrue(
                report.accepted,
                f"{variant.value} rejected on {failed}: "
                + "; ".join(g.details for g in report.gates if not g.passed),
            )

    def test_each_active_variant_runs_exactly_its_own_roster(self) -> None:
        ws = self.fresh_workspace()
        for variant in RLVR_TASK_VARIANTS:
            report = gates.run_variant_gates(
                variant, self.task, ws, self.gold, self.rewards
            )
            self.assertEqual(
                tuple(g.gate for g in report.gates),
                gates.VARIANT_GATE_NAMES[variant],
            )

    def test_roster_gate_records_not_applicable_reasons_and_counts(self) -> None:
        ws = self.fresh_workspace()
        el = gates.run_variant_gates(V.EXTRACT_LOAD, self.task, ws, self.gold, self.rewards)
        roster = gate_by_name(el, "variant-roster")
        self.assertTrue(roster.passed)
        # Denominator moved 12 -> 13 when B2 added mart-key-unique.
        self.assertEqual(roster.evidence["applicable_parent_gates"], "12/13")
        self.assertEqual(roster.evidence["not_applicable_gates"], "1")
        na = roster.evidence["not-applicable:dual-build-agreement"]
        self.assertIn("REPLACED BY: el-artifact-census, el-independent-load", na)
        self.assertIn("TRANSFORM", na)
        t = gates.run_variant_gates(V.TRANSFORM, self.task, ws, self.gold, self.rewards)
        t_roster = gate_by_name(t, "variant-roster")
        self.assertIn(
            "REPLACED BY: warehouses-load",
            t_roster.evidence["not-applicable:populations-load"],
        )

    def test_no_gate_result_ever_says_skipped_or_not_applicable(self) -> None:
        """Not-applicability must never be a PASSING GateResult."""
        ws = self.fresh_workspace()
        for variant in RLVR_TASK_VARIANTS:
            report = gates.run_variant_gates(variant, self.task, ws, self.gold, self.rewards)
            for g in report.gates:
                if g.gate == "variant-roster":
                    continue
                self.assertNotIn("not applicable", g.details.lower())
                self.assertNotIn("skipped", g.details.lower())

    def test_failure_class_agrees_with_the_consumer_that_reads_the_evidence(self) -> None:
        """verification/variant_battery.py reads `failure_scope` off the gate
        evidence BEFORE delegating to classify_variant_failure. Both keys must
        be written, and they must agree: a disagreement about whether a failure
        is task-level is a disagreement about whether the two-unit parent may
        be admitted."""
        from elt_taskgen.verification import variant_battery

        report = self.run_variant(
            V.EXTRACT_LOAD,
            task=demo_fixture.demo_task(),
            gold=build_gold(demo_fixture.demo_task()),
            rewards={V.EXTRACT_LOAD: {}},
        )
        failing = [g for g in report.gates if not g.passed]
        self.assertTrue(failing)
        for g in failing:
            self.assertEqual(g.evidence["failure_class"], g.evidence["failure_scope"])
            self.assertEqual(
                variant_battery.classify_gate_failure(V.EXTRACT_LOAD, g),
                g.evidence["failure_class"],
            )

    def test_failing_variant_gate_is_stamped_with_its_failure_class(self) -> None:
        report = self.run_variant(V.EXTRACT_LOAD, task=demo_fixture.demo_task(),
                                  gold=build_gold(demo_fixture.demo_task()),
                                  rewards={V.EXTRACT_LOAD: {}})
        required = gate_by_name(report, "required-mutants")
        self.assertFalse(required.passed)
        self.assertEqual(
            required.evidence["failure_class"], gates.FAILURE_VARIANT_LOCAL
        )

    def test_legacy_full_delegates_to_the_shared_parent_battery(self) -> None:
        ws = self.fresh_workspace()
        via_variant = gates.run_variant_gates(V.FULL, self.task, ws, self.gold, self.rewards)
        direct = gates.run_gates(self.task, ws, self.gold, self.rewards[V.FULL])
        self.assertEqual(via_variant.model_dump(), direct.model_dump())


# ---------------------------------------------------------------------------
# THE CRUX: EL may not be certified by transform mutants
# ---------------------------------------------------------------------------

class TestElRequiredMutants(VariantBatteryCase):
    def test_el_required_mutants_refuses_transform_mutants(self) -> None:
        """A task whose required cases are ALL transform kinds fails EL closed.

        The demo now ships the extract-load catalogue, so the transform-only
        world this test pins is reconstructed by stripping the load-directive
        cases — the exact shape every task had before the EL battery existed.
        Under the EL reward every transform mutant scores 1.0 (compare_stage1
        never looks at a mart), so a gate that read them as EL evidence would
        report a green EL battery for a task with zero extraction
        discrimination.
        """
        plain = transform_only(demo_fixture.demo_task())
        gold = build_gold(plain)
        rewards = compute_rewards_by_variant(plain, gold)
        # Precondition: every transform mutant IS a free pass under the EL reward.
        for case in plain.attack_cases:
            for pop in P:
                self.assertEqual(rewards[V.EXTRACT_LOAD][case.name][pop], 1.0)
        report = self.run_variant(
            V.EXTRACT_LOAD, task=plain, gold=gold, rewards=rewards
        )
        gate = gate_by_name(report, "required-mutants")
        self.assertFalse(gate.passed)
        self.assertIn("NO required extract-load mutant", gate.details)
        self.assertFalse(report.accepted)
        # and every excluded case is RECORDED, not silently dropped
        for case in plain.attack_cases:
            self.assertIn(
                "INADMISSIBLE", gate.evidence[f"inadmissible:{case.name}"]
            )

    def test_el_required_mutants_green_on_an_extraction_mutant(self) -> None:
        report = self.run_variant(V.EXTRACT_LOAD)
        gate = gate_by_name(report, "required-mutants")
        self.assertTrue(gate.passed, gate.details)
        self.assertIn("skip_postgres_backend", gate.evidence)
        self.assertIn("kind=skip_extraction", gate.evidence["skip_postgres_backend"])

    def test_el_required_mutants_catches_a_leak(self) -> None:
        rewards = {
            v: {c: dict(p) for c, p in cases.items()}
            for v, cases in self.rewards.items()
        }
        rewards[V.EXTRACT_LOAD]["skip_postgres_backend"][P.PRIMARY] = 1.0
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, rewards=rewards), "required-mutants"
        )
        self.assertFalse(gate.passed)
        self.assertIn("LEAK", gate.details)

    def test_variant_battery_never_reads_the_legacy_rewards_field(self) -> None:
        """Rewards must arrive as rewards_by_variant, per variant."""
        report = self.run_variant(V.TRANSFORM, rewards={})
        gate = gate_by_name(report, "required-mutants")
        self.assertFalse(gate.passed)
        self.assertIn("rewards_by_variant", gate.details)

    def test_t_required_mutants_excludes_skip_extraction(self) -> None:
        gate = gate_by_name(self.run_variant(V.TRANSFORM), "required-mutants")
        self.assertTrue(gate.passed, gate.details)
        self.assertIn("inadmissible:skip_postgres_backend", gate.evidence)


# ---------------------------------------------------------------------------
# Degenerate probes, per variant
# ---------------------------------------------------------------------------

class TestDegenerateProbes(VariantBatteryCase):
    def test_el_degenerate_probes_are_counts_maps_and_all_score_zero(self) -> None:
        gate = gate_by_name(self.run_variant(V.EXTRACT_LOAD), "degenerate-zero")
        self.assertTrue(gate.passed, gate.details)
        for probe in ("empty_load", "one_table_only", "uniform_source"):
            self.assertEqual(
                gate.evidence[f"{probe}:{P.PRIMARY.value}"], "reward=0.000000"
            )
        # the memorization probe is only a probe where the vector actually moves
        self.assertIn(
            "IS the correct answer",
            gate.evidence[f"fabricate_primary_vector:{P.RESAMPLED.value}"],
        )
        self.assertEqual(
            gate.evidence[f"fabricate_primary_vector:{P.STRESS.value}"],
            "reward=0.000000",
        )

    def test_el_degenerate_red_when_all_source_counts_are_equal(self) -> None:
        """uniform_source is a working exploit when every table shares a count."""
        flat = {
            pop.value: {t: 7 for t in counts}
            for pop, counts in [(p, self.gold.stage1[p.value]) for p in P]
        }
        flat[P.STRESS.value] = {t: 9 for t in flat[P.STRESS.value]}
        gold = self.gold.model_copy(update={"stage1": flat})
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, gold=gold), "degenerate-zero"
        )
        self.assertFalse(gate.passed)
        self.assertIn("uniform_source scored 1.0", gate.details)

    def test_el_degenerate_red_when_the_count_vector_never_moves(self) -> None:
        constant = {pop.value: dict(self.gold.stage1[P.PRIMARY.value]) for pop in P}
        gold = self.gold.model_copy(update={"stage1": constant})
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, gold=gold), "degenerate-zero"
        )
        self.assertFalse(gate.passed)
        self.assertIn("fabricate_primary_vector", gate.details)
        self.assertEqual(gate.evidence["primary_vector_moves_on"], "NOWHERE")

    def test_t_degenerate_zero_catches_the_empty_gold_mart_mask(self) -> None:
        """THE MASK: compare_mart matches empty gold against an empty submission.

        Under the parent reward no_op scores 0 only because it submits an empty
        stage-1 map and evaluate() short-circuits. Under the T reward stage 1 is
        not scored, so an empty gold mart pays no_op full credit.
        """
        stage2 = {pop: dict(marts) for pop, marts in self.gold.stage2_csv.items()}
        header = stage2[P.COUNTERFACTUAL.value][MART_NAME].splitlines()[0]
        stage2[P.COUNTERFACTUAL.value][MART_NAME] = header + "\n"
        gold = self.gold.model_copy(update={"stage2_csv": stage2})

        # the mask, demonstrated through the ONE reward implementation
        self.assertEqual(
            upstream_eval.evaluate(
                self.task, gold, P.COUNTERFACTUAL, {}, {MART_NAME: []}
            ).reward,
            0.0,
        )
        self.assertEqual(
            evaluate_variant(
                V.TRANSFORM, self.task, gold, P.COUNTERFACTUAL,
                actual_marts={MART_NAME: []},
            ).reward,
            1.0,
        )
        gate = gate_by_name(self.run_variant(V.TRANSFORM, gold=gold), "degenerate-zero")
        self.assertFalse(gate.passed)
        self.assertIn("no_op scored 1.0 on counterfactual", gate.details)
        self.assertIn("empty gold mart", gate.details)
        self.assertEqual(gate.evidence["failure_class"], gates.FAILURE_TASK_LEVEL)

    def test_t_degenerate_zero_uses_the_transform_reward_on_every_graded_pop(self) -> None:
        gate = gate_by_name(self.run_variant(V.TRANSFORM), "degenerate-zero")
        self.assertTrue(gate.passed, gate.details)
        for pop in gates.GRADED_POPULATIONS:
            for strategy in ("no_op", "keys_only", "constants"):
                self.assertEqual(
                    gate.evidence[f"{strategy}:{pop.value}"], "reward=0.000000"
                )


# ---------------------------------------------------------------------------
# Shortcut sets
# ---------------------------------------------------------------------------

class TestShortcutSets(VariantBatteryCase):
    def test_el_excludes_mart_shortcuts_with_a_recorded_reason(self) -> None:
        gate = gate_by_name(self.run_variant(V.EXTRACT_LOAD), "shortcut-probes")
        self.assertTrue(gate.passed, gate.details)
        self.assertIn("ignores actual_marts", gate.evidence["excluded:constants"])
        self.assertIn("excluded:keys_only", gate.evidence)
        # the CONSTANTS probe must not appear as EL evidence at all
        self.assertNotIn("hardcoded_primary_outputs", gate.evidence)

    def test_el_shortcut_probes_red_when_no_el_probe_exists(self) -> None:
        plain = transform_only(demo_fixture.demo_task())
        gold = build_gold(plain)
        rewards = compute_rewards_by_variant(plain, gold)
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, task=plain, gold=gold, rewards=rewards),
            "shortcut-probes",
        )
        self.assertFalse(gate.passed)
        self.assertIn("no shortcut-kind probe was compiled", gate.details)
        self.assertIn("extract-load shortcut set", gate.details)

    def test_t_excludes_skip_extraction_with_a_recorded_reason(self) -> None:
        gate = gate_by_name(self.run_variant(V.TRANSFORM), "shortcut-probes")
        self.assertTrue(gate.passed, gate.details)
        self.assertIn(
            "hands the solver the materialized warehouse",
            gate.evidence["excluded:skip_extraction"],
        )
        self.assertNotIn("skip_postgres_backend", gate.evidence)
        self.assertIn("hardcoded_primary_outputs", gate.evidence)

    def test_parent_shortcut_gate_behaviour_is_unchanged(self) -> None:
        ws = self.fresh_workspace()
        report = gates.run_gates(self.task, ws, self.gold, self.rewards[V.FULL])
        self.assertTrue(gate_by_name(report, "shortcut-probes").passed)


# ---------------------------------------------------------------------------
# EL certification: census, three legs, independent load
# ---------------------------------------------------------------------------

class TestElCertification(VariantBatteryCase):
    def test_census_missing_reds_both_census_and_trusted_solution(self) -> None:
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.EL_CENSUS_EVIDENCE_REL).unlink()
        report = self.run_variant(V.EXTRACT_LOAD, workspace=ws)
        self.assertFalse(gate_by_name(report, "el-artifact-census").passed)
        trusted = gate_by_name(report, "trusted-solution")
        self.assertFalse(trusted.passed)
        self.assertIn("cannot self-certify", trusted.details)
        self.assertEqual(trusted.evidence["failure_class"], gates.FAILURE_TASK_LEVEL)

    def test_el_trusted_solution_discards_the_gold_vs_gold_tautology(self) -> None:
        report = self.run_variant(V.EXTRACT_LOAD)
        trusted = gate_by_name(report, "trusted-solution")
        self.assertTrue(trusted.passed, trusted.details)
        self.assertIn("DISCARDED", trusted.evidence["gold_vs_gold"])
        # the tautology itself, demonstrated
        ok, _ = upstream_eval.compare_stage1(
            self.gold.stage1[P.PRIMARY.value], self.gold.stage1[P.PRIMARY.value]
        )
        self.assertTrue(ok)

    def test_census_disagreement_is_caught_three_ways(self) -> None:
        ws = self.fresh_workspace()
        bad = {pop: dict(counts) for pop, counts in self.gold.stage1.items()}
        bad[P.PRIMARY.value]["orders"] += 1
        write_census(ws, self.task, self.gold, counts=bad)
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-artifact-census"
        )
        self.assertFalse(gate.passed)
        self.assertIn("three-legged reconciliation disagrees", gate.details)

    def test_census_agreeing_with_gold_but_not_with_generator_rows_fails(self) -> None:
        """Any TWO legs agreeing is not proof: only rows/*.jsonl is renderer-free."""
        ws = self.fresh_workspace()
        rows = (
            ws / "tasks" / self.task.task_id / "populations"
            / P.PRIMARY.value / "rows" / "orders.jsonl"
        )
        rows.write_text(
            "".join(rows.read_text(encoding="utf-8").splitlines(keepends=True)[:-1]),
            encoding="utf-8",
        )
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-artifact-census"
        )
        self.assertFalse(gate.passed)
        self.assertIn("three-legged reconciliation disagrees", gate.details)

    def test_census_without_independence_declaration_fails(self) -> None:
        ws = self.fresh_workspace()
        write_census(ws, self.task, self.gold, independent_of=[])
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-artifact-census"
        )
        self.assertFalse(gate.passed)
        self.assertIn("independent_of", gate.details)

    def test_census_of_the_wrong_kind_is_not_accepted_by_filename(self) -> None:
        ws = self.fresh_workspace()
        write_census(ws, self.task, self.gold, kind="something-else")
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-artifact-census"
        )
        self.assertFalse(gate.passed)

    def test_stale_census_is_rejected(self) -> None:
        ws = self.fresh_workspace()
        write_census(ws, self.task, self.gold, task_content_hash="0" * 64)
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-artifact-census"
        )
        self.assertFalse(gate.passed)
        self.assertIn("STALE", gate.details)

    def test_independent_load_missing_is_red_and_variant_local(self) -> None:
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.EL_INDEPENDENT_LOAD_EVIDENCE_REL).unlink()
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-independent-load"
        )
        self.assertFalse(gate.passed)
        self.assertEqual(gate.evidence["failure_class"], gates.FAILURE_VARIANT_LOCAL)

    def test_independent_load_rejects_a_transform_build_record(self) -> None:
        ws = self.fresh_workspace()
        write_independent_load(ws, self.task, role="independent_implementer")
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-independent-load"
        )
        self.assertFalse(gate.passed)
        self.assertIn("must not be read as an extract-load witness", gate.details)

    def test_independent_load_requires_a_complete_load_plan(self) -> None:
        ws = self.fresh_workspace()
        record = json.loads(
            (ws / "tasks" / self.task.task_id / gates.EL_INDEPENDENT_LOAD_EVIDENCE_REL)
            .read_text(encoding="utf-8")
        )
        record["samples"][0]["load_plan"].pop("order_items")
        (ws / "tasks" / self.task.task_id / gates.EL_INDEPENDENT_LOAD_EVIDENCE_REL).write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8"
        )
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-independent-load"
        )
        self.assertFalse(gate.passed)
        self.assertIn("omits table(s) ['order_items']", gate.details)

    def test_el_independent_load_never_stands_in_for_the_census(self) -> None:
        """Deleting the census must not be survivable by the load build alone."""
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.EL_CENSUS_EVIDENCE_REL).unlink()
        report = self.run_variant(V.EXTRACT_LOAD, workspace=ws)
        self.assertTrue(gate_by_name(report, "el-independent-load").passed)
        self.assertFalse(report.accepted)


class TestElSurfaceAndInformation(VariantBatteryCase):
    def test_populations_load_requires_the_rendered_artifact_to_resolve(self) -> None:
        ws = self.fresh_workspace()
        (
            ws / "tasks" / self.task.task_id / "populations" / P.STRESS.value
            / "rendered" / "files" / "order_items.csv"
        ).unlink()
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "populations-load"
        )
        self.assertFalse(gate.passed)
        self.assertIn("no resolvable rendered artifact", gate.details)
        self.assertEqual(gate.evidence["failure_class"], gates.FAILURE_TASK_LEVEL)

    def test_populations_load_requires_census_resolver_path_agreement(self) -> None:
        """Equal counts over DIFFERENT files certify nothing. A census that
        independently resolved another artifact with a coincidentally equal
        count must be a red gate, not a silent pass — otherwise the divergent
        discovery mechanic (el_probes walks the disk; find_rendered_artifact
        probes a candidate list) is worthless."""
        ws = self.fresh_workspace()
        path = ws / "tasks" / self.task.task_id / gates.EL_CENSUS_EVIDENCE_REL
        record = json.loads(path.read_text(encoding="utf-8"))
        entry = record["populations"][P.PRIMARY.value]["orders"]
        self.assertNotEqual(entry["artifact"], "populations/primary/rendered/files/orders.jsonl")
        entry["artifact"] = "populations/primary/rendered/files/orders.jsonl"
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "populations-load"
        )
        self.assertFalse(gate.passed)
        self.assertIn("discovery mechanics diverge", gate.details)
        self.assertIn(
            "census_artifact=populations/primary/rendered/files/orders.jsonl",
            gate.evidence["rendered:primary:orders"],
        )

    def test_el_info_content_red_on_a_zero_row_table(self) -> None:
        stage1 = {pop: dict(c) for pop, c in self.gold.stage1.items()}
        stage1[P.STRESS.value]["order_items"] = 0
        gold = self.gold.model_copy(update={"stage1": stage1})
        gate = gate_by_name(self.run_variant(V.EXTRACT_LOAD, gold=gold), "info-content")
        self.assertFalse(gate.passed)
        self.assertIn("ZERO expected rows", gate.details)

    def test_el_info_content_red_when_all_counts_are_identical(self) -> None:
        stage1 = {pop: {t: 4 for t in c} for pop, c in self.gold.stage1.items()}
        stage1[P.STRESS.value] = {t: 9 for t in stage1[P.STRESS.value]}
        gold = self.gold.model_copy(update={"stage1": stage1})
        gate = gate_by_name(self.run_variant(V.EXTRACT_LOAD, gold=gold), "info-content")
        self.assertFalse(gate.passed)
        self.assertIn("SAME primary row count", gate.details)

    def test_el_data_sensitivity_asserts_invariance_on_the_memorization_pair(self) -> None:
        gate = gate_by_name(self.run_variant(V.EXTRACT_LOAD), "data-sensitivity")
        self.assertTrue(gate.passed, gate.details)
        self.assertEqual(gate.evidence["memorization_pair"], "invariant")
        self.assertIn("stress", gate.evidence["moves_vs_primary"])

    def test_el_data_sensitivity_red_on_generator_drift(self) -> None:
        stage1 = {pop: dict(c) for pop, c in self.gold.stage1.items()}
        stage1[P.RESAMPLED.value]["orders"] += 1
        gold = self.gold.model_copy(update={"stage1": stage1})
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, gold=gold), "data-sensitivity"
        )
        self.assertFalse(gate.passed)
        self.assertIn("generator drifted", gate.details)

    def test_el_data_sensitivity_red_when_no_graded_pair_moves(self) -> None:
        constant = {pop.value: dict(self.gold.stage1[P.PRIMARY.value]) for pop in P}
        gold = self.gold.model_copy(update={"stage1": constant})
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, gold=gold), "data-sensitivity"
        )
        self.assertFalse(gate.passed)
        self.assertIn("ONE constant vector", gate.details)


# ---------------------------------------------------------------------------
# T: warehouse witnesses
# ---------------------------------------------------------------------------

class TestTransformWarehouse(VariantBatteryCase):
    def test_warehouses_load_rejects_a_non_source_relation(self) -> None:
        ws = self.fresh_workspace()
        write_warehouse_census(ws, self.task, self.gold, extra_relation="answer_key")
        gate = gate_by_name(
            self.run_variant(V.TRANSFORM, workspace=ws), "warehouses-load"
        )
        self.assertFalse(gate.passed)
        self.assertIn("hands the T solver the answer", gate.details)
        self.assertEqual(gate.evidence["failure_class"], gates.FAILURE_TASK_LEVEL)

    def test_warehouses_load_rejects_a_row_count_that_is_not_the_frozen_state(self) -> None:
        ws = self.fresh_workspace()
        bad = {pop: dict(c) for pop, c in self.gold.stage1.items()}
        bad[P.PRIMARY.value]["customers"] = 4
        write_warehouse_census(ws, self.task, self.gold, counts=bad)
        gate = gate_by_name(
            self.run_variant(V.TRANSFORM, workspace=ws), "warehouses-load"
        )
        self.assertFalse(gate.passed)
        self.assertIn("frozen stage-1 gold expects", gate.details)

    def test_warehouses_load_missing_census_is_red(self) -> None:
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.WAREHOUSE_CENSUS_EVIDENCE_REL).unlink()
        report = self.run_variant(V.TRANSFORM, workspace=ws)
        self.assertFalse(gate_by_name(report, "warehouses-load").passed)
        self.assertFalse(report.accepted)

    def test_t_determinism_uses_a_census_digest_not_duckdb_bytes(self) -> None:
        ws = self.fresh_workspace()
        write_warehouse_census(ws, self.task, self.gold, rebuild_moves=True)
        gate = gate_by_name(self.run_variant(V.TRANSFORM, workspace=ws), "determinism")
        self.assertFalse(gate.passed)
        self.assertIn("warehouse census moved between builds", gate.details)

    def test_determinism_requires_split_component_digests(self) -> None:
        # A record bound to this task and hash but carrying NO split digests:
        # every battery is red, naming the missing components — the split
        # digests are what binds the recorded runs to the frozen gold, so the
        # PARENT battery no longer accepts a joint-only record either.
        ws = self.fresh_workspace()
        write_determinism(
            ws,
            self.task,
            evidence={
                "task_id": self.task.task_id,
                "task_content_hash": self.task.content_hash(),
                "runs": "3",
            },
        )
        for variant in (V.EXTRACT_LOAD, V.TRANSFORM):
            gate = gate_by_name(self.run_variant(variant, workspace=ws), "determinism")
            self.assertFalse(gate.passed)
            self.assertIn(gates.DETERMINISM_STAGE1_DIGEST_KEY, gate.details)
            self.assertIn(gates.DETERMINISM_STAGE2_DIGEST_KEY, gate.details)
            self.assertIn("not the answer key that ships", gate.details)
        parent = gate_by_name(
            gates.run_gates(self.task, ws, self.gold, self.rewards[V.FULL]),
            "determinism",
        )
        self.assertFalse(parent.passed)

    def test_variant_determinism_red_when_stage_digest_not_golds(self) -> None:
        # Self-consistent runs whose stage-2 digest is not the frozen gold's:
        # the recorded runs are not the answer key that ships.
        ws = self.fresh_workspace()
        ev = determinism_evidence_for(self.task, self.gold)
        ev[gates.DETERMINISM_STAGE2_DIGEST_KEY] = "e" * 64
        write_determinism(ws, self.task, evidence=ev)
        for variant in (V.EXTRACT_LOAD, V.TRANSFORM):
            gate = gate_by_name(self.run_variant(variant, workspace=ws), "determinism")
            self.assertFalse(gate.passed, variant.value)
            self.assertIn("not the answer key that ships", gate.details)
        # and a stale content hash is refused before any digest is compared
        ev = determinism_evidence_for(self.task, self.gold)
        ev["task_content_hash"] = "0" * 64
        write_determinism(ws, self.task, evidence=ev)
        gate = gate_by_name(self.run_variant(V.TRANSFORM, workspace=ws), "determinism")
        self.assertFalse(gate.passed)
        self.assertIn("STALE", gate.details)
        self.assertIn("re-run reference-run", gate.details)

    def test_t_determinism_red_when_shipped_warehouse_recensus_disagrees(self) -> None:
        # Two agreeing recorded builds are a QUOTE; the file on disk is the
        # measurement. Mutate one row in the shipped primary warehouse.
        ws = self.fresh_workspace()
        self.assertTrue(gate_by_name(self.run_variant(V.TRANSFORM, workspace=ws), "determinism").passed)
        db = ws / "tasks" / self.task.task_id / WAREHOUSE_REL.format(pop=P.PRIMARY.value)
        con = duckdb.connect(str(db))
        try:
            con.execute("UPDATE customers SET customer_name = 'tampered' WHERE customer_id = 1")
        finally:
            con.close()
        gate = gate_by_name(self.run_variant(V.TRANSFORM, workspace=ws), "determinism")
        self.assertFalse(gate.passed)
        self.assertIn("shipped warehouse no longer matches its recorded census", gate.details)
        self.assertIn("primary", gate.details)
        # a MISSING shipped file is a failure too, never a skipped leg
        db.unlink()
        gate = gate_by_name(self.run_variant(V.TRANSFORM, workspace=ws), "determinism")
        self.assertFalse(gate.passed)
        self.assertIn("nothing to re-census", gate.details)

    def test_t_determinism_green_records_the_live_recensus(self) -> None:
        gate = gate_by_name(self.run_variant(V.TRANSFORM), "determinism")
        self.assertTrue(gate.passed, gate.details)
        for pop in P:
            self.assertEqual(
                gate.evidence[f"warehouse_recensus:{pop.value}"],
                gate.evidence[f"warehouse_census:{pop.value}"],
            )
        self.assertIn("live re-census", gate.details)

    def test_t_data_sensitivity_rejects_identical_warehouses_with_different_gold(self) -> None:
        ws = self.fresh_workspace()
        write_warehouse_census(ws, self.task, self.gold, shared_digest=True)
        gate = gate_by_name(
            self.run_variant(V.TRANSFORM, workspace=ws), "data-sensitivity"
        )
        self.assertFalse(gate.passed)
        self.assertIn("the T task is unsolvable", gate.details)

    def test_t_info_content_extends_the_row_floor_to_graded_populations(self) -> None:
        stage2 = {pop: dict(m) for pop, m in self.gold.stage2_csv.items()}
        header = stage2[P.STRESS.value][MART_NAME].splitlines()[0]
        stage2[P.STRESS.value][MART_NAME] = header + "\n"
        gold = self.gold.model_copy(update={"stage2_csv": stage2})
        gate = gate_by_name(self.run_variant(V.TRANSFORM, gold=gold), "info-content")
        self.assertFalse(gate.passed)
        self.assertIn("pays a no-op FULL credit", gate.details)

    def test_t_trusted_solution_red_on_ragged_gold(self) -> None:
        """A ragged gold CSV fails parse_canonical_csv — fail closed, not a pass."""
        stage2 = {pop: dict(m) for pop, m in self.gold.stage2_csv.items()}
        stage2[P.PRIMARY.value][MART_NAME] += "1,2\n"  # ragged row
        gold = self.gold.model_copy(update={"stage2_csv": stage2})
        gate = gate_by_name(
            self.run_variant(V.TRANSFORM, gold=gold), "trusted-solution"
        )
        self.assertFalse(gate.passed)
        self.assertIn("no frozen stage-2 gold for mart", gate.details)

    def test_t_trusted_solution_measures_a_reward_the_el_half_cannot(self) -> None:
        """The asymmetry the matrix records, demonstrated side by side.

        T's gold-vs-gold half RUNS: it parses the frozen bytes, sorts both sides
        into the mart's total order and compares vectors, so a mart's reward is
        a measured 1.0 that malformed gold can take away (previous test). EL's
        half cannot do any of that — compare_stage1 is handed the gold count map
        as BOTH sides, so its 1.0 is an identity, which is why the EL gate
        discards it instead of reporting it.
        """
        t_gate = gate_by_name(self.run_variant(V.TRANSFORM), "trusted-solution")
        self.assertTrue(t_gate.passed, t_gate.details)
        for pop in P:
            self.assertEqual(t_gate.evidence[f"reward:{pop.value}"], "1.000000")

        el_gate = gate_by_name(self.run_variant(V.EXTRACT_LOAD), "trusted-solution")
        self.assertTrue(el_gate.passed, el_gate.details)
        self.assertNotIn("reward:primary", el_gate.evidence)
        self.assertIn("DISCARDED", el_gate.evidence["gold_vs_gold"])


# ---------------------------------------------------------------------------
# A kill by CRASH is not evidence (V3)
# ---------------------------------------------------------------------------

class TestCrashKillsAreNotEvidence(VariantBatteryCase):
    def _el_case(self) -> AttackCase:
        return next(c for c in self.task.attack_cases if c.name == "partial_backend")

    def _t_case(self) -> AttackCase:
        return next(c for c in self.task.attack_cases if c.name == "inner_join")

    def test_el_required_mutant_killed_by_load_crash_is_not_evidence(self) -> None:
        ws = self.fresh_workspace()
        case = self._el_case()
        self.assertTrue(case.required)
        rewards = {c: dict(p) for c, p in self.rewards[V.EXTRACT_LOAD].items()}
        # Control: the honest record (errors {}) passes.
        gate = gates._gate_variant_required_mutants(
            self.task, rewards, V.EXTRACT_LOAD, workspace=ws
        )
        self.assertTrue(gate.passed, gate.details)
        self.assertEqual(gate.evidence[f"crash_check:{case.name}"], "kills are by the reward")
        self.assertIn("kills are by the reward, not by crashes", gate.details)
        # The same numbers, but the kill on primary was a coercion crash.
        write_attack_record(
            ws, self.task, case, self.rewards,
            errors={"primary/__load__": "ValueError: table 'x' column 'id': cannot coerce 'id' to bigint"},
        )
        gate = gates._gate_variant_required_mutants(
            self.task, rewards, V.EXTRACT_LOAD, workspace=ws
        )
        self.assertFalse(gate.passed)
        self.assertIn("LOAD CRASH", gate.details)
        self.assertIn("compare_stage1 never ran", gate.details)
        self.assertIn("LOAD CRASH", gate.evidence[f"crash_check:{case.name}"])
        # A load crash on a population that must KEEP full reward is not a
        # crash-kill (there is no kill there); it is the numeric leak instead.
        write_attack_record(
            ws, self.task, case, self.rewards,
            errors={"development/__load__": "ValueError: boom"},
        )
        rewards2 = {c: dict(p) for c, p in rewards.items()}
        gate = gates._gate_variant_required_mutants(
            self.task, rewards2, V.EXTRACT_LOAD, workspace=ws
        )
        # partial_backend expects False everywhere; the crash on development
        # IS a crash-kill and must be refused as evidence.
        self.assertFalse(gate.passed)
        self.assertIn("kill on development is by LOAD CRASH", gate.details)

    def test_t_required_mutant_killed_by_sql_crash_is_not_evidence(self) -> None:
        ws = self.fresh_workspace()
        case = self._t_case()
        write_attack_record(
            ws, self.task, case, self.rewards,
            errors={f"counterfactual/{MART_NAME}": "Catalog Error: Table with name no_such_table does not exist"},
        )
        rewards = {c: dict(p) for c, p in self.rewards[V.TRANSFORM].items()}
        gate = gates._gate_variant_required_mutants(
            self.task, rewards, V.TRANSFORM, workspace=ws
        )
        self.assertFalse(gate.passed)
        self.assertIn("SQL CRASH", gate.details)
        self.assertIn(MART_NAME, gate.details)
        self.assertIn("not a wrong implementation", gate.details)
        # partial_backend regression guard: a LOAD-kind case whose only errors
        # are mart-side (the transform crashed because a table is missing)
        # still passes EL — mart errors are ignored under EL by construction.
        el_case = self._el_case()
        write_attack_record(
            ws, self.task, el_case, self.rewards,
            errors={f"{p.value}/{MART_NAME}": "Catalog Error: missing table" for p in P},
        )
        el_rewards = {c: dict(p) for c, p in self.rewards[V.EXTRACT_LOAD].items()}
        gate = gates._gate_variant_required_mutants(
            self.task, el_rewards, V.EXTRACT_LOAD, workspace=ws
        )
        self.assertTrue(gate.passed, gate.details)
        # ...and load errors are ignored under T the same way.
        write_attack_record(
            ws, self.task, case, self.rewards,
            errors={"counterfactual/__load__": "ValueError: irrelevant to T"},
        )
        gate = gates._gate_variant_required_mutants(
            self.task, rewards, V.TRANSFORM, workspace=ws
        )
        self.assertTrue(gate.passed, gate.details)

    def test_crash_check_reads_only_records_at_the_current_hash(self) -> None:
        ws = self.fresh_workspace()
        case = self._t_case()
        write_attack_record(ws, self.task, case, self.rewards, content_hash="0" * 64)
        rewards = {c: dict(p) for c, p in self.rewards[V.TRANSFORM].items()}
        gate = gates._gate_variant_required_mutants(
            self.task, rewards, V.TRANSFORM, workspace=ws
        )
        self.assertFalse(gate.passed)
        self.assertIn("crash check impossible", gate.details)
        self.assertIn("STALE", gate.details)
        # a record with no `errors` field is not this runner's record either
        path = ws / "tasks" / self.task.task_id / "attacks" / case.name / "rewards.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["task_content_hash"] = self.task.content_hash()
        del record["errors"]
        path.write_text(json.dumps(record), encoding="utf-8")
        gate = gates._gate_variant_required_mutants(
            self.task, rewards, V.TRANSFORM, workspace=ws
        )
        self.assertFalse(gate.passed)
        self.assertIn("record has no errors field", gate.details)
        # without a workspace (the precheck path) the check is RECORDED as
        # skipped, never silently passed
        gate = gates._gate_variant_required_mutants(self.task, rewards, V.TRANSFORM)
        self.assertTrue(gate.passed, gate.details)
        self.assertEqual(gate.evidence["crash_check"], "skipped: no workspace")

    def test_t_required_mutants_records_the_semantic_count(self) -> None:
        gate = gate_by_name(self.run_variant(V.TRANSFORM), "required-mutants")
        self.assertTrue(gate.passed, gate.details)
        # inner_join / no_dedup / no_null_default are semantic; constants is not
        self.assertEqual(gate.evidence["t_semantic_mutants"], "3")
        self.assertEqual(gate.evidence["t_executable_semantic_mutants"], "3")
        constants_only = self.task.model_copy(
            update={
                "attack_cases": tuple(
                    c if c.kind is AttackKind.CONSTANTS or not c.required
                    else c.model_copy(update={"required": False, "expected_pass": {}})
                    for c in self.task.attack_cases
                )
            }
        )
        rewards = {c: dict(p) for c, p in self.rewards[V.TRANSFORM].items()}
        gate = gates._gate_variant_required_mutants(constants_only, rewards, V.TRANSFORM)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.evidence["t_semantic_mutants"], "0")
        self.assertEqual(gate.evidence["t_executable_semantic_mutants"], "0")
        self.assertIn("no executable semantic transform mutant", gate.details)

    def test_development_only_semantic_kill_is_not_executable_evidence(self) -> None:
        semantic = self._t_case().model_copy(
            update={"expected_pass": {P.DEVELOPMENT: False}}
        )
        task = self.task.model_copy(update={"attack_cases": (semantic,)})
        rewards = {
            semantic.name: {
                pop: (0.0 if pop is P.DEVELOPMENT else 1.0) for pop in P
            }
        }
        gate = gates._gate_variant_required_mutants(task, rewards, V.TRANSFORM)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.evidence["t_semantic_mutants"], "1")
        self.assertEqual(gate.evidence["t_executable_semantic_mutants"], "0")
        self.assertIn("no executable semantic transform mutant", gate.details)


# ---------------------------------------------------------------------------
# Census identity, gold leg, and the counter's independence (V4c)
# ---------------------------------------------------------------------------

class TestCensusIdentity(VariantBatteryCase):
    def test_el_census_red_when_gold_lacks_a_table_count(self) -> None:
        stage1 = {pop: dict(c) for pop, c in self.gold.stage1.items()}
        del stage1[P.PRIMARY.value]["orders"]
        gold = self.gold.model_copy(update={"stage1": stage1})
        ws = self.fresh_workspace()
        write_census(ws, self.task, self.gold)  # census counted orders fine
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws, gold=gold), "el-artifact-census"
        )
        self.assertFalse(gate.passed)
        self.assertIn("gold leg missing", gate.details)
        self.assertIn("primary/orders", gate.details)

    def test_el_census_red_on_foreign_counter_version(self) -> None:
        ws = self.fresh_workspace()
        write_census(ws, self.task, self.gold, counter_version="el-probes/0.9.0")
        report = self.run_variant(V.EXTRACT_LOAD, workspace=ws)
        gate = gate_by_name(report, "el-artifact-census")
        self.assertFalse(gate.passed)
        self.assertIn("counter version", gate.details)
        self.assertIn(el_probes.COUNTER_VERSION, gate.details)
        # the census reader is shared: EL trusted-solution cannot be certified
        # by another counter's record either
        self.assertFalse(gate_by_name(report, "trusted-solution").passed)

    def test_el_census_red_when_independence_claim_differs(self) -> None:
        ws = self.fresh_workspace()
        write_census(
            ws, self.task, self.gold,
            independent_of=list(el_probes.INDEPENDENT_OF)[:-1],
        )
        gate = gate_by_name(
            self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-artifact-census"
        )
        self.assertFalse(gate.passed)
        self.assertIn("independence claim differs", gate.details)

    def test_el_census_red_when_counter_independence_violated(self) -> None:
        # The claim is re-derived from el_probes' AST: declare independence
        # from a module the counter DOES import (json) and the gate goes red.
        ws = self.fresh_workspace()
        with unittest.mock.patch.object(
            el_probes, "INDEPENDENT_OF", el_probes.INDEPENDENT_OF + ("json",)
        ):
            write_census(ws, self.task, self.gold)  # records the (patched) claim
            gate = gate_by_name(
                self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-artifact-census"
            )
        self.assertFalse(gate.passed)
        self.assertIn("imports a module it declares independence from", gate.details)
        self.assertIn("json", gate.details)


# ---------------------------------------------------------------------------
# T: reward-equivalence, census version, transform surface, producer notes
# ---------------------------------------------------------------------------

class TestTransformSurfaceAndCurrency(VariantBatteryCase):
    def test_t_data_sensitivity_does_not_flag_reward_equivalent_gold_with_identical_census(self) -> None:
        # Two populations whose gold is byte-different but reward-EQUIVALENT
        # are solvable by one submission: an identical warehouse there is not
        # an "unsolvable" T task. (Base-gate redness for such a pair is
        # asserted separately in tests/test_gates_eval.py.)
        stage2 = {pop: dict(m) for pop, m in self.gold.stage2_csv.items()}
        cols, rows = upstream_eval.parse_canonical_csv(stage2[P.STRESS.value][MART_NAME])
        moved = [dict(r) for r in rows]
        for r in moved:
            if r.get("total_spend") not in (None, "0.0"):
                r["total_spend"] = repr(float(r["total_spend"]) * 1.005)
        stage2[P.DEVELOPMENT.value] = {MART_NAME: rows_to_canonical_csv(moved, cols)}
        self.assertNotEqual(stage2[P.DEVELOPMENT.value][MART_NAME], stage2[P.STRESS.value][MART_NAME])
        gold = self.gold.model_copy(update={"stage2_csv": stage2})
        ws = self.fresh_workspace()
        # give development and stress the SAME census digest
        path = ws / "tasks" / self.task.task_id / gates.WAREHOUSE_CENSUS_EVIDENCE_REL
        record = json.loads(path.read_text(encoding="utf-8"))
        record["populations"][P.DEVELOPMENT.value]["census_digest"] = (
            record["populations"][P.STRESS.value]["census_digest"]
        )
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        gate = gate_by_name(self.run_variant(V.TRANSFORM, workspace=ws, gold=gold), "data-sensitivity")
        self.assertNotIn("unsolvable", gate.details)
        # ...whereas the REAL development gold (reward-distinct from stress)
        # behind that shared census IS an unsolvable T task
        gate = gate_by_name(self.run_variant(V.TRANSFORM, workspace=ws), "data-sensitivity")
        self.assertFalse(gate.passed)
        self.assertIn("unsolvable", gate.details)

    def test_warehouses_load_rejects_a_stale_census_version(self) -> None:
        ws = self.fresh_workspace()
        write_warehouse_census(ws, self.task, self.gold, census_version="0")
        report = self.run_variant(V.TRANSFORM, workspace=ws)
        for name in ("warehouses-load", "determinism", "data-sensitivity"):
            gate = gate_by_name(report, name)
            self.assertFalse(gate.passed, name)
            self.assertIn("census_version", gate.details)
            self.assertIn("re-run validate-t", gate.details)
        # a record with NO census_version predates versioning ('1'); it is
        # only current while the census algorithm is still v1
        ws2 = self.fresh_workspace()
        path = ws2 / "tasks" / self.task.task_id / gates.WAREHOUSE_CENSUS_EVIDENCE_REL
        record = json.loads(path.read_text(encoding="utf-8"))
        del record["census_version"]
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        gate = gate_by_name(self.run_variant(V.TRANSFORM, workspace=ws2), "warehouses-load")
        self.assertEqual(gate.passed, str(eltbench.CENSUS_VERSION) == "1", gate.details)

    def test_transform_surface_is_in_the_t_roster_and_variant_local(self) -> None:
        self.assertIn("transform-surface", gates.VARIANT_GATE_NAMES[V.TRANSFORM])
        self.assertNotIn("transform-surface", gates.VARIANT_GATE_NAMES[V.EXTRACT_LOAD])
        self.assertNotIn("transform-surface", gates.GATE_NAMES)
        self.assertEqual(
            gates.classify_variant_failure(V.TRANSFORM, "transform-surface"),
            gates.FAILURE_VARIANT_LOCAL,
        )
        gates._check_matrix_wellformed()  # still well-formed with the new cell
        from elt_taskgen.generation.mart_plan import AGGREGATE_FAMILY_KINDS

        self.assertTrue(AGGREGATE_FAMILY_KINDS <= gates.TRANSFORM_SURFACE_KINDS)
        for kind in (MartOpKind.SOURCE, MartOpKind.DERIVE, MartOpKind.TIE_BREAK):
            self.assertNotIn(kind, gates.TRANSFORM_SURFACE_KINDS)

    def test_demo_task_passes_transform_surface(self) -> None:
        gate = gate_by_name(self.run_variant(V.TRANSFORM), "transform-surface")
        self.assertTrue(gate.passed, gate.details)
        self.assertIn("surface ops:", gate.evidence[MART_NAME])

    def test_projection_only_task_fails_transform_surface_variant_locally(self) -> None:
        projection = MartSpec(
            name="dim_customers",
            grain="one row per customer",
            key_columns=("customer_id",),
            columns=(
                MartColumn(name="customer_id", type=ColumnType.INTEGER, description="id"),
                MartColumn(name="name", type=ColumnType.TEXT, description="renamed"),
            ),
            plan=MartPlan(
                mart="dim_customers",
                ops=(
                    MartOp(kind=MartOpKind.SOURCE, description="customers", tables=("customers",)),
                    MartOp(kind=MartOpKind.DERIVE, description="rename", columns=("name",)),
                    MartOp(kind=MartOpKind.TIE_BREAK, description="order by id", columns=("customer_id",)),
                ),
            ),
        )
        task = self.task.model_copy(update={"marts": (projection,)})
        direct = gates._gate_transform_surface(task)
        self.assertFalse(direct.passed)
        self.assertIn("pure projection", direct.details)
        self.assertIn("PROJECTION ONLY", direct.evidence["dim_customers"])
        # through the battery: stamped variant-local, EL roster untouched
        report = gates.run_variant_gates(V.TRANSFORM, task, self.fresh_workspace(), self.gold, self.rewards)
        gate = gate_by_name(report, "transform-surface")
        self.assertFalse(gate.passed)
        self.assertEqual(gate.evidence["failure_scope"], gates.FAILURE_VARIANT_LOCAL)
        # one surfaced mart is enough
        two = self.task.model_copy(update={"marts": (projection,) + self.task.marts})
        self.assertTrue(gates._gate_transform_surface(two).passed)

    def test_el_independent_load_red_detail_names_the_transport_from_a_bound_note(self) -> None:
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.EL_INDEPENDENT_LOAD_EVIDENCE_REL).unlink()
        notes_path = ws / "tasks" / self.task.task_id / gates.EL_EVIDENCE_NOTES_REL
        notes_path.write_text(
            json.dumps(
                {
                    "task_id": self.task.task_id,
                    "task_content_hash": self.task.content_hash(),
                    "notes": [
                        "independent LOAD build not performed: MissingCredentialsError: "
                        "ELT_TASKGEN_OSS_BASE_URL is not configured (searched: /a/licenses/b)"
                    ],
                }
            ),
            encoding="utf-8",
        )
        gate = gate_by_name(self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-independent-load")
        self.assertFalse(gate.passed)
        self.assertIn("the independent LOAD build was never performed", gate.details)
        self.assertIn("MissingCredentialsError", gate.details)
        self.assertNotIn("searched:", gate.details)
        self.assertNotIn("licens", gate.details)
        self.assertIn("MissingCredentialsError", gate.evidence["producer_note"])
        # stale note: ignored (detail unchanged, gate still red)
        notes_path.write_text(
            json.dumps({"task_id": self.task.task_id, "task_content_hash": "0" * 64,
                        "notes": ["independent LOAD build not performed: X"]}),
            encoding="utf-8",
        )
        gate = gate_by_name(self.run_variant(V.EXTRACT_LOAD, workspace=ws), "el-independent-load")
        self.assertFalse(gate.passed)
        self.assertNotIn("producer:", gate.details)


# ---------------------------------------------------------------------------
# Inherited gates are still gates
# ---------------------------------------------------------------------------

class TestInheritedGates(VariantBatteryCase):
    def test_contamination_is_re_checked_in_every_variant_report(self) -> None:
        ws = self.fresh_workspace()
        path = ws / "tasks" / self.task.task_id / gates.CONTAMINATION_POST_EVIDENCE_REL
        path.write_text(
            json.dumps(
                {
                    "task_id": self.task.task_id,
                    "task_content_hash": "0" * 64,
                    "collisions": [],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        for variant in (V.EXTRACT_LOAD, V.TRANSFORM):
            report = self.run_variant(variant, workspace=ws)
            gate = gate_by_name(report, "contamination-clean")
            self.assertFalse(gate.passed, f"{variant.value} inherited a stale scan")
            self.assertIn("STALE", gate.details)
            self.assertFalse(report.accepted)

    def test_t_inherits_the_dual_build_as_its_own_gate_result(self) -> None:
        ws = self.fresh_workspace()
        (ws / "tasks" / self.task.task_id / gates.DUAL_BUILD_EVIDENCE_REL).unlink()
        report = self.run_variant(V.TRANSFORM, workspace=ws)
        self.assertFalse(gate_by_name(report, "dual-build-agreement").passed)
        self.assertFalse(gate_by_name(report, "trusted-solution").passed)
        self.assertFalse(report.accepted)


# ---------------------------------------------------------------------------
# Proof printer
# ---------------------------------------------------------------------------

def _report_text() -> str:
    task = task_with_el_mutant()
    plain = demo_fixture.demo_task()
    gold = build_gold(task)
    plain_gold = build_gold(plain)
    rewards = compute_rewards_by_variant(task, gold)
    plain_rewards = compute_rewards_by_variant(plain, plain_gold)
    tmp = Path(tempfile.mkdtemp(prefix="variant-gate-proof-"))
    lines: list[str] = []
    try:
        for label, t, g, r in (
            ("DEMO TASK AS SHIPPED (4 required cases, all TRANSFORM kinds)",
             plain, plain_gold, plain_rewards),
            ("DEMO TASK + one required EXTRACTION mutant (skip_extraction)",
             task, gold, rewards),
        ):
            ws = Path(tempfile.mkdtemp(dir=tmp))
            write_workspace(ws, t, g)
            lines.append("=" * 78)
            lines.append(label)
            lines.append("=" * 78)
            reports = gates.run_all_variant_gates(t, ws, g, r)
            for variant, report in reports.items():
                lines.append("")
                lines.append(
                    f"--- {variant.value.upper()}  task_id={report.task_id}  "
                    f"accepted={report.accepted}  "
                    f"gates={len(report.gates)}"
                )
                for res in report.gates:
                    cell = gates.variant_applicability(variant, res.gate)
                    verdict = cell.verdict.value if cell else "?"
                    flag = "PASS" if res.passed else "FAIL"
                    cls = res.evidence.get("failure_class", "")
                    lines.append(
                        f"  [{flag}] {res.gate:<22} {verdict:<18}"
                        + (f" ({cls})" if cls else "")
                    )
                    if not res.passed:
                        lines.append(f"         -> {res.details[:150]}")
                na = [
                    c
                    for c in gates.VARIANT_GATE_MATRIX[variant]
                    if c.verdict is gates.VariantGateVerdict.NOT_APPLICABLE
                ]
                for cell in na:
                    lines.append(
                        f"  [ N/A] {cell.gate:<22} not-applicable     "
                        f"-> replaced by {', '.join(cell.replaced_by)}"
                    )
                    lines.append(f"         reason: {cell.reason[:220]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "\n".join(lines)


if __name__ == "__main__":
    if "--report" in sys.argv:
        print(_report_text())
    else:
        unittest.main()

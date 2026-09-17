"""Run references in clean environments and verify byte-level determinism."""

from __future__ import annotations

from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict

from elt_taskgen.models import (
    GateResult,
    MartSpec,
    PopulationName,
    Row,
    TaskIR,
    canonical_json,
    sha256_hex,
)
from elt_taskgen.reference.duckdb_sandbox import (
    assert_sandboxed,
    current_scope_memory_limit_mb,
    register_interruptible,
)
from elt_taskgen.reference.solution import (
    _insert_rows,
    _read_jsonl,
    build_reference,
    create_table,
    execute_mart,
    find_rendered_artifact,
    load_sources_duckdb,
)
from elt_taskgen.sql_identifiers import quote_sql_identifier
from elt_taskgen.verification.upstream_eval import (
    rows_to_canonical_csv,
    sort_rows,
)


class RunResult(BaseModel):
    """One population's trusted-reference outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    population: PopulationName
    #: table -> row count actually loaded by the trusted E+L.
    stage1_counts: dict[str, int]
    #: mart -> rows in gold (total) order.
    mart_rows: dict[str, list[Row]]


# Workspace paths

def population_dir(workspace: Path, task_id: str, population: PopulationName) -> Path:
    return workspace / "tasks" / task_id / "populations" / population.value


def rendered_dir(workspace: Path, task_id: str, population: PopulationName) -> Path:
    return population_dir(workspace, task_id, population) / "rendered"


# Ordering + canonical CSV (the reward implementation is the source of truth)

def sort_mart_rows(rows: list[Row], mart: MartSpec) -> list[Row]:
    """Sort rows into the gold TOTAL order (keys first, then every column)."""
    columns = tuple(c.name for c in mart.columns)
    return sort_rows(list(rows), tuple(mart.key_columns), columns)


def mart_rows_to_csv(rows: list[Row], mart: MartSpec) -> str:
    """Canonical CSV text (header + rows) for one mart's ordered output."""
    columns = tuple(c.name for c in mart.columns)
    return rows_to_canonical_csv(list(rows), columns)


# Smoke check + clean-environment execution

def _count_jsonl_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def smoke_check(task: TaskIR, population: PopulationName, workspace: Path) -> None:
    """Verify source artifacts and any frozen row-count ground truth."""
    rdir = rendered_dir(workspace, task.task_id, population)
    if not rdir.is_dir():
        raise FileNotFoundError(
            f"population {population.value!r}: rendered dir missing: {rdir}"
        )
    for table in task.tables:
        find_rendered_artifact(task, rdir, table.name)


def _expected_row_counts(
    task: TaskIR, population: PopulationName, workspace: Path
) -> dict[str, int]:
    """Row counts from the frozen rows/<table>.jsonl artifacts, when present."""
    rows_dir = population_dir(workspace, task.task_id, population) / "rows"
    expected: dict[str, int] = {}
    if rows_dir.is_dir():
        for table in task.tables:
            path = rows_dir / f"{table.name}.jsonl"
            if path.exists():
                expected[table.name] = _count_jsonl_rows(path)
    return expected


#: Prefix of the throwaway tables the content cross-check uses (dropped after).
_FROZEN_TABLE_PREFIX = "__frozen_rows__"


def _frozen_content_divergences(
    con: duckdb.DuckDBPyConnection,
    task: TaskIR,
    population: PopulationName,
    workspace: Path,
) -> dict[str, tuple[int, str]]:
    """Return loaded/frozen multiset differences by table.

    Comparison uses loader coercion and skips tables without frozen JSONL.
    """
    rows_dir = population_dir(workspace, task.task_id, population) / "rows"
    if not rows_dir.is_dir():
        return {}
    divergences: dict[str, tuple[int, str]] = {}
    for table in task.tables:
        path = rows_dir / f"{table.name}.jsonl"
        if not path.exists():
            continue
        frozen_name = f"{_FROZEN_TABLE_PREFIX}{table.name}"
        frozen_spec = table.model_copy(update={"name": frozen_name})
        loaded_rel = quote_sql_identifier(table.name, force=True)
        frozen_rel = quote_sql_identifier(frozen_name, force=True)
        create_table(con, frozen_spec)
        try:
            _insert_rows(con, frozen_spec, _read_jsonl(path))
            diff = (
                f"(SELECT * FROM {loaded_rel} EXCEPT ALL SELECT * FROM {frozen_rel}) "
                f"UNION ALL "
                f"(SELECT * FROM {frozen_rel} EXCEPT ALL SELECT * FROM {loaded_rel})"
            )
            (count,) = con.execute(f"SELECT COUNT(*) FROM ({diff})").fetchone()
            if int(count):
                order = ", ".join(str(i) for i in range(1, len(table.columns) + 1))
                example = con.execute(
                    f"SELECT * FROM ({diff}) ORDER BY {order} NULLS LAST LIMIT 1"
                ).fetchone()
                divergences[table.name] = (
                    int(count),
                    repr(dict(zip((c.name for c in table.columns), example, strict=True))),
                )
        finally:
            con.execute(f"DROP TABLE {frozen_rel}")
    return divergences


def _pin_session(con: duckdb.DuckDBPyConnection) -> None:
    """Pin ordering and single-thread execution before configuration is locked."""
    con.execute("SET default_collation = 'binary'")
    con.execute("SET default_null_order = 'NULLS_LAST'")
    con.execute("SET threads = 1")


def open_reference_connection() -> duckdb.DuckDBPyConnection:
    """Open the pinned, sandboxed connection used for gold-producing runs.

    An active interruptible scope supplies its memory limit and registration.
    """
    con = duckdb.connect(":memory:")
    _pin_session(con)
    memory_limit_mb = current_scope_memory_limit_mb()
    if memory_limit_mb is not None:
        con.execute(f"SET memory_limit='{int(memory_limit_mb)}MiB'")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    assert_sandboxed(con)
    register_interruptible(con)
    return con


def run_reference(
    task: TaskIR, population: PopulationName, workspace: Path
) -> RunResult:
    """Run trusted E+L and transforms for one population in memory.

    Stage-1 data is checked against frozen rows; mart gold uses the strict typed
    projection, including ``DECIMAL(38,9)`` inputs.
    """
    smoke_check(task, population, workspace)
    rdir = rendered_dir(workspace, task.task_id, population)
    reference = build_reference(task)
    missing_sql = [m.name for m in task.marts if m.name not in reference.sql_by_mart]
    if missing_sql:
        raise ValueError(f"reference solution has no SQL for marts: {missing_sql}")

    con = open_reference_connection()
    try:
        loaded = load_sources_duckdb(task, rdir, con)
        stage1_counts = {t.name: loaded.counts[t.name] for t in task.tables}

        expected = _expected_row_counts(task, population, workspace)
        mismatched = {
            t: (expected[t], stage1_counts[t])
            for t in expected
            if expected[t] != stage1_counts[t]
        }
        if mismatched:
            raise ValueError(
                f"population {population.value!r}: loaded row counts diverge from "
                f"frozen generated rows (table: expected, loaded): {mismatched}"
            )
        diverged = _frozen_content_divergences(con, task, population, workspace)
        if diverged:
            raise ValueError(
                f"population {population.value!r}: loaded contents diverge from "
                "frozen generated rows (table: n_mismatched_rows, first example): "
                f"{diverged}"
            )

    finally:
        con.close()

    # Compute mart gold with the strict loader to preserve declared decimals.
    from elt_taskgen.verification.strict_diagnostic import (
        load_sources_duckdb_strict,
    )

    strict_con = open_reference_connection()
    try:
        strict_counts = load_sources_duckdb_strict(task, rdir, strict_con)
        if strict_counts != stage1_counts:
            raise ValueError(
                f"population {population.value!r}: strict reference load counts "
                f"diverge from compatibility load counts (compatibility "
                f"{stage1_counts}, strict {strict_counts})"
            )
        mart_rows: dict[str, list[Row]] = {}
        for mart in task.marts:
            rows = execute_mart(
                strict_con, mart, reference.sql_by_mart[mart.name]
            )
            mart_rows[mart.name] = sort_mart_rows(rows, mart)
    finally:
        strict_con.close()
    return RunResult(
        population=population, stage1_counts=stage1_counts, mart_rows=mart_rows
    )


# Determinism evidence: destroy, rebuild, rerun N, byte-compare

class RunDigests(BaseModel):
    """Digests for stage-1 counts, stage-2 CSVs, and their joint surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage1: str
    stage2: str
    joint: str


#: Bumped when a digest INPUT or its layout changes; hashed in, so two layouts
#: can never collide silently.
DIGEST_LAYOUT_VERSION = 1

#: Evidence keys the recorded determinism GateResult carries; gates.py reads
#: them BY NAME. Declared here (not imported) because gates imports this module.
DETERMINISM_STAGE1_DIGEST_KEY = "stage1_digest"
DETERMINISM_STAGE2_DIGEST_KEY = "stage2_digest"
#: Whole-run digest: the FULL variant legitimately cites both stages at once.
DETERMINISM_JOINT_DIGEST_KEY = "run_digest"


def stage1_digest_from_counts(population: str, counts: dict[str, int]) -> str:
    """Hash the population name and canonical per-table counts."""
    payload = {
        "kind": "stage1-counts",
        "version": DIGEST_LAYOUT_VERSION,
        "population": population,
        "tables": {str(table): int(count) for table, count in counts.items()},
    }
    return sha256_hex(canonical_json(payload))


def stage2_digest_from_csv(population: str, csv_by_mart: dict[str, str]) -> str:
    """Hash mart CSV outputs in framed, sorted name order."""
    header = canonical_json(
        {
            "kind": "stage2-marts",
            "version": DIGEST_LAYOUT_VERSION,
            "population": population,
            "marts": sorted(csv_by_mart),
        }
    )
    parts = [header]
    for mart_name in sorted(csv_by_mart):
        parts.append(f"--- mart:{mart_name} ---")
        parts.append(csv_by_mart[mart_name])
    return sha256_hex("\n".join(parts))


def joint_digest_from_parts(counts: dict[str, int], csv_by_mart: dict[str, str]) -> str:
    """Hash canonical counts followed by sorted mart CSVs."""
    parts = [canonical_json(counts)]
    for mart_name in sorted(csv_by_mart):
        parts.append(f"--- mart:{mart_name} ---")
        parts.append(csv_by_mart[mart_name])
    return sha256_hex("\n".join(parts))


def _run_csv_by_mart(task: TaskIR, result: RunResult) -> dict[str, str]:
    return {
        mart.name: mart_rows_to_csv(result.mart_rows[mart.name], mart)
        for mart in task.marts
    }


def run_digests(task: TaskIR, result: RunResult) -> RunDigests:
    """The stage-1, stage-2 and joint digests of one run's outputs."""
    counts = {t.name: int(result.stage1_counts[t.name]) for t in task.tables}
    csv_by_mart = _run_csv_by_mart(task, result)
    population = result.population.value
    return RunDigests(
        stage1=stage1_digest_from_counts(population, counts),
        stage2=stage2_digest_from_csv(population, csv_by_mart),
        joint=joint_digest_from_parts(counts, csv_by_mart),
    )


def determinism_evidence(
    task: TaskIR,
    population: PopulationName,
    workspace: Path,
    *,
    runs: int = 3,
) -> GateResult:
    """Rebuild at least twice and report stable component digests only.

    Any execution error or component mismatch fails the evidence.
    """
    evidence = {"population": population.value, "runs": str(runs)}
    if runs < 2:
        return GateResult(
            gate="determinism",
            passed=False,
            details=f"determinism requires at least 2 runs, got {runs}",
            evidence=evidence,
        )
    per_component: dict[str, list[str]] = {"joint": [], "stage1": [], "stage2": []}
    try:
        for i in range(runs):
            result = run_reference(task, population, workspace)
            digests = run_digests(task, result)
            per_component["joint"].append(digests.joint)
            per_component["stage1"].append(digests.stage1)
            per_component["stage2"].append(digests.stage2)
            evidence[f"run_{i}_sha256"] = digests.joint
            evidence[f"run_{i}_stage1_sha256"] = digests.stage1
            evidence[f"run_{i}_stage2_sha256"] = digests.stage2
    except Exception as exc:  # fail closed: missing evidence is a failed gate
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        return GateResult(
            gate="determinism",
            passed=False,
            details=(
                f"population {population.value!r}: run failed before "
                f"{runs} determinism runs completed"
            ),
            evidence=evidence,
        )

    stable = {name: len(set(seen)) == 1 for name, seen in per_component.items()}
    # Only a component that held still across every rebuild gets published as
    # a citable digest; the rest are named as divergences.
    if stable["stage1"]:
        evidence[DETERMINISM_STAGE1_DIGEST_KEY] = per_component["stage1"][0]
    if stable["stage2"]:
        evidence[DETERMINISM_STAGE2_DIGEST_KEY] = per_component["stage2"][0]
    if stable["joint"]:
        evidence[DETERMINISM_JOINT_DIGEST_KEY] = per_component["joint"][0]

    diverged = [name for name in ("stage1", "stage2") if not stable[name]]
    if not stable["joint"] and not diverged:
        # Cannot happen unless the joint layout stops being a function of the
        # two components; say so rather than reporting a clean run.
        diverged = ["joint"]
    identical = stable["joint"] and not diverged
    if identical:
        details = (
            f"{runs} destroy/rebuild/rerun cycles on population "
            f"{population.value!r}: outputs byte-identical "
            f"(stage1 and stage2 components each stable and recorded separately)"
        )
    else:
        evidence["diverged_components"] = ",".join(diverged)
        details = (
            f"{runs} destroy/rebuild/rerun cycles on population "
            f"{population.value!r}: outputs DIVERGED in component(s) "
            + ", ".join(diverged)
        )
    return GateResult(
        gate="determinism",
        passed=identical,
        details=details,
        evidence=evidence,
    )

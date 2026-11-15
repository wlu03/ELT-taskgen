"""Generate source populations, freeze references, and apply deterministic intake filters."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from elt_taskgen.engine import (
    Engine,
    EngineError,
    InfrastructureFailure,
    StageName,
    StageOutcome,
    StagePayload,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
    FINAL_REJECTED,
    variant_gate_stage,
)
from elt_taskgen.models import (
    PopulationName,
    RepairRoute,
    RLVR_TASK_VARIANTS,
    TaskIR,
    TaskStatus,
    canonical_json,
    sha256_hex,
)
from elt_taskgen.pipeline import payloads as _pipeline_payloads
from elt_taskgen.pipeline import support as _pipeline_support


class _CollisionLike(Protocol):
    """Structural type shared by every adapter's contamination result."""

    kind: str
    against: str
    detail: str
    fatal: bool


def _collision_fingerprint(collision: dict) -> str:
    """Stable id of ONE recorded contamination collision: sha256 over the canonical
    {kind, against, detail}. Approvals bind to these ids, so sign-off coverage is
    checked mechanically, never by prose matching."""
    return sha256_hex(
        canonical_json(
            {
                "against": collision.get("against"),
                "detail": collision.get("detail"),
                "kind": collision.get("kind"),
            }
        )
    )


def _coverage_payload(engine: Engine, coverage) -> dict:
    """IndexCoverage as JSON, with `index_dir` made WORKSPACE-RELATIVE.

    Nothing reads the value (it is provenance), but the persisted absolute paths
    outlived the directories they named. A path outside the workspace is left
    absolute rather than guessed at."""
    dumped = coverage.model_dump(mode="json")
    raw = dumped.get("index_dir")
    if isinstance(raw, str) and raw:
        try:
            dumped["index_dir"] = _pipeline_support._workspace_rel(engine, Path(raw))
        except ValueError:
            pass  # not under this workspace: leave it as recorded
    return dumped


def _contamination_index(engine: Engine):
    from elt_taskgen.verification.contamination import ContaminationIndex

    return ContaminationIndex(engine.workspace / "state" / "contamination")


def _seed_embedded_deny_lists(idx) -> None:
    """Arm an empty index with the embedded benchmark family deny lists.

    SEEDING IS NOT ARMING: this writes benchmark family NAMES and nothing
    structural, so the index answers "is this task NAMED like a benchmark?".
    Callers go through `_arm_contamination_index`, which says so out loud."""
    from elt_taskgen.verification import contamination as cont

    for name, families in (
        ("eltbench", cont.ELTBENCH_FAMILIES),
        ("spider2_dbt", cont.SPIDER2_DBT_FAMILIES),
        ("ade_bench", cont.ADE_BENCH_FAMILIES),
    ):
        idx.add_benchmark(
            name, sorted({f"family:{cont.normalize_name(f)}" for f in families})
        )


def _arm_contamination_index(idx, *, announce: bool = True):
    """Seed an empty index, then REPORT what the firewall actually contains.

    `is_armed()` is satisfied by this function's own seeding, so it proves nothing
    — hence the coverage banner, and hence every caller records the returned
    IndexCoverage beside its collisions. WARNS rather than refuses by default (the
    tests have no benchmark checkout); `ELT_TASKGEN_REQUIRE_FIREWALL=1` refuses."""
    if not idx.is_armed():
        _seed_embedded_deny_lists(idx)
    coverage = idx.coverage()
    if announce and not coverage.is_firewall:
        print(f"  {coverage.summary()}")
    return coverage


def _required_firewall_coverage():
    """Minimum contamination coverage this process REFUSES below, or None."""
    from elt_taskgen.verification import contamination as cont

    return cont.required_coverage_from_env()


# --- Stage runners (engine.run executes these; cf. docs/INTERFACES.md) ---

def run_contamination_pre(engine: Engine, task: TaskIR) -> StageOutcome:
    """`contamination_pre`: pre-generation scan (fatal collision -> reject)."""
    idx = _contamination_index(engine)
    seeded = not idx.is_armed()
    _arm_contamination_index(idx, announce=False)
    result = idx.scan_pre(task, require=_required_firewall_coverage())
    fatal = [c for c in result.collisions if c.fatal]
    payload = _pipeline_payloads.ContaminationPayload(
        call_point="pre",
        collisions=tuple(c.model_dump(mode="json") for c in result.collisions),
        fatal_count=len(fatal),
        seeded_embedded_deny_lists=seeded,
        coverage_level=result.coverage.level.value,
        coverage=_coverage_payload(engine, result.coverage),
        detail=(
            result.detail()
            + ("; index seeded with embedded deny lists" if seeded else "")
        ),
    )
    if fatal:
        return StageOutcome(VERDICT_FATAL, payload)
    return StageOutcome(VERDICT_PASS, payload)


#: Generation scratch is a sibling excluded from repair fingerprints after crashes.
_REBUILD_SCRATCH_DIR = ".rebuild"
_REBUILD_TMP_SUFFIX = ".rebuild-tmp"
_REBUILD_STALE_SUFFIX = ".stale"


def _drift_lines(derived: dict[str, str], on_disk: dict[str, str]) -> tuple[str, ...]:
    """Human-readable difference between a fresh derivation and what is on disk."""
    lines: list[str] = []
    for rel in sorted(set(derived) | set(on_disk)):
        if rel not in on_disk:
            lines.append(f"{rel}: missing on disk")
        elif rel not in derived:
            lines.append(f"{rel}: not derived from the IR")
        elif derived[rel] != on_disk[rel]:
            lines.append(f"{rel}: differs")
    return tuple(lines)


def run_generate(engine: Engine, task: TaskIR) -> StageOutcome:
    """Materialize all IR populations and replace only byte-different artifacts."""
    from elt_taskgen.generation import populations as populations_mod
    from elt_taskgen.generation import source_data

    problems = populations_mod.validate_population_coverage(task)
    if problems:
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error="population coverage: " + "; ".join(problems)),
            route=RepairRoute.POPULATION,
        )

    tdir = engine.task_dir(task.task_id)
    scratch = tdir / _REBUILD_SCRATCH_DIR
    # Whatever a previous crash left behind goes first, including the pre-fix
    # in-place scratch trees under populations/.
    shutil.rmtree(scratch, ignore_errors=True)
    for legacy in sorted((tdir / "populations").glob("*")):
        if legacy.is_dir() and legacy.name.endswith(
            (_REBUILD_TMP_SUFFIX, _REBUILD_STALE_SUFFIX)
        ):
            shutil.rmtree(legacy, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    built: list[str] = []
    reused: list[str] = []
    data: dict[str, str] = {}
    for pop_spec in sorted(task.populations, key=lambda p: p.name.value):
        pop = pop_spec.name
        pop_dir = tdir / "populations" / pop.value
        tmp_dir = scratch / (pop.value + _REBUILD_TMP_SUFFIX)
        stale_dir = scratch / (pop.value + _REBUILD_STALE_SUFFIX)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(stale_dir, ignore_errors=True)
        existed = pop_dir.is_dir()
        hashes = source_data.materialize_population(task, pop, tmp_dir)
        drift = _drift_lines(
            source_data.tree_digest(tmp_dir), source_data.tree_digest(pop_dir)
        )
        if not drift:
            # Byte-identical: leave the pinned tree alone, so artifact hashes and
            # repair fingerprints do not move just because the stage re-attested.
            shutil.rmtree(tmp_dir, ignore_errors=True)
            reused.append(pop.value)
            continue
        # Atomic swap, so a crash can never leave a half-written population. It
        # also DELETES files the derivation no longer produces.
        pop_dir.parent.mkdir(parents=True, exist_ok=True)
        if pop_dir.exists():
            pop_dir.rename(stale_dir)
        tmp_dir.rename(pop_dir)
        shutil.rmtree(stale_dir, ignore_errors=True)
        for table_name in sorted(hashes):
            engine.record_artifact(
                task,
                f"tasks/{task.task_id}/populations/{pop.value}/rows/{table_name}.jsonl",
                hashes[table_name],
            )
        built.append(pop.value)
        if existed:
            # WHY it rebuilt, in the ledger — 'reused' is unfalsifiable without it.
            # Only for a population that WAS there.
            data[f"drift:{pop.value}"] = "; ".join(drift[:6]) + (
                f" (+{len(drift) - 6} more)" if len(drift) > 6 else ""
            )
    shutil.rmtree(scratch, ignore_errors=True)
    data.update({"built": ",".join(built), "reused": ",".join(reused)})
    data["verified"] = "rederived"
    return StageOutcome(
        VERDICT_PASS,
        StagePayload(
            detail=(
                f"populations re-derived from the IR: built={built or '[]'} "
                f"reused(byte-identical)={reused or '[]'}"
            ),
            data=data,
        ),
    )


def _population_drift_failure(engine: Engine, task: TaskIR) -> StageOutcome | None:
    """A stage FAILURE when any population on disk is not the IR's derivation.

    The populations are the graded EL surface and the input the gold was frozen
    from, and the content hash covers the IR only — so this recomputation is the
    check (one edited rendered/ file otherwise passed everything while the true
    reference scored 0.0). Routed RUNTIME, which re-runs `generate`."""
    from elt_taskgen.generation import source_data

    populations_root = engine.task_dir(task.task_id) / "populations"
    if not populations_root.is_dir():
        # NOTHING MATERIALIZED AT ALL is `generate`'s business: the batteries
        # already fail closed on absent sources. This check is for what the content
        # hash cannot see — a tree that EXISTS but is no longer the IR's derivation.
        return None
    problems: list[str] = []
    for pop_spec in sorted(task.populations, key=lambda p: p.name.value):
        drift = source_data.population_drift(
            task, pop_spec.name, populations_root / pop_spec.name.value
        )
        if drift:
            shown = "; ".join(drift[:4]) + (
                f" (+{len(drift) - 4} more)" if len(drift) > 4 else ""
            )
            problems.append(f"{pop_spec.name.value}: {shown}")
    if not problems:
        return None
    return StageOutcome(
        VERDICT_FAIL,
        StagePayload(
            error=(
                "population artifacts drift: what is on disk is not what the "
                "IR derives — " + " | ".join(problems) + ". Re-run `generate` "
                "(it re-derives and rewrites); the gold and every battery must "
                "then re-attest against the restored bytes."
            ),
            data={"drifted_populations": ",".join(p.split(":")[0] for p in problems)},
        ),
        route=RepairRoute.RUNTIME,
    )


def _ensure_perturbation_probe(engine: Engine, task: TaskIR, gold) -> None:
    """Record the data-sensitivity scratch probe when the gate will demand it.

    Only a 'provided-rows' task needs one, and cannot pass data-sensitivity without
    it. Bound to the current content hash (a stale record is rejected); the probe
    never writes the pinned population artifacts."""
    from elt_taskgen.verification import gates as gates_mod
    from elt_taskgen.verification import perturbation as perturb_mod

    if not gates_mod.needs_perturbation_probe(task, engine.workspace):
        return
    perturb_mod.record_perturbation_probe(task, engine.workspace, gold)


def run_reference_stage(engine: Engine, task: TaskIR) -> StageOutcome:
    """`reference`: private reference execution + determinism evidence + gold freeze."""
    from elt_taskgen.models import GateResult
    from elt_taskgen.reference import gold as gold_mod
    from elt_taskgen.reference import runner as runner_mod
    from elt_taskgen.verification import gates as gates_mod

    akd = _pipeline_support._answer_key_dir(engine, task)
    det_path = _pipeline_support._evidence_dir(engine, task) / "determinism.json"
    current = task.content_hash()

    # Resume shortcut: gold already frozen AT THIS EXACT identity, with the
    # determinism record bound to it. An unbound or stale record is rebuilt instead.
    if det_path.is_file() and (akd / gold_mod.MANIFEST_FILENAME).is_file():
        try:
            frozen = gold_mod.load_gold(akd)
            recorded = GateResult.model_validate_json(
                det_path.read_text(encoding="utf-8")
            )
            det_bound = (
                recorded.passed
                and recorded.evidence.get("task_id") == task.task_id
                and recorded.evidence.get(gates_mod.DETERMINISM_CONTENT_HASH_KEY)
                == current
            )
            if (
                det_bound
                and frozen.task_id == task.task_id
                and frozen.task_content_hash == current
            ):
                # RESUMING MEANS "the gold on disk is still the right answer to the
                # data on disk", which the content hash cannot say — so the
                # populations are re-derived and byte-compared first.
                drifted = _population_drift_failure(engine, task)
                if drifted is not None:
                    return drifted
                # The probe is bound to the same identity, so the resume path must
                # (re)produce it or resuming drops the gate's only evidence.
                _ensure_perturbation_probe(engine, task, frozen)
                return StageOutcome(
                    VERDICT_PASS,
                    StagePayload(detail="gold already frozen at current content hash"),
                )
        except Exception:
            pass  # stale/corrupt gold: fall through and rebuild (fail closed)

    results = {}
    for pop_spec in task.populations:
        results[pop_spec.name] = runner_mod.run_reference(
            task, pop_spec.name, engine.workspace
        )

    det = runner_mod.determinism_evidence(
        task, PopulationName.PRIMARY, engine.workspace, runs=3
    )
    det = det.model_copy(
        update={
            "evidence": {
                **det.evidence,
                "task_id": task.task_id,
                gates_mod.DETERMINISM_CONTENT_HASH_KEY: current,
            }
        }
    )
    det_path.write_text(
        canonical_json(det.model_dump(mode="json")), encoding="utf-8"
    )
    if not det.passed:
        # 'determinism' keyword routes RUNTIME via repair.route_for_failure.
        return StageOutcome(VERDICT_FAIL, det)

    gold = gold_mod.freeze_gold(task, results, akd)
    _ensure_perturbation_probe(engine, task, gold)
    for rel in sorted(gold.file_hashes):
        engine.record_artifact(
            task,
            f"tasks/{task.task_id}/answer_key/{rel}",
            gold.file_hashes[rel],
        )
    return StageOutcome(
        VERDICT_PASS,
        StagePayload(
            detail=f"gold frozen for {len(results)} population(s); determinism x3 identical",
            data={"gold_files": str(len(gold.file_hashes))},
        ),
    )


def _attack_matrix_failure_detail(engine: Engine, task: TaskIR) -> str | None:
    """Deterministic attack-matrix precondition on live spend.

    `expected_pass` is a claim about data the adapter never measured, and executing
    a mutant needs no provider (deterministic DuckDB against the gold `reference`
    froze a stage earlier), so it is checked at the earliest point the data allows
    rather than at `gates`, two live stages later. The comparison IS the gate's own
    on a subset of the cases, so it can only move a refusal EARLIER and never
    invent one. Returns the detail, or None — including when gold is absent, which
    is NOT a certification. Routes to POPULATION."""
    from elt_taskgen.verification import attack_matrix

    # Duck-typed stand-ins (the stage-logic unit tests drive these runners with no
    # workspace on disk) carry neither accessor: no measurement, never a refusal.
    workspace = getattr(engine, "workspace", None)
    task_dir = getattr(engine, "task_dir", None)
    if workspace is None or task_dir is None:
        return None
    return attack_matrix.check_declared_matrix_if_measurable(
        task, _pipeline_support._answer_key_dir(engine, task), workspace
    )


# Deterministic pre-council filters run before provider calls. Every result is
# recorded and either routes for repair or rejects according to configuration.

def _filter_config():
    from elt_taskgen.verification import filters as filters_mod

    return filters_mod.load_filter_config()


def _record_filter_report(engine: Engine, task: TaskIR, report) -> str:
    """Write + ledger-record one filter artifact; returns its workspace path."""
    from elt_taskgen.verification import filters as filters_mod

    path, sha = filters_mod.write_filter_report(report, _pipeline_support._evidence_dir(engine, task))
    rel = _pipeline_support._workspace_rel(engine, path)
    engine.record_artifact(task, rel, sha)
    return rel


def _filter_outcome(report, rel_path: str) -> StageOutcome:
    """Turn a tripped filter into the configured stage outcome (never a pass)."""
    from elt_taskgen.verification import filters as filters_mod

    summaries = "; ".join(f.summary for f in report.findings)
    payload = StagePayload(
        error=f"{report.filter} filter: {summaries}",
        data={
            "filter": report.filter,
            "artifact": rel_path,
            "action": report.action.value if report.action else "",
            "findings": str(len(report.findings)),
            "config": report.config_source,
        },
    )
    if report.action is filters_mod.FilterAction.REJECT:
        return StageOutcome(VERDICT_FATAL, payload)
    return StageOutcome(VERDICT_FAIL, payload, route=report.route)


def _task_was_admitted(engine: Engine, task_id: str) -> bool:
    """Did this corpus already admit this task? (near-duplicate firewall only)

    HASH-BOUND, DELIBERATELY NOT ROSTER-AWARE: `final_verdict` answers "may this
    ship RIGHT NOW under the CURRENT scorer", and reusing it made the admitted
    corpus shrink on every SCORER_VERSION bump — a fail-OPEN direction for a
    firewall. So: a frozen `release` PASS at this identity, or both RLVR unit
    batteries recorded PASS claiming acceptance. Both hash-bound, so an EDITED task
    stops counting."""
    from elt_taskgen.models import TaskStatus

    try:
        other = engine.load_task(task_id)
    except EngineError:
        return False
    if other.status is TaskStatus.REJECTED:
        # A rejected task is not in the corpus, and refusing a fresh candidate for
        # resembling one is a yield loss with nothing behind it.
        return False
    current = other.content_hash()

    def _passed_here(stage: StageName):
        row = engine.latest_report(task_id, stage.value)
        if row is None or row.verdict != VERDICT_PASS:
            return None
        if engine.report_is_superseded(other, row):
            return None
        return row if row.content_hash == current else None

    if _passed_here(StageName.RELEASE) is not None:
        return True
    for variant in RLVR_TASK_VARIANTS:
        row = _passed_here(variant_gate_stage(variant))
        if row is None:
            return False
        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        if payload.get("accepted") is not True:
            return False
    return True


def _admitted_tasks(engine: Engine, task: TaskIR) -> list[TaskIR]:
    """Every OTHER registered task this corpus has already admitted.

    Admission is read from the ledger, never from a side file: a task that was never
    admitted cannot make a later candidate a duplicate. See `_task_was_admitted` for
    why the test is hash-bound rather than roster-aware."""
    tasks_root = engine.workspace / "tasks"
    if not tasks_root.is_dir():
        return []
    admitted: list[TaskIR] = []
    for tdir in sorted(tasks_root.iterdir()):
        if tdir.name == task.task_id or not (tdir / "task_ir.json").is_file():
            continue
        try:
            if not _task_was_admitted(engine, tdir.name):
                continue
            admitted.append(engine.load_task(tdir.name))
        except Exception:  # noqa: BLE001 - an unreadable task is not evidence
            continue
    return admitted


def _record_intake_filter_reports(
    engine: Engine, task: TaskIR, reports: Iterable
) -> StageOutcome | None:
    """Record a complete intake-filter bundle and return its strongest block."""
    from elt_taskgen.verification import filters as filters_mod

    blocked: list[tuple] = []
    for report in reports:
        rel = _record_filter_report(engine, task, report)
        if not report.passed:
            blocked.append((report, rel))
    if not blocked:
        return None
    report, rel = next(
        (
            (r, p)
            for r, p in blocked
            if r.action is filters_mod.FilterAction.REJECT
        ),
        blocked[0],
    )
    return _filter_outcome(report, rel)


def run_intake_filters(engine: Engine, task: TaskIR) -> StageOutcome | None:
    """Intake format blacklist + near-duplicate dedup. None => admitted.

    BOTH artifacts are recorded even when the first already blocks — the ledger must
    show what was checked, not just what stopped the task. When both trip, the
    strongest consequence wins: a REJECT is never softened into a repairable route."""
    from elt_taskgen.verification import filters as filters_mod

    reports = filters_mod.run_intake_filters(
        task, _admitted_tasks(engine, task), _filter_config()
    )
    return _record_intake_filter_reports(engine, task, reports)


def _pipeline_batch_intake_preflight(
    engine: Engine,
    scheduled_tasks: dict[str, TaskIR],
) -> tuple[frozenset[str], tuple[dict, ...]]:
    """Serially filter fresh candidates before concurrent pipeline intake.

    Stable ordering makes each admitted candidate a comparator for later peers.
    Current contamination passes remain resume incumbents.
    """
    from elt_taskgen.verification import filters as filters_mod

    if not scheduled_tasks:
        return frozenset(), ()

    ordered_ids = tuple(sorted(scheduled_tasks))
    stopped: set[str] = set()
    results: list[dict] = []
    with engine.task_locks(ordered_ids):
        current_tasks: dict[str, TaskIR] = {}
        for task_id in ordered_ids:
            engine.recover_pending_repair(task_id)
            current = engine.load_task(task_id)
            expected = scheduled_tasks[task_id]
            if current.content_hash() != expected.content_hash():
                raise _pipeline_support.CliUsageError(
                    f"pipeline task {task_id!r} changed during intake preflight; "
                    "no workers were started — re-run the pipeline"
                )
            current_tasks[task_id] = current

        incumbents: dict[str, TaskIR] = {}
        fresh: list[TaskIR] = []
        for task_id in ordered_ids:
            task = current_tasks[task_id]
    # Ignore REJECTED only under an exact, still-valid evidence supersession.
            final = engine.final_verdict(task_id)
            reopen = (
                task.status is TaskStatus.REJECTED
                and final != FINAL_REJECTED
                and engine.has_active_report_supersession(task)
            )
            if final == FINAL_REJECTED or (
                task.status is TaskStatus.REJECTED and not reopen
            ):
                stopped.add(task_id)
                results.append(
                    {
                        "task_id": task_id,
                        "ok": False,
                        "state": FINAL_REJECTED,
                        "detail": (
                            "task was already rejected before candidate preparation"
                        ),
                        "usd": 0.0,
                    }
                )
                continue
            row = engine.latest_report(task_id, StageName.CONTAMINATION_PRE.value)
            current_pass = (
                row is not None
                and engine.report_is_current(
                    task, StageName.CONTAMINATION_PRE, row
                )[0]
            )
            if current_pass:
                # A resumed task already crossed intake. It is an incumbent for
                # fresh scheduled candidates even if later stages are unfinished.
                incumbents[task_id] = task
            else:
                fresh.append(task)

        # Preserve the existing ledger-derived admitted-index check. The first
        # fresh task cannot itself be admitted (otherwise its hash-bound stage
        # evidence would already be current), so it is a safe scan subject.
        if fresh:
            for admitted in _admitted_tasks(engine, fresh[0]):
                incumbents[admitted.task_id] = admitted

        config = _filter_config()
        provisional: dict[str, TaskIR] = {}
        for task in fresh:
            comparators = [
                candidate
                for task_id, candidate in sorted(
                    {**incumbents, **provisional}.items()
                )
                if task_id != task.task_id
            ]
            reports = filters_mod.run_intake_filters(task, comparators, config)

            def run_preflight_intake(
                locked_engine: Engine,
                locked_task: TaskIR,
                *,
                frozen_reports=reports,
            ) -> StageOutcome:
                blocked = _record_intake_filter_reports(
                    locked_engine, locked_task, frozen_reports
                )
                if blocked is not None:
                    return blocked
                return run_contamination_pre(locked_engine, locked_task)

            engine.set_stage_runner(
                StageName.CONTAMINATION_PRE, run_preflight_intake
            )
            try:
                engine.run(task.task_id, until=StageName.CONTAMINATION_PRE.value)
            except InfrastructureFailure as exc:
                stopped.add(task.task_id)
                results.append(
                    {
                        "task_id": task.task_id,
                        "ok": False,
                        "state": "infrastructure",
                        "detail": str(exc),
                        "usd": 0.0,
                    }
                )
                continue

            current = engine.load_task(task.task_id)
            row = engine.latest_report(
                task.task_id, StageName.CONTAMINATION_PRE.value
            )
            current_pass = (
                row is not None
                and engine.report_is_current(
                    current, StageName.CONTAMINATION_PRE, row
                )[0]
            )
            if current_pass:
                provisional[current.task_id] = current
                continue

            stopped.add(task.task_id)
            final = engine.final_verdict(task.task_id)
            state = FINAL_REJECTED if final == FINAL_REJECTED else "blocked"
            detail = "candidate intake did not pass"
            if row is not None:
                try:
                    payload = json.loads(row.payload_json)
                    detail = str(
                        payload.get("error") or payload.get("detail") or detail
                    )
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            results.append(
                {
                    "task_id": task.task_id,
                    "ok": False,
                    "state": state,
                    "detail": detail,
                    "usd": 0.0,
                }
            )

    return frozenset(stopped), tuple(results)


def run_execution_effect_filter(engine: Engine, task: TaskIR) -> StageOutcome | None:
    """Counterfactual/stress populations must move an observable output."""
    from elt_taskgen.reference import gold as gold_mod
    from elt_taskgen.verification import filters as filters_mod

    gold = gold_mod.load_gold(_pipeline_support._answer_key_dir(engine, task))
    report = filters_mod.execution_effect_filter(task, gold, _filter_config())
    rel = _record_filter_report(engine, task, report)
    return None if report.passed else _filter_outcome(report, rel)


def _with_intake_filters(inner):
    """Run the intake filters BEFORE the wrapped stage does any work."""

    def wrapped(engine: Engine, task: TaskIR) -> StageOutcome:
        blocked = run_intake_filters(engine, task)
        if blocked is not None:
            return blocked
        return inner(engine, task)

    return wrapped


def _with_execution_effect_filter(inner):
    """Run the execution-effect filter on the outputs the stage just froze."""

    def wrapped(engine: Engine, task: TaskIR) -> StageOutcome:
        outcome = inner(engine, task)
        if outcome.verdict != VERDICT_PASS:
            return outcome
        blocked = run_execution_effect_filter(engine, outcome.task or task)
        return blocked if blocked is not None else outcome

    return wrapped

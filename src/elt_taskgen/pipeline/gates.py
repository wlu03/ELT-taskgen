"""Run acceptance batteries against current independent and runtime evidence."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

from elt_taskgen import repair
from elt_taskgen.engine import (
    BLOCKED_ON_ENVIRONMENT,
    BLOCKED_ON_HUMAN,
    BLOCKED_ON_KEY,
    RETRY_GUARD_EXPLICIT,
    RETRY_GUARD_KEY,
    Engine,
    StageName,
    StageOutcome,
    StagePayload,
    STAGE_ORDER,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
    variant_gate_stage,
)
from elt_taskgen.models import (
    TaskIR,
    TaskVariant,
    canonical_json,
    readable_json,
    sha256_hex,
)
from elt_taskgen.pipeline import generation as _pipeline_generation
from elt_taskgen.pipeline import payloads as _pipeline_payloads
from elt_taskgen.pipeline import support as _pipeline_support


def _ensure_independent_build(engine: Engine, task: TaskIR, gold, provider) -> str:
    """Run or reuse the independent build for the current task hash.

    Missing results fail gates; harness faults are recorded as transport halts.
    """
    from elt_taskgen.reference import adjudication as independent_adjudication
    from elt_taskgen.reference import independent
    from elt_taskgen.review import providers as providers_mod
    from elt_taskgen.review.session import SessionFault

    existing = independent.load_build_result(engine.workspace, task.task_id)
    recovery_generation = 0
    decision_digest = ""
    if (
        existing is not None
        and existing.get("task_id") == task.task_id
        and existing.get("task_content_hash") == task.content_hash()
    ):
        if existing.get("status") != independent.STATUS_NEEDS_ADJUDICATION:
            return f"recorded independent build reused (status={existing.get('status')!r})"
        try:
            decision = independent_adjudication.load_fresh_build_decision(
                engine.workspace,
                task,
            )
        except ValueError as exc:
            return (
                "recorded independent build reused "
                f"(status={existing.get('status')!r}); invalid bound "
                f"adjudication decision: {type(exc).__name__}: {exc}"
            )
        if decision is None:
            return f"recorded independent build reused (status={existing.get('status')!r})"
        # The decision cannot green-light anything. It only permits one fresh
        # blind reconstruction, under a transcript generation that differs
        # from the disputed witness while exposing no diagnostic material.
        recovery_generation = independent.next_recovery_generation(
            engine.workspace,
            task.task_id,
        )
        decision_digest = decision.digest()
    try:
        result = independent.run_independent_build(
            task,
            engine.workspace,
            provider,
            gold,
            recovery_generation=recovery_generation,
        )
    except (
        providers_mod.TranscriptMissingError,
        providers_mod.MissingCredentialsError,
        SessionFault,
    ) as exc:
        # Prefix the note with the canonical exception marker from the MRO so
        # transport failures route as infrastructure even for subclasses.
        # SessionFault messages contain only boundary and code metadata.
        from elt_taskgen.engine import _infra_marker_for

        name = type(exc).__name__
        marker = _infra_marker_for(exc)
        head = f"{marker}: {name}" if marker and marker != name else name
        return f"independent build not performed: {head}: {exc}"
    independent.record_build_result(engine.workspace, task, result)
    recovery_note = (
        f", fresh blind generation={recovery_generation}, "
        f"decision={decision_digest[:12]}"
        if recovery_generation
        else ""
    )
    return f"independent build recorded (status={result.status!r}{recovery_note})"


def _independent_adjudication_block(
    engine: Engine, task: TaskIR
) -> StageOutcome | None:
    """Block a current dual-build disagreement pending adjudication or new evidence."""
    from elt_taskgen.reference import independent

    record = independent.load_build_result(engine.workspace, task.task_id)
    if not isinstance(record, dict):
        return None
    if (
        record.get("task_id") != task.task_id
        or record.get("task_content_hash") != task.content_hash()
        or record.get("status") != independent.STATUS_NEEDS_ADJUDICATION
    ):
        return None
    evidence_digest = sha256_hex(canonical_json(record))
    return StageOutcome(
        VERDICT_BLOCKED,
        StagePayload(
            error=(
                "the current independent transformation build passes the public "
                "development examples but disagrees with hidden gold; the task, "
                "reference, data, comparator, and witness remain unjudged until "
                "the disagreement is explicitly adjudicated"
            ),
            data={
                "failure_class": "pending_adjudication",
                "failure_code": "independent_gold_disagreement",
                BLOCKED_ON_KEY: BLOCKED_ON_HUMAN,
                "independent_build_sha256": evidence_digest,
                "recovery_prerequisite": (
                    "bound_adjudication_or_new_independent_build"
                ),
                RETRY_GUARD_KEY: RETRY_GUARD_EXPLICIT,
            },
        ),
    )


#: A gate whose failure names a RE-RUN as its remedy is refusing STALE EVIDENCE,
#: not judging the task. Every currency refusal gates.py writes ends in one; a
#: genuine defect never does.
_EVIDENCE_RERUN_MARKER = "re-run"


def _is_currency_refusal(gate) -> bool:
    """Is this failing gate refusing STALE EVIDENCE rather than judging?

    ONE reader, shared with the repair router (`repair.is_currency_refusal`), so the
    BLOCKED answer here and the route the engine picks can never disagree about what
    "not measured yet" means. Prefers a structured flag over the prose match."""
    return repair.is_currency_refusal(gate)


def _evidence_currency_block(report) -> str | None:
    """The remedy text when a battery refused ONLY because its evidence is not
    current, else None.

    A fix that legitimately makes recorded evidence stale must produce "run the
    stage again", never a rejection: this is a wait on the operator or on a stage
    re-run, i.e. BLOCKED. Measured — one refusal naming contamination was read as a
    contamination REJECTION and a certified drive was rejected outright."""
    gates = getattr(report, "gates", None)
    if not gates:
        return None
    failing = [g for g in gates if not getattr(g, "passed", False)]
    if not failing:
        return None
    if not all(_is_currency_refusal(g) for g in failing):
        return None
    return "; ".join(
        f"{getattr(g, 'gate', '?')}: {getattr(g, 'details', '')}" for g in failing
    )


#: Remedy phrase -> the ladder stage that RE-DERIVES that evidence.
#: `measure-target` is deliberately absent: it needs an ELT-Bench checkout, so no
#: stage in the ladder can clear it and only the operator can.
_EVIDENCE_PRODUCER_STAGES: tuple[tuple[str, StageName], ...] = (
    ("re-run reference-run", StageName.REFERENCE),
    ("re-run contamination-post", StageName.CONTAMINATION_POST),
    ("re-run validate-el", StageName.GATES_EXTRACT_LOAD),
    ("re-run validate-t", StageName.GATES_TRANSFORM),
)

#: Ledger stage -> the CLI subcommand that runs it (for remedy messages).
STAGE_SUBCOMMANDS: dict[str, str] = {
    StageName.GENERATE.value: "generate",
    StageName.REFERENCE.value: "reference-run",
    # The author runs inside `review` (no `author` subcommand exists), so a
    # block or refusal at that stage is re-run through `review`.
    StageName.AUTHOR.value: "review",
    StageName.REVIEW.value: "review",
    StageName.ATTACK.value: "attack",
    StageName.TASK_INTEGRITY.value: "validate",
    StageName.GATES_EXTRACT_LOAD.value: "validate-el",
    StageName.GATES_TRANSFORM.value: "validate-t",
    StageName.CALIBRATE.value: "calibrate",
    StageName.SELECT.value: "select",
    StageName.RELEASE.value: "release",
    StageName.CONTAMINATION_POST.value: "release",
}


def _shadow_stale_evidence_producers(
    engine: Engine, task: TaskIR, stage: StageName, remedies: str
) -> tuple[str, ...]:
    """Shadow the upstream stages a currency refusal names. Returns their names.

    THE REMEDY HAS TO BE REACHABLE: `engine.run` skips a stage whose latest row is a
    PASS at the current hash, so the re-run a refusal named did nothing. Shadowing
    the producer here is APPEND-ONLY (the idiom `repair.apply_repair` uses), spends
    no repair round and rejects nothing. Only stages STRICTLY UPSTREAM are shadowed
    — a battery cannot be its own producer, and a downstream stage has not attested."""
    order = [s.value for s in STAGE_ORDER]
    here = order.index(stage.value)
    text = remedies.lower()
    shadowed: list[str] = []
    for phrase, producer in _EVIDENCE_PRODUCER_STAGES:
        if phrase not in text or order.index(producer.value) >= here:
            continue
        row = engine.latest_report(task.task_id, producer.value)
        if row is None or not engine.report_is_current(task, producer, row)[0]:
            continue  # already going to run: nothing to shadow
        _pipeline_support._force_stage_rerun(
            engine,
            task,
            producer,
            f"stale evidence for {stage.value!r}: {phrase}",
        )
        shadowed.append(producer.value)
    return tuple(shadowed)


def _blocked_on_stale_evidence(
    engine: Engine, task: TaskIR, stage: StageName, remedies: str
) -> StageOutcome:
    """BLOCKED because recorded evidence is not current — and the producer of
    that evidence is scheduled to re-run so the block can actually clear."""
    scheduled = _shadow_stale_evidence_producers(engine, task, stage, remedies)
    if scheduled:
        remedy = (
            "already scheduled: "
            + ", ".join(sorted(scheduled))
            + " will re-derive on the next run of this workspace — just re-run "
            "the same command"
        )
    else:
        subcommand = STAGE_SUBCOMMANDS.get(stage.value, stage.value)
        remedy = (
            "no ladder stage can re-derive this: run what the refusal names "
            f"(e.g. `elt-taskgen measure-target --workspace {engine.workspace} "
            "--bench-root <ELT-Bench checkout>`), then re-run `elt-taskgen "
            f"{subcommand} --workspace {engine.workspace} --task-id "
            f"{task.task_id}`"
        )
    data = {BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT}
    if scheduled:
        data["rerun_scheduled"] = ",".join(sorted(scheduled))
    return StageOutcome(
        VERDICT_BLOCKED,
        StagePayload(
            error=(
                "recorded evidence is not current, so this battery could not "
                "judge the task — nothing is rejected and no repair round is "
                f"spent. Refusals: {remedies}. Remedy: {remedy}."
            ),
            data=data,
        ),
    )


def _canonical_block(error: str, *, blocked_on: str, failure_code: str) -> StageOutcome:
    return StageOutcome(
        VERDICT_BLOCKED,
        StagePayload(
            error=error + " Nothing is rejected and no repair round is spent.",
            data={BLOCKED_ON_KEY: blocked_on, "failure_code": failure_code},
        ),
    )


def _ensure_canonical_reachability(
    engine: Engine, task: TaskIR, variant_out_dir: Path
) -> StageOutcome | None:
    """Derive and score the canonical Terraform + dbt artifact for the T unit,
    once per destination the task ships.

    Writes ``reports/canonical_reachability.json`` and the private artifacts
    under ``answer_key/runtime/canonical/<destination>/`` for the battery's
    canonical-reachability gate to judge, then returns None. It RECORDS only
    outcomes that describe the task on the workspace channel: a full score and
    a measured shortfall or an emitter gap. Anything that says nothing about
    the task returns BLOCKED without recording: the pinned dbt runtime
    missing, a substrate or IO failure, a harness, infrastructure or
    real-runtime failure, a timeout of the reference solution (environment),
    or the grader refusing the task package (human). No outcome rejects.
    """
    import tempfile

    from elt_taskgen.training import canonical
    from elt_taskgen.training.package import WorkspacePackageError, shipped_destinations

    task_root = engine.task_dir(task.task_id)
    answer_key_dir = _pipeline_support._answer_key_dir(engine, task)
    # A previous identity's or run's evidence never survives into this one.
    stale_evidence = task_root / canonical.REACHABILITY_EVIDENCE_REL
    if stale_evidence.is_file() and not stale_evidence.is_symlink():
        stale_evidence.unlink()
    stale_artifacts = answer_key_dir / canonical.CANONICAL_ARTIFACT_REL
    if stale_artifacts.exists():
        canonical.remove_scratch_tree(stale_artifacts)

    runtime_config = canonical.default_dbt_runtime_config()
    if not canonical.dbt_runtime_available(runtime_config):
        return _canonical_block(
            "The canonical-reachability check needs the pinned dbt runtime at "
            f"{canonical.DBT_RUNTIME_ROOT}; provision it with: uv sync --project "
            "runtime-images/dbt-duckdb --locked.",
            blocked_on=BLOCKED_ON_ENVIRONMENT,
            failure_code="canonical_runtime_unavailable",
        )
    try:
        destinations = shipped_destinations(answer_key_dir)
    except Exception as exc:  # noqa: BLE001 - the private tree is unreadable
        return _canonical_block(
            "The task's private connector contracts could not be read "
            f"({type(exc).__name__}: {exc}).",
            blocked_on=BLOCKED_ON_ENVIRONMENT,
            failure_code="canonical_substrate_failed",
        )

    records: dict[str, canonical.CanonicalReachabilityRecord] = {}
    files_by_destination: dict[str, dict[str, str]] = {}
    scratch = Path(tempfile.mkdtemp(prefix="canonical-reachability-"))
    try:
        for destination in destinations:
            name = destination.value
            try:
                package = canonical.build_workspace_substrate(
                    task=task,
                    answer_key_dir=answer_key_dir,
                    public_dir=_pipeline_support._public_dir(engine, task),
                    populations_root=task_root / "populations",
                    oracle_dir=Path(variant_out_dir) / "task" / _warehouse_dirname(),
                    scratch=scratch / f"substrate-{name}",
                    destination=destination,
                )
            except WorkspacePackageError as exc:
                return _canonical_block(
                    "The workspace grader refused the exported task package while "
                    f"preparing the canonical-reachability check for {name} ({exc}).",
                    blocked_on=BLOCKED_ON_HUMAN,
                    failure_code="canonical_task_package_invalid",
                )
            except Exception as exc:  # noqa: BLE001 - environment, never a judgement
                return _canonical_block(
                    "The canonical-reachability workspace could not be laid out for "
                    f"{name} ({type(exc).__name__}: {exc}).",
                    blocked_on=BLOCKED_ON_ENVIRONMENT,
                    failure_code="canonical_substrate_failed",
                )
            try:
                files = canonical.render_canonical_project(package)
            except canonical.CanonicalArtifactError as exc:
                records[name] = canonical.build_render_failure_record(
                    task,
                    destination=destination,
                    runtime_config=runtime_config,
                    error=f"{type(exc).__name__}: {exc}",
                    airbyte_contract_sha256=package.airbyte_contract_sha256,
                )
                files_by_destination[name] = {}
                continue
            try:
                sealed, result = canonical.score_canonical_artifact(
                    package,
                    files,
                    scratch=scratch / f"score-{name}",
                    runtime_config=runtime_config,
                )
            except Exception as exc:  # noqa: BLE001 - install/seal/scorer faults
                return _canonical_block(
                    "Scoring the canonical artifact failed inside the workspace "
                    f"harness for {name} ({type(exc).__name__}: {exc}).",
                    blocked_on=BLOCKED_ON_ENVIRONMENT,
                    failure_code="canonical_harness_failed",
                )
            outcome = canonical.classify_score(result)
            if outcome == canonical.OUTCOME_ENVIRONMENT:
                return _canonical_block(
                    "The workspace grader produced no label for the canonical "
                    f"artifact on {name} ({canonical.shortfall_summary(result)}); this "
                    "describes the host or the harness, not the task.",
                    blocked_on=BLOCKED_ON_ENVIRONMENT,
                    failure_code="canonical_grader_unavailable",
                )
            if outcome == canonical.OUTCOME_TASK_PACKAGE:
                return _canonical_block(
                    "The workspace grader refused the task package for the canonical "
                    f"artifact on {name} ({canonical.shortfall_summary(result)}).",
                    blocked_on=BLOCKED_ON_HUMAN,
                    failure_code="canonical_task_package_invalid",
                )
            records[name] = canonical.build_reachability_record(
                package,
                files=files,
                sealed=sealed,
                result=result,
                runtime_config=runtime_config,
            )
            files_by_destination[name] = dict(files)
        canonical.record_canonical_reachability(
            task_dir=task_root,
            answer_key_dir=answer_key_dir,
            records=records,
            files=files_by_destination,
        )
        return None
    finally:
        canonical.remove_scratch_tree(scratch)


def _warehouse_dirname() -> str:
    from elt_taskgen.export import eltbench as eltbench_mod

    return eltbench_mod.WAREHOUSE_DIRNAME


def _configured_destination_of(engine: Engine, task: TaskIR) -> str:
    """The bundle-root destination of the exported public task, else snowflake."""
    import yaml

    from elt_taskgen.export import eltbench as eltbench_mod

    try:
        config = yaml.safe_load(
            (_pipeline_support._public_dir(engine, task) / "config.yaml").read_text(encoding="utf-8")
        )
        return eltbench_mod.destination_from_config(config).value
    except Exception:  # noqa: BLE001 - a missing bundle is reported by the gate
        return "snowflake"


def make_gates_runner(provider, *, destination="snowflake", extra_destinations=()):
    """`gates` (TASK_INTEGRITY): shared project-integrity evidence and checks.

    The stage value stays ``gates`` for workspace compatibility, but this runner
    does NOT create a FULL RLVR task: its composite checks are internal diagnostics,
    and EL and T are admitted only by the two unit batteries that follow."""

    def run_gates_stage(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.export import eltbench as eltbench_mod
        from elt_taskgen.reference import gold as gold_mod
        from elt_taskgen.verification import gates as gates_mod

        akd = _pipeline_support._answer_key_dir(engine, task)
        public = _pipeline_support._public_dir(engine, task)
        gold = gold_mod.load_gold(akd)

        # Emit the public bundle + private evaluation artifacts (upstream
        # shape); the post-generation scan and two gates read these trees.
        eltbench_mod.export_task(
            task,
            gold,
            public,
            akd,
            destination=destination,
            extra_destinations=tuple(extra_destinations),
        )

        idx = _pipeline_generation._contamination_index(engine)
        # ARMING BANNER at the gate: a reader watching the run must see what the
        # firewall contained at the moment the contamination-clean gate read it.
        coverage = _pipeline_generation._arm_contamination_index(idx)
        result = idx.scan_post(
            task, public, akd, require=_pipeline_generation._required_firewall_coverage()
        )
        collisions = list(result.collisions)
        evidence = {
            "task_id": task.task_id,
            "task_content_hash": task.content_hash(),
            "collisions": [c.model_dump(mode="json") for c in collisions],
            "coverage": _pipeline_generation._coverage_payload(engine, coverage),
            "coverage_summary": coverage.summary(),
        }
        (_pipeline_support._evidence_dir(engine, task) / "contamination_post.json").write_text(
            canonical_json(evidence), encoding="utf-8"
        )
        if any(c.fatal for c in collisions):
            return StageOutcome(
                VERDICT_FATAL,
                _pipeline_payloads.ContaminationPayload(
                    call_point="post",
                    collisions=tuple(c.model_dump(mode="json") for c in collisions),
                    fatal_count=sum(1 for c in collisions if c.fatal),
                    coverage_level=coverage.level.value,
                    coverage=_pipeline_generation._coverage_payload(engine, coverage),
                    detail=(
                        "fatal contamination collision in emitted artifacts; "
                        + coverage.summary()
                    ),
                ),
            )

        # Dual-build evidence for the trusted-solution/dual-build-agreement gates:
        # run and record here (or reuse the record at this exact identity). The note
        # is PERSISTED, so why a witness is missing survives in the workspace.
        build_note = _ensure_independent_build(engine, task, gold, provider)
        _write_evidence_notes(engine, task, DUAL_BUILD_NOTES_FILENAME, [build_note])

        attack_payload = _pipeline_support._require_pass_payload(engine, task, StageName.ATTACK)
        report = gates_mod.run_gates(
            task, engine.workspace, gold, attack_payload.get("rewards", {})
        )
        (
            _pipeline_support._evidence_dir(engine, task)
            / VARIANT_ACCEPTANCE_EVIDENCE.format(variant=TaskVariant.FULL.value)
        ).write_text(
            readable_json(report.model_dump(mode="json")), encoding="utf-8"
        )
        if report.accepted:
            return StageOutcome(VERDICT_PASS, report)
        adjudication = _independent_adjudication_block(engine, task)
        if adjudication is not None:
            return adjudication
        stale = _evidence_currency_block(report)
        if stale is not None:
            return _blocked_on_stale_evidence(
                engine, task, StageName.TASK_INTEGRITY, stale
            )
        return StageOutcome(
            VERDICT_FAIL,
            report,
            # A witness the TRANSPORT could not produce is not a task defect: halt
            # naming the transport instead of spending a repair round on a
            # deterministic re-run that reproduces it.
            infrastructure=_transport_marker([build_note]),
        )

    return run_gates_stage


# Per-variant acceptance batteries. Each subtask has its own ledger stage,
# reward, witnesses, and parent hash for resumable repair and release checks.

#: Per-variant AcceptanceReport copy under tasks/<id>/reports/.
VARIANT_ACCEPTANCE_EVIDENCE = "acceptance_{variant}.json"


def _variants_root(engine: Engine, task: TaskIR) -> Path:
    return engine.task_dir(task.task_id) / "variants"


def _ensure_el_evidence(engine: Engine, task: TaskIR, gold, provider) -> list[str]:
    """Run both extract/load witnesses for the current content hash.

    ``el-artifact-census`` independently counts each format without solution
    readers. ``el-independent-load`` gives a cross-family model only the public
    EL bundle. Neither witness substitutes for the other. If either producer
    cannot run, record no evidence and return notes; missing evidence fails the
    gates."""
    notes: list[str] = []

    from elt_taskgen.reference import independent

    census_writer = None
    try:
        from elt_taskgen.verification import el_probes  # type: ignore

        census_writer = getattr(el_probes, "record_artifact_census", None)
    except ImportError:
        pass
    if census_writer is None:
        notes.append(
            "artifact census not performed: verification/el_probes.py provides "
            "no record_artifact_census (el-artifact-census will be RED)"
        )
    else:
        try:
            census_writer(engine.workspace, task, gold)
            notes.append("artifact census recorded")
        except Exception as exc:  # noqa: BLE001 — a producer failure is evidence
        # Persist census-production errors for the owning gate to report.
            notes.append(f"artifact census FAILED: {type(exc).__name__}: {exc}")

    load_build = getattr(independent, "run_independent_load_build", None)
    recorder = getattr(independent, "record_load_build_result", None)
    if load_build is None or recorder is None:
        notes.append(
            "independent LOAD build not performed: reference/independent.py "
            "provides no independent_loader role (el-independent-load will be RED)"
        )
        return notes
    from elt_taskgen.review import providers as providers_mod

    loader = getattr(independent, "load_load_build_result", None)
    existing = loader(engine.workspace, task.task_id) if loader else None
    if (
        existing is not None
        and existing.get("task_id") == task.task_id
        and existing.get("task_content_hash") == task.content_hash()
    ):
        notes.append("recorded independent LOAD build reused")
        return notes
    try:
        result = load_build(task, engine.workspace, provider, gold)
    except providers_mod.BudgetExceededError as exc:
        # Preserve task/total budget scope for coordinator handling.
        if str(getattr(exc, "scope", "") or "").strip().lower() in {
            "task",
            "total",
        }:
            raise
        notes.append(
            f"independent LOAD build FAILED: {type(exc).__name__}: {exc}"
        )
        return notes
    except (
        providers_mod.TranscriptMissingError,
        providers_mod.MissingCredentialsError,
    ) as exc:
        # Class name first — see _ensure_independent_build for why.
        notes.append(
            f"independent LOAD build not performed: {type(exc).__name__}: {exc}"
        )
        return notes
    except Exception as exc:  # noqa: BLE001 — same rule as the census above
        notes.append(
            f"independent LOAD build FAILED: {type(exc).__name__}: {exc}"
        )
        return notes
    recorder(engine.workspace, task, result)
    notes.append("independent LOAD build recorded")
    return notes


#: Producer-notes files, bound to (task_id, task_content_hash). The gates read
#: them to say WHY a witness is missing; they can never make a gate green.
EL_EVIDENCE_NOTES_FILENAME = "el_evidence_notes.json"
DUAL_BUILD_NOTES_FILENAME = "independent_build_notes.json"


def _write_evidence_notes(
    engine: Engine, task: TaskIR, filename: str, notes: list[str]
) -> None:
    """Persist what a witness producer did (or why it could not).

    A breadcrumb, not a gate and not evidence FOR one: the gates read the census and
    independent-build records themselves and go red on absence. This names the
    producer's own error instead of only its consequence."""
    (_pipeline_support._evidence_dir(engine, task) / filename).write_text(
        canonical_json(
            {
                "task_id": task.task_id,
                "task_content_hash": task.content_hash(),
                "notes": list(notes),
            }
        ),
        encoding="utf-8",
    )


def _write_el_evidence_notes(engine: Engine, task: TaskIR, notes: list[str]) -> None:
    """Back-compatible alias for the EL witness notes file."""
    _write_evidence_notes(engine, task, EL_EVIDENCE_NOTES_FILENAME, notes)


def _transport_marker(notes: Iterable[str]) -> str:
    """Return the first anchored transport or harness marker in a producer note."""
    from elt_taskgen.engine import _INFRA_EXCEPTION_NAMES

    names = sorted(_INFRA_EXCEPTION_NAMES, key=lambda name: (-len(name), name))
    for note in notes:
        text = str(note)
        hits = [(text.find(name), rank, name) for rank, name in enumerate(names) if name in text]
        if hits:
            return min(hits)[2]
    return ""


def make_variant_gates_runner(
    variant: TaskVariant,
    provider=None,
    *,
    destination="snowflake",
    extra_destinations=(),
):
    """Stage runner for one subtask variant's acceptance battery.

    EL and T are BOTH MANDATORY: a stage PASS requires this variant's own battery to
    accept with complete coverage, and any refusal is a stage FAIL entering bounded
    repair (a PASS/accepted=false row would be skipped forever on resume and could
    leave a one-unit parent). A refused variant's emitted bundle is DELETED."""
    variant = TaskVariant(variant)
    stage = variant_gate_stage(variant)

    def run_variant_gates_stage(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.export import eltbench as eltbench_mod
        from elt_taskgen.reference import gold as gold_mod
        from elt_taskgen.verification import variant_battery as battery_mod

        gold = gold_mod.load_gold(_pipeline_support._answer_key_dir(engine, task))
        out_dir = _variants_root(engine, task) / variant.value

        # THE BUNDLE IS EMITTED FROM THE POPULATIONS, so what is on disk must BE the
        # IR's derivation before a byte is measured: an edited rendered/ file
        # otherwise sails through while the true reference scores 0.0 against gold.
        drifted = _pipeline_generation._population_drift_failure(engine, task)
        if drifted is not None:
            return drifted

        # Emit the bundle FIRST so the battery measures the artifact that would
        # actually ship — this closes the export-time drift hole: the gate sees the
        # same bytes release re-verifies.
        eltbench_mod.emit_variant(
            task,
            gold,
            variant,
            out_dir,
            populations_dir=engine.task_dir(task.task_id) / "populations",
            destination=destination,
            extra_destinations=tuple(extra_destinations),
        )

        transport = ""
        if variant is TaskVariant.EXTRACT_LOAD:
            notes = _ensure_el_evidence(engine, task, gold, provider)
            _write_el_evidence_notes(engine, task, notes)
            transport = _transport_marker(notes)
        if variant is TaskVariant.TRANSFORM:
            # The canonical Terraform + dbt artifact is derived from the answer
            # key and scored through the real workspace grader HERE, so the
            # battery's canonical-reachability gate judges evidence bound to
            # the bundle just emitted (the T oracles it reads are under out_dir).
            blocked = _ensure_canonical_reachability(engine, task, out_dir)
            if blocked is not None:
                return blocked

        attack_payload = _pipeline_support._require_pass_payload(engine, task, StageName.ATTACK)
        # NEVER the legacy `rewards` field: reusing it would make this battery a
        # relabelling of the parent battery. `rewards_by_variant` is computed from
        # the SAME execution, and its absence is a RED battery, not a green one.
        rewards_by_variant = attack_payload.get("rewards_by_variant", {})

        report = battery_mod.run_variant_battery(
            variant, task, engine.workspace, gold, rewards_by_variant
        )
        (
            _pipeline_support._evidence_dir(engine, task)
            / VARIANT_ACCEPTANCE_EVIDENCE.format(variant=variant.value)
        ).write_text(
            readable_json(report.model_dump(mode="json")), encoding="utf-8"
        )

        if report.accepted:
            try:
                battery_mod.verify_coverage(variant, report)
            except ValueError as exc:
                # An ACCEPTED battery with a roster hole is the fail-open shape
                # itself: it claims a verdict it never measured. Task-level.
                shutil.rmtree(out_dir, ignore_errors=True)
                return StageOutcome(
                    VERDICT_FAIL, StagePayload(error=f"roster coverage: {exc}")
                )
            return StageOutcome(VERDICT_PASS, report)

        stale = _evidence_currency_block(report)
        if stale is not None:
            # Evidence that is not current is a WAIT, not a refusal: keep the emitted
            # bundle and schedule the producer stage that re-derives what went stale.
            return _blocked_on_stale_evidence(engine, task, stage, stale)

        shutil.rmtree(out_dir, ignore_errors=True)
        # Variant refusals fail for repair; witness transport faults halt instead.
        judging = [
            g for g in report.gates if not g.passed and not _is_currency_refusal(g)
        ]
        if [g.gate for g in judging] == ["canonical-reachability"]:
            # Task repair cannot change unsupported compiler or connector
            # capabilities. Wait for a pipeline change and rescore afterward.
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error=(
                        "the task's own canonical Terraform + dbt solution is not "
                        "reachable through the RLVR workspace channel: "
                        f"{judging[0].details} Extend the portable dbt subset "
                        "(training/dbt_runner.py) or the canonical emitter "
                        "(training/canonical.py), then run validate-t again. "
                        "Nothing is rejected and no repair round is spent."
                    ),
                    data={
                        BLOCKED_ON_KEY: BLOCKED_ON_HUMAN,
                        "failure_code": "canonical_workspace_unreachable",
                    },
                ),
            )
        return StageOutcome(VERDICT_FAIL, report, infrastructure=transport)

    run_variant_gates_stage.__name__ = f"run_{stage.value}_stage"
    return run_variant_gates_stage

"""Corpus preparation, selection, and immutable release publication."""

from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path

from elt_taskgen.generation.config import DEFAULT_GENERATION_CONFIG
from elt_taskgen.generation.difficulty_profiles import DEFAULT_GENERATION_PROFILE
from elt_taskgen.engine import (
    Engine,
    InfrastructureFailure,
    StageName,
    StageOutcome,
    StagePayload,
    STAGE_ORDER,
    VERDICT_FATAL,
    VERDICT_PASS,
    FINAL_ACCEPTED,
    FINAL_REJECTED,
)
from elt_taskgen.models import (
    DifficultyMeasurement,
    Origin,
    RLVR_TASK_VARIANTS,
    TaskIR,
    canonical_json,
    readable_json,
)
from elt_taskgen.pipeline import generation as _pipeline_generation
from elt_taskgen.pipeline import release as _pipeline_release
from elt_taskgen.pipeline import support as _pipeline_support


def run_corpus(
    args, *, open_engine, active_campaign_fingerprint, registered_task_ids,
    worker_payload, worker,
) -> int:
    """Run all registered source tasks with isolated concurrent workers.

    Agent-backed work happens per task through ``CONTAMINATION_POST``. The
    coordinator performs deterministic corpus-wide selection once, records that
    common decision for selected tasks, and optionally cuts one
    immutable release. No provider/session object or SQLite connection is shared
    between workers.
    """
    from elt_taskgen.adapters import eltbench_anchor
    from elt_taskgen.corpus import selection as selection_mod
    from elt_taskgen.export import release as release_mod
    from elt_taskgen.models import Origin

    workspace = Path(args.workspace).resolve()
    allow_partial_candidates = bool(
        getattr(args, "allow_partial_candidates", False)
    )
    configured_readiness_spec = None
    if getattr(args, "configured_run_spec", None) is not None:
        from elt_taskgen.pipeline_readiness import GenerationRunSpec

        configured_readiness_spec = GenerationRunSpec.model_validate(
            args.configured_run_spec
        )
    if args.release_dir is None:
        release_only_flags = []
        if args.sandbox_attestation is not None:
            release_only_flags.append("--sandbox-attestation")
        if args.development_release:
            release_only_flags.append("--development-release")
        if args.allow_unlocked_env:
            release_only_flags.append("--allow-unlocked-env")
        if release_only_flags:
            raise _pipeline_support.CliUsageError(
                f"{', '.join(release_only_flags)} require --release-dir; "
                "without an output directory pipeline prepares and selects "
                "the corpus but does not freeze a release"
            )

    # Refuse argument/configuration errors before a typed ingest can mutate the
    # workspace. Roster-dependent bounds remain below, after task discovery.
    if args.workers < 1:
        raise _pipeline_support.CliUsageError("--workers must be at least 1")
    if args.size is not None and args.size < 1:
        raise _pipeline_support.CliUsageError("--size must be at least 1")
    per_task_limit = float(args.budget_per_task)
    if not math.isfinite(per_task_limit) or per_task_limit <= 0.0:
        raise _pipeline_support.CliUsageError("--budget-per-task must be a finite positive number")
    val_fraction = float(args.val_fraction)
    if not math.isfinite(val_fraction) or not 0.0 <= val_fraction <= 1.0:
        raise _pipeline_support.CliUsageError("--val-fraction must be a finite number between 0 and 1")
    if bool(args.record) and bool(args.replay_only):
        raise _pipeline_support.CliUsageError("--record and --replay-only cannot be used together")
    aggregate_limit = None
    if args.budget_total is not None:
        aggregate_limit = float(args.budget_total)
        if not math.isfinite(aggregate_limit) or aggregate_limit <= 0.0:
            raise _pipeline_support.CliUsageError("--budget-total must be a finite positive number")

    allowed_sources = {
        Origin.DBT.value,
        Origin.DLT.value,
        Origin.SYNSQL.value,
        Origin.SCHEMAPILE.value,
        Origin.WIKIDBS.value,
    }
    required_source_values = {
        value.strip()
        for value in str(args.require_sources or "").split(",")
        if value.strip()
    }
    unknown_sources = sorted(required_source_values - allowed_sources)
    if unknown_sources:
        raise _pipeline_support.CliUsageError(f"unknown --require-sources values: {unknown_sources}")

    release_mode = "development" if args.development_release else "certified"
    if args.release_dir is not None:
        if release_mode == "certified" and not required_source_values:
            raise _pipeline_support.CliUsageError(
                "a certified pipeline release requires at least one "
                "--require-sources origin; an empty source policy is development-only"
            )
        if release_mode == "certified" and args.allow_structural_difficulty:
            raise _pipeline_support.CliUsageError(
                "--allow-structural-difficulty is development-only; pair it with "
                "--development-release or enable empirical calibration"
            )
        if release_mode == "certified" and args.sandbox_attestation is None:
            raise _pipeline_support.CliUsageError(
                "certified pipeline release requires --sandbox-attestation; "
                "use --development-release only for an explicitly unlabelled local cut"
            )
        if release_mode == "development" and args.sandbox_attestation is not None:
            raise _pipeline_support.CliUsageError(
                "--sandbox-attestation is only valid for a certified release"
            )
        requested_release_dir = Path(args.release_dir).resolve()
        if requested_release_dir.exists() and not (
            requested_release_dir.is_dir()
            and (requested_release_dir / "release_manifest.json").is_file()
        ):
            raise _pipeline_support.CliUsageError(
                "pipeline release directory already exists without a complete "
                "manifest (immutable output; move it aside): "
                f"{requested_release_dir}"
            )

    expected_campaign_fingerprint = active_campaign_fingerprint(args)
    ingest_manifest = getattr(args, "ingest_manifest", None)
    ingest_reingest = bool(getattr(args, "reingest", False))
    if ingest_reingest and ingest_manifest is None:
        raise _pipeline_support.CliUsageError("pipeline --reingest requires --ingest-manifest")
    if ingest_manifest is not None:
        from elt_taskgen.ingest_manifest import (
            FiveSourceIngestError,
            ingest_five_sources,
        )

        try:
            ingested = ingest_five_sources(
                Path(ingest_manifest),
                workspace=workspace,
                reingest=ingest_reingest,
                difficulty_profile=DEFAULT_GENERATION_PROFILE.name,
                recipe_selection=DEFAULT_GENERATION_CONFIG.recipe_selection(getattr(args, "selection_seed", None) or 0),
            )
        except (FiveSourceIngestError, OSError, ValueError) as exc:
            raise _pipeline_support.CliUsageError(
                f"five-source ingest preflight refused: {exc}"
            ) from None
        print(
            f"five-source ingest {ingested.manifest_sha256[:16]} registered/"
            f"confirmed {len(ingested.tasks)} task(s) before pipeline"
        )
    task_ids = registered_task_ids(workspace, args.task_id)
    if args.size is not None and args.size > len(task_ids):
        raise _pipeline_support.CliUsageError(
            f"--size must be between 1 and the {len(task_ids)} scheduled tasks"
        )
    requested_size = int(args.size or len(task_ids))
    per_task_total = (
        None
        if getattr(args, "configured_run_spec", None) is not None
        else aggregate_limit / len(task_ids)
        if aggregate_limit is not None
        else None
    )

    # Validate the source contract from registered TaskIRs before constructing a
    # provider or spending on any candidate. Re-load after workers too: repairs
    # may legitimately replace the semantic identity while a task is prepared.
    preflight_engine = open_engine(workspace)
    try:
        scheduled_tasks = {
            task_id: preflight_engine.load_task(task_id) for task_id in task_ids
        }
    finally:
        preflight_engine.close()
    present_sources = {task.origin.value for task in scheduled_tasks.values()}
    missing_sources = sorted(required_source_values - present_sources)
    if missing_sources:
        raise _pipeline_support.CliUsageError(
            f"registered pipeline roster is missing required sources: {missing_sources}"
        )
    if len(required_source_values) > requested_size:
        raise _pipeline_support.CliUsageError(
            f"--size {requested_size} cannot represent {len(required_source_values)} "
            "required sources"
        )

    attestation = None
    if args.release_dir is not None:
        if args.sandbox_attestation is not None:
            from elt_taskgen.export.attestation_gate import (
                AttestationRefusal,
                require_attestation_for_labels,
            )
            from elt_taskgen.runtime.attestation import (
                SandboxAttestation,
                SandboxAttestationError,
                verify_sandbox_attestation,
            )
            from elt_taskgen.verification import contamination as contamination_mod

            try:
                attestation = verify_sandbox_attestation(
                    SandboxAttestation.model_validate_json(
                        Path(args.sandbox_attestation).read_text(encoding="utf-8")
                    )
                )
                require_attestation_for_labels(
                    {
                        "corpus_profile": release_mod.COMBINED_CORPUS_PROFILE,
                        "public_layout": release_mod.COMBINED_PUBLIC_LAYOUT,
                        "variants": {
                            task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)
                            for task_id in task_ids
                        },
                    },
                    attestation,
                    contamination_mode=contamination_mod.enforcement(),
                    agents_config=args.agents_config,
                )
            except (
                OSError,
                ValueError,
                SandboxAttestationError,
                AttestationRefusal,
            ) as exc:
                raise _pipeline_support.CliUsageError(f"invalid sandbox attestation: {exc}") from None

    # The duplicate firewall must serialize admission of fresh candidates. If
    # this happened inside workers, two clones could each observe an admitted
    # corpus that did not yet contain the other and both incur provider spend.
    intake_engine = open_engine(workspace, max_repair_rounds=0)
    try:
        preflight_stopped, preflight_results = _pipeline_generation._pipeline_batch_intake_preflight(
            intake_engine, scheduled_tasks
        )
    finally:
        intake_engine.close()

    configured_spec_payload = getattr(args, "configured_run_spec", None)
    payloads = [
        worker_payload(
            args,
            task_id,
            task_budget=per_task_total,
            run_id=str(getattr(args, "configured_run_id", "") or ""),
            enforce_evidence_contract=configured_spec_payload is not None,
            run_spec=configured_spec_payload,
        )
        for task_id in task_ids
        if task_id not in preflight_stopped
    ]
    max_workers = min(int(args.workers), len(payloads))
    print(
        f"pipeline: {len(task_ids)} task(s), {max_workers} isolated worker(s), "
        + (
            "empirical difficulty required"
            if not args.allow_structural_difficulty
            else "DEVELOPMENT structural-only difficulty allowed"
        )
    )
    results: list[dict] = list(preflight_results)
    for result in preflight_results:
        print(f"  {result['task_id']}: {result['state']}")
    from elt_taskgen.pipeline.workers import run_workers

    results = run_workers(
        payloads, worker=worker, max_workers=max_workers,
        initial_results=preflight_results, failure_policy="raise_parallel",
    )
    total_usd = sum(float(row.get("usd", 0.0)) for row in results)
    print(f"pipeline live spend: ${total_usd:.4f}")
    rejected_workers = [
        row for row in results if row.get("state") == FINAL_REJECTED
    ]
    operational_failures = [
        row
        for row in results
        if not row.get("ok") and row.get("state") != FINAL_REJECTED
    ]
    for row in rejected_workers:
        print(f"  {row['task_id']}: {row['detail']}")
    if operational_failures and not allow_partial_candidates:
        for row in operational_failures:
            print(f"  {row['task_id']}: {row['detail']}")
        return 2

    ready_ids = tuple(row["task_id"] for row in results if row.get("ok"))
    if not ready_ids:
        for row in operational_failures:
            print(f"  {row['task_id']}: {row['detail']}")
        print("no candidate reached release preparation; nothing to select")
        return 2 if operational_failures else 1
    selection_target = (
        min(requested_size, len(ready_ids))
        if allow_partial_candidates
        else requested_size
    )
    coordinator = open_engine(workspace, max_repair_rounds=0)
    coordinator.expected_campaign_fingerprint = expected_campaign_fingerprint
    try:
        def derive_selection():
            """Re-read every input to the deterministic cohort decision."""

            current_tasks = [
                coordinator.load_task(task_id) for task_id in ready_ids
            ]
            measurements: dict[str, DifficultyMeasurement] = {}
            accepted_variants: dict[str, frozenset[str]] = {}
            for current_task in current_tasks:
                path = _pipeline_support._evidence_dir(coordinator, current_task) / "difficulty.json"
                try:
                    measurements[current_task.task_id] = (
                        DifficultyMeasurement.model_validate_json(
                            path.read_text(encoding="utf-8")
                        )
                    )
                except (OSError, ValueError) as exc:
                    raise _pipeline_support.CliUsageError(
                        "difficulty evidence for prepared task "
                        f"{current_task.task_id!r} is unreadable: {exc}"
                    ) from None
                accepted_variants[current_task.task_id] = _pipeline_release._accepted_variants(
                    coordinator, current_task
                )
            try:
                anchors = eltbench_anchor.load_anchor_store(workspace)
            except FileNotFoundError:
                anchors = []
            current_selection = selection_mod.select(
                current_tasks,
                measurements,
                anchors,
                selection_mod.Quotas(
                    size=selection_target,
                    val_fraction=val_fraction,
                    origin_mix={
                        # ``--require-sources`` is a coverage floor, not a
                        # requested distribution. Give every required origin a
                        # one-slot deficit; remaining slots keep stable order.
                        Origin(value): 1.0 / selection_target
                        for value in sorted(required_source_values)
                    }
                    if required_source_values
                    else {},
                ),
                accepted_variants=accepted_variants,
                require_empirical=not bool(args.allow_structural_difficulty),
                expected_campaign_fingerprint=expected_campaign_fingerprint,
            )
            if rejected_workers:
                rejected = dict(current_selection.rejected)
                rejected.update(
                    {
                        str(row["task_id"]): str(
                            row.get("detail") or "candidate rejected"
                        )
                        for row in rejected_workers
                    }
                )
                current_selection = current_selection.model_copy(
                    update={"rejected": rejected}
                )
            return current_selection, current_tasks

        selection, tasks = derive_selection()
        selected = tuple(selection.train) + tuple(selection.val)
        if len(selected) != selection_target:
            reasons = "; ".join(
                f"{task_id}: {reason}"
                for task_id, reason in sorted(selection.rejected.items())
            )
            print(
                f"corpus selection produced {len(selected)}/{selection_target} tasks: "
                f"{reasons}"
            )
            if not allow_partial_candidates or not selected:
                return 1

        selected_tasks = {
            task.task_id: task for task in tasks if task.task_id in selected
        }
        selected_sources = {task.origin.value for task in selected_tasks.values()}
        missing_selected_sources = sorted(required_source_values - selected_sources)
        if missing_selected_sources:
            print(
                "corpus selection omitted required sources: "
                f"{missing_selected_sources}"
            )
            return 1

        # The tentative decision is not published globally until every selected
        # task's selection passes and a lock-scoped re-derivation proves that
        # none of its task/evidence inputs changed in the meantime.
        selected_set = frozenset(selected)

        def run_global_selection(_engine: Engine, task: TaskIR) -> StageOutcome:
            if task.task_id not in selected_set:
                return StageOutcome(
                    VERDICT_FATAL,
                    StagePayload(error="task is outside the global corpus selection"),
                )
            _pipeline_support._atomic_replace_text(
                _pipeline_support._evidence_dir(_engine, task) / "selection.json",
                canonical_json(selection.model_dump(mode="json")),
            )
            return StageOutcome(VERDICT_PASS, selection)

        coordinator.set_stage_runner(StageName.SELECT, run_global_selection)
        selection_rejections: list[tuple[str, str]] = []
        selection_blocks: list[tuple[str, str]] = []
        for task_id in selected:
            def locked_global_selection_pre_run(
                locked_engine: Engine, task: TaskIR
            ) -> None:
                # A fresh corpus decision must be re-derived under the lock, so
                # a stale SELECT row cannot appear to endorse a decision it never
                # observed.
                stage = StageName.SELECT
                row = locked_engine.latest_report(task.task_id, stage.value)
                if row is not None and locked_engine.report_is_current(
                    task, stage, row
                )[0]:
                    _pipeline_support._force_stage_rerun(
                        locked_engine,
                        task,
                        stage,
                        "re-derived by the corpus-wide pipeline selection",
                    )

            try:
                coordinator.run(
                    task_id,
                    until=StageName.SELECT.value,
                    pre_run=locked_global_selection_pre_run,
                )
            except InfrastructureFailure as exc:
                selection_blocks.append((task_id, str(exc)))
                continue
            task = coordinator.load_task(task_id)
            blocked = coordinator.blocked_stage(task_id)
            if blocked is not None:
                try:
                    detail = str(json.loads(blocked.payload_json).get("error") or "")
                except (TypeError, ValueError):
                    detail = "selection is blocked with unreadable evidence"
                selection_blocks.append((task_id, detail))
                continue
            select = coordinator.latest_report(task_id, StageName.SELECT.value)
            if coordinator.final_verdict(task_id) == FINAL_REJECTED:
                detail = "selection rejected this candidate"
                if select is not None:
                    try:
                        detail = str(
                            json.loads(select.payload_json).get("error") or detail
                        )
                    except (TypeError, ValueError):
                        pass
                selection_rejections.append((task_id, detail))
            elif not coordinator.report_is_current(task, StageName.SELECT, select)[0]:
                selection_blocks.append(
                    (task_id, "selection did not pass at the current content hash")
                )

        if selection_blocks:
            for task_id, detail in selection_blocks:
                print(f"  {task_id}: {detail}")
            return 2
        if selection_rejections:
            for task_id, detail in selection_rejections:
                print(f"  {task_id}: {detail}")
            return 1

        # One workspace publication epoch serializes even DISJOINT pipeline
        # rosters, which still share corpus_selection.json.  Every scheduled
        # task is then locked in stable order while selection inputs, stage
        # currency and the optional release snapshot are re-read.
        with coordinator.coordinator_lock():
            with coordinator.task_locks(task_ids):
                for task_id in task_ids:
                    coordinator.recover_pending_repair(task_id)
                locked_selection, _locked_tasks = derive_selection()
                if locked_selection.model_dump(mode="json") != selection.model_dump(
                    mode="json"
                ):
                    raise _pipeline_support.CliUsageError(
                        "pipeline selection inputs changed between selection "
                        "and publication; nothing was published — re-run the "
                        "pipeline"
                    )

                locked_selected = tuple(locked_selection.train) + tuple(
                    locked_selection.val
                )
                for task_id in locked_selected:
                    task = coordinator.load_task(task_id)
                    for stage in STAGE_ORDER:
                        if stage is StageName.RELEASE:
                            break
                        row = coordinator.latest_report(task_id, stage.value)
                        current, why = coordinator.report_is_current(task, stage, row)
                        if not current:
                            raise _pipeline_support.CliUsageError(
                                f"task {task_id!r} changed before corpus "
                                f"publication: stage {stage.value!r}: {why}"
                            )
                    final = coordinator.final_verdict(task_id)
                    if final != FINAL_ACCEPTED:
                        raise _pipeline_support.CliUsageError(
                            f"task {task_id!r} changed before corpus publication: "
                            f"final verdict is {final!r}"
                        )
                    if configured_readiness_spec is not None:
                        from elt_taskgen.pipeline_readiness import (
                            seal_configured_stage,
                        )

                        seal_configured_stage(
                            coordinator,
                            task,
                            StageName.SELECT,
                            configured_readiness_spec,
                        )

                selection = locked_selection
                serialized_selection = (
                    readable_json(selection.model_dump(mode="json")) + "\n"
                )
                selection_path = workspace / "state" / "corpus_selection.json"
                _pipeline_support._atomic_replace_text(selection_path, serialized_selection)
                for task_id in locked_selected:
                    task = coordinator.load_task(task_id)
                    _pipeline_support._atomic_replace_text(
                        _pipeline_support._evidence_dir(coordinator, task) / "selection.json",
                        canonical_json(selection.model_dump(mode="json")),
                    )
                print(
                    f"corpus selection: {selection_path} "
                    f"({len(locked_selected)} task(s))"
                )

                if args.release_dir is None:
                    return 0

                out = Path(args.release_dir).resolve()
                if out.exists():
                    verification = release_mod.verify_release(out)
                    if not verification.ok:
                        raise _pipeline_support.CliUsageError(
                            "existing immutable release failed verification: "
                            + "; ".join(verification.failures[:3])
                        )
                    try:
                        manifest = release_mod.ReleaseManifest.model_validate_json(
                            (out / "release_manifest.json").read_text(
                                encoding="utf-8"
                            )
                        )
                    except (OSError, ValueError) as exc:
                        raise _pipeline_support.CliUsageError(
                            f"existing release manifest is unreadable: {exc}"
                        ) from None
                    expected_splits = {
                        task_id: "train" for task_id in selection.train
                    } | {task_id: "val" for task_id in selection.val}
                    mismatch = []
                    if manifest.schema_version != release_mod.RELEASE_SCHEMA_VERSION:
                        mismatch.append(
                            f"schema {manifest.schema_version!r} is not current"
                        )
                    if (
                        manifest.corpus_profile
                        != release_mod.COMBINED_CORPUS_PROFILE
                    ):
                        mismatch.append("corpus profile differs")
                    if (
                        manifest.public_layout
                        != release_mod.COMBINED_PUBLIC_LAYOUT
                    ):
                        mismatch.append("public layout differs")
                    if manifest.release_mode != release_mode:
                        mismatch.append(
                            f"mode {manifest.release_mode!r} != {release_mode!r}"
                        )
                    if manifest.tasks != selection.task_content_hashes:
                        mismatch.append("task identities differ")
                    if manifest.splits != expected_splits:
                        mismatch.append("train/val splits differ")
                    if manifest.variants != selection.variants:
                        mismatch.append("variant roster differs")
                    if (
                        manifest.difficulty_measurements
                        != selection.difficulty_measurements
                    ):
                        mismatch.append("difficulty evidence differs")
                    if mismatch:
                        raise _pipeline_support.CliUsageError(
                            "pipeline release directory already contains a "
                            "different immutable release (move it aside): "
                            + "; ".join(mismatch)
                        )
                    adopted = True
                else:
                    try:
                        manifest = release_mod.freeze_release(
                            coordinator,
                            selection,
                            out,
                            allow_unlocked_env=bool(args.allow_unlocked_env),
                            release_mode=release_mode,
                            sandbox_attestation=attestation,
                            agents_config=args.agents_config,
                        )
                    except (OSError, ValueError) as exc:
                        raise _pipeline_support.CliUsageError(str(exc)) from None
                    verification = release_mod.verify_release(out)
                    if not verification.ok:
                        raise _pipeline_support.CliUsageError(
                            "new release failed self-verification: "
                            + "; ".join(verification.failures[:3])
                        )
                    adopted = False

                release_manifest_sha = hashlib.sha256(
                    (out / "release_manifest.json").read_bytes()
                ).hexdigest()
                for task_id in locked_selected:
                    released_task = coordinator.load_task(task_id)
                    coordinator.record_report(
                        released_task,
                        StageName.RELEASE.value,
                        VERDICT_PASS,
                        StagePayload(
                            detail=(
                                f"batch release {manifest.release_id} verified at "
                                "the configured output path"
                            ),
                            data={
                                "release_id": manifest.release_id,
                                "release_dir": str(out),
                                "release_manifest_sha256": release_manifest_sha,
                                "verified_files": str(verification.files_checked),
                            },
                        ),
                    )
                    if configured_readiness_spec is not None:
                        from elt_taskgen.pipeline_readiness import (
                            seal_configured_stage,
                        )

                        seal_configured_stage(
                            coordinator,
                            released_task,
                            StageName.RELEASE,
                            configured_readiness_spec,
                        )
                _pipeline_support._atomic_replace_text(
                    workspace / "state" / "corpus_release.json",
                    readable_json(
                        {
                            "release_id": manifest.release_id,
                            "release_dir": str(out),
                            "release_mode": manifest.release_mode,
                            "tasks": manifest.tasks,
                            "manifest_sha256": release_manifest_sha,
                            "files_verified": verification.files_checked,
                        }
                    )
                    + "\n",
                )
                verb = "adopted and re-verified" if adopted else "created"
                print(
                    f"release {manifest.release_id}: {out} ({verb}; "
                    f"{verification.files_checked} file(s) verified)"
                )
                return 0
    finally:
        coordinator.close()

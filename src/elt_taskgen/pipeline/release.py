"""Select accepted tasks and publish verified release artifacts."""

from __future__ import annotations

import json

from elt_taskgen.engine import (
    BLOCKED_ON_ENVIRONMENT,
    BLOCKED_ON_KEY,
    Engine,
    StageOutcome,
    StagePayload,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
)
from elt_taskgen.models import (
    DifficultyMeasurement,
    RLVR_TASK_VARIANTS,
    TaskIR,
    canonical_json,
    sha256_hex,
)
from elt_taskgen.pipeline import generation as _pipeline_generation
from elt_taskgen.pipeline import payloads as _pipeline_payloads
from elt_taskgen.pipeline import support as _pipeline_support


def run_contamination_post(engine: Engine, task: TaskIR) -> StageOutcome:
    """`contamination_post`: post-generation scan verdict row (same service as check_pre)."""
    idx = _pipeline_generation._contamination_index(engine)
    _pipeline_generation._arm_contamination_index(idx, announce=False)
    result = idx.scan_post(
        task,
        _pipeline_support._public_dir(engine, task),
        _pipeline_support._answer_key_dir(engine, task),
        require=_pipeline_generation._required_firewall_coverage(),
    )
    collisions = list(result.collisions)
    evidence = {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "collisions": [c.model_dump(mode="json") for c in collisions],
        # The contamination-clean gate reads this file; the coverage travels with it
        # so the evidence can never be a bare "0 collisions".
        "coverage": _pipeline_generation._coverage_payload(engine, result.coverage),
        "coverage_summary": result.coverage.summary(),
    }
    (_pipeline_support._evidence_dir(engine, task) / "contamination_post.json").write_text(
        canonical_json(evidence), encoding="utf-8"
    )
    fatal = [c for c in collisions if c.fatal]
    payload = _pipeline_payloads.ContaminationPayload(
        call_point="post",
        collisions=tuple(c.model_dump(mode="json") for c in collisions),
        fatal_count=len(fatal),
        coverage_level=result.coverage.level.value,
        coverage=_pipeline_generation._coverage_payload(engine, result.coverage),
        detail=result.detail(),
    )
    if fatal:
        return StageOutcome(VERDICT_FATAL, payload)
    return StageOutcome(VERDICT_PASS, payload)


def _accepted_variants(engine: Engine, task: TaskIR) -> frozenset[str]:
    """Variant values whose OWN battery passed at the CURRENT content hash.

    Read through export.release.variant_acceptance so selection, release and this
    CLI share one acceptance semantics — a second opinion would be a second one."""
    from elt_taskgen.export import release as release_mod

    records = release_mod.variant_acceptance(engine, task)
    return frozenset(name for name, rec in records.items() if rec.accepted)


def run_select(engine: Engine, task: TaskIR) -> StageOutcome:
    """`select`: corpus selection. Per-task orchestration selects a pool of
    one; corpus-wide quota selection over many tasks uses corpus/selection.py
    directly with the same predicate."""
    from elt_taskgen.adapters import eltbench_anchor
    from elt_taskgen.corpus import selection as selection_mod

    diff_path = _pipeline_support._evidence_dir(engine, task) / "difficulty.json"
    if not diff_path.is_file():
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error=f"no difficulty measurement at {diff_path} (fail closed)"),
        )
    measurement = DifficultyMeasurement.model_validate_json(
        diff_path.read_text(encoding="utf-8")
    )
    require_empirical = bool(
        getattr(engine, "require_empirical_difficulty", False)
    )
    if require_empirical:
        problem = selection_mod.empirical_evidence_problem(
            measurement,
            expected_campaign_fingerprint=getattr(
                engine, "expected_campaign_fingerprint", None
            ),
        )
        if problem is not None:
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error=(
                        f"selection requires empirical difficulty: {problem}; "
                        "run `calibrate --empirical` and resume"
                    ),
                    data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT},
                ),
            )
    try:
        anchors = eltbench_anchor.load_anchor_store(engine.workspace)
    except FileNotFoundError:
        anchors = []

    quotas = selection_mod.Quotas(size=1, val_fraction=0.0)
    result = selection_mod.select(
        [task],
        {task.task_id: measurement},
        anchors,
        quotas,
        accepted_variants={task.task_id: _accepted_variants(engine, task)},
        require_empirical=require_empirical,
        expected_campaign_fingerprint=getattr(
            engine, "expected_campaign_fingerprint", None
        ),
    )
    if task.task_id in result.rejected:
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error=f"selection rejected: {result.rejected[task.task_id]}"),
        )
    _pipeline_support._atomic_replace_text(
        _pipeline_support._evidence_dir(engine, task) / "selection.json",
        canonical_json(result.model_dump(mode="json")),
    )
    return StageOutcome(VERDICT_PASS, result)


def _release_names_el_sources(recorded: dict, task: TaskIR) -> bool:
    """Does an existing release manifest carry this task's EL source roots?

    The EL reward is computed over EVERY graded population, so a manifest WITHOUT
    them is a stale release and 'already released' would be a false statement.
    Capability-checked, so a tree whose exporter predates the field is unaffected
    rather than permanently blocked."""
    from elt_taskgen.export import release as release_mod

    if "el_sources" not in release_mod.ReleaseManifest.model_fields:
        return True  # exporter predates EL source shipping: nothing to require
    return bool(recorded.get("el_sources", {}).get(task.task_id))


def _release_coverage_refusal(engine: Engine) -> str | None:
    """Why `release` must not ship from this workspace's firewall, or None.

    RELEASE IS THE ONLY STAGE THAT EMITS TRAINABLE DATA, so ARMED coverage is
    required UNCONDITIONALLY here while every other call point stays warn-only (the
    tests and offline paths have no benchmark checkout). A name-only index answers
    "is this task NAMED like a benchmark", which is not a firewall."""
    from elt_taskgen.verification import contamination as _cont
    if not _cont.enforcing():
        return None  # coverage is recorded, not required
    from elt_taskgen.verification import contamination as cont

    coverage = _pipeline_generation._contamination_index(engine).coverage()
    if coverage.level is cont.CoverageLevel.ARMED:
        return None
    return (
        "contamination coverage is "
        f"{coverage.level.value!r}, not ARMED — release is the only stage that "
        "emits trainable data and it will not ship behind a name-only check. "
        f"{coverage.summary()} Run: elt-taskgen measure-target --workspace "
        "<ws> --bench-root <ELT-Bench checkout>."
    )


def _environment_drift_refusal(args_allow_unlocked: bool) -> tuple[str | None, dict[str, str]]:
    """(refusal or None, recorded environment provenance).

    A release cut from an environment that does not match uv.lock either says so or
    does not happen; `--allow-unlocked-env` records the drift instead of hiding it.
    The check itself lives in export/release.py; this pre-check exists so the answer
    is BLOCKED(environment) — a wait on `uv sync --frozen` — instead of a FAIL that
    spends a repair round and rejects the task for an environment condition."""
    from elt_taskgen.export import release as release_mod

    drift_fn = getattr(release_mod, "environment_drift", None)
    if drift_fn is None:  # exporter predates the provenance check
        return None, {}
    drift = drift_fn()
    if not drift:
        return None, {}
    described = ", ".join(
        f"{pkg} installed {installed!r} != locked {locked!r}"
        for pkg, (installed, locked) in sorted(drift.items())
    )
    if args_allow_unlocked:
        return None, {"environment_drift": described}
    return (
        f"runtime environment does not match uv.lock: {described}. Run "
        "`uv sync --frozen`, or pass --allow-unlocked-env to release anyway "
        "(the drift is then RECORDED in the release manifest, never hidden)."
    ), {}


def _existing_certified_attestation_problem(
    engine: Engine,
    task: TaskIR,
    recorded: dict,
) -> str | None:
    """Why a supplied attestation cannot authorize an existing certified tree."""
    from elt_taskgen.export import release as release_mod
    from elt_taskgen.export.attestation_gate import (
        AttestationRefusal,
        require_attestation_for_labels,
    )
    from elt_taskgen.runtime.attestation import (
        SandboxAttestationError,
        seal_sandbox_attestation,
        verify_sandbox_attestation,
    )
    from elt_taskgen.verification import contamination as contamination_mod

    supplied = getattr(engine, "sandbox_attestation", None)
    if supplied is None:
        return "the requested certified release has no sandbox attestation"
    release_id = str(recorded.get("release_id") or "")
    if not release_id:
        return "the existing certified release has no release identity"
    try:
        supplied = verify_sandbox_attestation(supplied)
        if not supplied.run_id:
            supplied = seal_sandbox_attestation(
                supplied.model_copy(
                    update={"run_id": release_id, "attestation_digest": ""}
                )
            )
        decision = require_attestation_for_labels(
            {
                "corpus_profile": release_mod.COMBINED_CORPUS_PROFILE,
                "public_layout": release_mod.COMBINED_PUBLIC_LAYOUT,
                "variants": {
                    task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)
                },
            },
            supplied,
            contamination_mode=contamination_mod.enforcement(),
            run_id=release_id,
        )
    except (AttestationRefusal, SandboxAttestationError, ValueError) as exc:
        return (
            "the requested sandbox attestation is not current for this release: "
            f"{exc}"
        )
    recorded_digest = str(recorded.get("sandbox_attestation_digest") or "")
    if decision.attestation_digest != recorded_digest:
        return (
            "the requested sandbox attestation does not match the existing "
            "certified release"
        )
    return None


def run_release(engine: Engine, task: TaskIR) -> StageOutcome:
    """`release`: frozen release (immutable tree; freeze never decides
    acceptance — it re-verifies the ledger and refuses anything unaccepted)."""
    from elt_taskgen.corpus.selection import SelectionResult
    from elt_taskgen.export import release as release_mod

    sel_path = _pipeline_support._evidence_dir(engine, task) / "selection.json"
    if not sel_path.is_file():
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error=f"no selection record at {sel_path} (fail closed)"),
        )
    selection = SelectionResult.model_validate_json(
        sel_path.read_text(encoding="utf-8")
    )
    requested_release_mode = str(getattr(engine, "release_mode", "development"))
    require_empirical = bool(
        getattr(engine, "require_empirical_difficulty", False)
    ) or requested_release_mode == release_mod.CERTIFIED_RELEASE_MODE
    difficulty_digest = ""
    if require_empirical:
        if not selection.empirical_required:
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error=(
                        "release requires an empirical selection record; the "
                        "current selection was structural-only — re-run `select`"
                    ),
                    data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT},
                ),
            )
        diff_path = _pipeline_support._evidence_dir(engine, task) / "difficulty.json"
        if not diff_path.is_file():
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error="release requires empirical difficulty evidence",
                    data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT},
                ),
            )
        from elt_taskgen.corpus import selection as selection_mod

        measurement = DifficultyMeasurement.model_validate_json(
            diff_path.read_text(encoding="utf-8")
        )
        if (
            measurement.task_id != task.task_id
            or measurement.task_content_hash != task.content_hash()
        ):
            problem = "difficulty evidence is not bound to the current task identity"
        else:
            problem = selection_mod.empirical_evidence_problem(
                measurement,
                expected_campaign_fingerprint=getattr(
                    engine, "expected_campaign_fingerprint", None
                ),
            )
        if problem is not None:
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error=f"release requires empirical difficulty: {problem}",
                    data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT},
                ),
            )
        difficulty_digest = sha256_hex(
            canonical_json(measurement.model_dump(mode="json"))
        )

    out_dir = engine.workspace / "release"
    if out_dir.exists():
        manifest_path = out_dir / "release_manifest.json"
        if manifest_path.is_file():
            recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_units = [v.value for v in RLVR_TASK_VARIANTS]
            recorded_mode = str(
                recorded.get("release_mode")
                or release_mod.DEVELOPMENT_RELEASE_MODE
            )
            policy_problem = None
            if recorded_mode != requested_release_mode:
                policy_problem = (
                    f"existing release mode is {recorded_mode!r}, but this run "
                    f"requires {requested_release_mode!r}"
                )
            elif require_empirical and (
                recorded.get("difficulty_measurements", {}).get(task.task_id)
                != difficulty_digest
            ):
                policy_problem = (
                    "existing release does not bind the current empirical "
                    "difficulty measurement"
                )
            elif requested_release_mode == release_mod.CERTIFIED_RELEASE_MODE:
                policy_problem = _existing_certified_attestation_problem(
                    engine, task, recorded
                )
            if (
                policy_problem is None
                and recorded.get("tasks", {}).get(task.task_id) == task.content_hash()
                and recorded.get("corpus_profile")
                == release_mod.COMBINED_CORPUS_PROFILE
                and recorded.get("public_layout")
                == release_mod.COMBINED_PUBLIC_LAYOUT
                and recorded.get("variants", {}).get(task.task_id) == expected_units
                and _release_names_el_sources(recorded, task)
            ):
                # NAMING THIS TASK IS NOT THE SAME AS CONTAINING IT: the shortcut
                # trusted the manifest's own words while a tampered file left
                # verify_release ok=False. Re-verify the BYTES before saying done.
                result = release_mod.verify_release(out_dir)
                if not result.ok:
                    # SAME CONDITION AS ITS SIBLING BELOW: a corrupt tree in the way
                    # is the ENVIRONMENT, not a task defect. As an ordinary FAIL it
                    # spends the budget re-running deterministic stages and ends in
                    # the inert/budget FATAL that rejects a certified task.
                    return StageOutcome(
                        VERDICT_BLOCKED,
                        StagePayload(
                            error=(
                                f"release dir {out_dir} names this task at the "
                                "current content hash but FAILS byte "
                                "verification: "
                                + "; ".join(result.failures[:3])
                                + " — release output is immutable; move it "
                                "aside to re-release"
                            ),
                            data={
                                "release_dir": _pipeline_support._workspace_rel(engine, out_dir),
                                BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT,
                            },
                        ),
                    )
                # Idempotent on the shortcut path too: the admitted-corpus index is
                # what stops a later near-duplicate from being ingested.
                _pipeline_generation._contamination_index(engine).add_admitted_task(task)
                return StageOutcome(
                    VERDICT_PASS,
                    StagePayload(
                        detail=(
                            f"already released as {recorded.get('release_id', '?')} "
                            f"({result.files_checked} file(s) re-verified)"
                        ),
                        data={"release_dir": _pipeline_support._workspace_rel(engine, out_dir)},
                    ),
                )
            if policy_problem is not None:
                return StageOutcome(
                    VERDICT_BLOCKED,
                    StagePayload(
                        error=(
                            f"release dir {out_dir} cannot satisfy the requested "
                            f"release policy: {policy_problem} — release output is "
                            "immutable; move it aside to re-release"
                        ),
                        data={
                            "release_dir": _pipeline_support._workspace_rel(engine, out_dir),
                            BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT,
                        },
                    ),
                )
        # NOT A DEFECT AND NOT A REPAIR: a directory in the way is the ENVIRONMENT.
        # Routed as an ordinary failure this rejected a certified task; the operator
        # moves it aside and re-runs.
        return StageOutcome(
            VERDICT_BLOCKED,
            StagePayload(
                error=f"release dir {out_dir} exists with different content; "
                "release output is immutable (move it aside to re-release)",
                data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT},
            ),
        )

    unarmed = _release_coverage_refusal(engine)
    if unarmed is not None:
    # Block environmental contamination-readiness refusals; do not route them fatal.
        return StageOutcome(
            VERDICT_BLOCKED,
            StagePayload(
                error=unarmed, data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT}
            ),
        )
    drift, environment_data = _environment_drift_refusal(
        bool(getattr(engine, "allow_unlocked_env", False))
    )
    if drift is not None:
        return StageOutcome(
            VERDICT_BLOCKED,
            StagePayload(
                error=drift, data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT}
            ),
        )

    try:
        manifest = release_mod.freeze_release(
            engine,
            selection,
            out_dir,
            release_mode=str(getattr(engine, "release_mode", "development")),
            sandbox_attestation=getattr(engine, "sandbox_attestation", None),
            agents_config=getattr(engine, "agents_config", None),
        )
    except Exception as exc:  # map evidence/isolation refusals to a resumable wait
        from elt_taskgen.export.attestation_gate import AttestationRefusal

        if isinstance(exc, AttestationRefusal):
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error=f"release attestation refused [{exc.code}]: {exc}",
                    data={
                        BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT,
                        "refusal_code": exc.code,
                    },
                ),
            )
        raise
    # SELF-CHECK THE FREEZE: verify_release was called from nowhere in src/, and the
    # README's `shasum -c` remedy skips every census-pinned .duckdb warehouse. A
    # partial write must fail the stage, not be discovered by a consumer.
    verified = release_mod.verify_release(out_dir)
    if not verified.ok:
        # A HALF-WRITTEN TREE IS THE ENVIRONMENT, NOT THE TASK: no edit to prose,
        # reference SQL or population conditions can make a partial write verify, so
        # a repair round here ends in the FATAL that rejects a certified task.
        return StageOutcome(
            VERDICT_BLOCKED,
            StagePayload(
                error=(
                    f"the freeze at {out_dir} does not verify against its own "
                    "manifest: " + "; ".join(verified.failures[:3])
                    + " — release output is immutable; move it aside and "
                    "re-run `release`"
                ),
                data={
                    "release_dir": _pipeline_support._workspace_rel(engine, out_dir),
                    BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT,
                },
            ),
        )
    _pipeline_generation._contamination_index(engine).add_admitted_task(task)
    return StageOutcome(
        VERDICT_PASS,
        StagePayload(
            detail=f"frozen release {manifest.release_id} "
            f"({len(manifest.tasks)} task(s), {len(manifest.checksums)} files)",
            data={
                "release_id": manifest.release_id,
                "release_dir": _pipeline_support._workspace_rel(engine, out_dir),
                "scorer_version": manifest.scorer_version,
                "generator_version": manifest.generator_version,
                "verified_files": str(verified.files_checked),
                **environment_data,
            },
        ),
    )

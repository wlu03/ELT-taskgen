"""Measure structural or empirical difficulty without erasing cached evidence."""

from __future__ import annotations

from pathlib import Path

from elt_taskgen.engine import Engine, StageOutcome, VERDICT_FAIL, VERDICT_PASS
from elt_taskgen.models import RepairRoute, TaskIR, TaskVariant, canonical_json
from elt_taskgen.pipeline import payloads as _pipeline_payloads
from elt_taskgen.pipeline import support as _pipeline_support


def _write_difficulty(engine: Engine, task: TaskIR, measurement) -> None:
    (_pipeline_support._evidence_dir(engine, task) / "difficulty.json").write_text(
        canonical_json(measurement.model_dump(mode="json")), encoding="utf-8"
    )


def _calibrate_outcome(engine: Engine, task: TaskIR, measurement, result) -> StageOutcome:
    """THE ONE place a calibration result becomes a ledger verdict.

    Shared by the structural runner (reading the cache) and the empirical one, so
    "no pinned solver can pass this task" means the same thing whichever command
    ran. Difficulty evidence is written here from the MERGED measurement — the
    structural runner used to overwrite difficulty.json with `empirical: null`."""
    from elt_taskgen.corpus import difficulty as difficulty_mod

    if result.empirical is not None:
        # The seam (never model_copy): with_empirical re-checks the
        # content-hash binding and refuses stale evidence.
        measurement = difficulty_mod.with_empirical(measurement, result.empirical)
    _write_difficulty(engine, task, measurement)

    pass_rates = {
        variant: record.pass_rate_vector()
        for variant, record in sorted(result.records.items())
    }
    if not result.records:
        detail = (
            f"structural difficulty only — {result.skipped_reason}"
            if result.skipped_reason
            else "structural difficulty only (empirical calibration not requested)"
        )
    else:
        detail = (
            f"calibrated {len(result.records)} variant(s) on roster "
            f"{result.roster_fingerprint[:12]}"
            + (f"; cached: {', '.join(result.from_cache)}" if result.from_cache else "")
            + (
                f"; IMPOSSIBLE: {', '.join(result.impossible_variants)} "
                "(routed to feasibility re-review)"
                if result.impossible_variants
                else ""
            )
            + (
                f"; trivial: {', '.join(result.trivial_variants)}"
                if result.trivial_variants
                else ""
            )
            + (f"; {result.skipped_reason}" if result.skipped_reason else "")
        )
    payload = _pipeline_payloads.CalibratePayload(
        measurement=measurement,
        # NOTHING MEASURED, NOTHING TO FINGERPRINT: an empty roster fingerprint says
        # "no campaign stands behind this row", the structural-only case.
        roster_fingerprint=result.roster_fingerprint if result.records else "",
        pass_rates=pass_rates,
        variants_from_cache=result.from_cache,
        impossible_variants=result.impossible_variants,
        trivial_variants=result.trivial_variants,
        skipped_reason=result.skipped_reason,
        detail=detail,
    )
    if result.impossible_variants:
    # Route measured infeasibility to specification repair and re-review.
        return StageOutcome(VERDICT_FAIL, payload, route=RepairRoute.SPECIFICATION)
    return StageOutcome(VERDICT_PASS, payload)


def make_structural_calibrate_runner(agents_config: Path | None = None):
    """The default, offline calibrate runner: structural difficulty, plus any
    empirical result ALREADY CACHED at this identity.

    Nothing here calls a provider, but a campaign that already ran at this content
    hash and roster is EVIDENCE, and another command must not erase it."""

    def run_calibrate_structural(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.corpus import calibration as calibration_mod
        from elt_taskgen.corpus import difficulty as difficulty_mod

        measurement = difficulty_mod.structural_difficulty(task)
        result = calibration_mod.cached_calibration(
            task, engine.workspace, agents_config=agents_config
        )
        return _calibrate_outcome(engine, task, measurement, result)

    return run_calibrate_structural


#: Module-level default so `cli.run_calibrate` stays importable by name.
run_calibrate = make_structural_calibrate_runner()


def make_calibrate_runner(
    provider,
    *,
    empirical: bool = False,
    variants: tuple[TaskVariant, ...] | None = None,
    refresh: bool = False,
    agents_config: Path | None = None,
):
    """`calibrate` with the SolverCalibrator wired in (`--empirical`).

    Each requested variant is calibrated SEPARATELY on the PUBLIC bundle, scored
    solely by upstream_eval and cached by (content hash, variant, roster). Without
    credentials AND transcripts the campaign is a VISIBLE SKIP with the reason
    recorded; no empirical number is ever invented."""
    if not empirical:
        return make_structural_calibrate_runner(agents_config)

    def run_calibrate_empirical(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.corpus import calibration as calibration_mod
        from elt_taskgen.corpus import difficulty as difficulty_mod
        from elt_taskgen.reference import gold as gold_mod

        measurement = difficulty_mod.structural_difficulty(task)
        gold = gold_mod.load_gold(_pipeline_support._answer_key_dir(engine, task))
        result = calibration_mod.calibrate_task(
            task,
            gold,
            engine.workspace,
            provider,
            variants=variants or calibration_mod.DEFAULT_VARIANTS,
            refresh=refresh,
            agents_config=agents_config,
        )
        return _calibrate_outcome(engine, task, measurement, result)

    # Tagged for the repair proposer's trial certifier (review/repair_proposer
    # `CALIBRATE_EMPIRICAL_TAG`): `trial_phase` substitutes the structural
    # runner over the same roster document on the trial copy, so a failure at
    # `calibrate` or later never launches the solver campaign inside a trial.
    run_calibrate_empirical.calibrate_empirical = True
    run_calibrate_empirical.agents_config = agents_config
    return run_calibrate_empirical

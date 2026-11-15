"""Wire the task-production stage runners independently of the CLI."""

from __future__ import annotations

import functools

from elt_taskgen.engine import (
    Engine,
    StageName,
    StageOutcome,
    StagePayload,
    VERDICT_FAIL,
    VERDICT_PASS,
)
from elt_taskgen.models import RepairRoute, TaskIR, TaskVariant
from elt_taskgen.pipeline import attacks as _pipeline_attacks
from elt_taskgen.pipeline import calibration as _pipeline_calibration
from elt_taskgen.pipeline import gates as _pipeline_gates
from elt_taskgen.pipeline import generation as _pipeline_generation
from elt_taskgen.pipeline import release as _pipeline_release
from elt_taskgen.pipeline import review as _pipeline_review


def _with_attack_matrix_precheck(inner):
    """Measure the declared attack matrix the moment gold exists.

    The reference stage freezes gold; that is the first point at which every
    required mutant can be executed against every population, deterministically
    and at no provider cost. A declared kill that does not reproduce is refused
    HERE, routed to POPULATION, rather than two stages later at the author's
    own precheck (which stays: it protects standalone runs of that stage and
    is a memo hit after this one).
    """

    def wrapped(engine: Engine, task: TaskIR) -> StageOutcome:
        outcome = inner(engine, task)
        if outcome.verdict != VERDICT_PASS:
            return outcome
        unreproducible = _pipeline_generation._attack_matrix_failure_detail(engine, outcome.task or task)
        if unreproducible is None:
            return outcome
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error=unreproducible),
            route=RepairRoute.POPULATION,
        )

    return wrapped


def build_stage_runners(
    provider,
    *,
    echo=None,
    calibrate_options=None,
    destination="snowflake",
    extra_destinations=(),
):
    """Wire a runner for every ledger stage except the built-in intake.

    `calibrate_options` opts `calibrate` into the empirical SolverCalibrator;
    omitted, calibration stays structural-only — a solver campaign is never a side
    effect of running the pipeline."""
    runners = {
        # The intake filters gate `contamination_pre` and the execution-effect filter
        # gates `reference`: deterministic, and before any provider call.
        StageName.CONTAMINATION_PRE: _pipeline_generation._with_intake_filters(_pipeline_generation.run_contamination_pre),
        StageName.GENERATE: _pipeline_generation.run_generate,
        StageName.REFERENCE: _with_attack_matrix_precheck(
            _pipeline_generation._with_execution_effect_filter(_pipeline_generation.run_reference_stage)
        ),
        StageName.AUTHOR: _pipeline_review.make_author_runner(provider),
        StageName.REVIEW: _pipeline_review.make_review_runner(provider),
        StageName.ATTACK: _pipeline_attacks.run_attack_stage,
        StageName.TASK_INTEGRITY: _pipeline_gates.make_gates_runner(
            provider, destination=destination, extra_destinations=extra_destinations
        ),
        # The two subtask batteries: same frozen artifacts, same content hash,
        # their OWN rewards, rosters and witnesses (design R1/R2).
        StageName.GATES_EXTRACT_LOAD: _pipeline_gates.make_variant_gates_runner(
            TaskVariant.EXTRACT_LOAD,
            provider,
            destination=destination,
            extra_destinations=extra_destinations,
        ),
        StageName.GATES_TRANSFORM: _pipeline_gates.make_variant_gates_runner(
            TaskVariant.TRANSFORM,
            provider,
            destination=destination,
            extra_destinations=extra_destinations,
        ),
        StageName.CALIBRATE: _pipeline_calibration.make_calibrate_runner(
            provider, **(dict(calibrate_options or {}))
        ),
        StageName.CONTAMINATION_POST: _pipeline_release.run_contamination_post,
        StageName.SELECT: _pipeline_release.run_select,
        StageName.RELEASE: _pipeline_release.run_release,
    }
    if echo is None:
        return runners

    def logged(stage: StageName, runner):
        # `wraps` carries the runner's tags (the empirical `calibrate` tag the
        # proposer's trial certifier reads) through the echo wrapper.
        @functools.wraps(runner)
        def wrapped(engine: Engine, task: TaskIR) -> StageOutcome:
            echo(f"  -> {stage.value} ...")
            outcome = runner(engine, task)
            echo(f"     {stage.value}: {outcome.verdict}")
            return outcome

        return wrapped

    return {stage: logged(stage, runner) for stage, runner in runners.items()}

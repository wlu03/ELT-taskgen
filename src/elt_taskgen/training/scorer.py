"""Score authenticated ``workspace-v1`` artifacts on same-state local replays.

For each population, replay the seal, normalize Terraform intent, load selected
streams with trusted readers, and run dbt on that DuckDB state. Only stable
codes and aggregate rewards leave the harness; private evidence stays internal.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elt_taskgen.models import PopulationName
from elt_taskgen.training.contract import (
    WORKSPACE_ERROR_CLASS_BY_CODE,
    WorkspaceErrorCode,
    WorkspaceFailureClass,
)
from elt_taskgen.training.airbyte_proxy import AirbyteProtocolProxy
from elt_taskgen.training.dbt_runner import (
    DbtErrorCode,
    DbtRunnerLimits,
    DbtRuntimeConfig,
    DbtTrustedFailure,
    run_dbt_project,
)
from elt_taskgen.training.local_sync import (
    LocalSyncExecution,
    LocalSyncHarnessError,
    run_local_sync,
)
from elt_taskgen.training.models import (
    PopulationWorkspaceScore,
    WorkspaceFailure,
    WorkspaceScoreResult,
)
from elt_taskgen.training.package import WorkspacePackage
from elt_taskgen.training.terraform_intent import (
    TerraformIntentGraph,
    TerraformIntentEvaluation,
    TerraformIntentHarnessError,
    evaluate_terraform_intent,
)
from elt_taskgen.training.workspace import (
    AttemptWorkspace,
    SealedWorkspace,
    WorkspaceLifecycleError,
    load_sealed_workspace,
    replay_workspace,
)
from elt_taskgen.verification.gates import GRADED_POPULATIONS


_ZERO_DIGEST = "0" * 64


@dataclass(frozen=True)
class _ScorerDependencies:
    """Narrow injection seam used only by orchestration unit tests."""

    replay: Callable[..., AttemptWorkspace] = replay_workspace
    terraform: Callable[[WorkspacePackage, Path], TerraformIntentEvaluation] = (
        evaluate_terraform_intent
    )
    sync: Callable[..., LocalSyncExecution] = run_local_sync
    dbt: Callable[..., Any] = run_dbt_project


_DEFAULT_DEPENDENCIES = _ScorerDependencies()


def _remove_owned_run_root(root: Path) -> None:
    """Remove the exact scorer-owned tree, including installed read-only dirs."""

    if not root.exists():
        return
    for directory, child_dirs, _files in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        if not current.is_symlink():
            current.chmod(0o700)
        for name in child_dirs:
            child = current / name
            if not child.is_symlink():
                child.chmod(0o700)
    shutil.rmtree(root)


def _code_values(*groups: Sequence[object]) -> tuple[str, ...]:
    """Serialize ordered enum codes without accepting exception messages."""

    result: list[str] = []
    for group in groups:
        for item in group:
            raw = getattr(item, "value", item)
            value = str(raw)
            if value not in result:
                result.append(value)
    return tuple(result)


def _artifact_digest(sealed: SealedWorkspace | Path) -> str:
    if isinstance(sealed, SealedWorkspace):
        return sealed.submission.artifact_sha256
    return _ZERO_DIGEST


def _failure_result(
    package: WorkspacePackage,
    *,
    artifact_sha256: str,
    valid_submission: bool,
    error_code: WorkspaceErrorCode,
) -> WorkspaceScoreResult:
    """Build a classified result without publishing the originating detail."""

    classification = WORKSPACE_ERROR_CLASS_BY_CODE[error_code]
    failure = WorkspaceFailure(
        classification=classification,
        error_code=error_code,
    )
    return WorkspaceScoreResult(
        release_id=package.manifest.release_id,
        task_id=package.task_id,
        task_content_hash=package.task.content_hash(),
        artifact_sha256=artifact_sha256,
        valid_submission=valid_submission,
        reward=0.0 if classification.label_eligible else None,
        failure=failure,
    )


def _trusted_failure_code(error: BaseException) -> WorkspaceErrorCode:
    """Map trusted component failures to the workspace no-label boundary."""

    if isinstance(error, LocalSyncHarnessError):
        classification = error.classification
    elif isinstance(error, DbtTrustedFailure):
        # Missing pinned runtimes are infrastructure failures, not candidate errors.
        if error.code is DbtErrorCode.RUNTIME_UNAVAILABLE:
            return WorkspaceErrorCode.INFRASTRUCTURE_UNAVAILABLE
        try:
            classification = WorkspaceFailureClass(error.classification.value)
        except ValueError:
            classification = WorkspaceFailureClass.HARNESS_DEFECT
    else:
        classification = WorkspaceFailureClass.HARNESS_DEFECT

    if classification is WorkspaceFailureClass.TASK_DEFECT:
        return WorkspaceErrorCode.TASK_PACKAGE_INVALID
    if classification is WorkspaceFailureClass.TRANSIENT_INFRASTRUCTURE:
        return WorkspaceErrorCode.INFRASTRUCTURE_UNAVAILABLE
    if classification is WorkspaceFailureClass.REAL_RUNTIME_FAILURE:
        return WorkspaceErrorCode.REAL_RUNTIME_FAILED
    return WorkspaceErrorCode.HARNESS_INTERNAL


def _normalize_populations(
    package: WorkspacePackage,
    populations: Sequence[PopulationName | str] | None,
) -> tuple[PopulationName, ...]:
    # Official reward is the minimum across every hidden population.
    # Callers may pass only the complete set; order is canonicalized.
    graded = tuple(GRADED_POPULATIONS)
    if populations is not None:
        supplied = tuple(PopulationName(item) for item in populations)
        if len(supplied) != len(set(supplied)) or set(supplied) != set(graded):
            raise ValueError(
                "workspace scorer requires the complete graded population set"
            )
    requested = graded
    expected_tables = {table.name for table in package.task.tables}
    expected_marts = {mart.name for mart in package.task.marts}
    for population in requested:
        package.source_root(population)
        if set(package.gold.stage1.get(population.value, {})) != expected_tables:
            raise ValueError("workspace scorer population lacks stage-1 gold")
        if set(package.gold.stage2_csv.get(population.value, {})) != expected_marts:
            raise ValueError("workspace scorer population lacks stage-2 gold")
    return requested


def _empty_population(
    population: PopulationName,
    *,
    terraform_reward: float,
    error_codes: Sequence[object],
) -> PopulationWorkspaceScore:
    return PopulationWorkspaceScore(
        population=population.value,
        graded=population in GRADED_POPULATIONS,
        terraform_contract=terraform_reward,
        sync_lifecycle=False,
        upstream_stage1=False,
        strict_raw_tables=0.0,
        strict_el_pass=False,
        dbt_project=0.0,
        mart_reward=0.0,
        raw_immutable=False,
        end_to_end_reward=0.0,
        error_codes=_code_values(tuple(error_codes)),
    )


def _score_population(
    package: WorkspacePackage,
    sealed: SealedWorkspace | Path,
    population: PopulationName,
    attempt_path: Path,
    *,
    expected_seal_sha256: str | None,
    runtime_config: DbtRuntimeConfig,
    dbt_limits: DbtRunnerLimits | None,
    dependencies: _ScorerDependencies,
) -> PopulationWorkspaceScore:
    """Execute one clean Terraform -> EL -> dbt same-state replay."""

    attempt = dependencies.replay(
        package,
        sealed,
        attempt_path,
        expected_seal_sha256=expected_seal_sha256,
    )
    # After replay, permit only creation of the private attempt-local runtime sibling.
    attempt.task_dir.chmod(0o755)
    terraform = dependencies.terraform(package, attempt.elt_dir / "main.tf")
    if terraform.graph is None:
        return _empty_population(
            population,
            terraform_reward=float(terraform.reward),
            error_codes=terraform.error_codes,
        )

    # Everything beneath this task-local directory is evaluator-owned and is
    # absent on replay.  The candidate never supplies the database path.
    database_path = (
        attempt.task_dir
        / ".workspace-runtime"
        / "raw"
        / "attempt.duckdb"
    )
    if isinstance(terraform.graph, TerraformIntentGraph):
        protocol = AirbyteProtocolProxy(
            package,
            population,
            terraform.graph,
            attempt.task_dir / ".workspace-runtime" / "airbyte",
            sync=dependencies.sync,
        )
        job = protocol.trigger_sync(database_path)
        sync = protocol.execution
        if sync is None:
            return _empty_population(
                population,
                terraform_reward=float(terraform.reward),
                error_codes=(job.error_code or "airbyte_proxy_connection_invalid",),
            )
    else:
        # Structural test doubles retain the pre-L2 seam; production compiler
        # output always takes the deterministic protocol-proxy path above.
        sync = dependencies.sync(
            package,
            population,
            terraform.graph,
            database_path,
        )
    if not sync.sync_lifecycle:
        return PopulationWorkspaceScore(
            population=population.value,
            graded=population in GRADED_POPULATIONS,
            terraform_contract=float(terraform.reward),
            sync_lifecycle=False,
            upstream_stage1=bool(sync.upstream_stage1),
            strict_raw_tables=float(sync.strict_raw_tables),
            strict_el_pass=False,
            dbt_project=0.0,
            mart_reward=0.0,
            raw_immutable=False,
            end_to_end_reward=0.0,
            error_codes=_code_values(terraform.error_codes, sync.error_codes),
        )

    dbt = dependencies.dbt(
        package,
        population,
        attempt_dir=attempt.task_dir,
        sync_execution=sync,
        runtime_config=runtime_config,
        limits=dbt_limits,
    )
    strict_el = (
        terraform.reward == 1.0
        and sync.sync_lifecycle
        and sync.strict_raw_tables == 1.0
    )
    # Incomplete raw state may yield diagnostics but never publishable mart reward.
    mart_reward = float(dbt.mart_reward) if strict_el else 0.0
    immutable = bool(dbt.raw_immutable)
    end_to_end = mart_reward if strict_el and immutable else 0.0
    return PopulationWorkspaceScore(
        population=population.value,
        graded=population in GRADED_POPULATIONS,
        terraform_contract=float(terraform.reward),
        sync_lifecycle=bool(sync.sync_lifecycle),
        upstream_stage1=bool(sync.upstream_stage1),
        strict_raw_tables=float(sync.strict_raw_tables),
        strict_el_pass=strict_el,
        dbt_project=float(dbt.dbt_project),
        mart_reward=mart_reward,
        raw_immutable=immutable,
        end_to_end_reward=end_to_end,
        error_codes=_code_values(
            terraform.error_codes,
            sync.error_codes,
            dbt.error_codes,
        ),
    )


def score_workspace(
    package: WorkspacePackage,
    sealed: SealedWorkspace | Path,
    *,
    attempts_root: Path,
    runtime_config: DbtRuntimeConfig,
    populations: Sequence[PopulationName | str] | None = None,
    expected_seal_sha256: str | None = None,
    dbt_limits: DbtRunnerLimits | None = None,
    _dependencies: _ScorerDependencies = _DEFAULT_DEPENDENCIES,
) -> WorkspaceScoreResult:
    """Score fresh hidden replays and return their minimum end-to-end reward.

    Trusted failures return ``reward=None``; candidate failures remain measured.
    """

    artifact_sha256 = _artifact_digest(sealed)
    try:
        requested = _normalize_populations(package, populations)
    except Exception:  # caller/package setup, never candidate evidence
        return _failure_result(
            package,
            artifact_sha256=artifact_sha256,
            valid_submission=True,
            error_code=WorkspaceErrorCode.TASK_PACKAGE_INVALID,
        )

    # Authenticate path inputs by the retained seal digest before using identity.
    replay_sealed: SealedWorkspace | Path = sealed
    if isinstance(sealed, Path):
        if expected_seal_sha256 is None:
            return _failure_result(
                package,
                artifact_sha256=artifact_sha256,
                valid_submission=False,
                error_code=WorkspaceErrorCode.SUBMISSION_INVALID,
            )
        try:
            replay_sealed = load_sealed_workspace(
                sealed,
                expected_seal_sha256=expected_seal_sha256,
            )
            artifact_sha256 = replay_sealed.submission.artifact_sha256
        except WorkspaceLifecycleError as error:
            return _failure_result(
                package,
                artifact_sha256=artifact_sha256,
                valid_submission=False,
                error_code=error.code,
            )

    scores: dict[str, PopulationWorkspaceScore] = {}
    try:
        parent = Path(attempts_root)
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise OSError("attempt root is not a trusted directory")
        run_root = Path(tempfile.mkdtemp(prefix="workspace-score-", dir=parent))
        try:
            for index, population in enumerate(requested):
                score = _score_population(
                    package,
                    replay_sealed,
                    population,
                    run_root / f"replay-{index:02d}",
                    expected_seal_sha256=expected_seal_sha256,
                    runtime_config=runtime_config,
                    dbt_limits=dbt_limits,
                    dependencies=_dependencies,
                )
                scores[population.value] = score
        finally:
            # ``run_root`` is the exact directory atomically created above;
            # no caller path or broad glob is ever removed.
            _remove_owned_run_root(run_root)
    except WorkspaceLifecycleError as error:
        return _failure_result(
            package,
            artifact_sha256=artifact_sha256,
            valid_submission=False,
            error_code=error.code,
        )
    except TerraformIntentHarnessError as error:
        return _failure_result(
            package,
            artifact_sha256=artifact_sha256,
            valid_submission=True,
            error_code=_trusted_failure_code(error),
        )
    except (LocalSyncHarnessError, DbtTrustedFailure) as error:
        return _failure_result(
            package,
            artifact_sha256=artifact_sha256,
            valid_submission=True,
            error_code=_trusted_failure_code(error),
        )
    except Exception:
        return _failure_result(
            package,
            artifact_sha256=artifact_sha256,
            valid_submission=True,
            error_code=WorkspaceErrorCode.HARNESS_INTERNAL,
        )

    graded = tuple(
        population.value
        for population in requested
        if population in GRADED_POPULATIONS
    )
    reward = min(scores[name].end_to_end_reward for name in graded)
    return WorkspaceScoreResult(
        release_id=package.manifest.release_id,
        task_id=package.task_id,
        task_content_hash=package.task.content_hash(),
        artifact_sha256=artifact_sha256,
        valid_submission=True,
        graded_populations=graded,
        populations=scores,
        reward=reward,
    )


__all__ = ["score_workspace"]

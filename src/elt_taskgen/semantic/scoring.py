"""Resource-isolated semantic scoring for one combined EL+T submission."""

from __future__ import annotations

import json
import multiprocessing
import subprocess
import sys
import time
from collections.abc import Sequence
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from sqlglot import exp, parse

from elt_taskgen.corpus import calibration as calibration_mod
from elt_taskgen.models import PopulationName, TaskVariant, canonical_json
from elt_taskgen.reference import solution as solution_mod
from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection
from elt_taskgen.reference.runner import sort_mart_rows
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.semantic.models import (
    SEMANTIC_RESULT_SCHEMA_VERSION,
    SEMANTIC_SCORER_VERSION,
    SEMANTIC_SUBMISSION_SCHEMA_VERSION,
    PopulationSemanticScore,
    SemanticLimits,
    SemanticScoreResult,
    SemanticSubmission,
)
from elt_taskgen.semantic.package import SemanticPackage
from elt_taskgen.verification import strict_diagnostic as strict_mod
from elt_taskgen.verification import upstream_eval
from elt_taskgen.verification.gates import GRADED_POPULATIONS

try:
    import resource
except ImportError:  # non-POSIX: rlimits unavailable, watchdog still applies
    resource = None  # type: ignore[assignment]

MAX_SUBMISSION_BYTES = 1024 * 1024
MAX_SQL_BYTES = 256 * 1024
MAX_PATH_BYTES = 4096

_MUTATING_SQL_NODES = tuple(
    node
    for node in (
        getattr(exp, name, None)
        for name in (
            "Alter",
            "Attach",
            "Command",
            "Copy",
            "Create",
            "Delete",
            "Detach",
            "Drop",
            "Grant",
            "Insert",
            "Merge",
            "Pragma",
            "Revoke",
            "Set",
            "Transaction",
            "Update",
            "Use",
        )
    )
    if isinstance(node, type)
)


class SemanticSubmissionError(ValueError):
    """A submitted attempt does not satisfy the versioned input contract."""


class SemanticHarnessError(RuntimeError):
    """Trusted release inputs could not be measured; never a solver zero."""


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticSubmissionError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def parse_semantic_submission(task, text: str) -> SemanticSubmission:
    """Parse the exact semantic v1 envelope, then reuse calibration parsing."""
    if len(text.encode("utf-8")) > MAX_SUBMISSION_BYTES:
        raise SemanticSubmissionError("submission exceeds the byte limit")
    try:
        payload = json.loads(text, object_pairs_hook=_no_duplicate_object)
    except SemanticSubmissionError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise SemanticSubmissionError("submission is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SemanticSubmissionError("submission must be a JSON object")
    expected = {"schema_version", "task_id", "load_plan", "sql_by_mart"}
    if set(payload) != expected:
        raise SemanticSubmissionError(
            f"submission must have exactly the keys {sorted(expected)}"
        )
    if payload.get("schema_version") != SEMANTIC_SUBMISSION_SCHEMA_VERSION:
        raise SemanticSubmissionError(
            "unsupported semantic submission schema_version"
        )
    if payload.get("task_id") != task.task_id:
        raise SemanticSubmissionError("submission task_id does not match the package")
    try:
        parsed = calibration_mod.parse_submission(
            task,
            TaskVariant.FULL,
            canonical_json(
                {
                    "load_plan": payload["load_plan"],
                    "sql_by_mart": payload["sql_by_mart"],
                }
            ),
        )
    except ProviderProtocolError as exc:
        raise SemanticSubmissionError("submission body is invalid") from exc
    for step in parsed.load_plan.values():
        if len(step.path.encode("utf-8")) > MAX_PATH_BYTES:
            raise SemanticSubmissionError("load-plan path exceeds the byte limit")
    for sql in parsed.sql_by_mart.values():
        if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
            raise SemanticSubmissionError("mart SQL exceeds the byte limit")
    return SemanticSubmission(
        schema_version=SEMANTIC_SUBMISSION_SCHEMA_VERSION,
        task_id=task.task_id,
        load_plan=parsed.load_plan,
        sql_by_mart=parsed.sql_by_mart,
    )


def _normalize_populations(
    package: SemanticPackage,
    populations: Sequence[PopulationName | str] | None,
) -> tuple[PopulationName, ...]:
    requested = (
        tuple(GRADED_POPULATIONS)
        if populations is None
        else tuple(PopulationName(item) for item in populations)
    )
    if not requested:
        raise SemanticHarnessError("no populations were requested")
    if len(set(requested)) != len(requested):
        raise SemanticHarnessError("a population was requested more than once")
    for population in requested:
        package.source_root(population)
        if population.value not in package.gold.stage1:
            raise SemanticHarnessError("a requested population lacks stage-1 gold")
        if population.value not in package.gold.stage2_csv:
            raise SemanticHarnessError("a requested population lacks stage-2 gold")
    return requested


def _submission_matches_package(
    package: SemanticPackage, submission: SemanticSubmission
) -> bool:
    """Validate callers that construct the typed model without the text parser."""
    if (
        submission.schema_version != SEMANTIC_SUBMISSION_SCHEMA_VERSION
        or submission.task_id != package.task.task_id
    ):
        return False
    if set(submission.load_plan) != {table.name for table in package.task.tables}:
        return False
    if set(submission.sql_by_mart) != {mart.name for mart in package.task.marts}:
        return False
    if any(
        len(step.path.encode("utf-8")) > MAX_PATH_BYTES
        for step in submission.load_plan.values()
    ):
        return False
    if any(
        not sql.strip() or len(sql.encode("utf-8")) > MAX_SQL_BYTES
        for sql in submission.sql_by_mart.values()
    ):
        return False
    return True


def _assert_safe_source_tree(root: Path) -> None:
    """Release source fixtures must not contain symlink-based escapes."""
    if root.is_symlink():
        raise SemanticHarnessError("a population source root is a symlink")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SemanticHarnessError("a population source tree contains a symlink")


def _validate_query(sql: str) -> None:
    """Require exactly one read-only DuckDB query statement."""
    try:
        statements = parse(sql, read="duckdb")
    except Exception as exc:  # noqa: BLE001 - stable public code below
        raise ValueError("query did not parse") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("mart SQL must contain exactly one read-only query")
    if any(
        isinstance(node, _MUTATING_SQL_NODES)
        for node in statements[0].walk()
    ):
        raise ValueError("mart SQL contains a state-changing operation")


def _new_connection(limits: SemanticLimits):
    return sandboxed_memory_connection(
        memory_limit_mb=limits.memory_limit_mb,
        threads=limits.threads,
        disable_temp_spill=True,
        deterministic_settings=True,
    )


def _strict_diagnostic_payload(diag: strict_mod.StrictMartDiagnostic) -> dict[str, Any]:
    """Pipe-safe plain-dict form of one strict verdict (models stay parent-side)."""
    return {
        "match": diag.strict_match,
        "code": diag.mismatch_code,
        "submission_columns": [
            [f.name, f.declared_type] for f in diag.submission_columns
        ],
        "reference_columns": [
            [f.name, f.declared_type] for f in diag.reference_columns
        ],
    }


def _strict_tier_payload(
    con,
    task,
    submission: SemanticSubmission,
    limits: SemanticLimits,
    invalid_marts: set[str],
) -> dict[str, Any]:
    """Run strict mart diagnostics without changing rewards or raising.

    Report ``error:query_failed`` for SQL rejected before execution.
    """
    reference_sql = solution_mod.build_reference(task).sql_by_mart
    payload: dict[str, Any] = {}
    for mart in task.marts:
        if mart.name in invalid_marts:
            diag = strict_mod.StrictMartDiagnostic(
                mart=mart.name,
                strict_match=False,
                mismatch_code="error:query_failed",
            )
        else:
            diag = strict_mod.strict_mart_diagnostic(
                con,
                mart,
                reference_sql[mart.name],
                submission.sql_by_mart[mart.name],
                max_rows=limits.max_result_rows_per_mart,
                max_bytes=limits.max_result_bytes_per_mart,
            )
        payload[mart.name] = _strict_diagnostic_payload(diag)
    return payload


def _execute_population(
    task,
    source_root: Path,
    expected_counts: dict[str, int],
    submission: SemanticSubmission,
    limits: SemanticLimits,
    strict_diagnostic: bool = False,
    strict_shadow: bool = False,
) -> dict[str, Any]:
    """Execute untrusted work in the child; never receive mart gold here."""
    _assert_safe_source_tree(source_root)

    # Rebuild and verify transform sources independently from EL.
    t_rows: dict[str, list[dict[str, Any]]] = {}
    t_errors: dict[str, str] = {}
    strict_payload: dict[str, Any] = {}
    strict_shadow_payload: dict[str, Any] = {}
    t_con = _new_connection(limits)
    try:
        try:
            trusted_counts = dict(
                solution_mod.load_sources_duckdb(task, source_root, t_con).counts
            )
        except Exception as exc:  # noqa: BLE001 - trusted harness failure
            raise SemanticHarnessError(
                "trusted source loading failed for a released population"
            ) from exc
        if trusted_counts != expected_counts:
            raise SemanticHarnessError(
                "released population sources disagree with frozen stage-1 gold"
            )
        for mart in task.marts:
            sql = submission.sql_by_mart[mart.name]
            try:
                _validate_query(sql)
            except ValueError:
                t_errors[mart.name] = "invalid_query"
                continue
            try:
                rows = solution_mod.execute_mart(
                    t_con,
                    mart,
                    sql,
                    max_rows=limits.max_result_rows_per_mart,
                    max_bytes=limits.max_result_bytes_per_mart,
                )
                t_rows[mart.name] = sort_mart_rows(rows, mart)
            except solution_mod.MartOutputLimitError:
                t_errors[mart.name] = "output_limit"
            except MemoryError:
                t_errors[mart.name] = "memory_limit"
            except Exception:  # noqa: BLE001 - no private exception text leaks
                t_errors[mart.name] = "query_failed"
        if strict_diagnostic:
            # Run diagnostics on the same warehouse used for reward scoring.
            strict_payload = _strict_tier_payload(
                t_con,
                task,
                submission,
                limits,
                {name for name, code in t_errors.items() if code == "invalid_query"},
            )
    finally:
        t_con.close()
    if strict_shadow:
        # Strict DECIMAL/JSON failures produce codes without changing rewards.
        s_con = _new_connection(limits)
        try:
            try:
                strict_mod.load_sources_duckdb_strict(task, source_root, s_con)
            except Exception:  # noqa: BLE001 - stable code, no text leaks
                strict_shadow_payload = {
                    mart.name: _strict_diagnostic_payload(
                        strict_mod.StrictMartDiagnostic(
                            mart=mart.name,
                            strict_match=False,
                            mismatch_code="error:strict_failed",
                        )
                    )
                    for mart in task.marts
                }
            else:
                strict_shadow_payload = _strict_tier_payload(
                    s_con,
                    task,
                    submission,
                    limits,
                    {
                        name
                        for name, code in t_errors.items()
                        if code == "invalid_query"
                    },
                )
        finally:
            s_con.close()
    # EL uses its own connection and the solver-selected reader/path mapping.
    el_error = ""
    el_con = _new_connection(limits)
    try:
        try:
            counts = calibration_mod.execute_load_plan(
                task, submission.load_plan, source_root, el_con
            )
        except Exception:  # noqa: BLE001 - stable code, never hidden values/path
            counts = {}
            el_error = "stage1_execution_error"
    finally:
        el_con.close()
    payload: dict[str, Any] = {
        "trusted_counts": trusted_counts,
        "el_counts": counts,
        "el_error_code": el_error,
        "mart_rows": t_rows,
        "t_error_codes": dict(sorted(t_errors.items())),
    }
    if strict_diagnostic:
        payload["strict"] = strict_payload
    if strict_shadow:
        payload["strict_shadow"] = strict_shadow_payload
    return payload


def _parse_strict_map(raw: Any) -> dict[str, strict_mod.StrictMartDiagnostic]:
    """Parse one pipe payload of strict verdicts; absent payloads stay empty."""
    parsed: dict[str, strict_mod.StrictMartDiagnostic] = {}
    for mart, item in sorted(dict(raw or {}).items()):
        entry = dict(item)
        parsed[str(mart)] = strict_mod.StrictMartDiagnostic(
            mart=str(mart),
            strict_match=bool(entry.get("match")),
            mismatch_code=str(entry.get("code") or ""),
            submission_columns=tuple(
                strict_mod.StrictColumnFingerprint(
                    name=str(name), declared_type=str(declared)
                )
                for name, declared in (entry.get("submission_columns") or ())
            ),
            reference_columns=tuple(
                strict_mod.StrictColumnFingerprint(
                    name=str(name), declared_type=str(declared)
                )
                for name, declared in (entry.get("reference_columns") or ())
            ),
        )
    return parsed


def _assemble_population_score(
    package: SemanticPackage,
    population: PopulationName,
    actual: dict[str, Any],
) -> PopulationSemanticScore:
    """Compare bounded child output against gold in the trusted parent."""
    expected_counts = dict(package.gold.stage1[population.value])
    if dict(actual.get("trusted_counts") or {}) != expected_counts:
        raise SemanticHarnessError(
            "released population sources disagree with frozen stage-1 gold"
        )
    el_result = upstream_eval.evaluate_variant(
        TaskVariant.EXTRACT_LOAD,
        package.task,
        package.gold,
        population,
        actual_stage1=dict(actual.get("el_counts") or {}),
    )
    el_error = str(actual.get("el_error_code") or "")
    if el_result.reward != 1.0 and not el_error:
        el_error = "stage1_mismatch"
    t_result = upstream_eval.evaluate_variant(
        TaskVariant.TRANSFORM,
        package.task,
        package.gold,
        population,
        actual_marts=dict(actual.get("mart_rows") or {}),
    )
    gated = float(t_result.reward) if el_result.reward == 1.0 else 0.0
    return PopulationSemanticScore(
        population=population.value,
        graded=population in GRADED_POPULATIONS,
        el_reward=float(el_result.reward),
        t_reward=float(t_result.reward),
        reward=gated,
        stage1_pass=bool(el_result.stage1_pass),
        mart_scores=dict(t_result.mart_scores),
        el_error_code=el_error,
        t_error_codes={
            str(key): str(value)
            for key, value in sorted(
                dict(actual.get("t_error_codes") or {}).items()
            )
        },
        strict_marts=_parse_strict_map(actual.get("strict")),
        strict_shadow_marts=_parse_strict_map(actual.get("strict_shadow")),
    )


def _result(
    package: SemanticPackage,
    *,
    valid_submission: bool,
    populations: dict[str, PopulationSemanticScore] | None = None,
    error_code: str = "",
    strict_diagnostic_ran: bool = False,
) -> SemanticScoreResult:
    scores = populations or {}
    # Development is solver-visible and diagnostic only.  Even when explicitly
    # requested, it never contributes to the aggregate training reward.
    graded_scores = [item for item in scores.values() if item.graded]
    el = min((item.el_reward for item in graded_scores), default=0.0)
    transform = min((item.t_reward for item in graded_scores), default=0.0)
    gated = min((item.reward for item in graded_scores), default=0.0)
    return SemanticScoreResult(
        schema_version=SEMANTIC_RESULT_SCHEMA_VERSION,
        scorer_version=SEMANTIC_SCORER_VERSION,
        comparator_version=package.manifest.scorer_version,
        release_id=package.manifest.release_id,
        task_id=package.task.task_id,
        task_content_hash=package.task.content_hash(),
        submission_schema_version=SEMANTIC_SUBMISSION_SCHEMA_VERSION,
        aggregation="minimum",
        valid_submission=valid_submission,
        graded_populations=tuple(
            name for name, item in scores.items() if item.graded
        ),
        populations=scores,
        semantic_el_reward=el,
        semantic_t_reward=transform,
        reward=gated,
        error_code=error_code,
        strict_diagnostic_ran=strict_diagnostic_ran,
        strict_diagnostic_version=(
            strict_mod.STRICT_DIAGNOSTIC_VERSION if strict_diagnostic_ran else ""
        ),
    )


def _score_in_worker(
    task,
    source_roots: dict[str, Path],
    expected_counts: dict[str, dict[str, int]],
    submission: SemanticSubmission,
    populations: tuple[PopulationName, ...],
    limits: SemanticLimits,
    strict_diagnostic: bool = False,
    strict_shadow: bool = False,
) -> dict[str, dict[str, Any]]:
    return {
        population.value: _execute_population(
            task,
            source_roots[population.value],
            expected_counts[population.value],
            submission,
            limits,
            strict_diagnostic,
            strict_shadow,
        )
        for population in populations
    }


#: Cadence of the parent's RSS watchdog over the scorer worker.
_WATCHDOG_INTERVAL_SECONDS = 0.25


def _apply_worker_memory_rlimits(limit_bytes: int) -> None:
    """Apply available worker memory limits; the parent watchdog is the fallback."""
    if resource is None:
        return
    for name in ("RLIMIT_AS", "RLIMIT_DATA"):
        rlim = getattr(resource, name, None)
        if rlim is None:
            continue
        try:
            _soft, hard = resource.getrlimit(rlim)
            if hard == resource.RLIM_INFINITY or hard > limit_bytes:
                new_hard = limit_bytes
            else:
                new_hard = hard
            resource.setrlimit(rlim, (min(limit_bytes, new_hard), new_hard))
        except (ValueError, OSError):
            pass


def _process_rss_bytes(pid: int) -> int | None:
    """Resident set size of ``pid`` in bytes, or ``None`` when unmeasurable.

    Measurement failures degrade the watchdog to the rlimit-only envelope;
    they are never a harness error, so this helper must never raise.
    """
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/status", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None
    try:
        completed = subprocess.run(
            ["/bin/ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        return int(completed.stdout.strip()) * 1024
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _worker_main(
    send: Connection,
    task,
    source_roots: dict[str, Path],
    expected_counts: dict[str, dict[str, int]],
    submission: SemanticSubmission,
    populations: tuple[PopulationName, ...],
    limits: SemanticLimits,
    strict_diagnostic: bool = False,
    strict_shadow: bool = False,
) -> None:
    try:
        _apply_worker_memory_rlimits(limits.worker_rss_limit_mb * 1024 * 1024)
        result = _score_in_worker(
            task,
            source_roots,
            expected_counts,
            submission,
            populations,
            limits,
            strict_diagnostic,
            strict_shadow,
        )
        send.send(("result", result))
    except SemanticHarnessError as exc:
        send.send(("harness", str(exc)))
    except MemoryError:
        try:
            send.send(("memory", "memory_limit"))
        except BaseException:  # noqa: BLE001 - the pipe itself may be starved
            pass
    except BaseException:  # noqa: BLE001 - worker crash becomes stable failure
        try:
            send.send(("worker", "worker_failed"))
        except BaseException:
            pass
    finally:
        send.close()


def _stop_process(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
        process.join(1.0)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(1.0)


def score_semantic_submission(
    package: SemanticPackage,
    submission: SemanticSubmission,
    *,
    populations: Sequence[PopulationName | str] | None = None,
    limits: SemanticLimits | None = None,
    strict_diagnostic: bool = False,
    strict_shadow: bool = False,
) -> SemanticScoreResult:
    """Score an attempt in a killable child process.

    Worker timeouts, limits, and crashes score zero. Trusted input failures
    raise ``SemanticHarnessError``. Strict diagnostics do not change rewards.
    """
    if not _submission_matches_package(package, submission):
        return _result(
            package, valid_submission=False, error_code="invalid_submission"
        )
    selected = _normalize_populations(package, populations)
    active_limits = limits or SemanticLimits()
    methods = multiprocessing.get_all_start_methods()
    # ``spawn`` avoids inheriting DuckDB/Python worker threads and open file
    # descriptors.  Fall back only on platforms that genuinely lack it.
    method = "spawn" if "spawn" in methods else methods[0]
    context = multiprocessing.get_context(method)
    receive, send = context.Pipe(duplex=False)
    source_roots = {
        population.value: package.source_root(population)
        for population in selected
    }
    expected_counts = {
        population.value: dict(package.gold.stage1[population.value])
        for population in selected
    }
    process = context.Process(
        target=_worker_main,
        args=(
            send,
            package.task,
            source_roots,
            expected_counts,
            submission,
            selected,
            active_limits,
            strict_diagnostic,
            strict_shadow,
        ),
        daemon=True,
    )
    process.start()
    send.close()
    deadline = time.monotonic() + active_limits.timeout_seconds
    rss_budget = active_limits.worker_rss_limit_mb * 1024 * 1024
    next_rss_check = time.monotonic()
    message: tuple[str, Any] | None = None
    try:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if receive.poll(min(0.05, remaining)):
                try:
                    message = receive.recv()
                except EOFError:
                    message = None
                break
            if not process.is_alive():
                break
            now = time.monotonic()
            if now >= next_rss_check and process.pid is not None:
                next_rss_check = now + _WATCHDOG_INTERVAL_SECONDS
                rss = _process_rss_bytes(process.pid)
                if rss is not None and rss > rss_budget:
                    _stop_process(process)
                    return _result(
                        package, valid_submission=True, error_code="memory_limit"
                    )
        if message is None and process.is_alive():
            _stop_process(process)
            return _result(
                package, valid_submission=True, error_code="execution_timeout"
            )
        process.join(1.0)
    finally:
        receive.close()
        _stop_process(process)
        process.close()
    if message is None:
        return _result(package, valid_submission=True, error_code="worker_failed")
    kind, payload = message
    if kind == "harness":
        raise SemanticHarnessError(str(payload))
    if kind == "memory":
        return _result(package, valid_submission=True, error_code="memory_limit")
    if kind != "result":
        return _result(package, valid_submission=True, error_code="worker_failed")
    try:
        scores = {
            population.value: _assemble_population_score(
                package, population, dict(payload[population.value])
            )
            for population in selected
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SemanticHarnessError("semantic worker returned malformed output") from exc
    return _result(
        package,
        valid_submission=True,
        populations=scores,
        strict_diagnostic_ran=strict_diagnostic or strict_shadow,
    )


def score_semantic_text(
    package: SemanticPackage,
    text: str,
    *,
    populations: Sequence[PopulationName | str] | None = None,
    limits: SemanticLimits | None = None,
    strict_diagnostic: bool = False,
    strict_shadow: bool = False,
) -> SemanticScoreResult:
    """Parse and score text; malformed solver input is a structured zero."""
    try:
        submission = parse_semantic_submission(package.task, text)
    except SemanticSubmissionError:
        return _result(
            package, valid_submission=False, error_code="invalid_submission"
        )
    return score_semantic_submission(
        package,
        submission,
        populations=populations,
        limits=limits,
        strict_diagnostic=strict_diagnostic,
        strict_shadow=strict_shadow,
    )

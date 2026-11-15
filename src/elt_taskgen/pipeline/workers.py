"""One worker executor for corpus runs and configured candidate runs."""

from __future__ import annotations
import concurrent.futures
from concurrent.futures.process import BrokenProcessPool
import json
from pathlib import Path
from typing import Callable, Literal

from elt_taskgen.engine import InfrastructureFailure
from elt_taskgen.file_io import atomic_write
from elt_taskgen.models import readable_json


def interrupt_attempt(attempts_dir: Path | None, task_id: str, detail: str) -> None:
    """Account for a worker that died while its durable attempt was RUNNING."""
    if attempts_dir is None:
        return
    for path in sorted((attempts_dir / task_id).glob("*.json")):
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            continue
        if not isinstance(prior, dict) or prior.get("state") != "RUNNING":
            continue
        prior.update(state="INTERRUPTED", detail=detail)
        atomic_write(path, readable_json(prior) + "\n")


def _shared_failure_detail(result: dict) -> str | None:
    state = str(result.get("state") or "")
    scope = str(result.get("infrastructure_scope", "") or "")
    if state != "usage_error" and not (state == "infrastructure" and scope != "task"):
        return None
    scope_detail = (
        f" with budget scope {scope!r}" if scope else
        " with no classified task-local budget scope" if state == "infrastructure" else ""
    )
    return (
        f"shared safety pause: sequential worker {str(result['task_id'])!r} returned {state!r}"
        f"{scope_detail}; later candidates were not dispatched in this invocation. "
        "Classify or remediate the operational failure, then resume "
        "the identical configured run."
    )


def run_workers(
    payloads: list[dict],
    *,
    worker: Callable[[dict], dict],
    max_workers: int,
    initial_results=(),
    failure_policy: Literal["complete_roster", "raise_parallel"] = "complete_roster",
    attempts_dir: Path | None = None,
    echo: Callable[[str], None] = print,
) -> list[dict]:
    """Execute isolated workers with explicit failure accounting.

    Complete-roster runs pause sequential siblings after shared failures and
    record crashed workers. Corpus commands preserve their established rule:
    parallel programming errors propagate, and a broken pool aborts the run.
    Task rejections and task-local budget failures never pause siblings.
    """
    if failure_policy not in {"complete_roster", "raise_parallel"}:
        raise ValueError(f"Unknown worker failure policy: {failure_policy}")
    if max_workers < 1 and payloads:
        raise ValueError("max_workers must be positive for a nonempty roster")
    complete = failure_policy == "complete_roster"
    results = list(initial_results)
    count = min(max_workers, len(payloads))

    def failed(payload, error):
        task_id = str(payload["task_id"])
        detail = f"worker exception: {type(error).__name__}: {error}"
        if complete:
            interrupt_attempt(attempts_dir, task_id, detail)
        return dict(task_id=task_id, ok=False, state="infrastructure", detail=detail, usd=0.0)

    def append(result):
        results.append(result)
        echo(f"  {result['task_id']}: {result['state']}")

    if count == 1:
        for position, payload in enumerate(payloads):
            try:
                result = worker(payload)
            except Exception as exc:
                result = failed(payload, exc)
            append(result)
            detail = _shared_failure_detail(result) if complete else None
            if detail is not None:
                for untouched in payloads[position + 1:]:
                    task_id = str(untouched["task_id"])
                    results.append(dict(task_id=task_id, ok=False, state="blocked", detail=detail, usd=0.0))
                    echo(f"  {task_id}: blocked (not dispatched)")
                break
    elif count > 1:
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=count) as executor:
                futures = {executor.submit(worker, payload): payload for payload in payloads}
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                    except BrokenProcessPool:
                        raise
                    except Exception as exc:
                        if not complete:
                            raise
                        result = failed(futures[future], exc)
                    append(result)
        except BrokenProcessPool as exc:
            if not complete:
                raise InfrastructureFailure("pipeline", "candidate_worker", "broken_process_pool") from exc
            completed = {str(row["task_id"]) for row in results}
            for payload in payloads:
                task_id = str(payload["task_id"])
                if task_id not in completed:
                    detail = f"broken_process_pool: {exc}"
                    interrupt_attempt(attempts_dir, task_id, detail)
                    results.append(dict(task_id=task_id, ok=False, state="infrastructure", detail=detail, usd=0.0))
    return sorted(results, key=lambda row: str(row["task_id"]))

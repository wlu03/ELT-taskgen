"""Shared stage paths, publication helpers, and application errors."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from elt_taskgen.engine import (
    Engine,
    EngineError,
    StageName,
    StagePayload,
    VERDICT_FAIL,
    VERDICT_PASS,
)
from elt_taskgen.models import TaskIR


class CliUsageError(ValueError):
    """The caller asked a question this command cannot answer as posed.

    A ValueError subclass so the `except ValueError` handlers that already wrap
    these parsers keep working; `main()` turns it into 'error: <msg>' + exit 2 — a
    usage error is not a rejected task, and a traceback is not an answer."""


def _answer_key_dir(engine: Engine, task: TaskIR) -> Path:
    return engine.task_dir(task.task_id) / "answer_key"


def _public_dir(engine: Engine, task: TaskIR) -> Path:
    return engine.task_dir(task.task_id) / "task"


def _evidence_dir(engine: Engine, task: TaskIR) -> Path:
    d = engine.task_dir(task.task_id) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _workspace_rel(engine: Engine, path: Path) -> str:
    """Workspace-relative POSIX path for report payloads — absolute paths must
    never reach report copies or they break cross-location byte-identity."""
    return path.relative_to(engine.workspace).as_posix()


def _require_pass_payload(engine: Engine, task: TaskIR, stage: StageName) -> dict:
    """Latest ledger payload of `stage`, required to be a pass at the CURRENT
    content hash. Missing/stale/failed evidence raises (fail closed)."""
    row = engine.latest_report(task.task_id, stage.value)
    if row is None:
        raise RuntimeError(f"no {stage.value!r} report in the ledger (fail closed)")
    if row.verdict != VERDICT_PASS:
        raise RuntimeError(
            f"latest {stage.value!r} report is {row.verdict!r}, not a pass (fail closed)"
        )
    if row.content_hash != task.content_hash():
        raise RuntimeError(
            f"latest {stage.value!r} report is stale (bound to {row.content_hash[:12]}, "
            f"task is {task.content_hash()[:12]}) — re-run it (fail closed)"
        )
    return json.loads(row.payload_json)


def _force_stage_rerun(engine: Engine, task: TaskIR, stage: StageName, reason: str) -> None:
    """Shadow a stage's current PASS so `engine.run` executes it again.

    APPEND-ONLY: a new FAIL row under MAX(id), exactly the way `repair.apply_repair`
    invalidates a stage; it never mutates or deletes the row it supersedes. Calling
    the runner directly instead bypassed `_handle_failure`, so a measured IMPOSSIBLE
    verdict was appended with no route, no repair round and no rejection — and the
    next command painted a PASS over it."""
    if not engine.task_lock_held(task.task_id):
        raise EngineError(
            "force-stage invalidation must run under the task's execution lock"
        )
    engine.record_report(
        task,
        stage.value,
        VERDICT_FAIL,
        StagePayload(
            detail=f"superseded: {reason}",
            data={"reason": reason, "stage": stage.value},
        ),
    )


def _atomic_replace_text(path: Path, text: str) -> None:
    """Durably publish a small workspace record with temp + rename.

    Corpus and per-task selection records are resumable coordinator state.  A
    process death may leave an unreferenced private temp file, but never a torn
    JSON document at the public path.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

"""Classify failures, route repairs, and apply certified patches.

Routes follow changed artifacts, not agent claims, in this order: reference,
population, runtime, specification. Reject repairs with unchanged fingerprints.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from elt_taskgen.models import RepairRoute, TaskIR, TaskStatus, canonical_json, task_to_json

if TYPE_CHECKING:  # avoid an import cycle at module load
    from elt_taskgen.engine import Engine


# --- Artifact snapshots + diff-based routing ---

@dataclass(frozen=True)
class ArtifactDiff:
    """rel_paths (workspace-relative) whose sha256 moved between snapshots,
    including files that appeared or disappeared."""

    changed: frozenset[str]


#: Snapshot skips these tasks/<id>/ subtrees: reports/ are ledger echoes, and
#: `.rebuild/` scratch from a crashed generate must not move the fingerprint.
_SNAPSHOT_EXCLUDED_DIRS = ("reports", ".rebuild")


def snapshot(workspace: Path, task_id: str) -> dict[str, str]:
    """Hash every artifact file of one task. Keys are workspace-relative POSIX
    paths (matching the artifacts ledger convention); deterministic order."""
    workspace = Path(workspace).resolve()
    root = workspace / "tasks" / task_id
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_to_task = path.relative_to(root).as_posix()
        first_part = rel_to_task.split("/", 1)[0]
        if first_part in _SNAPSHOT_EXCLUDED_DIRS:
            continue
        rel = path.relative_to(workspace).as_posix()
        result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> ArtifactDiff:
    changed = {
        rel
        for rel in before.keys() | after.keys()
        if before.get(rel) != after.get(rel)
    }
    return ArtifactDiff(changed=frozenset(changed))


def route_from_diff(diff: ArtifactDiff) -> RepairRoute:
    """Route changed artifacts by precedence; reject an empty diff."""
    if not diff.changed:
        raise ValueError("empty artifact diff: nothing changed, nothing to route")
    paths = ["/" + p.strip("/") + ("/" if p.endswith("/") else "") for p in sorted(diff.changed)]
    if any("/answer_key/reference/" in p or "/answer_key/gold/" in p for p in paths):
        return RepairRoute.REFERENCE
    if any("/populations/" in p and "/rendered/" not in p for p in paths):
        return RepairRoute.POPULATION
    if any("/rendered/" in p or "/task/sources/" in p for p in paths):
        return RepairRoute.RUNTIME
    return RepairRoute.SPECIFICATION


# --- Failure-report routing (no patch exists yet) ---

#: Exclude bare "leak": required-mutant leaks require population repair.
_FATAL_KEYWORDS = ("contamination", "licens", "plagiar")
#: Checked before the reference keywords so "determinism -> runtime" holds even
#: when the detail text mentions the gold CSVs it compared.
_RUNTIME_FIRST_KEYWORDS = ("smoke", "determinism")
_REFERENCE_KEYWORDS = ("gold", "trusted")
#: Match failing gate names before prose; population-gate prose may mention gold.
_POPULATION_GATE_NAMES = (
    "data-sensitivity",
    "info-content",
    "degenerate",
    "populations-load",
    "declared-scale-reconciliation",
    "required-mutants",
    "referential-integrity",
)
#: These gates indicate prose/reference disagreement, so repair the specification.
#: ``trusted-solution`` remains reference-routed because its meaning is overloaded.
_SPECIFICATION_GATE_NAMES = (
    "dual-build-agreement",
    "column-attribution",
)
_POPULATION_KEYWORDS = (
    "population",
    "data-sensitivity",
    "required-mutants",
    "required mutant",
    # gates._gate_required_mutants leak detail: "LEAK — must lose reward on <pop>"
    "must lose reward",
    "info-content",
    "degenerate",
    "coverage",
    "skew",
)
_RUNTIME_KEYWORDS = ("environment", "render", "rebuild", "connection", "docker", "load")

#: Fallback when the payload text is uninformative.
_STAGE_FALLBACK: dict[str, RepairRoute] = {
    "intake": RepairRoute.SPECIFICATION,
    "contamination_pre": RepairRoute.FATAL,
    "generate": RepairRoute.POPULATION,
    "reference": RepairRoute.RUNTIME,
    "author": RepairRoute.SPECIFICATION,
    "review": RepairRoute.SPECIFICATION,
    "attack": RepairRoute.POPULATION,
    "gates": RepairRoute.POPULATION,
    # A TASK-LEVEL variant-battery failure is a defect in the frozen
    # populations/gold, so it repairs the TASK; variant-local ones never get here.
    "gates_extract_load": RepairRoute.POPULATION,
    "gates_transform": RepairRoute.POPULATION,
    "calibrate": RepairRoute.SPECIFICATION,
    "contamination_post": RepairRoute.FATAL,
    "select": RepairRoute.SPECIFICATION,
    "release": RepairRoute.RUNTIME,
}


#: Currency refusals use "re-run <stage>" for evidence under an obsolete identity.
EVIDENCE_RERUN_MARKER = "re-run"


def is_currency_refusal(gate: Any) -> bool:
    """Return whether a failing gate refuses stale evidence without judging it."""
    if isinstance(gate, Mapping):
        if bool(gate.get("currency", False)):
            return True
        return EVIDENCE_RERUN_MARKER in str(gate.get("details", "")).lower()
    if bool(getattr(gate, "currency", False)):
        return True
    return EVIDENCE_RERUN_MARKER in str(getattr(gate, "details", "")).lower()


def _judging_failures(gates: list[Any]) -> list[Any]:
    """The failing gates that actually JUDGED — currency refusals removed, so
    routing never keyword-matches text nobody measured."""
    return [g for g in gates if not is_currency_refusal(g)]


def _failing_gate_names(payload: BaseModel) -> tuple[str, ...]:
    """Return non-currency failing gate names from an acceptance report."""
    try:
        dumped = payload.model_dump(mode="json")
    except AttributeError:
        dumped = payload if isinstance(payload, dict) else None
    gates = dumped.get("gates") if isinstance(dumped, dict) else None
    if not (
        isinstance(gates, list)
        and gates
        and all(isinstance(g, dict) and "passed" in g for g in gates)
    ):
        return ()
    return tuple(
        str(g.get("gate", "")).lower()
        for g in _judging_failures([g for g in gates if not g.get("passed")])
    )


def _routing_text(payload: BaseModel) -> str:
    """Return searchable text from gates that produced an actual judgment."""
    dumped = payload.model_dump(mode="json")
    gates = dumped.get("gates") if isinstance(dumped, dict) else None
    if (
        isinstance(gates, list)
        and gates
        and all(isinstance(g, dict) and "passed" in g for g in gates)
    ):
        failing = [g for g in gates if not g.get("passed")]
        return canonical_json(_judging_failures(failing)).lower()
    return canonical_json(dumped).lower()


def route_for_failure(stage: str, payload: BaseModel) -> RepairRoute:
    """Route by gate name, detail keywords, then stage fallback.

    Contamination and licensing failures are fatal.
    """
    stage_v = str(stage)
    if stage_v not in _STAGE_FALLBACK:
        raise ValueError(f"unknown stage {stage_v!r}")
    if stage_v in ("contamination_pre", "contamination_post"):
        return RepairRoute.FATAL
    text = _routing_text(payload)
    if any(k in text for k in _FATAL_KEYWORDS):
        return RepairRoute.FATAL
    if any(k in text for k in _RUNTIME_FIRST_KEYWORDS):
        return RepairRoute.RUNTIME
    failing = _failing_gate_names(payload)
    if any(key in name for name in failing for key in _POPULATION_GATE_NAMES):
        return RepairRoute.POPULATION
    if any(key in name for name in failing for key in _SPECIFICATION_GATE_NAMES):
        return RepairRoute.SPECIFICATION
    if any(k in text for k in _REFERENCE_KEYWORDS):
        return RepairRoute.REFERENCE
    if any(k in text for k in _POPULATION_KEYWORDS):
        return RepairRoute.POPULATION
    if any(k in text for k in _RUNTIME_KEYWORDS):
        return RepairRoute.RUNTIME
    return _STAGE_FALLBACK[stage_v]


# --- Rerun sets + repair application ---

#: The ledger stages each route forces to re-run (FIX_PIPELINE.md §1). Population
#: repairs invalidate gold, attacks, gate evidence and calibration; specification
#: repairs leave the data pipeline alone and re-attest from authoring onward.
RERUN_STAGES: dict[RepairRoute, tuple[str, ...]] = {
    RepairRoute.SPECIFICATION: (
        "author",
        "review",
        "attack",
        "gates",
        "gates_extract_load",
        "gates_transform",
        "calibrate",
        "contamination_post",
        "select",
        "release",
    ),
    RepairRoute.POPULATION: (
        "generate",
        "reference",
        "attack",
        "gates",
        "gates_extract_load",
        "gates_transform",
        "calibrate",
        "contamination_post",
        "select",
        "release",
    ),
    # RUNTIME leads with `generate`: it re-derives populations and rewrites only
    # what drifted. Without it a drift FAIL died on the inert-repair rule.
    RepairRoute.RUNTIME: (
        "generate",
        "reference",
        "attack",
        "gates",
        "gates_extract_load",
        "gates_transform",
        "calibrate",
        "contamination_post",
        "select",
        "release",
    ),
    RepairRoute.REFERENCE: (
        "reference",
        "attack",
        "gates",
        "gates_extract_load",
        "gates_transform",
        "calibrate",
        "contamination_post",
        "select",
        "release",
    ),
    RepairRoute.FATAL: (),
}


def stages_to_rerun(route: RepairRoute) -> tuple[str, ...]:
    return RERUN_STAGES[RepairRoute(route)]


# Literal-row repair may only expand the measured discrimination matrix without
# losing cases. Reject an empty baseline; generation failures need adjudication.

#: Per-case measured reward record written by verification/attacks._record_attack.
ATTACK_REWARDS_FILENAME = "rewards.json"

#: A mutant "discriminates" on a population when it does not keep full reward
#: there — the threshold the required-mutants gate uses to call a mutant KILLED.
FULL_REWARD = 1.0


def discrimination_matrix(
    workspace: Path, task_id: str, *, task_content_hash: str | None = None
) -> dict[str, frozenset[str]]:
    """Map current attack cases to populations where they lost reward."""
    root = Path(workspace) / "tasks" / task_id / "attacks"
    matrix: dict[str, frozenset[str]] = {}
    if not root.is_dir():
        return matrix
    for case_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        path = case_dir / ATTACK_REWARDS_FILENAME
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(record, dict):
            continue
        if (
            task_content_hash is not None
            and record.get("task_content_hash") != task_content_hash
        ):
            continue
        rewards = record.get("rewards")
        if not isinstance(rewards, dict):
            continue
        lost = {
            str(pop)
            for pop, value in rewards.items()
            if isinstance(value, (int, float)) and float(value) < FULL_REWARD
        }
        matrix[case_dir.name] = frozenset(lost)
    return matrix


def discrimination_problems(
    before: dict[str, frozenset[str]], after: dict[str, frozenset[str]]
) -> list[str]:
    """Report absent or weakened attack discrimination; fail closed on absence."""
    problems: list[str] = []
    discriminating_before = {c: pops for c, pops in before.items() if pops}
    if not discriminating_before:
        problems.append(
            "no attack case discriminated before this patch (measured "
            "discrimination matrix is empty), so counterfactual rows cannot be "
            "repairing anything — writing them could only satisfy the coverage "
            "check's precondition and make the complaint disappear (fail "
            "closed; this is a human-adjudication case, not a repair)"
        )
        return problems
    for case in sorted(discriminating_before):
        if case not in after:
            problems.append(
                f"attack case {case!r} discriminated before the patch but has "
                "NO measured record after it — a deleted mutant is a deleted "
                "test, not a repair"
            )
            continue
        lost = sorted(discriminating_before[case] - after[case])
        if lost:
            problems.append(
                f"attack case {case!r} lost reward on {lost} before the patch "
                "and keeps FULL reward there after it — the patch weakened the "
                "very discrimination the gates measure"
            )
    return problems


def counterfactual_row_counts(task: TaskIR) -> dict[str, int]:
    """Return declared counterfactual literal-row counts by table."""
    from elt_taskgen.models import PopulationName

    for pop in task.populations:
        if pop.name is PopulationName.COUNTERFACTUAL:
            return {t: len(rows) for t, rows in pop.literal_rows.items()}
    return {}


def counterfactual_row_problems(
    before: dict[str, int], after: dict[str, int]
) -> list[str]:
    """Reject any counterfactual literal-row DELETION (adding is fine)."""
    problems: list[str] = []
    for table in sorted(before):
        had, has = before[table], after.get(table, 0)
        if has < had:
            problems.append(
                f"counterfactual literal rows for table {table!r} dropped from "
                f"{had} to {has}: a repair may add or rewrite constructed rows, "
                "never delete them"
            )
    return problems


# --- Inert-repair detection ---

class InertRepairError(RuntimeError):
    """The preceding repair left task state unchanged."""


def repair_fingerprint(workspace: Path, task: TaskIR) -> str:
    """Hash TaskIR semantics and non-report artifacts for inertness checks."""
    # `snapshot` keys are workspace-relative POSIX paths by construction.
    return _repair_fingerprint_from_snapshot(
        task, snapshot(workspace, task.task_id)
    )


def _repair_fingerprint_from_snapshot(
    task: TaskIR, artifacts: Mapping[str, str]
) -> str:
    """Hash ``task`` plus an already-computed artifact snapshot."""
    ir_rel = f"tasks/{task.task_id}/task_ir.json"
    fingerprinted = {
        rel: sha
        for rel, sha in artifacts.items()
        if rel != ir_rel
    }
    doc = {"content_hash": task.content_hash(), "artifacts": fingerprinted}
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def repair_fingerprint_after_changes(
    workspace: Path,
    task: TaskIR,
    changed_files: Mapping[str, bytes | None],
) -> str:
    """Fingerprint live state with staged changes overlaid before installation."""
    artifacts = snapshot(workspace, task.task_id)
    for rel_path, content in changed_files.items():
        if content is None:
            artifacts.pop(str(rel_path), None)
        else:
            artifacts[str(rel_path)] = hashlib.sha256(content).hexdigest()
    return _repair_fingerprint_from_snapshot(task, artifacts)


def assert_repair_not_inert(
    *,
    previous_fingerprint: str | None,
    current_fingerprint: str,
    route: RepairRoute,
    stage: str,
    rounds_used: int,
) -> None:
    """Raise when a recorded predecessor fingerprint matches current state."""
    if not previous_fingerprint or previous_fingerprint != current_fingerprint:
        return
    raise InertRepairError(
        f"INERT REPAIR PATH: repair round {rounds_used} routed as "
        f"{RepairRoute(route).value} left the task byte-identical "
        f"(fingerprint {current_fingerprint[:12]} unchanged: same content "
        "hash, same artifact hashes), and stage "
        f"{stage!r} then failed again from exactly that state. Every stage "
        "here is deterministic, so a further round can only reproduce this "
        "failure. Rejecting now with ONE wasted round instead of burning the "
        "budget: the defect is in the repair path (nothing patched the task "
        "and the re-run produced identical output), not in the task's "
        "capacity to be repaired."
    )


def apply_repair(
    engine: "Engine",
    task: TaskIR,
    route: RepairRoute,
    reason: str,
    *,
    fingerprint: str | None = None,
    staged_files: Mapping[str, bytes | None] | None = None,
) -> TaskIR:
    """Commit a nonfatal repair and invalidate its downstream stages.

    Build the new TaskIR before durable changes. Journal staged bytes and the
    starting fingerprint before installation.
    """
    from elt_taskgen.engine import StagePayload  # lazy: engine <-> repair

    route = RepairRoute(route)
    if route is RepairRoute.FATAL:
        raise ValueError(
            "fatal route (contamination/licensing) is a rejection, not a repair"
        )
    if not reason:
        raise ValueError("repair reason must be non-empty")

    # --- prepare the complete target before the journal transaction ---
    new_task = task.with_status(TaskStatus.IN_REPAIR).with_revision(
        route=route, reason=reason
    )
    task_to_json(new_task)  # serialization must succeed pre-commit
    if fingerprint is None:
        fingerprint = (
            repair_fingerprint_after_changes(engine.workspace, task, staged_files)
            if staged_files
            else repair_fingerprint(engine.workspace, task)
        )
    rerun = stages_to_rerun(route)
    payload = StagePayload(
        detail=f"invalidated by {route.value} repair",
        data={
            "route": route.value,
            "reason": reason,
            # The state this round starts from: if the NEXT round starts from
            # the same one, this round changed nothing (InertRepairError).
            "start_fingerprint": fingerprint,
        },
    )

    # --- commit: one SQLite transaction, then replayable live-byte install ---
    return engine.commit_repair(
        new_task,
        route,
        reason,
        fingerprint=fingerprint,
        rerun_stages=rerun,
        invalidation_payload=payload,
        staged_files=staged_files,
    )

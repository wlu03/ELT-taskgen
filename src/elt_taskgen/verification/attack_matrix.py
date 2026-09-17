"""Verify that a declared attack matrix reproduces before council review.

The check uses the gate battery's own required-mutant predicate. Missing frozen gold is
not measurable and cannot certify or reject.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from elt_taskgen.models import AttackCase, PopulationName, TaskIR

#: The failure-detail name; deliberately the same string `gates` and `repair` use.
GATE_NAME = "required-mutants"

#: Reported instead of a verdict when the inputs are not on disk — never a refusal.
NOT_MEASURABLE = "not measurable here"


def required_cases(task: TaskIR) -> tuple[AttackCase, ...]:
    """The cases `gates._gate_required_mutants` will judge, in a stable order.

    Exactly that set: inspecting more could refuse a task the gate would pass.
    """
    return tuple(sorted((c for c in task.attack_cases if c.required), key=lambda c: c.name))


def measure_required_matrix(
    task: TaskIR, gold: object, workspace: Path
) -> tuple[dict[str, dict[PopulationName, float]], list[str]]:
    """Execute every required mutant; return (rewards, hard errors).

    Execution is `attacks.run_attack`, never a re-implementation; its exceptions
    are the finding (a required mutant with no surface is a catalogue lie),
    returned as error strings rather than crashing the stage.
    """
    from elt_taskgen.verification import attacks as attacks_mod

    rewards: dict[str, dict[PopulationName, float]] = {}
    errors: list[str] = []
    for case in required_cases(task):
        try:
            measured = attacks_mod.run_attack(task, case, gold, Path(workspace))
        except Exception as exc:  # noqa: BLE001 — any failure here IS the finding
            errors.append(f"{case.name}: {type(exc).__name__}: {exc}")
            continue
        if not measured:
            # `run_attack` refuses the inapplicable path for a required case, so
            # nothing measured means no evidence at all: fail closed.
            errors.append(
                f"{case.name}: required mutant produced no measurement on any "
                "population"
            )
            continue
        rewards[case.name] = measured
    return rewards, errors


def check_declared_matrix(
    task: TaskIR, gold: object, workspace: Path
) -> str | None:
    """None when every required case reproduces its declared matrix, else why.

    The comparison is `gates._gate_required_mutants` — imported, not restated.
    """
    # Same-package private on purpose: the ONE implementation of "declared False
    # means measured < 1.0" lives there and must not be copied here.
    from elt_taskgen.verification.gates import _gate_required_mutants

    rewards, errors = measure_required_matrix(task, gold, workspace)
    result = _gate_required_mutants(task, rewards)
    if result.passed and not errors:
        return None
    parts = [] if result.passed else [result.details]
    parts.extend(errors)
    return (
        f"{GATE_NAME}: a declared attack matrix is not reproducible — measured "
        "deterministically before any live call; " + "; ".join(parts)
    )


#: Memo of a MEASUREMENT, not of a verdict: inputs fingerprint -> verdict.
#: Process-local, so nothing on disk can go stale under a later run.
_MEASURED: dict[str, str | None] = {}


def _inputs_fingerprint(task: TaskIR, workspace: Path) -> str:
    """Digest of every input the measurement reads, so anything that could move
    a reward re-runs the mutants.

    Excludes the `attacks/` subtree: the measurement's own output would make the
    fingerprint depend on itself.
    """
    from elt_taskgen.repair import snapshot  # local: avoid an import cycle

    workspace = Path(workspace).resolve()
    own_output = f"tasks/{task.task_id}/attacks/"
    parts = [str(workspace), task.task_id, task.content_hash()]
    parts.extend(
        f"{rel}={digest}"
        for rel, digest in sorted(snapshot(workspace, task.task_id).items())
        if not rel.startswith(own_output)
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def check_declared_matrix_if_measurable(
    task: TaskIR, answer_key_dir: Path, workspace: Path
) -> str | None:
    """`check_declared_matrix` when gold is on disk; None when it is not.

    A missing bundle means "no evidence either way", never a refusal invented
    out of a missing file, and the None certifies nothing: `gates` still
    measures the same matrix against the same declaration before acceptance.
    """
    answer_key_dir = Path(answer_key_dir)
    if not answer_key_dir.is_dir():
        return None
    from elt_taskgen.reference import gold as gold_mod

    try:
        gold = gold_mod.load_gold(answer_key_dir)
    except Exception:  # noqa: BLE001 — an unreadable bundle is `reference`'s failure
        return None
    fingerprint = _inputs_fingerprint(task, workspace)
    if fingerprint in _MEASURED:
        return _MEASURED[fingerprint]
    detail = check_declared_matrix(task, gold, Path(workspace))
    _MEASURED[fingerprint] = detail
    return detail

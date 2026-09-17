"""Run the PRE-COUNCIL attack-matrix gate over whole workspaces and report.

WHY THIS EXISTS
`verification/attack_matrix.py` moves `gates._gate_required_mutants` forward to
the `author`/`review` admission path so a task whose declared matrix
measurement contradicts is refused for $0 instead of after a council run. The
house rule for any new gate is that it must be PROVEN not to fire on work
already certified — a gate that would have refused a released task is a false
positive, and finding that is worth more than shipping the gate. That proof
cannot live in the unit suite, because the released workspaces are local
artifacts rather than repository fixtures, so it lives here where it can be
re-run on demand against whatever workspaces exist.

Usage:
    .venv/bin/python tools/prove_attack_matrix_gate.py <workspace> [<workspace>...]

Reports per task: the required cases the gate judges, the wall time the
measurement cost, and either "no refusal" or the exact detail string the
`author` stage would return. Exit code 0 always — this is an instrument, not a
verdict; read the output.

Recorded (mutants executed, no provider calls, $0):
    runs/synsql       14/14 required reproduce   24.0s  no refusal
    runs/schemapile   10/10 required reproduce   15.1s  no refusal
    runs/wikidbs5     12 required, 1 leak         8.2s  REFUSE
      custom__wrong_boundary_else: LEAK — must lose reward on primary, got 1.0
Run against COPIES of the released workspaces: `run_attack` records its
artifacts under tasks/<id>/attacks/, and a release tree is immutable.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from elt_taskgen.models import TaskIR
from elt_taskgen.verification import attack_matrix


def prove(workspace: Path) -> int:
    """Print one block per task; return the number that would be refused."""
    refused = 0
    tasks_dir = workspace / "tasks"
    if not tasks_dir.is_dir():
        print(f"{workspace}: no tasks/ directory")
        return 0
    for task_dir in sorted(d for d in tasks_dir.iterdir() if d.is_dir()):
        ir_path = task_dir / "task_ir.json"
        if not ir_path.is_file():
            continue
        task = TaskIR.model_validate_json(ir_path.read_text(encoding="utf-8"))
        required = [c.name for c in attack_matrix.required_cases(task)]
        started = time.monotonic()
        detail = attack_matrix.check_declared_matrix_if_measurable(
            task, task_dir / "answer_key", workspace
        )
        elapsed = time.monotonic() - started
        print(f"\n=== {workspace.name} :: {task.task_id}")
        print(f"    required cases ({len(required)}): {', '.join(required)}")
        print(f"    measured in {elapsed:.1f}s, zero provider calls")
        if detail is None:
            print("    NO REFUSAL — every declared matrix reproduces")
        else:
            refused += 1
            print(f"    REFUSE: {detail}")
    return refused


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    total = 0
    for arg in sys.argv[1:]:
        total += prove(Path(arg).resolve())
    print(f"\n{total} task(s) would be refused before council spend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

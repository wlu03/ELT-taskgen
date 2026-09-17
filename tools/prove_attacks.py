"""Execute every declared attack case of a reference-stage task and report kills.

WHY THIS EXISTS
Declaring an attack is a claim; executing it is the evidence. The pipeline's
attack stage sits behind the author/review stages, which need live providers,
so this runs the SAME machinery (`verification/attacks.py::run_attack`, which
scores exclusively through the one reward implementation) directly against a
frozen gold bundle. It reports, per case, the reward on every population and
whether the declared `expected_pass` matrix was MET — the fail-closed property
the gate asserts. Nothing here re-implements scoring or mutation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from elt_taskgen.models import TaskIR
from elt_taskgen.reference import gold as gold_mod
from elt_taskgen.verification import attacks as attacks_mod

PLAN_LEVEL = {"constants", "keys_only", "no_op", "skip_extraction"}


def prove(workspace: Path, task_id: str) -> dict:
    task_dir = workspace / "tasks" / task_id
    task = TaskIR.model_validate_json((task_dir / "task_ir.json").read_text())
    gold = gold_mod.load_gold(task_dir / "answer_key")

    rows = []
    for case in task.attack_cases:
        try:
            measured = attacks_mod.run_attack(task, case, gold, workspace)
        except Exception as exc:  # a mutation that cannot compile is a finding
            rows.append(
                {
                    "case": case.name,
                    "kind": case.kind.value,
                    "error": f"{type(exc).__name__}: {exc}",
                    "met": False,
                }
            )
            continue
        rewards = {p.value: round(float(r), 4) for p, r in measured.items()}
        met = all(
            (rewards.get(pop.value, 0.0) >= 1.0) is bool(expected)
            for pop, expected in case.expected_pass.items()
        )
        killed_on = sorted(p for p, r in rewards.items() if r < 1.0)
        rows.append(
            {
                "case": case.name,
                "kind": case.kind.value,
                "discriminating": case.kind.value not in PLAN_LEVEL,
                "rewards": rewards,
                "expected_pass": {p.value: v for p, v in case.expected_pass.items()},
                "killed_on": killed_on,
                "met": met,
            }
        )
    return {
        "task_id": task_id,
        "marts": len(task.marts),
        "target_columns": sum(len(m.columns) for m in task.marts),
        "computed_columns": sum(
            1 for m in task.marts for c in m.columns if getattr(c, "computed", False)
        ),
        "cases": rows,
        "cases_total": len(rows),
        "cases_met": sum(1 for r in rows if r["met"]),
        "discriminating_killed": sum(
            1 for r in rows if r.get("discriminating") and r.get("killed_on")
        ),
    }


def main() -> int:
    workspace = Path(sys.argv[1])
    task_ids = sys.argv[2:] or [
        d.name for d in sorted((workspace / "tasks").iterdir()) if d.is_dir()
    ]
    out = []
    for task_id in task_ids:
        report = prove(workspace, task_id)
        out.append(report)
        print(
            f"{report['task_id']}: marts={report['marts']} "
            f"target={report['target_columns']} computed={report['computed_columns']} "
            f"cases={report['cases_total']} met={report['cases_met']} "
            f"discriminating_killed={report['discriminating_killed']}"
        )
        for row in report["cases"]:
            status = "MET " if row["met"] else "MISS"
            detail = row.get("error") or (
                f"killed_on={row['killed_on']} rewards={row['rewards']}"
            )
            print(f"    [{status}] {row['case']:24s} {detail}")
    print(json.dumps(out, indent=2, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

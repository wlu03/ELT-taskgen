"""Execute every declared attack of every gold-frozen task, per NEW construct.

WHY THIS EXISTS
Width without discrimination is worthless: a mart can carry a RATIO, a CASE
ladder and an ARGMAX and still be a task no solver can fail, if the wrong
implementation of each of those constructs scores 1.0 on every population.
This runs the pipeline's OWN mutation + scoring machinery
(`verification/attacks.py::run_attack`, which scores exclusively through the
one reward implementation) against the frozen gold of every task that reached
reference, and reports per attack kind:

  * how many task-instances DECLARED it,
  * on how many the mutant actually scored < 1.0 somewhere (a KILL),
  * which populations did the killing,
  * and, loudly, every declared attack that scored 1.0 EVERYWHERE — an
    unrealizable surface, which is the failure mode this verification exists
    to catch.

`tools/prove_attacks.py` does one task; this aggregates a corpus and binds each
attack kind to the construct it is supposed to police.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from elt_taskgen.models import TaskIR
from elt_taskgen.reference import gold as gold_mod
from elt_taskgen.verification import attacks as attacks_mod

PLAN_AGNOSTIC = {"constants", "keys_only", "no_op", "skip_extraction"}

#: Which NEW construct each attack kind is the named wrong implementation OF.
#: The `custom` kind carries its construct in the case name/directive, so it is
#: resolved per case rather than per kind.
CONSTRUCT_OF_KIND = {
    "dropped_filter": "filtered_aggregate (CASE inside the aggregate)",
    "no_dedup": "distinct (COUNT DISTINCT)",
    "wrong_denominator": "ratio (CAST/NULLIF guard)",
    "wrong_window": "window + extrema (PARTITION BY / ORDER BY / tie-break)",
    "wrong_grain": "aggregate grain (GROUP BY key set)",
    "inner_join": "LEFT JOIN hop",
    "no_null_default": "COALESCE null default",
}


def construct_for(case) -> str:
    kind = case.kind.value
    if kind == "custom":
        name = case.name.lower()
        if "boundary" in name or "else" in name or "ladder" in name:
            return "conditional (CASE ladder boundary / ELSE)"
        return f"custom:{case.name}"
    return CONSTRUCT_OF_KIND.get(kind, kind)


def _dest() -> Path:
    return Path(__file__).resolve().parents[1] / "docs" / "difficulty" / "verify_attacks.json"


def prove_task(workspace: Path, task_id: str) -> dict | None:
    task_dir = workspace / "tasks" / task_id
    ak = task_dir / "answer_key"
    if not (ak / "manifest.json").is_file():
        return None
    task = TaskIR.model_validate_json((task_dir / "task_ir.json").read_text())
    gold = gold_mod.load_gold(ak)
    rows = []
    for case in task.attack_cases:
        entry = {
            "case": case.name,
            "kind": case.kind.value,
            "construct": construct_for(case),
            "discriminating": case.kind.value not in PLAN_AGNOSTIC,
        }
        try:
            measured = attacks_mod.run_attack(task, case, gold, workspace)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["killed_on"] = []
            entry["met"] = False
            rows.append(entry)
            continue
        rewards = {p.value: round(float(r), 4) for p, r in measured.items()}
        entry["rewards"] = rewards
        entry["killed_on"] = sorted(p for p, r in rewards.items() if r < 1.0)
        entry["met"] = all(
            (rewards.get(p.value, 0.0) >= 1.0) is bool(v)
            for p, v in case.expected_pass.items()
        )
        rows.append(entry)
    return {
        "task_id": task_id,
        "workspace": str(workspace),
        "target_columns": sum(len(m.columns) for m in task.marts),
        "computed_columns": sum(
            1 for m in task.marts for c in m.columns if getattr(c, "computed", False)
        ),
        "cases": rows,
    }


def main() -> int:
    workspaces = [Path(a) for a in sys.argv[1:]] or [Path("/tmp/verify_ws")]
    reports = []
    for ws in workspaces:
        tdir = ws / "tasks"
        if not tdir.is_dir():
            print(f"missing {tdir}")
            continue
        for d in sorted(tdir.iterdir()):
            if not d.is_dir():
                continue
            r = prove_task(ws, d.name)
            if r is None:
                print(f"SKIP (no gold) {d.name}")
                continue
            reports.append(r)
            killed = sum(1 for c in r["cases"] if c["discriminating"] and c["killed_on"])
            decl = sum(1 for c in r["cases"] if c["discriminating"])
            print(
                f"{d.name}: target={r['target_columns']} "
                f"computed={r['computed_columns']} "
                f"discriminating {killed}/{decl} killed",
                flush=True,
            )
            # Persist after EVERY task, not at the end: a full corpus pass is
            # ~4 minutes per task, and an interrupted run that has proved 29
            # tasks must not throw those 29 away.
            _dest().write_text(json.dumps(reports, indent=2, sort_keys=True) + "\n")

    by_construct: dict[str, dict] = defaultdict(
        lambda: {"declared": 0, "killed": 0, "errors": 0, "pops": defaultdict(int),
                 "survivors": [], "failures": []}
    )
    for r in reports:
        for c in r["cases"]:
            if not c["discriminating"]:
                continue
            b = by_construct[c["construct"]]
            b["declared"] += 1
            if c.get("error"):
                b["errors"] += 1
                b["failures"].append(f"{r['task_id']}::{c['case']}: {c['error'][:160]}")
                continue
            if c["killed_on"]:
                b["killed"] += 1
                for p in c["killed_on"]:
                    b["pops"][p] += 1
            else:
                b["survivors"].append(f"{r['task_id']}::{c['case']}")

    print("\n===== PER-CONSTRUCT ATTACKABILITY =====")
    for construct in sorted(by_construct):
        b = by_construct[construct]
        pops = dict(sorted(b["pops"].items(), key=lambda kv: -kv[1]))
        print(
            f"{construct}\n"
            f"    declared on {b['declared']} task-instances; "
            f"KILLED on {b['killed']}; unkillable {len(b['survivors'])}; "
            f"errors {b['errors']}\n"
            f"    killing populations: {pops}"
        )
        for s in b["survivors"][:6]:
            print(f"    SURVIVED EVERYWHERE (reward 1.0 on every population): {s}")
        for f in b["failures"][:6]:
            print(f"    MUTATION ERROR: {f}")

    dest = _dest()
    dest.write_text(json.dumps(reports, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {dest}")
    unkillable = sum(len(b["survivors"]) for b in by_construct.values())
    errors = sum(b["errors"] for b in by_construct.values())
    print(f"TOTAL unkillable declared attacks: {unkillable}; mutation errors: {errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Execute the EXTRACT-LOAD attack catalogue and report which mutants kill.

WHY THIS EXISTS
`tools/prove_attacks.py` measures the transform mutants; every number it has
ever printed is evidence about the TRANSFORM subtask. This is its extract-load
counterpart: it compiles the EL catalogue
(`generation/populations.py::el_attack_cases`) for a frozen task, executes each
case through the ONE mutation engine (`verification/attacks.py::run_attack`,
which mutates the RENDERED ARTIFACTS and loads them with the trusted loader),
and scores with the ONE reward implementation via
`upstream_eval.evaluate_variant(EXTRACT_LOAD)` — strict binary over the frozen
stage-1 count vector.

WHAT IT REPORTS, AND WHY EACH COLUMN EXISTS
  * el       — the EL reward per population. 0.0 is a kill, 1.0 is a LEAK.
  * broke    — the tables whose counts broke, quoted from the reward's own
               stage-1 detail. A strict-binary reward collapses every count
               error into one 0.0, so without this a kill is unreadable.
  * how      — `count` (the load landed the wrong number of rows) or
               `load-error` (the wrong load crashed before producing a
               warehouse). Both score 0.0 and only one of them is evidence
               that the COUNT VECTOR discriminates, so they are never merged.
  * inert    — the mutant kept FULL EL reward everywhere; `run_attack` raises
               on this by design and it is recorded, never swallowed.

Nothing here re-implements mutation or scoring. The workspace is cloned
(hardlinks) before anything runs, so a measurement never writes into the
workspace it measured.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from elt_taskgen.generation.populations import (
    POLICY_CONSTRUCTED,
    _skip_extraction_case,
    el_attack_cases,
)
from elt_taskgen.models import PopulationName, TaskIR, TaskVariant
from elt_taskgen.reference import gold as gold_mod
from elt_taskgen.verification import attacks as attacks_mod
from elt_taskgen.verification import gates as gates_mod


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _clone_task(workspace: Path, task_id: str) -> Path:
    scratch = Path(tempfile.mkdtemp(prefix=".el_prove_", dir=str(workspace)))
    dst = scratch / "tasks" / task_id
    shutil.copytree(
        workspace / "tasks" / task_id, dst, copy_function=_link_or_copy
    )
    return scratch


def prove(workspace: Path, task_id: str) -> dict:
    task_dir = workspace / "tasks" / task_id
    task = TaskIR.model_validate_json((task_dir / "task_ir.json").read_text())
    gold = gold_mod.load_gold(task_dir / "answer_key")

    backend_count = len({b.backend for b in task.backends})
    cases = list(
        el_attack_cases(
            task.tables,
            backend_assignments=task.backends,
            backends=backend_count,
            policy=POLICY_CONSTRUCTED,
            populations=task.populations,
        )
    )
    # `skip_extraction` is declared with the other plan-level degenerates, but
    # it is a LOAD mutant like the rest of this matrix and belongs in the same
    # measurement — reporting the EL surface without it would understate it.
    if backend_count > 1:
        cases.append(_skip_extraction_case(backend_count, POLICY_CONSTRUCTED))
    # Kinds the census (gates.EXTRACTION_MUTANT_KIND_NAMES) knows but this
    # task cannot honestly declare — recorded WITH the reason, never silently
    # skipped: an absent kind nobody can see is indistinguishable from a hole.
    declared_kinds = {c.kind.value for c in cases}
    undeclared: dict[str, str] = {}
    for kind_name in gates_mod.EXTRACTION_MUTANT_KIND_NAMES:
        if kind_name in declared_kinds:
            continue
        directive = "skip_backend" if kind_name == "skip_extraction" else kind_name
        try:
            attacks_mod.resolve_load_mutation(task, directive, "", gold)
        except attacks_mod.InapplicableLoadMutationError as exc:
            undeclared[kind_name] = str(exc)
        except Exception as exc:  # noqa: BLE001 — visibility over polish
            undeclared[kind_name] = f"{type(exc).__name__}: {exc}"
        else:
            undeclared[kind_name] = (
                "resolvable on this data but NOT declared by el_attack_cases "
                "— a declaration gap worth investigating"
            )
    scratch = _clone_task(workspace, task_id)
    rows: list[dict] = []
    try:
        for case in cases:
            record: dict = {
                "case": case.name,
                "required": case.required,
                "mutation": case.mutation,
            }
            try:
                measured_rewards = attacks_mod.run_attack(task, case, gold, scratch)
            except attacks_mod.InertLoadMutationError as exc:
                record.update(inert=True, error=str(exc), killed_on=[])
                rows.append(record)
                continue
            except Exception as exc:  # noqa: BLE001 — a mutant that cannot run is a finding
                record.update(
                    inert=False,
                    error=f"{type(exc).__name__}: {exc}",
                    killed_on=[],
                    unavailable=True,
                )
                rows.append(record)
                continue
            if not measured_rewards:
                # An informational probe whose surface this task's data does
                # not offer: run_attack recorded the reason instead of rewards.
                reason = json.loads(
                    (
                        scratch / "tasks" / task_id / "attacks" / case.name
                        / "inapplicable.json"
                    ).read_text(encoding="utf-8")
                )["inapplicable"]
                record.update(
                    inert=False, error=reason, killed_on=[], unavailable=True
                )
                rows.append(record)
                continue
            measured = json.loads(
                (scratch / "tasks" / task_id / "attacks" / case.name / "rewards.json")
                .read_text(encoding="utf-8")
            )
            el = measured["rewards_by_variant"][TaskVariant.EXTRACT_LOAD.value]
            breaks = measured.get("stage1_breaks", {})
            errors = measured.get("errors", {})
            record.update(
                inert=False,
                el=el,
                full=measured["rewards"],
                killed_on=sorted(p for p, r in el.items() if r < 1.0),
                leaked_on=sorted(p for p, r in el.items() if r >= 1.0),
                broke={
                    pop: sorted(tables)[:4] for pop, tables in sorted(breaks.items())
                },
                how={
                    pop: (
                        "load-error"
                        if f"{pop}/__load__" in errors
                        else ("count" if pop in breaks else "unexplained")
                    )
                    for pop in sorted(p for p, r in el.items() if r < 1.0)
                },
                detail=measured.get("load_mutation", {}).get("detail", ""),
            )
            rows.append(record)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    graded = [p.value for p in PopulationName if p in {q.name for q in task.populations}]
    return {
        "task_id": task_id,
        "tables": len(task.tables),
        "backends": sorted({b.backend.value for b in task.backends}),
        "graded_populations": graded,
        "cases": rows,
        "cases_total": len(rows),
        "cases_killing_everywhere": sum(
            1
            for r in rows
            if not r.get("inert")
            and not r.get("unavailable")
            and r.get("killed_on")
            and not r.get("leaked_on")
        ),
        "cases_inert": sum(1 for r in rows if r.get("inert")),
        "cases_unavailable": sum(1 for r in rows if r.get("unavailable")),
        "kinds_undeclared": undeclared,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute the extract-load attack catalogue for frozen tasks."
    )
    parser.add_argument("workspace", type=Path)
    parser.add_argument("task_ids", nargs="*")
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="explicit JSON report path (generated evidence is never written at repo root implicitly)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace = args.workspace.resolve()
    task_ids = args.task_ids or [
        d.name
        for d in sorted((workspace / "tasks").iterdir())
        if d.is_dir() and (d / "answer_key" / "manifest.json").is_file()
    ]
    out = []
    for task_id in task_ids:
        report = prove(workspace, task_id)
        out.append(report)
        print(
            f"{report['task_id']}: tables={report['tables']} "
            f"backends={','.join(report['backends'])} "
            f"cases={report['cases_total']} "
            f"kill_everywhere={report['cases_killing_everywhere']} "
            f"inert={report['cases_inert']} "
            f"unavailable={report['cases_unavailable']}"
        )
        for kind_name, why in sorted(report["kinds_undeclared"].items()):
            print(f"    [undeclared] {kind_name}: {why}")
        for row in report["cases"]:
            if row.get("inert"):
                print(f"    {row['case']}: INERT")
            elif row.get("unavailable"):
                print(f"    {row['case']}: UNAVAILABLE — {row['error']}")
            else:
                print(
                    f"    {row['case']}: killed={','.join(row['killed_on']) or '-'} "
                    f"leaked={','.join(row['leaked_on']) or '-'} "
                    f"how={row['how']} broke={row['broke']}"
                )
    destination = args.out.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"report -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

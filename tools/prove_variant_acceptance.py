"""Drive the demo task through the 15-stage pipeline and show three verdicts.

WHY THIS EXISTS
The per-variant acceptance wiring makes three claims that are only convincing
when a real run makes them: (1) one task produces THREE acceptance reports,
each bound to the same parent content hash but carrying its own roster and its
own verdict; (2) a VARIANT-LOCAL failure refuses one graded unit while the
parent and its sibling still ship; (3) the release manifest says why the
refused unit is missing. This script runs the actual pipeline (offline,
replay-only transcripts) and prints the evidence.

Usage:
    python tools/prove_variant_acceptance.py <workspace> [--stub] [--fail el|t]

`--fail` flips ONE gate red before the report is recorded, so the refusal path
can be exercised without corrupting a task. It patches the gate RESULT, never
the reward — there is one reward implementation, and this script does not get
to be a second one.

`--stub` replaces `gates.run_variant_gates` with a roster-complete all-green
battery. BE HONEST ABOUT WHAT THIS DOES AND DOES NOT SHOW: it proves the
PIPELINE, ACCEPTANCE and EXPORT wiring — three ledger rows, three rosters,
per-unit shipping, manifest bookkeeping — and it proves nothing whatsoever
about the tasks. It exists because the EL/T batteries depend on evidence
producers that are still landing (the artifact census, the independent load
build, the split determinism digests, the extraction-mutant catalogue), so
without it the real battery correctly refuses every subtask and the happy
path cannot be reached. Run WITHOUT `--stub` to see the real battery: it
refuses, and because its failures are TASK-LEVEL the whole task is held back.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from elt_taskgen import cli, demo_fixture
from elt_taskgen.engine import Engine, variant_gate_stage
from elt_taskgen.models import (
    AcceptanceReport,
    GateResult,
    TaskVariant,
    variant_task_id,
)
from elt_taskgen.verification import gates as gates_mod
from elt_taskgen.verification import variant_battery as battery_mod

#: A VARIANT-LOCAL gate for each subtask (design R5): failing it refuses that
#: unit alone. Using a task-level gate here would (correctly) repair the task.
LOCAL_GATE = {
    TaskVariant.EXTRACT_LOAD: "shortcut-probes",
    TaskVariant.TRANSFORM: "shortcut-probes",
}


def patch_stub() -> None:
    """Stand-in battery: every roster gate green. Wiring proof only."""
    original = gates_mod.run_variant_gates

    def stub(v, task, workspace, gold, rewards_by_variant=None):
        v = TaskVariant(v)
        if v is TaskVariant.FULL:
            return original(v, task, workspace, gold, rewards_by_variant)
        return AcceptanceReport.from_gates(
            task_id=variant_task_id(task.task_id, v),
            revision=task.current_revision,
            task_content_hash=task.content_hash(),
            gates=[
                GateResult(
                    gate=name, passed=True, details="STAND-IN (proof harness)"
                )
                for name in battery_mod.gate_roster(v)
            ],
            scorer_version=gates_mod.SCORER_VERSION,
        )

    gates_mod.run_variant_gates = stub


def patch_failure(variant: TaskVariant) -> None:
    original = gates_mod.run_variant_gates
    gate_name = LOCAL_GATE[variant]

    def wrapped(v, task, workspace, gold, rewards_by_variant=None):
        report = original(v, task, workspace, gold, rewards_by_variant)
        if TaskVariant(v) is not variant:
            return report
        gates = [
            g.model_copy(update={"passed": False, "details": "FORCED (proof harness)"})
            if g.gate == gate_name
            else g
            for g in report.gates
        ]
        return AcceptanceReport.from_gates(
            task_id=report.task_id,
            revision=report.revision,
            task_content_hash=report.task_content_hash,
            gates=gates,
            scorer_version=report.scorer_version,
        )

    gates_mod.run_variant_gates = wrapped


def show(workspace: Path) -> None:
    task = demo_fixture.demo_task()
    engine = Engine(workspace)
    try:
        stored = engine.load_task(task.task_id)
        current = stored.content_hash()
        print(f"\nparent content hash: {current}")
        print("\nTHREE ACCEPTANCE REPORTS (one per graded unit)")
        print("-" * 78)
        for variant in TaskVariant:
            stage = variant_gate_stage(variant).value
            row = engine.latest_report(stored.task_id, stage)
            if row is None:
                print(f"{variant.value:<13} {stage:<20} (no battery recorded)")
                continue
            payload = json.loads(row.payload_json)
            roster = battery_mod.gate_roster(variant)
            summary = battery_mod.summarize_payload(payload, variant)
            failing = ", ".join(summary["failing_gates"]) or "-"
            print(
                f"{variant.value:<13} stage={stage:<20} ledger={row.verdict:<5} "
                f"accepted={str(bool(payload.get('accepted'))):<5} "
                f"gates={summary['gates_passed']}/{summary['gates_recorded']} "
                f"roster={len(roster)} bound={row.content_hash == current} "
                f"unit_id={payload.get('task_id')}"
            )
            print(f"{'':<13} failing: {failing}")
    finally:
        engine.close()

    manifest_path = workspace / "release" / "release_manifest.json"
    if not manifest_path.is_file():
        print("\n(no release manifest: nothing shipped)")
        return
    manifest = json.loads(manifest_path.read_text())
    print("\nRELEASE MANIFEST")
    print("-" * 78)
    print("shipped variants:", manifest.get("variants"))
    print("rejected variants:")
    for vid, reason in sorted(manifest.get("rejected_variants", {}).items()):
        print(f"  {vid}: {reason}")
    for tid, records in sorted(manifest.get("variant_acceptance", {}).items()):
        print(f"per-unit acceptance for {tid}:")
        for record in records:
            print(
                f"  {record['variant']:<13} accepted={str(record['accepted']):<5} "
                f"shipped={str(record['shipped']):<5} "
                f"gates {record['gates_passed']}/{record['gates_recorded']} "
                f"of {record['gates_applicable']} applicable "
                f"class={record['classification'] or '-'}"
            )
    public = workspace / "release" / "public"
    if public.is_dir():
        print("public/ units:", sorted(p.name for p in public.iterdir()))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    workspace = Path(argv[0]).resolve()
    if "--stub" in argv:
        print("### STAND-IN variant batteries (wiring proof; certifies NOTHING)")
        patch_stub()
    if "--fail" in argv:
        which = argv[argv.index("--fail") + 1]
        variant = {"el": TaskVariant.EXTRACT_LOAD, "t": TaskVariant.TRANSFORM}[which]
        print(f"### forcing a VARIANT-LOCAL failure on {variant.value}")
        patch_failure(variant)
    code = cli.main(["demo", "--workspace", str(workspace)])
    print(f"\ndemo exit code: {code}")
    show(workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Define variant-battery roster, scope, coverage, currency, and dispatch.

Each variant requires its own battery. Gate applicability and replacements come from
`gates`; skipped or missing results never represent success.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from elt_taskgen.models import (
    AcceptanceReport,
    GateResult,
    RLVR_TASK_VARIANTS,
    TaskIR,
    TaskVariant,
    variant_task_id,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from elt_taskgen.reference.gold import GoldBundle


#: Replaces `populations-load` for TRANSFORM: T sees warehouse/, not populations/.
WAREHOUSES_LOAD = "warehouses-load"
#: Replace `dual-build-agreement` for EXTRACT_LOAD; neither alone suffices: the
#: census certifies the GOLD, the independent load certifies BUNDLE SUFFICIENCY.
EL_ARTIFACT_CENSUS = "el-artifact-census"
EL_INDEPENDENT_LOAD = "el-independent-load"


def gate_roster(variant: TaskVariant) -> tuple[str, ...]:
    """The gates that MUST appear in `variant`'s AcceptanceReport.

    Delegated to ``gates.VARIANT_GATE_NAMES``, never duplicated. A missing or
    empty roster RAISES — an empty roster accepts everything.
    """
    variant = TaskVariant(variant)
    from elt_taskgen.verification import gates as gates_mod

    published = getattr(gates_mod, "VARIANT_GATE_NAMES", None)
    if not isinstance(published, dict):
        raise ValueError(
            "gates.VARIANT_GATE_NAMES is missing: without a published roster "
            "there is no coverage requirement to enforce, and a battery that "
            "cannot be checked for coverage must not ship (fail closed)"
        )
    for key, names in published.items():
        if TaskVariant(key) is variant:
            if not names:
                raise ValueError(
                    f"gates.VARIANT_GATE_NAMES[{variant.value!r}] is empty; "
                    "an empty roster accepts everything (fail closed)"
                )
            return tuple(names)
    raise ValueError(f"gates.VARIANT_GATE_NAMES has no roster for {variant.value!r}")


def variant_gate_names() -> dict[TaskVariant, tuple[str, ...]]:
    """Active RLVR unit -> mandatory roster, read live (never cached)."""
    return {variant: gate_roster(variant) for variant in RLVR_TASK_VARIANTS}


# R5 — failure partition

TASK_LEVEL = "task-level"
VARIANT_LOCAL = "variant-local"

#: Evidence key a gate sets to affirm VARIANT-LOCAL scope; silence stays TASK-LEVEL.
SCOPE_EVIDENCE_KEY = "failure_scope"


def classify_gate_failure(variant: TaskVariant, gate: GateResult | dict[str, Any]) -> str:
    """TASK_LEVEL or VARIANT_LOCAL for one failing gate.

    Delegated to ``gates.classify_variant_failure``; the default is TASK-LEVEL
    because an unaffirmed failure may be a masked parent claim.
    """
    variant = TaskVariant(variant)
    if isinstance(gate, dict):
        name = str(gate.get("gate", ""))
        evidence = gate.get("evidence") or {}
    else:
        name = gate.gate
        evidence = dict(gate.evidence)
    if str(evidence.get(SCOPE_EVIDENCE_KEY, "")).strip().lower() == VARIANT_LOCAL:
        return VARIANT_LOCAL
    from elt_taskgen.verification import gates as gates_mod

    published = getattr(gates_mod, "classify_variant_failure", None)
    if published is None:
        # No published partition means no evidence that this failure is local.
        return TASK_LEVEL
    return str(published(variant, name))


def classify_report(variant: TaskVariant, report: AcceptanceReport) -> tuple[str, tuple[str, ...]]:
    """(classification, failing gate names) for a whole variant battery.

    The strongest failure wins: ANY task-level failure classifies the battery
    TASK_LEVEL, so a masked parent claim is never diluted by variant-local ones.
    """
    failing = tuple(g.gate for g in report.gates if not g.passed)
    if not failing:
        return VARIANT_LOCAL, ()
    for gate in report.gates:
        if not gate.passed and classify_gate_failure(variant, gate) == TASK_LEVEL:
            return TASK_LEVEL, failing
    return VARIANT_LOCAL, failing


# Coverage (R2/R3) — the half AcceptanceReport cannot enforce: it is fail-closed
# on gate RESULTS but says nothing about gate COVERAGE.

def missing_gates(variant: TaskVariant, gate_names: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    """Roster entries absent from a recorded battery, in roster order."""
    recorded = {str(name) for name in gate_names}
    return tuple(name for name in gate_roster(variant) if name not in recorded)


def verify_coverage(variant: TaskVariant, report: AcceptanceReport) -> None:
    """Raise unless every roster gate is PRESENT in `report`.

    Unconditional, not only for reports claiming acceptance: a battery that
    skipped gates has to be re-run whatever verdict it came out with.
    """
    missing = missing_gates(variant, [g.gate for g in report.gates])
    if missing:
        raise ValueError(
            f"variant {TaskVariant(variant).value!r}: battery is missing "
            f"{list(missing)} — a gate that 'does not apply' is expressed by "
            "being absent from the ROSTER with a named replacement in it, "
            "never by an absent or skipped result; re-run the battery"
        )


# Dispatch

_UNWIRED_GATE = "variant-battery-wired"


def _unwired_report(variant: TaskVariant, task: TaskIR, detail: str) -> AcceptanceReport:
    """The fail-closed shape of 'no battery exists yet': red AND
    coverage-incomplete, so release refuses the variant twice over."""
    return AcceptanceReport.from_gates(
        task_id=variant_task_id(task.task_id, variant),
        revision=task.current_revision,
        task_content_hash=task.content_hash(),
        gates=[
            GateResult(
                gate=_UNWIRED_GATE,
                passed=False,
                details=detail,
                evidence={"variant": TaskVariant(variant).value},
            )
        ],
        scorer_version=scorer_version(),
        roster_digest=roster_digest(),
    )


def scorer_version() -> str:
    from elt_taskgen.verification.gates import SCORER_VERSION

    return str(SCORER_VERSION)


def roster_digest() -> str:
    """The live roster identity (gates.ROSTER_DIGEST) — delegated, never copied."""
    from elt_taskgen.verification import gates as gates_mod

    return str(gates_mod.roster_digest())


def run_variant_battery(
    variant: TaskVariant,
    task: TaskIR,
    workspace: Path,
    gold: "GoldBundle",
    rewards_by_variant: dict[str, dict[str, dict[str, float]]],
) -> AcceptanceReport:
    """Run `variant`'s own battery with its own reward and its own witnesses.

    Nothing inherits a PASS from another variant; gates that "apply as-is" still
    execute. The only guarantee made here is the report's identity binding.
    """
    variant = TaskVariant(variant)
    if variant is TaskVariant.FULL:
        raise ValueError(
            "FULL's battery is gates.run_gates on the `gates` stage and is "
            "deliberately unchanged (R4); run_variant_battery serves the "
            "subtask variants only"
        )
    from elt_taskgen.verification import gates as gates_mod

    runner = getattr(gates_mod, "run_variant_gates", None)
    if runner is None:
        return _unwired_report(
            variant,
            task,
            "gates.run_variant_gates is not implemented in this tree, so this "
            "variant has no battery; refusing to ship it (fail closed) rather "
            "than re-labelling the parent battery as a variant one",
        )
    report = runner(variant, task, workspace, gold, rewards_by_variant)
    _assert_binding(variant, task, report)
    return report


def _assert_binding(variant: TaskVariant, task: TaskIR, report: AcceptanceReport) -> None:
    """Raise unless the report carries the VARIANT's unit id at the PARENT's
    content hash, under this reader's scorer and roster. Any other combination
    means the battery measured something other than what is about to ship."""
    expected_id = variant_task_id(task.task_id, variant)
    if report.task_id != expected_id:
        raise ValueError(
            f"variant battery for {variant.value!r} reported task_id "
            f"{report.task_id!r}, expected {expected_id!r}"
        )
    if report.task_content_hash != task.content_hash():
        raise ValueError(
            f"variant battery for {variant.value!r} is bound to content hash "
            f"{report.task_content_hash[:12]}, task is now "
            f"{task.content_hash()[:12]} — re-run, do not record"
        )
    # A runner/reader scorer or roster mismatch records a battery stale on
    # arrival and re-run every sweep: a code inconsistency, not a task defect.
    if report.scorer_version != scorer_version() or report.roster_digest != roster_digest():
        raise ValueError(
            f"variant battery for {variant.value!r} was produced under scorer "
            f"{report.scorer_version}/roster {report.roster_digest or '(none)'}, "
            f"this reader is {scorer_version()}/{roster_digest()} — the runner "
            "and the reader disagree; refusing to record a battery that would "
            "be stale on arrival"
        )


# Reading a recorded battery back (release + selection consume these)

def summarize_payload(payload: dict[str, Any], variant: TaskVariant) -> dict[str, Any]:
    """Roster-aware summary of a recorded AcceptanceReport payload.

    Returns accepted, gates_applicable, gates_recorded, gates_passed,
    failing_gates, missing_gates, classification. Never raises: an unreadable
    payload comes back `accepted=False` with a reason (fail closed).
    """
    variant = TaskVariant(variant)
    roster = gate_roster(variant)
    gates = payload.get("gates") if isinstance(payload, dict) else None
    if not isinstance(gates, list):
        return {
            "accepted": False,
            "gates_applicable": len(roster),
            "gates_recorded": 0,
            "gates_passed": 0,
            "failing_gates": (),
            "missing_gates": roster,
            "classification": TASK_LEVEL,
            "reason": "battery payload is unreadable (fail closed)",
        }
    entries = [g for g in gates if isinstance(g, dict)]
    names = [str(g.get("gate")) for g in entries]
    failing = tuple(str(g.get("gate")) for g in entries if not g.get("passed"))
    missing = missing_gates(variant, names)
    classification = VARIANT_LOCAL
    for entry in entries:
        if not entry.get("passed") and classify_gate_failure(variant, entry) == TASK_LEVEL:
            classification = TASK_LEVEL
            break
    if missing:
        # A roster hole is never variant-local: the battery did not measure
        # what it claims to, so the parent's evidence is in question.
        classification = TASK_LEVEL
    return {
        "accepted": bool(payload.get("accepted")) and not missing,
        "gates_applicable": len(roster),
        "gates_recorded": len(entries),
        "gates_passed": sum(1 for g in entries if g.get("passed")),
        "failing_gates": failing,
        "missing_gates": missing,
        "classification": classification,
        "reason": "",
    }


# Currency: is a recorded battery evidence about the CURRENT roster/scorer?

#: Gate carrying the roster a legacy battery ran against (top-level `roster` is newer).
ROSTER_GATE = "variant-roster"


def recorded_roster(payload: dict[str, Any]) -> tuple[str, ...]:
    """The gate roster a recorded battery says it was measured against.

    Three sources, most explicit first, so legacy payloads stay readable:
    top-level ``roster``, the ``variant-roster`` gate's comma-joined
    ``evidence['roster']``, then the recorded gate names. Empty when unreadable.
    """
    if not isinstance(payload, dict):
        return ()
    roster = payload.get("roster")
    if isinstance(roster, (list, tuple)) and roster:
        return tuple(str(name) for name in roster)
    gates = payload.get("gates")
    if not isinstance(gates, list):
        return ()
    entries = [g for g in gates if isinstance(g, dict)]
    for entry in entries:
        if str(entry.get("gate")) != ROSTER_GATE:
            continue
        evidence = entry.get("evidence")
        recorded = evidence.get("roster") if isinstance(evidence, dict) else None
        if isinstance(recorded, str) and recorded.strip():
            return tuple(
                name.strip() for name in recorded.split(",") if name.strip()
            )
    return tuple(str(g.get("gate")) for g in entries if g.get("gate") is not None)


def roster_staleness(payload: dict[str, Any], variant: TaskVariant) -> str:
    """Return an empty string for a current battery, otherwise a rerun reason.

    Currency requires readable evidence, current scorer and roster digests, and full
    roster coverage. Missing provenance or claimed-but-unrun gates are stale, not
    exceptions or rejections.
    """
    variant = TaskVariant(variant)
    if not isinstance(payload, dict) or not isinstance(payload.get("gates"), list):
        return "battery payload unreadable"
    recorded_scorer = str(payload.get("scorer_version") or "")
    current_scorer = scorer_version()
    if recorded_scorer != current_scorer:
        return (
            f"battery recorded under scorer {recorded_scorer or '(none)'}, "
            f"current {current_scorer}"
        )
    summary = summarize_payload(payload, variant)
    missing = tuple(summary.get("missing_gates") or ())
    if missing:
        recorded = recorded_roster(payload)
        current = gate_roster(variant)
        claimed = [name for name in missing if name in recorded]
        if claimed:
            return (
                f"battery claims roster gate(s) [{', '.join(claimed)}] it "
                f"never ran (recorded {len(recorded)} of current {len(current)})"
            )
        return (
            f"battery predates current roster gate(s) [{', '.join(missing)}] "
            f"(recorded {len(recorded)} of current {len(current)})"
        )
    # Every current-scorer battery stamps a digest, so a payload without one is
    # unknown provenance, not a legacy shape: stale, and release reads it the
    # same way so currency and acceptance can never disagree.
    recorded_digest = str(payload.get("roster_digest") or "")
    if recorded_digest != roster_digest():
        return (
            f"battery recorded under roster {recorded_digest or '(none)'}, "
            f"current {roster_digest()}"
        )
    return ""

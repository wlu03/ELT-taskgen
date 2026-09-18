"""Define the only code path that can accept a task.

Every gate checks bound, current recorded evidence and fails closed.
`AcceptanceReport.accepted` is true only when the complete gate roster passes.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict

from elt_taskgen.generation.source_data import (
    REALIZED_DIVERGENCE_MAX_PCT,
    REALIZED_DIVERGENCE_MIN_PCT,
    REALIZED_DIVERGENCE_MIN_SCALE,
    declares_dangling,
)
from elt_taskgen.generation.source_data import _POLICIES as _GENERATION_POLICIES
from elt_taskgen.verification.contamination import enforcement, enforcing
from elt_taskgen.models import (
    AcceptanceReport,
    AttackCase,
    AttackKind,
    GateResult,
    MartOpKind,
    MartSpec,
    PopulationName,
    RLVR_TASK_VARIANTS,
    Row,
    TaskIR,
    TaskVariant,
    canonical_json,
    sha256_hex,
    variant_task_id,
)
from elt_taskgen.verification import upstream_eval

if TYPE_CHECKING:  # GoldBundle is owned by reference/gold.py (see INTERFACES.md)
    from elt_taskgen.reference.gold import GoldBundle

#: SEMANTIC scorer version — bump whenever a gate's logic or the roster
#: changes; a battery recorded under another version is STALE, never accepted.
SCORER_VERSION: str = "1.3.0"

#: Exact gate names, in battery order (INTERFACES.md).
GATE_NAMES: tuple[str, ...] = (
    "trusted-solution",
    "determinism",
    "degenerate-zero",
    "required-mutants",
    "shortcut-probes",
    "data-sensitivity",
    "info-content",
    "populations-load",
    "contamination-clean",
    "dual-build-agreement",
    "referential-integrity",
    "declared-scale-reconciliation",
    "mart-key-unique",
)

#: Recorded-evidence locations, relative to tasks/<task_id>/.
DETERMINISM_EVIDENCE_REL = "reports/determinism.json"
CONTAMINATION_POST_EVIDENCE_REL = "reports/contamination_post.json"
#: Recorded by reference/independent.py; kept in sync with its own constant.
DUAL_BUILD_EVIDENCE_REL = "reports/independent_build.json"
#: Recorded by verification/perturbation.py; duplicated rather than imported
#: because importing it would close an import cycle through the reference runner.
PERTURBATION_PROBE_EVIDENCE_REL = "reports/perturbation_probe.json"
#: In sync with perturbation.PROBE_KIND: another probe shape landing at the same
#: filename must not be accepted as this one.
PERTURBATION_PROBE_KIND = "value-bijection"

#: Status string the independent build records on full agreement.
DUAL_BUILD_STATUS_AGREED = "agreed"
DUAL_BUILD_ROLE = "independent_implementer"

#: Producer notes naming why a witness build was not performed or FAILED (the
#: transport class name included). Read only at the current identity and only to
#: NAME the cause in a red gate's detail; a note never turns a gate green.
EL_EVIDENCE_NOTES_REL = "reports/el_evidence_notes.json"
DUAL_BUILD_NOTES_REL = "reports/independent_build_notes.json"
#: Substrings marking a note as describing a not-performed/failed witness.
_PRODUCER_NOTE_MARKERS = ("not performed", "FAILED")
#: The "(searched: <dirs>)" clause, dropped from surfaced notes.
_SEARCHED_CLAUSE = re.compile(r"\s*\(searched:[^)]*\)")

#: Attack kinds that score without solving. Every compiled probe of one of these
#: kinds must lose reward on at least one graded population.
SHORTCUT_KINDS: frozenset[AttackKind] = frozenset(
    {
        AttackKind.CONSTANTS,
        AttackKind.KEYS_ONLY,
        AttackKind.NO_OP,
        AttackKind.SKIP_EXTRACTION,
    }
)

#: Populations whose rewards actually grade a solver; DEVELOPMENT is excluded
#: because it is solver-visible, so full reward there proves nothing.
GRADED_POPULATIONS: tuple[PopulationName, ...] = tuple(
    p for p in PopulationName if p is not PopulationName.DEVELOPMENT
)


def task_dir(workspace: Path, task_id: str) -> Path:
    return workspace / "tasks" / task_id


# Shared helpers

def _fail(gate: str, details: str, evidence: dict[str, str] | None = None) -> GateResult:
    return GateResult(gate=gate, passed=False, details=details, evidence=evidence or {})


def _ok(gate: str, details: str, evidence: dict[str, str] | None = None) -> GateResult:
    return GateResult(gate=gate, passed=True, details=details, evidence=evidence or {})


def _guarded(gate: str, fn: Callable[[], GateResult]) -> GateResult:
    """Run one gate; any exception is a fail-closed failure, never a pass."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — fail closed on anything
        return _fail(gate, f"gate crashed (fail closed): {exc!r}")


def _gold_problems(task: TaskIR, gold: "GoldBundle") -> list[str]:
    """Structural problems with the gold bundle (empty list = sane). The bundle
    hash is provenance, not an equality constraint: a specification-only repair
    legally changes the TaskIR hash without invalidating gold."""
    problems: list[str] = []
    if getattr(gold, "task_id", None) != task.task_id:
        problems.append(
            f"gold bundle is for task {getattr(gold, 'task_id', None)!r}, "
            f"not {task.task_id!r}"
        )
    stage1 = getattr(gold, "stage1", None)
    stage2 = getattr(gold, "stage2_csv", None)
    if not isinstance(stage1, dict) or not stage1:
        problems.append("gold bundle has no stage-1 counts")
    if not isinstance(stage2, dict) or not stage2:
        problems.append("gold bundle has no stage-2 mart CSVs")
    return problems


def _gold_mart_rows(
    gold: "GoldBundle", population: str, mart: str
) -> tuple[tuple[str, ...], list[Row]] | None:
    """Parsed (columns, rows) of one frozen mart CSV, or None if absent/bad."""
    csv_text = (getattr(gold, "stage2_csv", None) or {}).get(population, {}).get(mart)
    if csv_text is None:
        return None
    try:
        cols, rows = upstream_eval.parse_canonical_csv(csv_text)
    except ValueError:
        return None
    if not cols:
        return None
    return cols, rows


# Dual-build evidence (shared by trusted-solution and dual-build-agreement)

def _bound_record(
    task: TaskIR,
    workspace: Path,
    rel: str,
    label: str,
    *,
    required_keys: tuple[str, ...],
    absent_hint: str,
) -> tuple[dict | None, str | None]:
    """(record, None) for a JSON record bound to this task_id and the CURRENT
    content hash, else (None, why-not). Missing, unreadable, mistyped,
    wrong-task and stale records all yield an error, never a guess.
    """
    path = task_dir(workspace, task.task_id) / rel
    if not path.is_file():
        return None, (
            f"no {label} evidence at {rel} — {absent_hint}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable {label} evidence at {rel}: {exc}"
    if not isinstance(data, dict):
        return None, f"{label} evidence is not a JSON object"
    missing = [k for k in required_keys if k not in data]
    if missing:
        return None, f"{label} evidence missing keys: {missing}"
    if data["task_id"] != task.task_id:
        return None, (
            f"{label} evidence is for task {data['task_id']!r}, not "
            f"{task.task_id!r}"
        )
    current = task.content_hash()
    if data["task_content_hash"] != current:
        return None, (
            f"{label} evidence is STALE: recorded for content hash "
            f"{str(data['task_content_hash'])[:12]}, task is now "
            f"{current[:12]} — re-run it"
        )
    return data, None


def _producer_notes(task: TaskIR, workspace: Path, rel: str) -> tuple[str, ...]:
    """Producer notes bound to THIS identity that describe a not-performed or
    FAILED witness, else (). Notes decorate a red detail; they never decide a
    verdict.
    """
    record, _ = _bound_record(
        task,
        workspace,
        rel,
        "producer-notes",
        required_keys=("task_id", "task_content_hash", "notes"),
        absent_hint="",
    )
    if record is None:
        return ()
    notes = record.get("notes")
    if not isinstance(notes, list):
        return ()
    return tuple(
        _trim_producer_note(n)
        for n in notes
        if isinstance(n, str) and any(m in n for m in _PRODUCER_NOTE_MARKERS)
    )


def _trim_producer_note(note: str) -> str:
    """Drop the '(searched: <dirs>)' clause and cap the length: a workspace path
    containing 'contamination'/'licens'/'plagiar' would otherwise be read by
    repair.route_for_failure as a FATAL keyword and REJECT the task.
    """
    trimmed = _SEARCHED_CLAUSE.sub("", note).strip()
    return trimmed if len(trimmed) <= 300 else trimmed[:297] + "..."


def _with_producer_note(
    task: TaskIR, workspace: Path, rel: str, detail: str
) -> tuple[str, dict[str, str]]:
    """(detail + '; producer: <note>', {'producer_note': ...}) when a bound
    producer note exists, else (detail, {}). The original wording is kept as a
    PREFIX so consumers keyed off it (tests, repair routing) still see it."""
    notes = _producer_notes(task, workspace, rel)
    if not notes:
        return detail, {}
    joined = " | ".join(notes)
    return f"{detail}; producer: {joined}", {"producer_note": joined}


def _dual_build_record(
    task: TaskIR, workspace: Path
) -> tuple[dict | None, str | None]:
    """(record, None) for a dual-build record bound to the CURRENT content hash,
    else (None, why-not). When ABSENT, a bound producer note is appended so the
    red gate names the transport that failed, not just the absence."""
    record, err = _bound_record(
        task,
        workspace,
        DUAL_BUILD_EVIDENCE_REL,
        "dual-build",
        required_keys=("task_id", "task_content_hash", "status", "agreement"),
        absent_hint=(
            "independent build not performed (run the implementer stage with a "
            "cross-family provider or recorded transcripts)"
        ),
    )
    if record is None and err and err.startswith("no dual-build evidence"):
        # Preserve the historical wording consumers/tests key off.
        err = (
            "independent build not performed — no dual-build evidence at "
            f"{(task_dir(workspace, task.task_id) / DUAL_BUILD_EVIDENCE_REL).relative_to(workspace).as_posix()} "
            "(run the implementer stage with a cross-family provider or "
            "recorded transcripts)"
        )
        err, _ = _with_producer_note(task, workspace, DUAL_BUILD_NOTES_REL, err)
    return record, err


# The parent gates (GATE_NAMES is the roster authority)

def _dual_build_problems(record: Mapping[str, Any]) -> list[str]:
    """Everything wrong with a recorded independent-build record.

    One reading shared by both consuming gates. Self-consistency is required:
    'agreed' with no samples is evidence that no build ever ran. And status
    alone is not enough: a record can claim agreement while recording
    disagreement.
    """
    problems: list[str] = []
    samples = record.get("samples")
    if not isinstance(samples, list) or not samples:
        problems.append(
            "no build samples recorded — the evidence proves no independent "
            "build was produced or executed"
        )
        samples = []
    agreement = record.get("agreement")
    if not isinstance(agreement, dict):
        problems.append("dual-build evidence 'agreement' is not an object")
        agreement = {}
    for pop in PopulationName:
        value = agreement.get(pop.value)
        if value is None:
            problems.append(f"no agreement recorded for population {pop.value!r}")
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            problems.append(
                f"agreement for population {pop.value!r} is not numeric: {value!r}"
            )
            continue
        if numeric != 1.0:
            problems.append(
                f"independent build disagrees on {pop.value!r} (agreement "
                f"{value}, need exactly 1.0)"
            )
    # The agreement map must be the FINAL sample's measured rewards, not an
    # independent assertion sitting next to them.
    if samples:
        final = samples[-1]
        rewards = final.get("rewards") if isinstance(final, dict) else None
        if not isinstance(rewards, dict):
            problems.append(
                "final recorded sample carries no 'rewards' map to corroborate "
                "the agreement claim"
            )
        elif isinstance(agreement, dict):
            for pop in PopulationName:
                claimed = agreement.get(pop.value)
                measured = rewards.get(pop.value)
                if claimed is None or measured is None:
                    continue
                try:
                    if float(claimed) != float(measured):
                        problems.append(
                            f"agreement for {pop.value!r} ({claimed}) contradicts "
                            f"the final sample's measured reward ({measured})"
                        )
                except (TypeError, ValueError):
                    problems.append(
                        f"uncomparable agreement/reward pair for {pop.value!r}"
                    )
    if record.get("status") != DUAL_BUILD_STATUS_AGREED:
        problems.append(
            f"recorded status is {record.get('status')!r}, not "
            f"{DUAL_BUILD_STATUS_AGREED!r}"
            + (
                " — pending human adjudication"
                if record.get("status") == "needs_adjudication"
                else ""
            )
        )
    return problems


def _transform_reconstruction_problems(
    task: TaskIR, record: Mapping[str, Any]
) -> list[str]:
    """Validate independent transform reconstruction in a dual-build record.

    The executed sample must contain nonempty SQL for exactly every target mart and
    rewards for every population. The record's hash binding carries the producer's
    cross-family routing proof.
    """

    problems: list[str] = []
    if record.get("role") != DUAL_BUILD_ROLE:
        problems.append(
            f"record role is {record.get('role')!r}, not {DUAL_BUILD_ROLE!r}"
        )
    samples = record.get("samples")
    if not isinstance(samples, list) or not samples:
        return problems  # `_dual_build_problems` reports the missing sample.
    final = samples[-1]
    if not isinstance(final, dict):
        problems.append("final independent-build sample is not an object")
        return problems
    sql_by_mart = final.get("sql_by_mart")
    expected_marts = {mart.name for mart in task.marts}
    if not isinstance(sql_by_mart, dict):
        problems.append(
            "final independent-build sample carries no 'sql_by_mart' "
            "reconstruction"
        )
    else:
        actual_marts = set(sql_by_mart)
        if actual_marts != expected_marts:
            problems.append(
                "final independent-build SQL mart roster differs from the task: "
                f"expected {sorted(expected_marts)}, got {sorted(actual_marts)}"
            )
        empty_sql = sorted(
            name
            for name, sql in sql_by_mart.items()
            if not isinstance(sql, str) or not sql.strip()
        )
        if empty_sql:
            problems.append(
                f"final independent-build sample has empty SQL for {empty_sql}"
            )
    rewards = final.get("rewards")
    expected_populations = {population.value for population in PopulationName}
    if not isinstance(rewards, dict) or set(rewards) != expected_populations:
        actual = sorted(rewards) if isinstance(rewards, dict) else []
        problems.append(
            "final independent-build reward roster is incomplete: "
            f"expected {sorted(expected_populations)}, got {actual}"
        )
    return problems


def _gate_trusted_solution(
    task: TaskIR, workspace: Path, gold: "GoldBundle"
) -> GateResult:
    """Gold/comparator consistency PLUS certification by the independent build.

    Gold never self-certifies: a wrong reference produces wrong gold that still
    scores 1.0 against itself, so no build, stale or disagreeing is RED.
    """
    name = "trusted-solution"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    evidence: dict[str, str] = {}
    for pop in PopulationName:
        if pop.value not in gold.stage1:
            return _fail(name, f"no frozen stage-1 gold for population {pop.value!r}")
        actual_marts: dict[str, list[Row]] = {}
        for mart in task.marts:
            parsed = _gold_mart_rows(gold, pop.value, mart.name)
            if parsed is None:
                return _fail(
                    name,
                    f"no frozen stage-2 gold for mart {mart.name!r} on "
                    f"population {pop.value!r}",
                )
            actual_marts[mart.name] = parsed[1]
        result = upstream_eval.evaluate(
            task, gold, pop, gold.stage1[pop.value], actual_marts
        )
        evidence[f"reward:{pop.value}"] = f"{result.reward:.6f}"
        if result.reward != 1.0:
            return _fail(
                name,
                f"trusted reference gold does not score 1.0 on {pop.value!r} "
                f"(got {result.reward}); comparator and gold disagree",
                evidence,
            )
    # Gold-vs-gold above is a comparator sanity check, NOT certification.
    record, problem = _dual_build_record(task, workspace)
    if record is None:
        return _fail(
            name,
            "gold cannot self-certify: " + str(problem),
            evidence,
        )
    build_problems = _dual_build_problems(record) + _transform_reconstruction_problems(
        task, record
    )
    if build_problems:
        return _fail(
            name,
            "gold cannot self-certify: " + "; ".join(build_problems),
            evidence,
        )
    evidence["independent_build"] = str(record.get("status"))
    return _ok(
        name,
        "gold is comparator-consistent on all five populations and certified "
        "by the recorded independent build",
        evidence,
    )


def _gate_dual_build_agreement(task: TaskIR, workspace: Path) -> GateResult:
    """The cross-family independent build agrees 1.0 on all five populations.

    Missing, stale, malformed, incomplete or disagreeing evidence is RED: an
    unperformed independent build proves nothing.
    """
    name = "dual-build-agreement"
    record, problem = _dual_build_record(task, workspace)
    if record is None:
        _, note_evidence = _with_producer_note(task, workspace, DUAL_BUILD_NOTES_REL, "")
        return _fail(name, str(problem), note_evidence)
    evidence: dict[str, str] = {
        "status": str(record.get("status")),
        "samples": str(len(record.get("samples") or [])),
    }
    agreement = record.get("agreement")
    if isinstance(agreement, dict):
        for pop in PopulationName:
            value = agreement.get(pop.value)
            if value is None:
                continue
            try:
                evidence[f"agreement:{pop.value}"] = f"{float(value):.6f}"
            except (TypeError, ValueError):
                evidence[f"agreement:{pop.value}"] = repr(value)
    problems = _dual_build_problems(record) + _transform_reconstruction_problems(
        task, record
    )
    if problems:
        return _fail(
            name,
            "dual-build agreement not established: " + "; ".join(problems),
            evidence,
        )
    return _ok(
        name,
        "cross-family independent build scores 1.0 against the frozen gold "
        "on all five populations",
        evidence,
    )


#: Binding the determinism writer stamps: without it an edited task could quote
#: a determinism run of another identity.
DETERMINISM_CONTENT_HASH_KEY = "task_content_hash"


def _gate_determinism(
    task: TaskIR, workspace: Path, gold: "GoldBundle"
) -> GateResult:
    """Repeated clean rebuilds were byte-identical AND they built THIS gold.

    Three fail-closed bindings: this task_id, the CURRENT content hash, and
    split digests equal to the frozen gold's — recorded runs that are not the
    answer key that ships are not determinism evidence.
    """
    name = "determinism"
    path = task_dir(workspace, task.task_id) / DETERMINISM_EVIDENCE_REL
    if not path.is_file():
        return _fail(name, f"no determinism evidence recorded at {path}")
    recorded = GateResult.model_validate_json(path.read_text(encoding="utf-8"))
    evidence = {
        # Workspace-RELATIVE so report copies stay byte-identical across
        # rebuilds in different workspace locations (determinism invariant).
        "evidence_path": path.relative_to(workspace).as_posix(),
        "recorded_gate": recorded.gate,
    }
    evidence.update({f"recorded:{k}": v for k, v in recorded.evidence.items()})
    if recorded.gate != name:
        return _fail(
            name,
            f"recorded evidence is a {recorded.gate!r} gate result, not "
            f"{name!r} (fail closed)",
            evidence,
        )
    recorded_task = recorded.evidence.get("task_id")
    if recorded_task is None:
        return _fail(
            name,
            "determinism evidence records no task_id binding — unbound "
            "evidence proves nothing (fail closed)",
            evidence,
        )
    if recorded_task != task.task_id:
        return _fail(
            name,
            f"determinism evidence is for task {recorded_task!r}, "
            f"not {task.task_id!r}",
            evidence,
        )
    recorded_hash = recorded.evidence.get(DETERMINISM_CONTENT_HASH_KEY)
    current = task.content_hash()
    if recorded_hash is None:
        return _fail(
            name,
            "determinism evidence records no task_content_hash binding — an "
            "edited task could quote a run of another identity (re-run "
            "reference-run; fail closed)",
            evidence,
        )
    if recorded_hash != current:
        return _fail(
            name,
            "determinism evidence is STALE: recorded for content hash "
            f"{str(recorded_hash)[:12]}, task is now {current[:12]} — re-run "
            "reference-run",
            evidence,
        )
    if not recorded.passed:
        return _fail(
            name, f"recorded determinism check failed: {recorded.details}", evidence
        )
    recorded_pop = recorded.evidence.get("population")
    if recorded_pop is not None and recorded_pop != PopulationName.PRIMARY.value:
        return _fail(
            name,
            f"determinism evidence was recorded on population {recorded_pop!r}, "
            "not 'primary' — the answer key's determinism witness is the "
            "primary run",
            evidence,
        )
    # The recorded runs must be THE frozen gold, not merely self-consistent.
    from elt_taskgen.reference.gold import gold_digests  # gold.py imports gates

    try:
        gd = gold_digests(gold, PopulationName.PRIMARY)
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        return _fail(
            name,
            f"cannot recompute the frozen gold's digests to check the recorded "
            f"determinism runs against ({type(exc).__name__}: {exc}); fail closed",
            evidence,
        )
    evidence["gold_stage1_digest"] = str(gd.stage1)[:16]
    evidence["gold_stage2_digest"] = str(gd.stage2)[:16]
    got1 = recorded.evidence.get(DETERMINISM_STAGE1_DIGEST_KEY)
    got2 = recorded.evidence.get(DETERMINISM_STAGE2_DIGEST_KEY)
    if got1 != gd.stage1 or got2 != gd.stage2:
        return _fail(
            name,
            "determinism runs produced "
            f"{DETERMINISM_STAGE1_DIGEST_KEY}={str(got1)[:12]}/"
            f"{DETERMINISM_STAGE2_DIGEST_KEY}={str(got2)[:12]}, frozen gold is "
            f"{DETERMINISM_STAGE1_DIGEST_KEY}={str(gd.stage1)[:12]}/"
            f"{DETERMINISM_STAGE2_DIGEST_KEY}={str(gd.stage2)[:12]} — the "
            "recorded runs are not the answer key that ships (re-run "
            "reference-run so determinism and the freeze describe one build)",
            evidence,
        )
    return _ok(
        name,
        "repeated clean rebuilds recorded byte-identical, bound to this content "
        "hash, and their split digests are the frozen gold's",
        evidence,
    )


def _degenerate_mart_strategies(
    task: TaskIR, gold: "GoldBundle", pop: PopulationName
) -> tuple[dict[str, dict[str, list[Row]]] | None, str | None]:
    """(strategies, None) — the three lazy stage-2 submissions built from one
    population's frozen gold, or (None, why). Shared by the parent and TRANSFORM
    batteries so the two cannot drift into different probes under one name.
    """
    parsed: dict[str, tuple[tuple[str, ...], list[Row]]] = {}
    for mart in task.marts:
        got = _gold_mart_rows(gold, pop.value, mart.name)
        if got is None:
            return None, (
                f"no frozen {pop.value} gold for mart {mart.name!r} (fail closed)"
            )
        parsed[mart.name] = got

    def keys_only_rows(mart_name: str) -> list[Row]:
        cols, rows = parsed[mart_name]
        key_lower = {k.lower() for k in task.mart(mart_name).key_columns}
        return [
            {c: (r.get(c) if c.lower() in key_lower else None) for c in cols}
            for r in rows
        ]

    def constant_rows(mart_name: str) -> list[Row]:
        _, rows = parsed[mart_name]
        return [dict(rows[0]) for _ in rows] if rows else []

    return (
        {
            "no_op": {m.name: [] for m in task.marts},
            "keys_only": {m.name: keys_only_rows(m.name) for m in task.marts},
            "constants": {m.name: constant_rows(m.name) for m in task.marts},
        },
        None,
    )


def _gate_degenerate_zero(task: TaskIR, gold: "GoldBundle") -> GateResult:
    name = "degenerate-zero"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    pop = PopulationName.PRIMARY
    stage1 = gold.stage1.get(pop.value)
    if stage1 is None:
        return _fail(name, "no frozen stage-1 gold for the primary population")

    strategies, why = _degenerate_mart_strategies(task, gold, pop)
    if strategies is None:
        return _fail(name, str(why))

    evidence: dict[str, str] = {}
    leaks: list[str] = []
    for strategy, marts in strategies.items():
        # Degenerate stage 2 with a CORRECT stage 1: loading raw sources is
        # cheap, so the floor must be pinned at 0 by the transform comparison.
        result = upstream_eval.evaluate(task, gold, pop, stage1, marts)
        evidence[strategy] = f"reward={result.reward:.6f}"
        if result.reward != 0.0:
            leaks.append(f"{strategy} scored {result.reward}")
    if leaks:
        return _fail(
            name,
            "degenerate submissions earn non-zero reward (reward floor is not "
            "pinned at 0): " + "; ".join(leaks),
            evidence,
        )
    return _ok(name, "no-op, keys-only, and constant submissions all score 0", evidence)


def _normalize_attack_rewards(
    attack_rewards: dict[str, dict[PopulationName, float]],
) -> dict[str, dict[PopulationName, float]]:
    out: dict[str, dict[PopulationName, float]] = {}
    for case_name, per_pop in attack_rewards.items():
        norm: dict[PopulationName, float] = {}
        for pop, reward in per_pop.items():
            key = pop if isinstance(pop, PopulationName) else PopulationName(str(pop))
            norm[key] = float(reward)
        out[case_name] = norm
    return out


def _gate_required_mutants(
    task: TaskIR, attack_rewards: dict[str, dict[PopulationName, float]]
) -> GateResult:
    name = "required-mutants"
    required: list[AttackCase] = [a for a in task.attack_cases if a.required]
    if not required:
        return _fail(
            name,
            "task declares no required attack cases — no evidence that wrong "
            "logic loses reward (fail closed)",
        )
    rewards = _normalize_attack_rewards(attack_rewards or {})
    evidence: dict[str, str] = {}
    leaks: list[str] = []
    for case in required:
        measured = rewards.get(case.name)
        if measured is None:
            leaks.append(f"{case.name}: no recorded rewards")
            continue
        evidence[case.name] = ",".join(
            f"{p.value}={r:.6f}" for p, r in sorted(measured.items(), key=lambda kv: kv[0].value)
        )
        for pop, expect_full in sorted(
            case.expected_pass.items(), key=lambda kv: kv[0].value
        ):
            got = measured.get(pop)
            if got is None:
                leaks.append(f"{case.name}: no reward recorded on {pop.value}")
            elif expect_full and got != 1.0:
                leaks.append(
                    f"{case.name}: expected FULL reward on {pop.value}, got {got}"
                )
            elif not expect_full and got >= 1.0:
                leaks.append(
                    f"{case.name}: LEAK — must lose reward on {pop.value}, got {got}"
                )
    if leaks:
        return _fail(
            name,
            "required mutant matrix not reproduced: " + "; ".join(leaks),
            evidence,
        )
    return _ok(
        name,
        "every required mutant wins and loses exactly where its expected_pass "
        "map says (0 leaks)",
        evidence,
    )


def _recorded_attack_kind(
    workspace: Path, task: TaskIR, case_name: str
) -> tuple[AttackKind | None, str | None]:
    """Return a recorded attack kind or a fail-closed reason.

    Read both measured and inapplicable records. Missing, unreadable, stale, or unknown
    evidence never permits guessing the probe kind.
    """
    case_dir = task_dir(workspace, task.task_id) / "attacks" / case_name
    rewards_path = case_dir / "rewards.json"
    inapplicable_path = case_dir / "inapplicable.json"
    path = rewards_path if rewards_path.is_file() else inapplicable_path
    if not path.is_file():
        return None, f"no recorded attack artifact at {rewards_path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable attack record {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"attack record {path} is not a JSON object"
    recorded_hash = data.get("task_content_hash")
    if recorded_hash != task.content_hash():
        return None, (
            f"attack record is STALE: measured at content hash {recorded_hash!r}, "
            f"task is now {task.content_hash()!r}"
        )
    try:
        return AttackKind(data.get("kind")), None
    except ValueError:
        return None, f"attack record declares unknown kind {data.get('kind')!r}"


def _recorded_attack_errors(
    workspace: Path, task: TaskIR, case_name: str
) -> tuple[dict[str, str] | None, str | None]:
    """(errors map, None) from the recorded attack artifact, or (None, why-not).

    Maps ``"<pop>/__load__"`` / ``"<pop>/<mart>"`` to the crash that replaced a
    measurement. A record without the ``errors`` key is not this runner's
    record and fails closed.
    """
    path = task_dir(workspace, task.task_id) / "attacks" / case_name / "rewards.json"
    if not path.is_file():
        return None, f"no recorded attack artifact at {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable attack record {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"attack record {path} is not a JSON object"
    recorded_hash = data.get("task_content_hash")
    if recorded_hash != task.content_hash():
        return None, (
            f"attack record is STALE: measured at content hash {recorded_hash!r}, "
            f"task is now {task.content_hash()!r}"
        )
    if "errors" not in data:
        return None, "record has no errors field"
    errors = data.get("errors")
    if not isinstance(errors, dict):
        return None, "record 'errors' is not an object"
    return {str(k): str(v) for k, v in errors.items()}, None


def _recorded_inapplicable_reason(
    workspace: Path, task: TaskIR, case_name: str
) -> str | None:
    """The recorded no-surface reason for a probe, or None.

    Counts only at the CURRENT content hash and only for a NON-required case:
    a required case with a missing surface fails at run time, never here.
    """
    path = task_dir(workspace, task.task_id) / "attacks" / case_name / "inapplicable.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("task_content_hash") != task.content_hash():
        return None
    required = {c.name: c.required for c in task.attack_cases}
    if required.get(case_name, False):
        return None
    reason = data.get("inapplicable")
    return str(reason) if reason else None


def _recorded_shortcut_cases(
    workspace: Path, task: TaskIR, kinds: frozenset[AttackKind] | None = None
) -> set[str]:
    """Names of shortcut-kind cases RECORDED as compiled at the current hash.

    Ground truth for "which probes exist" is the artifact tree, NOT the measured
    payload: a probe whose reward entry never reached the battery would
    otherwise vanish silently instead of failing."""
    wanted = SHORTCUT_KINDS if kinds is None else kinds
    root = task_dir(workspace, task.task_id) / "attacks"
    if not root.is_dir():
        return set()
    current = task.content_hash()
    found: set[str] = set()
    for case_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        path = case_dir / "rewards.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("task_content_hash") != current:
            continue
        try:
            kind = AttackKind(data.get("kind"))
        except ValueError:
            continue
        if kind in wanted:
            found.add(case_dir.name)
    return found


def _gate_shortcut_probes(
    task: TaskIR,
    workspace: Path,
    attack_rewards: dict[str, dict[PopulationName, float]],
    *,
    kinds: frozenset[AttackKind] | None = None,
    excluded: Mapping[AttackKind, str] | None = None,
    variant_note: str = "",
) -> GateResult:
    """Require every compiled shortcut probe to lose reward on a graded population.

    Cross-check the attack tree with measured payloads; a compiled but absent probe
    fails because its evidence was not measured.
    """
    name = "shortcut-probes"
    wanted = SHORTCUT_KINDS if kinds is None else kinds
    rewards = _normalize_attack_rewards(attack_rewards or {})
    standing = {c.name: c.kind for c in task.attack_cases}

    problems: list[str] = []
    probes: dict[str, AttackKind] = {
        case_name: kind
        for case_name, kind in standing.items()
        if kind in wanted
    }
    # Compiled (finding-driven) cases are not on the TaskIR; their kind comes
    # from the recorded attack artifact. Unknown kind => fail, never guess.
    for case_name in sorted(rewards):
        if case_name in standing:
            continue
        kind, err = _recorded_attack_kind(workspace, task, case_name)
        if kind is None:
            problems.append(f"{case_name}: cannot establish probe kind ({err})")
        elif kind in wanted:
            probes[case_name] = kind
    # A probe compiled at this identity but missing from the payload must fail:
    # dropping the key is how an unmeasured probe would otherwise disappear.
    for case_name in sorted(_recorded_shortcut_cases(workspace, task, wanted)):
        if case_name in standing or case_name in rewards:
            continue
        problems.append(
            f"{case_name}: compiled shortcut probe has NO entry in the measured "
            "attack rewards — evidence missing, not absent"
        )

    # Kinds outside this variant's shortcut set are RECORDED with their reason:
    # an exclusion nobody can see is indistinguishable from a hole.
    exclusion_evidence: dict[str, str] = {
        f"excluded:{kind.value}": reason for kind, reason in (excluded or {}).items()
    }
    if variant_note:
        exclusion_evidence["scope"] = variant_note

    if not probes and not problems:
        return _fail(
            name,
            "no shortcut-kind probe was compiled at all — no evidence that "
            "scoring-without-solving loses reward (fail closed)"
            + (f" [{variant_note}]" if variant_note else ""),
            exclusion_evidence,
        )

    evidence: dict[str, str] = dict(exclusion_evidence)
    measured_count = 0
    for case_name, kind in sorted(probes.items()):
        measured = rewards.get(case_name)
        if not measured:
            inapplicable = _recorded_inapplicable_reason(workspace, task, case_name)
            if inapplicable is not None:
                # Surface missing from this task's DATA with a recorded reason:
                # an honest absence, surfaced as evidence rather than dropped.
                evidence[f"inapplicable:{case_name}"] = inapplicable
                continue
            problems.append(
                f"{case_name} ({kind.value}): no measured rewards recorded"
            )
            continue
        measured_count += 1
        evidence[case_name] = f"kind={kind.value};" + ",".join(
            f"{p.value}={r:.6f}"
            for p, r in sorted(measured.items(), key=lambda kv: kv[0].value)
        )
        graded = {p: r for p, r in measured.items() if p in GRADED_POPULATIONS}
        if not graded:
            problems.append(
                f"{case_name} ({kind.value}): no graded-population rewards measured"
            )
        elif min(graded.values()) >= 1.0:
            problems.append(
                f"{case_name} ({kind.value}): keeps full reward on every graded "
                "population — a working shortcut"
            )
    if problems:
        return _fail(
            name,
            "shortcut probes leak reward or lack evidence: " + "; ".join(problems),
            evidence,
        )
    if measured_count == 0:
        # All-inapplicable is still ZERO measured probes, and a gate that passes
        # on none proves nothing (fail closed).
        return _fail(
            name,
            f"every shortcut-kind probe ({len(probes)}) is recorded "
            "inapplicable — zero measured probes is no evidence that "
            "scoring-without-solving loses reward (fail closed)"
            + (f" [{variant_note}]" if variant_note else ""),
            evidence,
        )
    return _ok(
        name,
        f"{measured_count} measured shortcut-kind probe(s) score < 1.0 on at "
        f"least one graded population ({len(probes) - measured_count} recorded "
        "inapplicable)" + (f" [{variant_note}]" if variant_note else ""),
        evidence,
    )


def _row_multiset(path: Path) -> frozenset[tuple[str, int]] | None:
    """Canonical-JSON row multiset of one rows/<table>.jsonl, or None if absent.

    Multiset, not list: invariant under reordering, sensitive to every value and
    every duplicate — the discrimination the pair classifier below needs.
    """
    if not path.is_file():
        return None
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            key = canonical_json(json.loads(line))
        except json.JSONDecodeError:
            return None
        counts[key] = counts.get(key, 0) + 1
    return frozenset(counts.items())


#: Population relation stamped into reward.json / release_manifest.json for a
#: rearranged pair, so trainers can down-weight it instead of double-counting.
POPULATION_RELATION_REARRANGEMENT = "rearrangement_of:primary"


def _pair_is_rearrangement(
    task: TaskIR, workspace: Path, a: str, b: str
) -> bool | None:
    """Is population `b` a pure REARRANGEMENT of `a` — same rows, new order?

    Read off the materialized rows BYTES, never off prose or an adapter flag: a
    task cannot buy this classification by writing it into its conditions. None
    when a rows artifact is missing/unreadable; the caller fails closed.
    """
    tdir = task_dir(Path(workspace), task.task_id)
    for table in task.tables:
        ma = _row_multiset(tdir / "populations" / a / "rows" / f"{table.name}.jsonl")
        mb = _row_multiset(tdir / "populations" / b / "rows" / f"{table.name}.jsonl")
        if ma is None or mb is None:
            return None
        if ma != mb:
            return False
    return True


def pair_is_rearrangement(task: TaskIR, workspace: Path, a: str, b: str) -> bool:
    """Public strict-bool form of `_pair_is_rearrangement`; the export side
    stamps `population_relations` through it.

    Unreadable rows answer False — an unprovable relation is no relation.
    Consumers that must fail closed on that case use the tri-state private form.
    """
    return _pair_is_rearrangement(task, workspace, a, b) is True


def needs_perturbation_probe(task: TaskIR, workspace: Path) -> bool:
    """Does this task's data-sensitivity gate require the scratch-layer probe?

    True when either recorded pair is a pure REARRANGEMENT, so no shipped
    population perturbs a value. An unclassifiable pair also answers True: the
    gate fails either way, and the probe produces the evidence why.
    """
    workspace = Path(workspace)
    for a, b in (
        (PopulationName.PRIMARY.value, PopulationName.RESAMPLED.value),
        (PopulationName.PRIMARY.value, PopulationName.COUNTERFACTUAL.value),
    ):
        if _pair_is_rearrangement(task, workspace, a, b) is not False:
            return True
    return False


def _perturbation_probe_problems(
    task: TaskIR, workspace: Path
) -> tuple[list[str], dict[str, str]]:
    """Problems with the recorded perturbation probe; absence is a failure.

    The probe is a value BIJECTION over a scratch copy of `primary`, recorded
    with the pinned populations/ digest before and after so "the pinned snapshot
    was not written" is evidence rather than a promise.
    """
    path = task_dir(workspace, task.task_id) / PERTURBATION_PROBE_EVIDENCE_REL
    if not path.is_file():
        return (
            [
                "no value-perturbation probe recorded at "
                f"{PERTURBATION_PROBE_EVIDENCE_REL}: with a rearrangement-only "
                "resampled population NOTHING else proves the reward reads the "
                "source VALUES, and a real-data task with an input-independent "
                "reward is exactly as worthless as a synthetic one (fail closed "
                "— the gate is never skipped)"
            ],
            {},
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return ([f"perturbation probe evidence is not valid JSON ({exc})"], {})
    if not isinstance(record, dict):
        return (["perturbation probe evidence is not a JSON object"], {})

    evidence: dict[str, str] = {
        "probe_path": path.relative_to(workspace).as_posix(),
        "probe_kind": str(record.get("kind", "")),
        "probe_passed": str(bool(record.get("passed"))).lower(),
    }
    perturbation = record.get("perturbation")
    if isinstance(perturbation, dict):
        evidence["probe_perturbed_cells"] = str(perturbation.get("perturbed_cells"))
    before = record.get("pinned_layer_sha256_before")
    after = record.get("pinned_layer_sha256_after")
    if isinstance(before, str):
        evidence["pinned_layer_before"] = before[:16]
    if isinstance(after, str):
        evidence["pinned_layer_after"] = after[:16]
    for key, value in (record.get("evidence") or {}).items():
        evidence[f"probe:{key}"] = str(value)

    problems: list[str] = []
    if record.get("kind") != PERTURBATION_PROBE_KIND:
        problems.append(
            f"perturbation probe is kind {record.get('kind')!r}, not "
            f"{PERTURBATION_PROBE_KIND!r} (fail closed)"
        )
    if record.get("task_id") != task.task_id:
        problems.append(
            f"perturbation probe is for task {record.get('task_id')!r}, not "
            f"{task.task_id!r}"
        )
    current = task.content_hash()
    if record.get("task_content_hash") != current:
        problems.append(
            "perturbation probe is STALE: recorded for content hash "
            f"{record.get('task_content_hash')!r}, task is now {current!r} — re-run"
        )
    if not isinstance(before, str) or not isinstance(after, str) or not before:
        problems.append(
            # No "licens"/"contamination"/"plagiar" wording in runtime details:
            # repair.route_for_failure reads those as FATAL and would reject.
            "perturbation probe records no pinned-layer digest pair — without "
            "it there is no proof the vendored snapshot was left untouched"
        )
    elif before != after:
        problems.append(
            "perturbation probe MOVED the pinned population layer "
            f"({before[:16]} -> {after[:16]}): the derived scratch layer must "
            "never write into the pinned artifact"
        )
    if not record.get("passed"):
        problems.append(
            f"value-perturbation probe did not pass: {record.get('details', '')}"
        )
    return problems, evidence


#: Wording of the reward-equivalence failure, deliberately free of
#: "licens"/"contamination"/"plagiar", which repair routing reads as FATAL.
_REWARD_EQUIVALENT_NOTE = (
    "byte-different but REWARD-EQUIVALENT under compare_mart (rtol 1e-2 / "
    "case / whitespace / NA-token)"
)


def _gold_reward_equivalent(
    csv_a: str, csv_b: str, mart: MartSpec
) -> bool | None:
    """Would a submission that memorized one population's gold score the OTHER?

    Decided by THE reward's comparator in BOTH directions (its tolerance is
    measured against the SUBMITTED side), never by bytes. None when a side is
    not parseable canonical CSV; the caller fails closed.
    """
    try:
        _, rows_a = upstream_eval.parse_canonical_csv(csv_a)
        _, rows_b = upstream_eval.parse_canonical_csv(csv_b)
    except ValueError:
        return None
    return bool(
        upstream_eval.compare_mart(csv_b, rows_a, mart)
        or upstream_eval.compare_mart(csv_a, rows_b, mart)
    )


def _gate_data_sensitivity(
    task: TaskIR, workspace: Path, gold: "GoldBundle"
) -> GateResult:
    """Verify that reward changes with source data for each perturbation.

    Classify pairs from materialized rows. Value or row changes must produce
    reward-distinct gold, pure rearrangements must preserve gold bytes, and
    rearrangement-only pairs require a recorded bijection probe.
    """
    name = "data-sensitivity"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    stage2 = gold.stage2_csv
    needed = (
        PopulationName.PRIMARY.value,
        PopulationName.RESAMPLED.value,
        PopulationName.COUNTERFACTUAL.value,
    )
    for pop in needed:
        if pop not in stage2:
            return _fail(name, f"no frozen stage-2 gold for population {pop!r}")

    pairs = (
        (PopulationName.PRIMARY.value, PopulationName.RESAMPLED.value),
        (PopulationName.PRIMARY.value, PopulationName.COUNTERFACTUAL.value),
    )
    evidence: dict[str, str] = {}
    kinds: dict[tuple[str, str], bool] = {}
    for a, b in pairs:
        rearranged = _pair_is_rearrangement(task, workspace, a, b)
        if rearranged is None:
            return _fail(
                name,
                f"cannot classify the {a}-vs-{b} perturbation: a materialized "
                "rows/<table>.jsonl artifact is missing or unreadable, so there "
                "is no evidence of WHAT was perturbed (fail closed)",
                evidence,
            )
        kinds[(a, b)] = rearranged
        evidence[f"perturbation:{a}-vs-{b}"] = (
            "rearrangement (identical row multisets)"
            if rearranged
            else "values/rows moved"
        )

    failures: list[str] = []
    for mart in task.marts:
        csvs = {}
        for pop in needed:
            text = stage2[pop].get(mart.name)
            if text is None:
                return _fail(
                    name, f"no frozen gold for mart {mart.name!r} on {pop!r}"
                )
            csvs[pop] = text
        for a, b in pairs:
            same = csvs[a] == csvs[b]
            if kinds[(a, b)]:
                # Pure permutation: the gold MUST be byte-invariant.
                evidence[f"{mart.name}:{a}-vs-{b}"] = "identical" if same else "differs"
                if not same:
                    failures.append(
                        f"mart {mart.name!r}: {a} and {b} hold identical row "
                        f"multisets in a different order, yet their gold "
                        "DIFFERS — the transform is order-dependent"
                    )
                continue
            # Values/rows moved: the gold must be reward-DISTINCT, measured
            # through THE reward's comparator, never through bytes.
            equiv = _gold_reward_equivalent(csvs[a], csvs[b], mart)
            if equiv is None:
                return _fail(
                    name,
                    f"frozen gold for mart {mart.name!r} on {a}/{b} is not "
                    "parseable canonical CSV (fail closed)",
                    evidence,
                )
            evidence[f"{mart.name}:{a}-vs-{b}"] = (
                "identical"
                if same
                else "differs (reward-equivalent)"
                if equiv
                else "differs (reward-distinct)"
            )
            if equiv:
                failures.append(
                    f"mart {mart.name!r}: {a} and {b} gold are "
                    f"{'identical' if same else _REWARD_EQUIVALENT_NOTE} — a "
                    f"submission that memorizes {a} scores 1.0 on {b}"
                )
    if failures:
        return _fail(
            name,
            "the frozen gold does not track the recorded perturbations: "
            + "; ".join(failures),
            evidence,
        )

    rearranged_pairs = [f"{a}-vs-{b}" for (a, b), r in kinds.items() if r]
    if rearranged_pairs:
        probe_problems, probe_evidence = _perturbation_probe_problems(task, workspace)
        evidence.update(probe_evidence)
        if probe_problems:
            return _fail(
                name,
                f"pair(s) {rearranged_pairs} perturb no VALUE, so the "
                "value-sensitivity obligation falls to the scratch-layer "
                "bijection probe, which did not discharge it: "
                + "; ".join(probe_problems),
                evidence,
            )
        return _ok(
            name,
            "every value/row perturbation moves the gold, every rearrangement "
            "leaves it invariant, and the scratch-layer value bijection "
            f"(pair(s) {rearranged_pairs} perturb order only) moves every "
            "mart's gold",
            evidence,
        )
    return _ok(
        name,
        "perturbed data conditions change every mart's gold (reward-distinct "
        "under compare_mart in both directions)",
        evidence,
    )


def _gate_info_content(task: TaskIR, gold: "GoldBundle") -> GateResult:
    name = "info-content"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    pop = PopulationName.PRIMARY.value
    evidence: dict[str, str] = {}
    trivial: list[str] = []
    for mart in task.marts:
        parsed = _gold_mart_rows(gold, pop, mart.name)
        if parsed is None:
            return _fail(name, f"no frozen primary gold for mart {mart.name!r}")
        cols, rows = parsed
        if len(rows) < 2:
            trivial.append(f"mart {mart.name!r}: only {len(rows)} gold row(s)")
            continue
        key_lower = {k.lower() for k in mart.key_columns}
        non_key = [c for c in cols if c.lower() not in key_lower]
        if not non_key:
            trivial.append(f"mart {mart.name!r}: has only key columns")
            continue
        varying = 0
        for c in non_key:
            distinct = {r.get(c) for r in rows if r.get(c) is not None}
            if len(distinct) >= 2:
                varying += 1
        evidence[mart.name] = (
            f"{varying}/{len(non_key)} non-key columns vary over {len(rows)} rows"
        )
        if varying == 0:
            trivial.append(
                f"mart {mart.name!r}: every non-key column is constant — the "
                "right constant scores it for free"
            )
    if trivial:
        return _fail(
            name, "mart outputs carry no information: " + "; ".join(trivial), evidence
        )
    return _ok(name, "every mart has at least one varying non-key column", evidence)


def _gate_populations_load(
    task: TaskIR, workspace: Path, gold: "GoldBundle"
) -> GateResult:
    name = "populations-load"
    if not task.has_all_populations():
        have = sorted(p.name.value for p in task.populations)
        return _fail(name, f"task does not declare all five populations (has {have})")
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    tdir = task_dir(workspace, task.task_id)
    evidence: dict[str, str] = {}
    for pop in PopulationName:
        counts = gold.stage1.get(pop.value)
        if counts is None:
            return _fail(name, f"no frozen stage-1 counts for population {pop.value!r}")
        pop_dir = tdir / "populations" / pop.value
        rendered = pop_dir / "rendered"
        if not rendered.is_dir() or not any(rendered.iterdir()):
            return _fail(
                name,
                f"population {pop.value!r}: rendered source environments missing "
                f"or empty at {rendered}",
            )
        total = 0
        for table in task.tables:
            rows_file = pop_dir / "rows" / f"{table.name}.jsonl"
            if not rows_file.is_file():
                return _fail(
                    name,
                    f"population {pop.value!r}: generated rows missing for table "
                    f"{table.name!r} at {rows_file}",
                )
            n = 0
            with rows_file.open(encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, start=1):
                    if not line.strip():
                        continue
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as exc:
                        return _fail(
                            name,
                            f"population {pop.value!r}: {rows_file} line {line_no} "
                            f"is not valid JSON ({exc})",
                        )
                    n += 1
            want = counts.get(table.name)
            if want is None:
                return _fail(
                    name,
                    f"population {pop.value!r}: no frozen count for table "
                    f"{table.name!r}",
                )
            if n != want:
                return _fail(
                    name,
                    f"population {pop.value!r}: table {table.name!r} has {n} "
                    f"generated rows but frozen stage-1 gold expects {want}",
                )
            total += n
        evidence[pop.value] = f"{total} rows across {len(task.tables)} tables"
    return _ok(name, "all five populations materialized and smoke-check clean", evidence)


#: coverage.level meaning the index is a real firewall; kept literal so the gate
#: need not import the scanner.
CONTAMINATION_COVERAGE_ARMED = "armed"


def _gate_contamination_clean(task: TaskIR, workspace: Path) -> GateResult:
    """The post-generation scan is clean AND was measured by a real firewall.

    "0 collisions" is only as strong as the index behind it: the coverage must
    be ARMED and carry TYPE-BLIND `shape:` fingerprints, because the TEXT-typed
    anchors never collide with typed schema hashes alone.
    """
    # OBSERVE/OFF: the scan still runs and its evidence is still written;
    # this gate simply stops being an admission barrier.
    if not enforcing():
        return GateResult(
            gate="contamination-clean",
            passed=True,
            details="contamination enforcement disabled (observe/off)",
            evidence={"enforcement": enforcement().value},
        )
    name = "contamination-clean"
    path = task_dir(workspace, task.task_id) / CONTAMINATION_POST_EVIDENCE_REL
    if not path.is_file():
        return _fail(name, f"no post-generation contamination scan recorded at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return _fail(name, "contamination evidence is not a JSON object")
    missing = [k for k in ("task_id", "task_content_hash", "collisions") if k not in data]
    if missing:
        return _fail(name, f"contamination evidence missing keys: {missing}")
    if data["task_id"] != task.task_id:
        return _fail(
            name,
            f"contamination scan is for task {data['task_id']!r}, not "
            f"{task.task_id!r}",
        )
    current = task.content_hash()
    if data["task_content_hash"] != current:
        return _fail(
            name,
            "contamination scan is STALE: recorded for content hash "
            f"{data['task_content_hash']!r}, task is now {current!r} — re-scan",
        )
    collisions = data["collisions"]
    if not isinstance(collisions, list):
        return _fail(name, "contamination evidence 'collisions' is not a list")
    fatal = [c for c in collisions if isinstance(c, dict) and c.get("fatal")]
    coverage = data.get("coverage")
    coverage_level = str(coverage.get("level", "")) if isinstance(coverage, dict) else ""
    shape_raw = coverage.get("shape_fingerprints", 0) if isinstance(coverage, dict) else 0
    shape_fps = shape_raw if isinstance(shape_raw, int) and not isinstance(shape_raw, bool) else 0
    evidence = {
        "collisions": str(len(collisions)),
        "fatal": str(len(fatal)),
        # Workspace-RELATIVE so report copies stay byte-identical across
        # rebuilds in different workspace locations (determinism invariant).
        "evidence_path": path.relative_to(workspace).as_posix(),
        "coverage_level": coverage_level or "(unrecorded)",
        "shape_fingerprints": str(shape_fps),
    }
    if fatal:
        kinds = ", ".join(
            f"{c.get('kind', '?')} vs {c.get('against', '?')}" for c in fatal
        )
        return _fail(name, f"fatal contamination collisions recorded: {kinds}", evidence)
    if not isinstance(coverage, dict):
        return _fail(
            name,
            "contamination scan records no index coverage — '0 collisions' "
            "against an unknown index is not evidence (re-run measure-target "
            "and contamination-post)",
            evidence,
        )
    if shape_fps <= 0:
        return _fail(
            name,
            "contamination scan predates type-blind shape fingerprints (index "
            "had no shape: coverage) — the TEXT-typed anchors never collide "
            "with typed schema hashes, so this scan could not have seen a "
            "retyped copy of a benchmark schema; re-run measure-target and "
            "contamination-post",
            evidence,
        )
    if coverage_level != CONTAMINATION_COVERAGE_ARMED:
        return _fail(
            name,
            f"contamination scan was measured at coverage level "
            f"{coverage_level or '(unrecorded)'!r}, not "
            f"{CONTAMINATION_COVERAGE_ARMED!r} — a clean result against an "
            "unarmed/name-only index is not a firewall verdict; arm the index "
            "(measure-target) and re-run contamination-post",
            evidence,
        )
    return _ok(
        name,
        f"post-generation scan clean at current content hash "
        f"({len(collisions)} non-fatal collision(s)) against an ARMED index "
        f"with {shape_fps} type-blind shape fingerprint(s)",
        evidence,
    )


# Data-integrity gates (referential-integrity, declared-scale)

def _table_rows(path: Path) -> list[Row] | None:
    """Parsed rows of one populations/<pop>/rows/<table>.jsonl, or None on a
    missing, unreadable or non-object-per-line artifact (caller fails closed).
    """
    if not path.is_file():
        return None
    out: list[Row] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    return None
                out.append(row)
    except (OSError, json.JSONDecodeError):
        return None
    return out


def _gate_referential_integrity(task: TaskIR, workspace: Path) -> GateResult:
    """Verify foreign keys against recorded materialized rows.

    Null links are allowed only for optional relationships, and dangling keys only when
    explicitly declared. Missing or unreadable row artifacts fail rather than skip.
    """
    name = "referential-integrity"
    evidence: dict[str, str] = {}
    if not task.relationships:
        evidence["relationships"] = "0"
        return _ok(
            name,
            "task declares no relationships — there is no referential claim "
            "to check (recorded, not waived)",
            evidence,
        )
    tdir = task_dir(workspace, task.task_id)
    failures: list[str] = []
    for pop in PopulationName:
        spec = task.population(pop)
        # Read through the SAME function source_data.py uses to mint dangling
        # anchors: a substring check would fail open on a NEGATED sentence.
        dangling_declared = declares_dangling(spec.conditions)
        cache: dict[str, list[Row] | None] = {}

        def rows_of(table: str) -> list[Row] | None:
            if table not in cache:
                cache[table] = _table_rows(
                    tdir / "populations" / pop.value / "rows" / f"{table}.jsonl"
                )
            return cache[table]

        for rel in task.relationships:
            label = f"{pop.value}:{rel.child_table}->{rel.parent_table}"
            parent_rows = rows_of(rel.parent_table)
            child_rows = rows_of(rel.child_table)
            if parent_rows is None or child_rows is None:
                missing = rel.parent_table if parent_rows is None else rel.child_table
                failures.append(
                    f"{label}: population rows artifact missing or unreadable "
                    f"for table {missing!r} — no evidence of the loaded key "
                    "space (fail closed)"
                )
                continue
            # Parent pool exactly as _generate_table builds it: unique key
            # tuples over the ACTUAL parent rows, None-containing keys skipped.
            pool: set[tuple] = set()
            for prow in parent_rows:
                key = tuple(prow.get(c) for c in rel.parent_columns)
                if not any(v is None for v in key):
                    pool.add(key)
            nulls = dangling = resolved = 0
            examples: list[str] = []
            violations = 0
            for i, crow in enumerate(child_rows):
                key = tuple(crow.get(c) for c in rel.child_columns)
                if all(v is None for v in key):
                    nulls += 1
                    if rel.required:
                        violations += 1
                        if len(examples) < 3:
                            examples.append(f"row {i}: NULL key on a REQUIRED link")
                    continue
                if any(v is None for v in key):
                    violations += 1
                    if len(examples) < 3:
                        examples.append(
                            f"row {i}: partially-NULL composite key {key!r} "
                            "resolves to no parent row"
                        )
                    continue
                if key in pool:
                    resolved += 1
                    continue
                if dangling_declared and not rel.required:
                    dangling += 1
                    continue
                violations += 1
                if len(examples) < 3:
                    examples.append(f"row {i}: key {key!r} has no parent row")
            evidence[label] = (
                f"children={len(child_rows)},resolved={resolved},null={nulls},"
                f"declared_dangling={dangling},violations={violations}"
            )
            if violations:
                failures.append(
                    f"{label}: {violations} child row(s) violate the declared "
                    "relationship (" + "; ".join(examples) + ")"
                )
    if failures:
        return _fail(
            name,
            "declared relationships are violated in the materialized "
            "population rows: " + "; ".join(failures),
            evidence,
        )
    return _ok(
        name,
        "every child FK value in every population's materialized rows "
        "resolves to an existing parent row (NULL links on optional "
        "relationships only)",
        evidence,
    )


def _gate_mart_key_unique(task: TaskIR, gold: "GoldBundle") -> GateResult:
    """A mart's declared `key_columns` are actually UNIQUE in the frozen gold.

    The uniqueness `key_columns` declares is asserted by the adapter key ladder
    and verified nowhere else, so a fabricated grain must fail HERE. Fail closed
    on an undeclared mart, a key column the CSV lacks, or an unparseable CSV.
    """
    name = "mart-key-unique"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    declared = {m.name: m for m in task.marts}
    evidence: dict[str, str] = {}
    failures: list[str] = []
    for population in sorted(gold.stage2_csv):
        for mart_name in sorted(gold.stage2_csv[population]):
            label = f"{population}:{mart_name}"
            mart = declared.get(mart_name)
            if mart is None:
                failures.append(
                    f"{label}: frozen gold carries a mart the task does not "
                    "declare — there is no key contract to check it against"
                )
                continue
            if not mart.key_columns:
                failures.append(
                    f"{label}: mart declares no key_columns — an unkeyed mart "
                    "has no identity to verify (fail closed, never a pass)"
                )
                continue
            parsed = _gold_mart_rows(gold, population, mart_name)
            if parsed is None:
                failures.append(
                    f"{label}: frozen mart CSV is missing or unparseable — no "
                    "evidence of the produced key space"
                )
                continue
            cols, rows = parsed
            missing = [c for c in mart.key_columns if c not in cols]
            if missing:
                failures.append(
                    f"{label}: declared key column(s) {missing} are absent from "
                    "the frozen CSV header — the declared grain names columns "
                    "the mart does not produce"
                )
                continue
            seen: dict[tuple, int] = {}
            dupes = 0
            example = ""
            for row in rows:
                key = tuple(row.get(c) for c in mart.key_columns)
                seen[key] = seen.get(key, 0) + 1
                if seen[key] == 2:
                    dupes += 1
                    if not example:
                        example = repr(key)
            evidence[label] = f"rows={len(rows)},distinct_keys={len(seen)}"
            if dupes:
                failures.append(
                    f"{label}: {dupes} key tuple(s) appear more than once in "
                    f"{len(rows)} rows (first duplicate: {example}) — the "
                    "declared grain is not the produced grain"
                )
    if failures:
        return _fail(
            name,
            "declared mart keys are not unique in the frozen gold: "
            + "; ".join(failures),
            evidence,
        )
    return _ok(
        name,
        "every mart's declared key_columns are distinct in every population's "
        "frozen gold — the declared grain is a verified fact, not an asserted "
        "proof",
        evidence,
    )


def _gate_el_source_key_unique(task: TaskIR, workspace: Path) -> GateResult:
    """Verify primary-key uniqueness on source rows used by EL.

    Null key components fail. Tables without declared primary keys are reported
    explicitly rather than skipped.
    """
    name = "mart-key-unique"
    tdir = task_dir(workspace, task.task_id)
    evidence: dict[str, str] = {}
    failures: list[str] = []
    keyed = [t for t in task.tables if t.primary_key]
    evidence["tables_without_primary_key"] = ",".join(
        sorted(t.name for t in task.tables if not t.primary_key)
    ) or "(none)"
    for pop in PopulationName:
        for table in keyed:
            label = f"{pop.value}:{table.name}"
            rows = _table_rows(
                tdir / "populations" / pop.value / "rows" / f"{table.name}.jsonl"
            )
            if rows is None:
                failures.append(
                    f"{label}: population rows artifact missing or unreadable — "
                    "no evidence of the loaded key space (fail closed)"
                )
                continue
            seen: dict[tuple, int] = {}
            dupes = nulls = 0
            example = ""
            for i, row in enumerate(rows):
                key = tuple(row.get(c) for c in table.primary_key)
                if any(v is None for v in key):
                    nulls += 1
                    if not example:
                        example = f"row {i}: NULL inside primary key {key!r}"
                    continue
                seen[key] = seen.get(key, 0) + 1
                if seen[key] == 2:
                    dupes += 1
                    if not example:
                        example = f"row {i}: duplicate primary key {key!r}"
            evidence[label] = (
                f"rows={len(rows)},distinct_keys={len(seen)},null_keys={nulls}"
            )
            if dupes or nulls:
                failures.append(
                    f"{label}: {dupes} duplicate and {nulls} NULL-bearing "
                    f"primary-key tuple(s) in {len(rows)} rows ({example})"
                )
    if failures:
        return _fail(
            name,
            "declared source primary keys are violated in the materialized "
            "population rows: " + "; ".join(failures),
            evidence,
        )
    return _ok(
        name,
        "every declared source primary key is fully present and distinct in "
        "every population's materialized rows",
        evidence,
    )


#: Populations the divergence BAND is asserted on — the only graded surface
#: where it holds unconditionally (stress composes duplicate injection on top).
_SCALE_BAND_POPULATIONS: frozenset[PopulationName] = frozenset(
    {PopulationName.PRIMARY, PopulationName.RESAMPLED}
)


def _gate_declared_scale_reconciliation(task: TaskIR, gold: "GoldBundle") -> GateResult:
    """Verify frozen stage-one counts diverge from declared scales.

    Recompute the divergence band independently of the generator. Record, rather than
    assert, sub-floor, literal-row, and duplicate-injected tables.
    """
    name = "declared-scale-reconciliation"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    vectors, why = _stage1_vectors(task, gold)
    if vectors is None:
        return _fail(name, str(why))

    evidence: dict[str, str] = {}
    failures: list[str] = []
    asserted = 0
    for pop in GRADED_POPULATIONS:
        spec = task.population(pop)
        policy = _GENERATION_POLICIES[pop]
        for table in task.tables:
            t = table.name
            frozen = vectors[pop.value][t]
            key = f"{pop.value}:{t}"
            if t in spec.literal_rows:
                evidence[key] = (
                    f"frozen={frozen} — literal rows, not scale-derived; no "
                    "divergence obligation"
                )
                continue
            declared = int(spec.scale.get(t, 0))
            if declared < REALIZED_DIVERGENCE_MIN_SCALE:
                evidence[key] = (
                    f"declared={declared},frozen={frozen} — below "
                    f"REALIZED_DIVERGENCE_MIN_SCALE={REALIZED_DIVERGENCE_MIN_SCALE}, "
                    "realized exactly by design; no divergence surface"
                )
                continue
            delta = frozen - declared
            if policy.dup_frac > 0 and not table.primary_key:
                evidence[key] = (
                    f"declared={declared},frozen={frozen},net_delta={delta:+d} "
                    f"— COMPOSED surface (+{policy.dup_frac:.0%} duplicate "
                    "injection on a table without a primary key): the net "
                    "count is not guaranteed to diverge or to stay in the "
                    "band, so it is recorded, not asserted"
                )
                continue
            # Same integer arithmetic as realized_row_count, never the function
            # itself: sharing it would be blind to the regression sought.
            span_min = max(1, -(-declared * REALIZED_DIVERGENCE_MIN_PCT // 100))
            span_max = declared * REALIZED_DIVERGENCE_MAX_PCT // 100
            asserted += 1
            evidence[key] = (
                f"declared={declared},frozen={frozen},delta={delta:+d},"
                f"band=[{span_min},{span_max}]"
            )
            if frozen == declared:
                failures.append(
                    f"{pop.value}/{t}: the frozen stage-1 count EQUALS the "
                    f"declared scale ({declared}) — the realized-count "
                    "divergence has regressed and the documented number IS "
                    "the extract-load answer again"
                )
            elif pop in _SCALE_BAND_POPULATIONS and not (
                span_min <= abs(delta) <= span_max
            ):
                failures.append(
                    f"{pop.value}/{t}: frozen count {frozen} sits outside the "
                    f"guaranteed {REALIZED_DIVERGENCE_MIN_PCT}-"
                    f"{REALIZED_DIVERGENCE_MAX_PCT}% divergence band around "
                    f"the declared scale {declared} (|delta|={abs(delta)}, "
                    f"band=[{span_min},{span_max}]) — these counts were not "
                    "produced by the realized-count derivation"
                )
    evidence["scaled_assertions"] = str(asserted)
    if failures:
        return _fail(
            name,
            "the frozen stage-1 counts do not reconcile with the DECLARED "
            "population scale: " + "; ".join(failures),
            evidence,
        )
    if asserted == 0:
        return _ok(
            name,
            "no graded population declares a scaled non-literal table at or "
            "above the divergence floor — the declared-scale divergence has "
            "no surface on this task (recorded; guessability below the floor "
            "is measured by the extract-load degenerate probes)",
            evidence,
        )
    return _ok(
        name,
        f"every scaled graded table diverges from its declared scale and the "
        f"memorization pair lands inside the {REALIZED_DIVERGENCE_MIN_PCT}-"
        f"{REALIZED_DIVERGENCE_MAX_PCT}% band ({asserted} assertion(s); "
        "composed and sub-floor surfaces recorded)",
        evidence,
    )


# The battery

def run_gates(
    task: TaskIR,
    workspace: Path,
    gold: "GoldBundle",
    attack_rewards: dict[str, dict[PopulationName, float]],
) -> AcceptanceReport:
    """Run every parent gate and build the fail-closed AcceptanceReport.

    `accepted` comes from AcceptanceReport.from_gates, which structurally cannot
    yield True unless every gate passed.
    """
    workspace = Path(workspace)
    gates = (
        _guarded(
            "trusted-solution",
            lambda: _gate_trusted_solution(task, workspace, gold),
        ),
        _guarded("determinism", lambda: _gate_determinism(task, workspace, gold)),
        _guarded("degenerate-zero", lambda: _gate_degenerate_zero(task, gold)),
        _guarded("required-mutants", lambda: _gate_required_mutants(task, attack_rewards)),
        _guarded(
            "shortcut-probes",
            lambda: _gate_shortcut_probes(task, workspace, attack_rewards),
        ),
        _guarded(
            "data-sensitivity",
            lambda: _gate_data_sensitivity(task, workspace, gold),
        ),
        _guarded("info-content", lambda: _gate_info_content(task, gold)),
        _guarded("populations-load", lambda: _gate_populations_load(task, workspace, gold)),
        _guarded("contamination-clean", lambda: _gate_contamination_clean(task, workspace)),
        _guarded(
            "dual-build-agreement",
            lambda: _gate_dual_build_agreement(task, workspace),
        ),
        _guarded(
            "referential-integrity",
            lambda: _gate_referential_integrity(task, workspace),
        ),
        _guarded(
            "declared-scale-reconciliation",
            lambda: _gate_declared_scale_reconciliation(task, gold),
        ),
        _guarded("mart-key-unique", lambda: _gate_mart_key_unique(task, gold)),
    )
    return AcceptanceReport.from_gates(
        task_id=task.task_id,
        revision=task.current_revision,
        task_content_hash=task.content_hash(),
        gates=gates,
        scorer_version=SCORER_VERSION,
        roster_digest=ROSTER_DIGEST,
        roster=VARIANT_GATE_NAMES[TaskVariant.FULL],
    )


# Per-unit batteries certify extract/load or transform independently. Missing
# or inapplicable evidence is recorded in the roster and never passes as a skip.

#: Evidence produced by verification/el_probes.py (the independent census).
EL_CENSUS_EVIDENCE_REL = "reports/el_artifact_census.json"
EL_CENSUS_KIND = "el-artifact-census"
#: Evidence produced by reference/independent.py's `independent_loader` role.
EL_INDEPENDENT_LOAD_EVIDENCE_REL = "reports/independent_load_build.json"
EL_INDEPENDENT_LOAD_ROLE = "independent_loader"
#: Evidence from the T warehouse builder, promoted out of export time into a
#: recorded gate bound to the ledger.
WAREHOUSE_CENSUS_EVIDENCE_REL = "reports/warehouse_census.json"
WAREHOUSE_CENSUS_KIND = "warehouse-census"

#: Keys that split the ONE run digest into a per-variant witness.
DETERMINISM_STAGE1_DIGEST_KEY = "stage1_digest"
DETERMINISM_STAGE2_DIGEST_KEY = "stage2_digest"

#: Length of a hex sha256; digests are validated structurally before compare so
#: a truncated or placeholder digest fails closed instead of matching another.
_HEX64 = 64


class VariantGateVerdict(str, Enum):
    """How one gate relates to one variant. Recorded, never implied."""

    #: Same claim, same witness, same reward-relevant evidence as the parent.
    APPLIES_AS_IS = "applies-as-is"
    #: Same claim, different artifact/reward: the parent witness is vacuous here.
    DIFFERENT_WITNESS = "different-witness"
    #: No claim this variant carries: removed with a named replacement, never waived.
    NOT_APPLICABLE = "not-applicable"
    #: A gate that exists only for this variant (replacement or roster self-check).
    NEW_FOR_VARIANT = "new-for-variant"


class VariantGateApplicability(BaseModel):
    """One cell of the gate x variant applicability matrix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: str
    verdict: VariantGateVerdict
    reason: str
    #: For NOT_APPLICABLE: the gate names that take over its obligation.
    replaced_by: tuple[str, ...] = ()
    #: For NEW_FOR_VARIANT replacements: the parent gate being replaced.
    replaces: str | None = None


#: Failure routing: TASK-LEVEL means the parent battery makes the same claim but
#: its measurement was MASKED, so FULL must not ship; VARIANT-LOCAL rejects one.
FAILURE_TASK_LEVEL = "task-level"
FAILURE_VARIANT_LOCAL = "variant-local"

_VARIANT_LOCAL_FAILURES: frozenset[tuple[TaskVariant, str]] = frozenset(
    {
        (TaskVariant.EXTRACT_LOAD, "info-content"),
        (TaskVariant.EXTRACT_LOAD, "degenerate-zero"),
        (TaskVariant.EXTRACT_LOAD, "required-mutants"),
        (TaskVariant.EXTRACT_LOAD, "shortcut-probes"),
        (TaskVariant.EXTRACT_LOAD, "data-sensitivity"),
        (TaskVariant.EXTRACT_LOAD, "el-independent-load"),
        (TaskVariant.TRANSFORM, "shortcut-probes"),
        # A projection-only mart set is a T unit with no transform to learn,
        # not a defect of the parent data: the EL variant and FULL still ship.
        (TaskVariant.TRANSFORM, "transform-surface"),
    }
)

#: Plan op kinds a T solver must COMPUTE rather than rename (SOURCE / DERIVE /
#: TIE_BREAK alone are a projection; UNION counts, stacking is not a rename).
#: Kept explicit here, not imported, so the claim is readable where asserted.
TRANSFORM_SURFACE_KINDS: frozenset[MartOpKind] = frozenset(
    {
        MartOpKind.AGGREGATE,
        MartOpKind.FILTERED_AGGREGATE,
        MartOpKind.DISTINCT,
        MartOpKind.EXTREMA,
        MartOpKind.WINDOW,
        MartOpKind.CONDITIONAL,
        MartOpKind.RATIO,
        MartOpKind.JOIN,
        MartOpKind.FILTER,
        MartOpKind.DEDUPE,
        MartOpKind.UNION,
    }
)

#: Kinds whose PARENT reward was decided by the stage-1 gate: their parent
#: numbers are not their T rewards, so a T battery copying them measures nothing.
STAGE1_GATED_KINDS: frozenset[AttackKind] = frozenset(
    {AttackKind.NO_OP, AttackKind.SKIP_EXTRACTION}
)

#: Mart (stage-2) mutations, INADMISSIBLE as extract-load evidence: the EL
#: reward never looks at a mart, so every one of them scores 1.0 under it.
TRANSFORM_MUTANT_KINDS: frozenset[AttackKind] = frozenset(
    {
        AttackKind.INNER_JOIN,
        AttackKind.NO_DEDUP,
        AttackKind.WRONG_GRAIN,
        AttackKind.WRONG_AGG_STAGE,
        AttackKind.WRONG_DENOMINATOR,
        AttackKind.CONSTANTS,
        AttackKind.KEYS_ONLY,
        AttackKind.DROPPED_FILTER,
        AttackKind.WRONG_WINDOW,
        AttackKind.NO_NULL_DEFAULT,
        AttackKind.CUSTOM,
    }
)

#: Extraction/load mutations by VALUE: resolved against whatever AttackKind
#: currently defines, with unresolved names RECORDED, never silently dropped.
EXTRACTION_MUTANT_KIND_NAMES: tuple[str, ...] = (
    "skip_extraction",
    "partial_backend",
    "duplicate_on_load",
    "truncate_table",
    "wrong_source_file",
    "stale_snapshot",
    "null_row_drop",
    "header_as_row",
    "fabricate_counts",
)


def _resolve_kinds(names: tuple[str, ...]) -> tuple[frozenset[AttackKind], tuple[str, ...]]:
    """(defined kinds, names not yet defined in models.AttackKind)."""
    defined: set[AttackKind] = set()
    missing: list[str] = []
    for value in names:
        try:
            defined.add(AttackKind(value))
        except ValueError:
            missing.append(value)
    return frozenset(defined), tuple(missing)


EXTRACTION_MUTANT_KINDS, _UNDECLARED_EL_KIND_NAMES = _resolve_kinds(
    EXTRACTION_MUTANT_KIND_NAMES
)

#: Shortcut sets per variant. The parent's single SHORTCUT_KINDS set spans both
#: stages, which is exactly why the surface LOOKS covered when it is not.
EL_SHORTCUT_KINDS: frozenset[AttackKind] = frozenset(
    {AttackKind.NO_OP}
) | EXTRACTION_MUTANT_KINDS
TRANSFORM_SHORTCUT_KINDS: frozenset[AttackKind] = frozenset(
    {AttackKind.CONSTANTS, AttackKind.KEYS_ONLY, AttackKind.NO_OP}
)

#: Recorded exclusions: one nobody can see is indistinguishable from a hole.
_EL_SHORTCUT_EXCLUSIONS: dict[AttackKind, str] = {
    AttackKind.CONSTANTS: (
        "mart mutation — evaluate_variant(EXTRACT_LOAD) ignores actual_marts "
        "entirely, so this probe would score 1.0 without ever touching "
        "extraction; EXCLUDED from EL's set rather than measured and 'passed'"
    ),
    AttackKind.KEYS_ONLY: (
        "mart mutation — same reason as constants: unscorable under the EL "
        "reward, so it is excluded rather than credited"
    ),
}
_T_SHORTCUT_EXCLUSIONS: dict[AttackKind, str] = {
    AttackKind.SKIP_EXTRACTION: (
        "the TRANSFORM bundle hands the solver the materialized warehouse, so "
        "'skip a source backend' is not a shortcut a T solver can take"
    ),
}

#: Floor of surviving parent gates: len(GATE_NAMES) - 1, since each shipped
#: variant waives exactly ONE. A mostly-waived variant is rejected, not quiet.
MIN_APPLICABLE_PARENT_GATES = 12


def _matrix(*cells: VariantGateApplicability) -> tuple[VariantGateApplicability, ...]:
    return tuple(cells)


VARIANT_GATE_MATRIX: dict[TaskVariant, tuple[VariantGateApplicability, ...]] = {
    TaskVariant.FULL: _matrix(
        *(
            VariantGateApplicability(
                gate=g,
                verdict=VariantGateVerdict.APPLIES_AS_IS,
                reason=(
                    "the parent is its own unit (acceptance rule R4): FULL keeps "
                    "the unchanged parent battery scored by evaluate(); "
                    "it is neither the union nor the conjunction of its variants"
                ),
            )
            for g in GATE_NAMES
        )
    ),
    TaskVariant.EXTRACT_LOAD: _matrix(
        VariantGateApplicability(
            gate="trusted-solution",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "the parent witness is a TAUTOLOGY under the EL reward: "
                "compare_stage1(gold.stage1[pop], gold.stage1[pop]) is True by "
                "construction because the gold count map is fed in as both "
                "sides. That half is DISCARDED. And both builds share "
                "load_sources_duckdb, so the frozen counts are self-certified "
                "by the very loader that produced them. EL's trusted-solution "
                "therefore reduces entirely to new certification: the "
                "independent artifact census plus the three-legged "
                "reconciliation generator-rows <-> rendered artifacts <-> "
                "frozen gold"
            ),
        ),
        VariantGateApplicability(
            gate="determinism",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "runner.run_digests produces a joint hash over stage-1 counts "
                "AND every mart CSV; a per-variant report citing that digest "
                "cites evidence about the other variant. EL cites the split "
                f"{DETERMINISM_STAGE1_DIGEST_KEY!r} component only"
            ),
        ),
        VariantGateApplicability(
            gate="degenerate-zero",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "the degenerate EL submission is not a mart shape, it is a "
                "COUNTS MAP: the EL reward consumes only actual_stage1. Four "
                "load-side probes replace the three mart strategies — "
                "empty_load, one_table_only, uniform_source, fabricate_counts"
            ),
        ),
        VariantGateApplicability(
            gate="required-mutants",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "the existing required cases are TRANSFORM mutants and are "
                "INADMISSIBLE as EL evidence — compare_stage1 never looks at a "
                "mart, so every one of them scores 1.0 under the EL reward. EL "
                "gets its own extraction-mutant matrix"
            ),
        ),
        VariantGateApplicability(
            gate="shortcut-probes",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "SHORTCUT_KINDS is one set spanning both stages, which is why "
                "the surface looks covered when it is not. CONSTANTS and "
                "KEYS_ONLY are unscorable under the EL reward and are excluded "
                "with a recorded reason rather than measured and 'passed'"
            ),
        ),
        VariantGateApplicability(
            gate="data-sensitivity",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "the EL gold IS the count map, so a structure-preserving VALUE "
                "bijection cannot move it and must not be asked to. The EL "
                "claim is CARDINALITY sensitivity: invariant on the "
                "memorization pair (forced by primary.scale == resampled.scale), "
                "and MOVING on at least one graded pair — measured, never "
                "assumed, because STRESS_CAP can make stress == primary"
            ),
        ),
        VariantGateApplicability(
            gate="info-content",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "the information in an EL task is the count VECTOR, not the "
                "mart columns: compare_stage1 grades one integer per table, so "
                "all-identical counts means the solver discovers ONE number "
                "instead of N and the uniform_source shortcut scores 1.0"
            ),
        ),
        VariantGateApplicability(
            gate="populations-load",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "the strongest EL check that already exists — it reconciles "
                "rows/<table>.jsonl against gold.stage1, which IS the EL gold — "
                "but its witness stops short of the graded surface: for "
                "`rendered` it only checks the directory is non-empty, and "
                "rendered/ is precisely what an EL solver reads. Extended to "
                "require per-table artifact RESOLUTION and census agreement"
            ),
        ),
        VariantGateApplicability(
            gate="contamination-clean",
            verdict=VariantGateVerdict.APPLIES_AS_IS,
            reason=(
                "contamination is a property of the task CONTENT and variants "
                "are views over ONE TaskIR at ONE content hash, so the parent "
                "scan is sound evidence — but an inherited gate is still a gate "
                "that must appear in the roster and re-check its task_id and "
                "content-hash binding"
            ),
        ),
        VariantGateApplicability(
            gate="dual-build-agreement",
            verdict=VariantGateVerdict.NOT_APPLICABLE,
            reason=(
                "reference/independent.py's implementer produces the TRANSFORM "
                "SQL while extract+load is performed by the trusted "
                "deterministic loader BOTH builds share — so the recorded "
                "independent build is a T-variant witness in full and asserts "
                "NOTHING about extraction. A variant with no independent check "
                "is weaker than the parent, so EL gets replacements, not an "
                "exemption, and it needs TWO because neither alone suffices"
            ),
            replaced_by=("el-artifact-census", "el-independent-load"),
        ),
        VariantGateApplicability(
            gate="referential-integrity",
            verdict=VariantGateVerdict.APPLIES_AS_IS,
            reason=(
                "a property of the materialized source rows at the ONE parent "
                "content hash: the EL solver loads exactly these rows (the "
                "three-legged reconciliation ties rows/<table>.jsonl to the "
                "rendered surface), and the witness is the same recorded "
                "artifact the parent gate reads — re-checked here, never "
                "inherited as a verdict"
            ),
        ),
        VariantGateApplicability(
            gate="declared-scale-reconciliation",
            verdict=VariantGateVerdict.APPLIES_AS_IS,
            reason=(
                "this gate IS an extract-load claim: compare_stage1 grades the "
                "frozen count vector, and the divergence from the declared "
                "scale is what keeps that vector from being readable off the "
                "task documentation. Same witness (frozen stage-1 counts + "
                "TaskIR scale), same assertion, re-run at this variant's "
                "roster"
            ),
        ),
        VariantGateApplicability(
            gate="mart-key-unique",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "the parent witness (mart CSV key projection) asserts nothing "
                "the EL reward reads — compare_stage1 never sees a mart. The "
                "same CLAIM ('declared keys are actually unique in produced "
                "data') is re-measured on the surface an EL solver loads: every "
                "declared SOURCE primary_key must be fully present and distinct "
                "in every population's materialized rows. The generator "
                "promises this and nothing verified it, and wikidbs ships "
                "vendor rows verbatim"
            ),
        ),
        VariantGateApplicability(
            gate="el-artifact-census",
            verdict=VariantGateVerdict.NEW_FOR_VARIANT,
            reason=(
                "a SECOND, independently written per-format record counter over "
                "populations/<pop>/rendered/, not built on solution.py's "
                "readers, required to equal the frozen stage-1 counts exactly. "
                "This is the fail-closed offline floor: it certifies the GOLD"
            ),
            replaces="dual-build-agreement",
        ),
        VariantGateApplicability(
            gate="el-independent-load",
            verdict=VariantGateVerdict.NEW_FOR_VARIANT,
            reason=(
                "a cross-family model shown only the EL public bundle emits a "
                "load_plan executed by the shared trusted readers and scored by "
                "evaluate_variant(EXTRACT_LOAD). Be honest about what it buys: "
                "there is usually one obvious artifact per table, so agreement "
                "is near-automatic and it decorrelates little error. Its real "
                "claim is BUNDLE SUFFICIENCY, and it must NEVER stand in for "
                "the census"
            ),
            replaces="dual-build-agreement",
        ),
        VariantGateApplicability(
            gate="variant-roster",
            verdict=VariantGateVerdict.NEW_FOR_VARIANT,
            reason=(
                "AcceptanceReport is fail-closed on gate RESULTS but not on "
                "gate COVERAGE. This gate records the applicability matrix, the "
                "applicable-gate count and every not-applicable gate with its "
                "reason and replacement, and fails if the produced roster is "
                "not exactly the declared one or if the battery is mostly "
                "waivers"
            ),
        ),
    ),
    TaskVariant.TRANSFORM: _matrix(
        VariantGateApplicability(
            gate="trusted-solution",
            verdict=VariantGateVerdict.APPLIES_AS_IS,
            reason=(
                "both halves survive: gold-vs-gold under the T reward is "
                "exactly _mart_fraction over the frozen CSVs (non-vacuous — a "
                "ragged or malformed gold fails parse_canonical_csv), and the "
                "recorded independent build IS a T-variant witness because the "
                "implementer writes the TRANSFORM SQL"
            ),
        ),
        VariantGateApplicability(
            gate="determinism",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                f"T cites the split {DETERMINISM_STAGE2_DIGEST_KEY!r} component "
                "of the run digest, plus a warehouse CENSUS digest agreeing "
                "across two independent builds — never the .duckdb bytes, "
                "which are not byte-stable (storage pages, version headers)"
            ),
        ),
        VariantGateApplicability(
            gate="degenerate-zero",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "same three strategies, but scored with "
                "evaluate_variant(TRANSFORM), which REMOVES A MASK rather than "
                "relabelling one: compare_mart('<header>\\n', [], mart) is True, "
                "so with no stage-1 gate a no-op earns credit for every mart "
                "whose gold is empty on that population. Asserted on every "
                "graded population, not only primary"
            ),
        ),
        VariantGateApplicability(
            gate="required-mutants",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "same form, but every case is RE-SCORED under "
                "evaluate_variant(TRANSFORM) from the SAME execution and read "
                "out of rewards_by_variant — never copied from the legacy "
                "`rewards` field. Copying it wholesale would make the T battery "
                "a relabelling of the parent battery: the fiction, pointed the "
                "other way"
            ),
        ),
        VariantGateApplicability(
            gate="shortcut-probes",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "the parent's cross-stage set minus SKIP_EXTRACTION (excluded "
                "with a recorded reason), scored with the T reward"
            ),
        ),
        VariantGateApplicability(
            gate="data-sensitivity",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "the parent claim carries over unchanged — emit_variant "
                "materializes a DIFFERENT warehouse per population from that "
                "population's frozen stage-1 state — but one witness is ADDED: "
                "the provided WAREHOUSE must differ wherever the gold differs. "
                "Two populations shipping identical censuses with different "
                "gold is an UNSOLVABLE T task, and no existing gate looks at "
                "the warehouse"
            ),
        ),
        VariantGateApplicability(
            gate="info-content",
            verdict=VariantGateVerdict.DIFFERENT_WITNESS,
            reason=(
                "the constant-column audit is already a pure stage-2 statement "
                "over primary mart gold, kept unchanged, plus one tightening: "
                "the >=2-gold-rows check extends to every GRADED population, "
                "because the empty-gold-mart hole the T degenerate probe "
                "exposes lives on the non-primary populations"
            ),
        ),
        VariantGateApplicability(
            gate="populations-load",
            verdict=VariantGateVerdict.NOT_APPLICABLE,
            reason=(
                "the T solver never sees populations/ — it is handed "
                "warehouse/<pop>.duckdb. Reconciling artifacts the solver "
                "cannot read asserts nothing about the T task"
            ),
            replaced_by=("warehouses-load",),
        ),
        VariantGateApplicability(
            gate="contamination-clean",
            verdict=VariantGateVerdict.APPLIES_AS_IS,
            reason=(
                "same inheritance as EL: one TaskIR, one content hash, one scan "
                "— re-checked here rather than assumed"
            ),
        ),
        VariantGateApplicability(
            gate="dual-build-agreement",
            verdict=VariantGateVerdict.APPLIES_AS_IS,
            reason=(
                "the recorded cross-family build IS the T witness: its own "
                "docstring says extract+load is performed by the trusted "
                "deterministic loader that both builds share, so what the "
                "implementer independently produced is exactly the transform"
            ),
        ),
        VariantGateApplicability(
            gate="referential-integrity",
            verdict=VariantGateVerdict.APPLIES_AS_IS,
            reason=(
                "the T warehouse is materialized from these same canonical "
                "rows (warehouses-load pins its counts to the same frozen "
                "stage-1 state), so FK resolution measured on the rows IS FK "
                "resolution of the warehouse content — and a dangling key "
                "silently empties every joined mart, which is a transform "
                "defect no other T gate can see"
            ),
        ),
        VariantGateApplicability(
            gate="declared-scale-reconciliation",
            verdict=VariantGateVerdict.APPLIES_AS_IS,
            reason=(
                "one frozen stage-1 state underlies all three batteries and "
                "the T warehouse ships at exactly these counts, so the same "
                "witness carries unchanged; a divergence regression is a "
                "parent-data defect (task-level) that must not ship under any "
                "variant"
            ),
        ),
        VariantGateApplicability(
            gate="mart-key-unique",
            verdict=VariantGateVerdict.APPLIES_AS_IS,
            reason=(
                "the mart CSVs ARE the T gold: the same frozen stage-2 "
                "projection onto key_columns is exactly the claim the T reward "
                "grades against, so the parent witness carries unchanged"
            ),
        ),
        VariantGateApplicability(
            gate="warehouses-load",
            verdict=VariantGateVerdict.NEW_FOR_VARIANT,
            reason=(
                "the T analogue of populations-load over the artifact the T "
                "solver actually gets: every population's warehouse opens, "
                "contains EXACTLY the task's source tables (no marts, no gold, "
                "no answer_key) and each count equals the frozen stage-1 gold. "
                "materialize_warehouse already asserts this — at EXPORT time, "
                "and export is not a gate and is not bound to the ledger"
            ),
            replaces="populations-load",
        ),
        VariantGateApplicability(
            gate="transform-surface",
            verdict=VariantGateVerdict.NEW_FOR_VARIANT,
            reason=(
                "a TRANSFORM unit must carry a transform: a package whose "
                "every mart is a pure single-table rename projection "
                "(source/derive/tie_break only — the dbt staging-grain marts) "
                "has a correct reward and NO transform signal for a T solver "
                "to learn. Refused VARIANT-LOCALLY on the plan ops (never on "
                "prose or MartColumn.kind), so the package's EL variant and "
                "the FULL task still ship"
            ),
        ),
        VariantGateApplicability(
            gate="canonical-reachability",
            verdict=VariantGateVerdict.NEW_FOR_VARIANT,
            reason=(
                "the T unit's private answer key must be REACHABLE through the "
                "RLVR workspace channel: a canonical Terraform + dbt project "
                "derived mechanically from the private connector contract and "
                "the reference SQL, scored by training.scorer.score_workspace "
                "on every graded population, must earn exactly 1.0. The "
                "validate-t runner produces the record "
                "(reports/canonical_reachability.json, artifact under "
                "answer_key/runtime/canonical/<destination>/) and this gate "
                "re-reads it bound to the content hash, the reference SQL, "
                "the artifact bytes, the portable dbt subset version and the "
                "workspace scorer version. Nothing else in the ladder exercises "
                "the Terraform intent validators or the portable dbt subset "
                "(batch20 api-20: dbt__twitter_ads packaged with every mart "
                "unreachable). TASK-LEVEL: an unreachable answer key blocks FULL"
            ),
        ),
        VariantGateApplicability(
            gate="variant-roster",
            verdict=VariantGateVerdict.NEW_FOR_VARIANT,
            reason=(
                "coverage is not implied by results: this gate records the "
                "matrix, the applicable-gate count and every not-applicable "
                "gate with its reason and replacement"
            ),
        ),
    ),
}


def _roster_from_matrix(variant: TaskVariant) -> tuple[str, ...]:
    return tuple(
        cell.gate
        for cell in VARIANT_GATE_MATRIX[variant]
        if cell.verdict is not VariantGateVerdict.NOT_APPLICABLE
    )


#: The gate roster actually RUN for each variant. Every name must be present
#: AND passed; "does not apply" is expressed by absence from this tuple plus a
#: named replacement inside it, never by a passing GateResult saying "skipped".
VARIANT_GATE_NAMES: dict[TaskVariant, tuple[str, ...]] = {
    variant: _roster_from_matrix(variant) for variant in TaskVariant
}


def _check_matrix_wellformed() -> None:
    """Import-time structural check (fail closed at load, not at release)."""
    if VARIANT_GATE_NAMES[TaskVariant.FULL] != GATE_NAMES:
        raise RuntimeError(
            "FULL's variant roster must be GATE_NAMES verbatim (acceptance "
            f"rule R4); got {VARIANT_GATE_NAMES[TaskVariant.FULL]}"
        )
    for variant, cells in VARIANT_GATE_MATRIX.items():
        names = [c.gate for c in cells]
        if len(names) != len(set(names)):
            raise RuntimeError(f"{variant.value}: duplicate gate in the matrix")
        covered = set(names)
        missing = [g for g in GATE_NAMES if g not in covered]
        if missing:
            raise RuntimeError(
                f"{variant.value}: gates absent from the applicability matrix "
                f"(a gate with no recorded verdict is a hole): {missing}"
            )
        for cell in cells:
            if cell.verdict is VariantGateVerdict.NOT_APPLICABLE:
                if not cell.replaced_by:
                    raise RuntimeError(
                        f"{variant.value}/{cell.gate}: not-applicable with no "
                        "named replacement is a waiver, not a verdict"
                    )
                for repl in cell.replaced_by:
                    if repl not in covered:
                        raise RuntimeError(
                            f"{variant.value}/{cell.gate}: replacement "
                            f"{repl!r} is not in the roster"
                        )


_check_matrix_wellformed()


def roster_digest() -> str:
    """DERIVED roster identity: 16-hex sha256 over the per-variant gate names.

    Any roster change moves it automatically, so a battery whose stamped digest
    is not the live one is STALE evidence — re-run, never accept, never crash.
    """
    return sha256_hex(
        canonical_json(
            {variant.value: list(names) for variant, names in VARIANT_GATE_NAMES.items()}
        )
    )[:16]


#: The live roster identity (see `roster_digest`), computed once.
ROSTER_DIGEST: str = roster_digest()


def variant_applicability(
    variant: TaskVariant, gate: str
) -> VariantGateApplicability | None:
    for cell in VARIANT_GATE_MATRIX[TaskVariant(variant)]:
        if cell.gate == gate:
            return cell
    return None


def classify_variant_failure(variant: TaskVariant, gate: str) -> str:
    """Is a failure of `gate` on `variant` TASK-LEVEL or VARIANT-LOCAL?

    Anything not explicitly variant-local is task-level: fail closed toward the
    stricter route, which stops FULL from shipping.
    """
    key = (TaskVariant(variant), gate)
    return FAILURE_VARIANT_LOCAL if key in _VARIANT_LOCAL_FAILURES else FAILURE_TASK_LEVEL


# Shared variant helpers

def _variant_rewards(
    rewards_by_variant: Mapping[Any, Any] | None, variant: TaskVariant
) -> tuple[dict[str, dict[PopulationName, float]] | None, str | None]:
    """The per-case rewards measured UNDER THIS VARIANT'S REWARD, or why not.

    Accepts TaskVariant or plain-string keys. Fails closed on absence: an
    unmeasured variant is not a variant that passed.
    """
    if rewards_by_variant is None:
        return None, "no rewards_by_variant map supplied (fail closed)"
    for key in (variant, variant.value):
        if key in rewards_by_variant:
            payload = rewards_by_variant[key]
            if not isinstance(payload, Mapping):
                return None, (
                    f"rewards_by_variant[{variant.value!r}] is not a mapping"
                )
            return _normalize_attack_rewards(dict(payload)), None
    return None, (
        f"no attack rewards recorded for variant {variant.value!r} — "
        "verification/attacks.py must record rewards_by_variant computed from "
        "the SAME execution via evaluate_variant (the legacy `rewards` field "
        "is never read by a variant battery)"
    )


def _stage1_vectors(
    task: TaskIR, gold: "GoldBundle"
) -> tuple[dict[str, dict[str, int]] | None, str | None]:
    """Frozen stage-1 count map per population, or why it is unusable."""
    stage1 = getattr(gold, "stage1", None)
    if not isinstance(stage1, dict) or not stage1:
        return None, "gold bundle has no stage-1 counts"
    out: dict[str, dict[str, int]] = {}
    for pop in PopulationName:
        counts = stage1.get(pop.value)
        if not isinstance(counts, dict) or not counts:
            return None, f"no frozen stage-1 counts for population {pop.value!r}"
        missing = [t.name for t in task.tables if t.name not in counts]
        if missing:
            return None, (
                f"population {pop.value!r}: no frozen count for table(s) {missing}"
            )
        out[pop.value] = {t.name: int(counts[t.name]) for t in task.tables}
    return out, None


def _vector_text(counts: Mapping[str, int]) -> str:
    return ",".join(f"{t}={counts[t]}" for t in sorted(counts))


def _is_hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HEX64
        and all(c in "0123456789abcdef" for c in value.lower())
    )


# EL evidence readers

def _el_census_record(task: TaskIR, workspace: Path) -> tuple[dict | None, str | None]:
    """The bound census record, or why it is not THIS counter's evidence.

    Beyond identity, `counter_version` and `independent_of` must equal
    el_probes' own: a record from another counter, or one whose independence
    claim differs from the code's, certifies nothing about the reader-bug class
    this census exists for.
    """
    record, err = _bound_record(
        task,
        workspace,
        EL_CENSUS_EVIDENCE_REL,
        "el-artifact-census",
        required_keys=("task_id", "task_content_hash", "kind", "populations"),
        absent_hint=(
            "the independent per-format record counter was never run; without "
            "it the frozen stage-1 counts are certified only by the loader that "
            "produced them"
        ),
    )
    if record is None:
        return None, err
    if record.get("kind") != EL_CENSUS_KIND:
        return None, (
            f"census evidence is kind {record.get('kind')!r}, not "
            f"{EL_CENSUS_KIND!r} (a differently-shaped record must not be "
            "accepted just because it landed at the same filename)"
        )
    # Lazy import: el_probes is the PRODUCER and must stay import-light.
    from elt_taskgen.verification import el_probes

    violations = el_probes.independence_violations()
    if violations:
        return None, (
            "census counter imports a module it declares independence from: "
            + "; ".join(violations)
            + " — the census is not independent of the readers it exists to check"
        )
    recorded_version = record.get("counter_version")
    if recorded_version != el_probes.COUNTER_VERSION:
        return None, (
            f"census evidence was recorded by counter version "
            f"{recorded_version!r}, current is {el_probes.COUNTER_VERSION!r} — "
            "a record from another counter is not this counter's evidence "
            "(re-run the census)"
        )
    independent_of = record.get("independent_of")
    if not isinstance(independent_of, list) or set(map(str, independent_of)) != set(
        el_probes.INDEPENDENT_OF
    ):
        return None, (
            f"census evidence declares independent_of={independent_of!r}, the "
            f"counter declares {list(el_probes.INDEPENDENT_OF)!r} — a census "
            "whose independence claim differs from its counter's is not that "
            "counter's evidence"
        )
    return record, None


def _el_census_problems(
    task: TaskIR, workspace: Path, gold: "GoldBundle", record: Mapping[str, Any]
) -> tuple[list[str], dict[str, str]]:
    """THE THREE-LEGGED RECONCILIATION: generator rows <-> rendered artifacts
    (the census) <-> the frozen stage-1 gold.

    Any TWO legs agreeing is not enough: the census shares the RENDERER's notion
    of a record, so only the rows/*.jsonl leg is independent of it.
    """
    problems: list[str] = []
    evidence: dict[str, str] = {}

    independent_of = record.get("independent_of")
    if not isinstance(independent_of, list) or not independent_of:
        problems.append(
            "census records no 'independent_of' declaration — a census built on "
            "solution.py's readers cannot catch a reader bug the loader and the "
            "gold agree on, which is the whole class it exists for"
        )
    else:
        evidence["census_independent_of"] = ",".join(str(x) for x in independent_of)
    evidence["census_counter_version"] = str(record.get("counter_version", ""))

    populations = record.get("populations")
    if not isinstance(populations, dict):
        return problems + ["census 'populations' is not an object"], evidence

    stage1 = getattr(gold, "stage1", None) or {}
    tdir = task_dir(workspace, task.task_id)
    for pop in PopulationName:
        per_pop = populations.get(pop.value)
        if not isinstance(per_pop, dict):
            problems.append(f"census has no record for population {pop.value!r}")
            continue
        frozen = stage1.get(pop.value)
        if not isinstance(frozen, dict):
            problems.append(
                f"no frozen stage-1 gold for population {pop.value!r} to "
                "reconcile the census against"
            )
            continue
        for table in task.tables:
            entry = per_pop.get(table.name)
            if not isinstance(entry, dict):
                problems.append(
                    f"{pop.value}/{table.name}: no census entry (an uncounted "
                    "artifact is an uncertified one)"
                )
                continue
            counted = entry.get("records")
            if not isinstance(counted, int) or isinstance(counted, bool):
                problems.append(
                    f"{pop.value}/{table.name}: census 'records' is not an "
                    f"integer ({counted!r})"
                )
                continue
            artifact = entry.get("artifact")
            if not isinstance(artifact, str) or not artifact:
                problems.append(
                    f"{pop.value}/{table.name}: census names no artifact — a "
                    "count with no artifact behind it certifies nothing"
                )
            want = frozen.get(table.name)
            rows_file = (
                tdir / "populations" / pop.value / "rows" / f"{table.name}.jsonl"
            )
            generated = _jsonl_line_count(rows_file)
            evidence[f"{pop.value}:{table.name}"] = (
                f"rows={generated if generated is not None else 'MISSING'},"
                f"census={counted},gold={want}"
            )
            if generated is None:
                problems.append(
                    f"{pop.value}/{table.name}: generator rows missing or "
                    f"unreadable at {rows_file.name} — the only leg of the "
                    "reconciliation that is independent of the RENDERER"
                )
                continue
            if not isinstance(want, int) or isinstance(want, bool):
                # A missing gold leg is no reconciliation at all: the census
                # cannot certify a count the gold never froze.
                problems.append(
                    f"{pop.value}/{table.name}: no frozen stage-1 count to "
                    "reconcile against (gold leg missing)"
                )
                continue
            legs = {"generator_rows": generated, "census": counted, "gold": want}
            if len(set(legs.values())) != 1:
                problems.append(
                    f"{pop.value}/{table.name}: three-legged reconciliation "
                    f"disagrees {legs}"
                )
    return problems, evidence


def _jsonl_line_count(path: Path) -> int | None:
    if not path.is_file():
        return None
    n = 0
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                json.loads(line)
                n += 1
    except (OSError, json.JSONDecodeError):
        return None
    return n


def _warehouse_census_record(
    task: TaskIR, workspace: Path
) -> tuple[dict | None, str | None]:
    record, err = _bound_record(
        task,
        workspace,
        WAREHOUSE_CENSUS_EVIDENCE_REL,
        "warehouse-census",
        required_keys=("task_id", "task_content_hash", "kind", "populations"),
        absent_hint=(
            "the TRANSFORM bundle ships warehouse/<pop>.duckdb, an artifact the "
            "parent battery never looked at; export-time assertions are not "
            "gates and are not bound to the ledger"
        ),
    )
    if record is None:
        return None, err
    if record.get("kind") != WAREHOUSE_CENSUS_KIND:
        return None, (
            f"warehouse census evidence is kind {record.get('kind')!r}, not "
            f"{WAREHOUSE_CENSUS_KIND!r}"
        )
    # Digests are per-version, so a record censused under an older algorithm
    # cannot be quoted now (no version recorded == v1). Lazy: eltbench produces.
    from elt_taskgen.export import eltbench as eltbench_mod

    current_version = str(eltbench_mod.CENSUS_VERSION)
    recorded_version = record.get("census_version")
    recorded_version = "1" if recorded_version is None else str(recorded_version)
    if recorded_version != current_version:
        return None, (
            f"warehouse census evidence was recorded under census_version "
            f"{recorded_version!r}, current is {current_version!r} — digests are "
            "per-version, so this record cannot certify the shipped warehouse "
            "(re-run validate-t to re-record the census)"
        )
    return record, None


# EXTRACT_LOAD gates

def _gate_el_artifact_census(
    task: TaskIR, workspace: Path, gold: "GoldBundle"
) -> GateResult:
    """EL replacement #1 for dual-build-agreement: certify the GOLD.

    An independently written per-format record counter over every population's
    rendered artifacts, reconciled three ways. This is the fail-closed offline
    floor; el-independent-load must never stand in for it.
    """
    name = "el-artifact-census"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    record, err = _el_census_record(task, workspace)
    if record is None:
        return _fail(name, str(err))
    census_problems, evidence = _el_census_problems(task, workspace, gold, record)
    if census_problems:
        return _fail(
            name,
            "independent artifact census does not certify the frozen stage-1 "
            "gold: " + "; ".join(census_problems),
            evidence,
        )
    return _ok(
        name,
        "an independently written per-format record counter agrees with the "
        "frozen stage-1 counts on all five populations, and the three-legged "
        "reconciliation (generator rows <-> rendered artifacts <-> gold) closes",
        evidence,
    )


def _gate_el_independent_load(task: TaskIR, workspace: Path) -> GateResult:
    """Verify public EL bundle sufficiency through an independent load plan.

    A cross-family model sees only the public bundle and must score full reward across
    all populations, proving that each source can be located and read.
    """
    name = "el-independent-load"
    record, err = _bound_record(
        task,
        workspace,
        EL_INDEPENDENT_LOAD_EVIDENCE_REL,
        "independent-load",
        required_keys=("task_id", "task_content_hash", "status", "agreement"),
        absent_hint=(
            "the independent LOAD build was never performed (run the "
            f"{EL_INDEPENDENT_LOAD_ROLE!r} role with a cross-family provider "
            "or recorded transcripts)"
        ),
    )
    if record is None:
        detail, note_evidence = _with_producer_note(
            task, workspace, EL_EVIDENCE_NOTES_REL, str(err)
        )
        return _fail(name, detail, note_evidence)
    evidence: dict[str, str] = {
        "status": str(record.get("status")),
        "role": str(record.get("role")),
        "samples": str(len(record.get("samples") or [])),
    }
    agreement = record.get("agreement")
    if isinstance(agreement, dict):
        for pop in PopulationName:
            value = agreement.get(pop.value)
            if value is None:
                continue
            try:
                evidence[f"agreement:{pop.value}"] = f"{float(value):.6f}"
            except (TypeError, ValueError):
                evidence[f"agreement:{pop.value}"] = repr(value)
    problems = list(_dual_build_problems(record))
    if record.get("role") != EL_INDEPENDENT_LOAD_ROLE:
        problems.append(
            f"record role is {record.get('role')!r}, not "
            f"{EL_INDEPENDENT_LOAD_ROLE!r} — a TRANSFORM build must not be "
            "read as an extract-load witness"
        )
    samples = record.get("samples")
    if isinstance(samples, list) and samples:
        final = samples[-1]
        plan = final.get("load_plan") if isinstance(final, dict) else None
        if not isinstance(plan, dict) or not plan:
            problems.append(
                "final sample carries no 'load_plan' — the artifact whose "
                "sufficiency this gate exists to test"
            )
        else:
            missing = [t.name for t in task.tables if t.name not in plan]
            if missing:
                problems.append(
                    f"the emitted load plan omits table(s) {missing}: the "
                    "bundle did not let an outsider find every source"
                )
            evidence["load_plan_tables"] = str(len(plan))
    if problems:
        return _fail(
            name,
            "independent load build does not establish bundle sufficiency: "
            + "; ".join(problems),
            evidence,
        )
    return _ok(
        name,
        "a cross-family loader, shown only the EL public bundle, emitted a "
        "complete load plan that scores 1.0 under evaluate_variant("
        "EXTRACT_LOAD) on all five populations (bundle sufficiency only — this "
        "does NOT certify the gold; el-artifact-census does)",
        evidence,
    )


def _gate_el_trusted_solution(
    task: TaskIR, workspace: Path, gold: "GoldBundle"
) -> GateResult:
    """EL trusted-solution: the tautology half is DISCARDED, not reported.

    compare_stage1(gold, gold) is True by construction, and both builds share
    load_sources_duckdb, so the frozen counts are self-certified by the loader
    that produced them. Only the census + three-legged reconciliation certify.
    """
    name = "trusted-solution"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    evidence: dict[str, str] = {
        "gold_vs_gold": (
            "DISCARDED — compare_stage1(gold.stage1[pop], gold.stage1[pop]) is "
            "True by construction; reporting it as evidence is the fiction in "
            "its purest form"
        ),
        "certification": "el-artifact-census + three-legged reconciliation",
    }
    record, err = _el_census_record(task, workspace)
    if record is None:
        return _fail(
            name,
            "EL gold cannot self-certify and has no shared-loader-independent "
            "witness: " + str(err),
            evidence,
        )
    census_problems, census_evidence = _el_census_problems(
        task, workspace, gold, record
    )
    evidence.update(census_evidence)
    if census_problems:
        return _fail(
            name,
            "EL gold is not certified by the independent census: "
            + "; ".join(census_problems),
            evidence,
        )
    return _ok(
        name,
        "the frozen stage-1 counts are certified by an independently written "
        "artifact census and reconcile three ways (generator rows <-> rendered "
        "artifacts <-> gold); the gold-vs-gold tautology is discarded",
        evidence,
    )


def _live_warehouse_census_digest(
    task: TaskIR, workspace: Path, per_pop: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """(census_digest of the shipped .duckdb on disk, None) or (None, why).

    Read-only re-census through export.eltbench (imported lazily); the file must
    exist, and an uncensusable one is a failure.
    """
    rel = per_pop.get("path")
    if not isinstance(rel, str) or not rel:
        return None, "census names no warehouse artifact to re-census"
    path = task_dir(workspace, task.task_id) / rel
    if not path.is_file():
        return None, f"shipped warehouse missing at {rel} — nothing to re-census"
    try:
        from elt_taskgen.export import eltbench as eltbench_mod

        live = eltbench_mod.warehouse_census(path).get("census_digest")
    except Exception as exc:  # noqa: BLE001 — an uncensusable file is a failure
        return None, f"shipped warehouse could not be re-censused: {type(exc).__name__}: {exc}"
    if not _is_hex64(live):
        return None, f"live census produced no digest ({live!r})"
    return str(live), None


def _gate_variant_determinism(
    task: TaskIR, workspace: Path, variant: TaskVariant, gold: "GoldBundle"
) -> GateResult:
    """Bind deterministic evidence to one variant.

    Derive a per-variant witness from the run digest. Transform variants also re-census
    the live shipped warehouse instead of hashing unstable database bytes.
    """
    name = "determinism"
    base = _gate_determinism(task, workspace, gold)
    if not base.passed:
        return base
    key = (
        DETERMINISM_STAGE1_DIGEST_KEY
        if variant is TaskVariant.EXTRACT_LOAD
        else DETERMINISM_STAGE2_DIGEST_KEY
    )
    evidence = dict(base.evidence)
    digest = base.evidence.get(f"recorded:{key}")
    if not _is_hex64(digest):
        return _fail(
            name,
            f"determinism evidence carries no separable {key!r} component "
            f"(got {digest!r}): the joint run digest mixes stage-1 counts and "
            "mart CSVs into one sha256, so citing it for one variant cites "
            "evidence about the other (fail closed — reference/runner.py must "
            "record the split component digests)",
            evidence,
        )
    evidence["variant_digest_component"] = key
    if variant is TaskVariant.TRANSFORM:
        record, err = _warehouse_census_record(task, workspace)
        if record is None:
            return _fail(
                name,
                "the TRANSFORM bundle ships a warehouse the parent digest never "
                "covered, and DuckDB files are not byte-stable, so the witness "
                "must be a census digest over two builds: " + str(err),
                evidence,
            )
        populations = record.get("populations")
        if not isinstance(populations, dict):
            return _fail(name, "warehouse census 'populations' is not an object", evidence)
        mismatches: list[str] = []
        for pop in PopulationName:
            per_pop = populations.get(pop.value)
            if not isinstance(per_pop, dict):
                mismatches.append(f"{pop.value}: no warehouse census recorded")
                continue
            first = per_pop.get("census_digest")
            second = per_pop.get("rebuild_census_digest")
            if not _is_hex64(first) or not _is_hex64(second):
                mismatches.append(
                    f"{pop.value}: census digests missing or malformed "
                    f"({first!r}, {second!r}) — two independent builds are "
                    "required, one is a claim"
                )
                continue
            evidence[f"warehouse_census:{pop.value}"] = str(first)[:16]
            if first != second:
                mismatches.append(
                    f"{pop.value}: warehouse census moved between builds "
                    f"({str(first)[:16]} -> {str(second)[:16]})"
                )
                continue
            # Live leg: two agreeing builds are not proof the file on disk is
            # either of them, so the shipped warehouse is re-censused.
            live, why = _live_warehouse_census_digest(task, workspace, per_pop)
            if live is None:
                mismatches.append(f"{pop.value}: {why}")
                continue
            evidence[f"warehouse_recensus:{pop.value}"] = live[:16]
            if live != first:
                mismatches.append(
                    f"{pop.value}: shipped warehouse no longer matches its "
                    f"recorded census ({str(first)[:16]} recorded, "
                    f"{live[:16]} on disk)"
                )
        if mismatches:
            return _fail(
                name,
                "the shipped TRANSFORM warehouse is not reproducible: "
                + "; ".join(mismatches),
                evidence,
            )
        return _ok(
            name,
            "the stage-2 component of the run digest is byte-identical across "
            "clean rebuilds, and the shipped warehouse's CENSUS digest (never "
            "its .duckdb bytes) agrees across two independent builds and with "
            "a live re-census of the file on disk",
            evidence,
        )
    return _ok(
        name,
        "the stage-1 component of the run digest is byte-identical across "
        "clean rebuilds (the EL shipped surface — config.yaml, schemas/, "
        "sources/ — is plain text already covered byte-wise)",
        evidence,
    )


def _el_degenerate_probes(
    task: TaskIR, vectors: Mapping[str, dict[str, int]], pop: PopulationName
) -> dict[str, dict[str, int] | None]:
    """The load-side degenerate submissions for one population.

    The EL reward consumes ONLY actual_stage1, so each probe is a cheap pure
    function population -> counts map; nothing is materialized.
    """
    expected = vectors[pop.value]
    tables = sorted(expected)
    counts = list(expected.values())
    modal = max(sorted(set(counts)), key=lambda v: (counts.count(v), -v))

    scale_probe: dict[str, int] | None = None
    try:
        scale = dict(task.population(pop).scale or {})
    except KeyError:  # a population with no spec has no hint
        scale = {}
    if scale and all(t in scale for t in tables):
        scale_probe = {t: int(scale[t]) for t in tables}

    return {
        # D1: load nothing — every table present, zero rows.
        "empty_load": {t: 0 for t in tables},
        # D2: load the lexicographically-first table correctly, omit the rest.
        "one_table_only": {tables[0]: expected[tables[0]]} if tables else {},
        # D3: point every load step at one artifact — the probe with real teeth.
        "uniform_source": {t: modal for t in tables},
        # D4a: counts read off nothing — the round scale-hint number per table.
        "fabricate_scale_hint": scale_probe,
        # D4b: submit the PRIMARY vector while grading a non-primary population.
        "fabricate_primary_vector": dict(vectors[PopulationName.PRIMARY.value]),
    }


def _gate_el_degenerate_zero(task: TaskIR, gold: "GoldBundle") -> GateResult:
    name = "degenerate-zero"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    vectors, why = _stage1_vectors(task, gold)
    if vectors is None:
        return _fail(name, str(why))

    primary = vectors[PopulationName.PRIMARY.value]
    evidence: dict[str, str] = {}
    leaks: list[str] = []

    # A memorization probe can only be KILLED where the gold vector moves; if it
    # moves nowhere, "remember primary" IS the solution on every population.
    movers = [
        p
        for p in GRADED_POPULATIONS
        if p is not PopulationName.PRIMARY and vectors[p.value] != primary
    ]
    if not movers:
        leaks.append(
            "fabricate_primary_vector: the frozen count vector is IDENTICAL on "
            "every graded population, so submitting the primary vector without "
            "reading anything is the correct answer everywhere"
        )
    evidence["primary_vector_moves_on"] = (
        ",".join(p.value for p in movers) if movers else "NOWHERE"
    )

    for pop in GRADED_POPULATIONS:
        probes = _el_degenerate_probes(task, vectors, pop)
        for probe, counts in probes.items():
            if counts is None:
                evidence[f"{probe}:{pop.value}"] = "not constructible (no scale hint)"
                continue
            if probe == "fabricate_primary_vector" and pop not in movers:
                evidence[f"{probe}:{pop.value}"] = (
                    "not a probe here: the gold vector equals primary's, so "
                    "this submission IS the correct answer (see "
                    "primary_vector_moves_on)"
                )
                continue
            result = upstream_eval.evaluate_variant(
                TaskVariant.EXTRACT_LOAD, task, gold, pop, actual_stage1=counts
            )
            evidence[f"{probe}:{pop.value}"] = f"reward={result.reward:.6f}"
            if result.reward != 0.0:
                leaks.append(
                    f"{probe} scored {result.reward} on {pop.value} "
                    f"(submitted {_vector_text(counts)})"
                )
    if leaks:
        return _fail(
            name,
            "degenerate LOAD submissions earn reward (the EL reward floor is "
            "not pinned at 0): " + "; ".join(leaks),
            evidence,
        )
    return _ok(
        name,
        "loading nothing, loading one table, loading everything with the modal "
        "count, and fabricating counts from the scale hint or from primary all "
        "score 0 on every graded population",
        evidence,
    )


def _gate_t_degenerate_zero(task: TaskIR, gold: "GoldBundle") -> GateResult:
    """Reject transform variants that reward degenerate empty submissions.

    Run all three zero strategies on every graded population. Any reward caused by empty
    gold is a task-level failure.
    """
    name = "degenerate-zero"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    evidence: dict[str, str] = {}
    leaks: list[str] = []
    for pop in GRADED_POPULATIONS:
        strategies, why = _degenerate_mart_strategies(task, gold, pop)
        if strategies is None:
            return _fail(name, str(why))
        for strategy, marts in strategies.items():
            result = upstream_eval.evaluate_variant(
                TaskVariant.TRANSFORM, task, gold, pop, actual_marts=marts
            )
            evidence[f"{strategy}:{pop.value}"] = f"reward={result.reward:.6f}"
            if result.reward != 0.0:
                empty = [
                    m.name
                    for m in task.marts
                    if (_gold_mart_rows(gold, pop.value, m.name) or ((), [1]))[1] == []
                ]
                leaks.append(
                    f"{strategy} scored {result.reward} on {pop.value}"
                    + (f" (empty gold mart(s): {empty})" if empty else "")
                )
    if leaks:
        return _fail(
            name,
            "degenerate TRANSFORM submissions earn reward once the stage-1 mask "
            "is removed: " + "; ".join(leaks),
            evidence,
        )
    return _ok(
        name,
        "no-op, keys-only and constant submissions all score 0 under the "
        "TRANSFORM reward on every graded population (no empty-gold mart is "
        "paying credit for computing nothing)",
        evidence,
    )


def _partition_required_cases(
    task: TaskIR, admissible: frozenset[AttackKind], label: str
) -> tuple[list[AttackCase], dict[str, str]]:
    """(admissible required cases, recorded reason for every excluded one)."""
    keep: list[AttackCase] = []
    excluded: dict[str, str] = {}
    for case in task.attack_cases:
        if not case.required:
            continue
        if case.kind in admissible:
            keep.append(case)
        else:
            excluded[case.name] = (
                f"kind {case.kind.value!r} is INADMISSIBLE as {label} evidence"
            )
    return keep, excluded


#: T required kinds that are NOT semantic wrong-SQL witnesses; recorded as
#: evidence so consumers can weight a T unit certified only by these.
_T_NON_SEMANTIC_KINDS: frozenset[AttackKind] = frozenset(
    {AttackKind.CONSTANTS, AttackKind.KEYS_ONLY, AttackKind.NO_OP}
)


def _gate_variant_required_mutants(
    task: TaskIR,
    rewards: dict[str, dict[PopulationName, float]],
    variant: TaskVariant,
    workspace: Path | None = None,
) -> GateResult:
    """Verify the required mutant matrix for one variant.

    EL permits only extraction-side kinds and rejects an empty matrix. Transform
    variants use `rewards_by_variant`, not parent rewards. Mutants killed only by load
    or SQL crashes fail; without a workspace, crash checking is recorded as skipped.
    """
    name = "required-mutants"
    if variant is TaskVariant.EXTRACT_LOAD:
        admissible = EXTRACTION_MUTANT_KINDS
        label = "extract-load"
    else:
        admissible = TRANSFORM_MUTANT_KINDS | {AttackKind.NO_OP}
        label = "transform"

    if variant is TaskVariant.EXTRACT_LOAD and (admissible & TRANSFORM_MUTANT_KINDS):
        return _fail(
            name,
            "INVARIANT VIOLATED: the extract-load admissible kind set overlaps "
            f"TRANSFORM_MUTANT_KINDS ({sorted(k.value for k in admissible & TRANSFORM_MUTANT_KINDS)}) "
            "— passing EL on transform mutants is the fiction this battery "
            "exists to remove",
        )

    required, excluded = _partition_required_cases(task, admissible, label)
    evidence: dict[str, str] = {
        f"inadmissible:{case}": why for case, why in sorted(excluded.items())
    }
    if variant is TaskVariant.EXTRACT_LOAD and _UNDECLARED_EL_KIND_NAMES:
        evidence["undeclared_el_kinds"] = ",".join(_UNDECLARED_EL_KIND_NAMES)
    if not required:
        declared = sorted({c.kind.value for c in task.attack_cases if c.required})
        return _fail(
            name,
            f"task declares NO required {label} mutant — its required cases are "
            f"{declared or 'none'}, none of which this variant's reward can "
            "distinguish. A subtask whose only wrong implementations belong to "
            "the OTHER stage is not independently validated (fail closed)",
            evidence,
        )

    leaks: list[str] = []
    for case in required:
        measured = rewards.get(case.name)
        if measured is None:
            leaks.append(f"{case.name}: no rewards recorded under the {label} reward")
            continue
        evidence[case.name] = f"kind={case.kind.value};" + ",".join(
            f"{p.value}={r:.6f}"
            for p, r in sorted(measured.items(), key=lambda kv: kv[0].value)
        )
        if case.kind in STAGE1_GATED_KINDS:
            evidence[f"stage1_gated:{case.name}"] = (
                "the PARENT reward for this kind was decided by the stage-1 "
                "gate, so its parent number is NOT its variant number — it must "
                "be re-scored, never copied"
            )
        for pop, expect_full in sorted(
            case.expected_pass.items(), key=lambda kv: kv[0].value
        ):
            got = measured.get(pop)
            if got is None:
                leaks.append(f"{case.name}: no reward recorded on {pop.value}")
            elif expect_full and got != 1.0:
                leaks.append(
                    f"{case.name}: expected FULL reward on {pop.value}, got {got}"
                )
            elif not expect_full and got >= 1.0:
                leaks.append(
                    f"{case.name}: LEAK — must lose reward on {pop.value}, got {got}"
                )
    # Crash check: a 0.0 produced by an exception satisfies "loses reward"
    # numerically and proves nothing about the reward.
    crash_killed_cases: set[str] = set()
    if workspace is None:
        evidence["crash_check"] = "skipped: no workspace"
    else:
        for case in required:
            measured = rewards.get(case.name)
            if measured is None:
                continue  # already a leak above ("no rewards recorded")
            errors, err = _recorded_attack_errors(Path(workspace), task, case.name)
            if errors is None:
                leaks.append(
                    f"{case.name}: crash check impossible — {err} (a kill "
                    "whose mechanism cannot be read is not evidence)"
                )
                evidence[f"crash_check:{case.name}"] = f"unreadable: {err}"
                continue
            crashes: list[str] = []
            for pop, expect_full in sorted(
                case.expected_pass.items(), key=lambda kv: kv[0].value
            ):
                got = measured.get(pop)
                if expect_full or got is None or got >= 1.0:
                    continue
                if variant is TaskVariant.EXTRACT_LOAD:
                    load_err = errors.get(f"{pop.value}/__load__")
                    if load_err is not None:
                        crashes.append(f"{pop.value}: LOAD CRASH ({load_err[:100]})")
                        leaks.append(
                            f"{case.name}: kill on {pop.value} is by LOAD CRASH "
                            f"({load_err[:100]}) — a crashed load is not "
                            "extract-load evidence (compare_stage1 never ran on "
                            "a warehouse)"
                        )
                else:
                    for mart in task.marts:
                        sql_err = errors.get(f"{pop.value}/{mart.name}")
                        if sql_err is None:
                            continue
                        crashes.append(
                            f"{pop.value}/{mart.name}: SQL CRASH ({sql_err[:100]})"
                        )
                        crash_killed_cases.add(case.name)
                        leaks.append(
                            f"{case.name}: kill on {pop.value} is by SQL CRASH on "
                            f"{mart.name} ({sql_err[:100]}) — a mutant that does "
                            "not run is not a wrong implementation"
                        )
            evidence[f"crash_check:{case.name}"] = (
                "kills are by the reward" if not crashes else "; ".join(crashes)
            )
    if variant is TaskVariant.TRANSFORM:
        semantic = [c for c in required if c.kind not in _T_NON_SEMANTIC_KINDS]
        evidence["t_semantic_mutants"] = str(len(semantic))
        executable_semantic = [
            case
            for case in semantic
            if case.name not in crash_killed_cases
            and (measured := rewards.get(case.name)) is not None
            and any(
                pop in GRADED_POPULATIONS
                and not expect_full
                and measured.get(pop, 1.0) < 1.0
                for pop, expect_full in case.expected_pass.items()
            )
        ]
        evidence["t_executable_semantic_mutants"] = str(len(executable_semantic))
        evidence["t_executable_semantic_mutant_names"] = (
            ",".join(case.name for case in executable_semantic) or "NONE"
        )
        if not executable_semantic:
            leaks.append(
                "no executable semantic transform mutant loses reward: "
                "constants, keys-only and no-op probes test shortcuts but do "
                "not demonstrate that wrong SQL semantics are distinguished"
            )
    if leaks:
        return _fail(
            name,
            f"required {label} mutant matrix not reproduced under the {label} "
            "reward: " + "; ".join(leaks),
            evidence,
        )
    return _ok(
        name,
        f"all {len(required)} required {label} mutant(s) win and lose exactly "
        "where their expected_pass map says, measured under "
        f"evaluate_variant({variant.value.upper()}) (0 leaks; kills are by the "
        f"reward, not by crashes); "
        f"{len(excluded)} required case(s) excluded as inadmissible",
        evidence,
    )


def _gate_el_data_sensitivity(task: TaskIR, gold: "GoldBundle") -> GateResult:
    """CARDINALITY sensitivity — the only sensitivity an EL reward can have.

    A value bijection cannot move a count map, so demanding movement there would
    demand a falsehood; and primary.scale == resampled.scale is enforced
    upstream, so the memorization pair carries ZERO EL signal by construction.
    """
    name = "data-sensitivity"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    vectors, why = _stage1_vectors(task, gold)
    if vectors is None:
        return _fail(name, str(why))
    evidence: dict[str, str] = {
        pop.value: _vector_text(vectors[pop.value]) for pop in PopulationName
    }
    failures: list[str] = []

    primary = vectors[PopulationName.PRIMARY.value]
    resampled = vectors[PopulationName.RESAMPLED.value]
    try:
        same_scale = dict(task.population(PopulationName.PRIMARY).scale or {}) == dict(
            task.population(PopulationName.RESAMPLED).scale or {}
        )
    except KeyError:  # missing spec is caught by populations-load
        same_scale = False
    evidence["memorization_pair"] = (
        "invariant" if primary == resampled else "MOVED"
    )
    if same_scale and primary != resampled:
        failures.append(
            "primary and resampled declare the SAME scale (validate_population_"
            "coverage enforces it) yet their frozen count vectors differ — the "
            "generator drifted"
        )

    moved = [
        p
        for p in GRADED_POPULATIONS
        if p is not PopulationName.PRIMARY and vectors[p.value] != primary
    ]
    evidence["moves_vs_primary"] = (
        ",".join(p.value for p in moved) if moved else "NO GRADED PAIR MOVES"
    )
    if not moved:
        failures.append(
            "no graded population's count vector differs from primary's "
            "(NB: STRESS_CAP=50000 makes stress == primary when the scale hint "
            "is already at the cap, so this is measured, never assumed) — the "
            "EL answer is ONE constant vector across all five populations, i.e. "
            "a reward that reads nothing about the data"
        )
    if failures:
        return _fail(
            name,
            "the frozen stage-1 gold is not a function of the source data: "
            + "; ".join(failures),
            evidence,
        )
    return _ok(
        name,
        "the count vector is invariant across the memorization pair (as the "
        "equal declared scale forces) and MOVES on "
        f"{','.join(p.value for p in moved)}",
        evidence,
    )


def _gate_t_data_sensitivity(
    task: TaskIR, workspace: Path, gold: "GoldBundle"
) -> GateResult:
    """The parent claim, plus coherence with the artifact T actually ships.

    Two populations shipping warehouses with identical censuses but different
    gold make the T task UNSOLVABLE, and no other gate looks at the warehouse.
    """
    name = "data-sensitivity"
    base = _gate_data_sensitivity(task, workspace, gold)
    if not base.passed:
        return base
    evidence = dict(base.evidence)
    record, err = _warehouse_census_record(task, workspace)
    if record is None:
        return _fail(
            name,
            "the parent perturbation claims hold, but the artifact the T solver "
            "is HANDED was never censused: " + str(err),
            evidence,
        )
    populations = record.get("populations")
    if not isinstance(populations, dict):
        return _fail(name, "warehouse census 'populations' is not an object", evidence)
    digests: dict[str, str] = {}
    for pop in PopulationName:
        per_pop = populations.get(pop.value)
        digest = per_pop.get("census_digest") if isinstance(per_pop, dict) else None
        if not _is_hex64(digest):
            return _fail(
                name,
                f"no warehouse census digest for population {pop.value!r} — "
                "without it, 'the provided warehouse differs wherever the gold "
                "differs' cannot be checked at all",
                evidence,
            )
        digests[pop.value] = str(digest)
        evidence[f"warehouse:{pop.value}"] = str(digest)[:16]

    stage2 = gold.stage2_csv
    unsolvable: list[str] = []
    names = [p.value for p in PopulationName]

    def _gold_differs(a: str, b: str) -> bool:
        # "Different gold" is measured under THE reward, not by bytes: a
        # reward-equivalent pair is solvable by one submission. A REAL gold
        # difference still requires a census difference (unparseable = differs).
        for mart in task.marts:
            csv_a = (stage2.get(a) or {}).get(mart.name)
            csv_b = (stage2.get(b) or {}).get(mart.name)
            if csv_a is None or csv_b is None:
                if csv_a is not csv_b:
                    return True
                continue
            if not _gold_reward_equivalent(csv_a, csv_b, mart):
                return True
        return False

    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            gold_differs = _gold_differs(a, b)
            if gold_differs and digests[a] == digests[b]:
                unsolvable.append(
                    f"{a} and {b} ship warehouses with IDENTICAL censuses but "
                    "DIFFERENT gold marts — the T task is unsolvable on at "
                    "least one of them"
                )
    if unsolvable:
        return _fail(
            name,
            "the provided warehouse does not carry the difference the gold "
            "records: " + "; ".join(unsolvable),
            evidence,
        )
    return _ok(
        name,
        base.details
        + "; and every population pair whose gold differs ships a warehouse "
        "whose census differs",
        evidence,
    )


def _gate_el_info_content(task: TaskIR, gold: "GoldBundle") -> GateResult:
    """The information in an EL task is the count VECTOR, not the columns.

    compare_stage1 grades one integer per table, so if all N tables share one
    count the solver discovers ONE number and the uniform_source probe scores 1.0.
    """
    name = "info-content"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    vectors, why = _stage1_vectors(task, gold)
    if vectors is None:
        return _fail(name, str(why))
    evidence: dict[str, str] = {
        pop.value: _vector_text(vectors[pop.value]) for pop in PopulationName
    }
    trivial: list[str] = []

    primary = vectors[PopulationName.PRIMARY.value]
    distinct = sorted(set(primary.values()))
    evidence["primary_distinct_counts"] = str(len(distinct))
    if len(primary) > 1 and len(distinct) < 2:
        trivial.append(
            f"every source table has the SAME primary row count ({distinct}): "
            "the solver discovers ONE number instead of "
            f"{len(primary)}, and the uniform_source shortcut scores 1.0"
        )

    for pop in PopulationName:
        empty = sorted(t for t, n in vectors[pop.value].items() if n < 1)
        if empty:
            trivial.append(
                f"population {pop.value!r}: table(s) {empty} have ZERO expected "
                "rows — 'skip that table' is the CORRECT answer there, so they "
                "contribute no discrimination and silently weaken every "
                "skip-shaped mutant"
            )

    graded_vectors = {tuple(sorted(vectors[p.value].items())) for p in GRADED_POPULATIONS}
    evidence["distinct_graded_vectors"] = str(len(graded_vectors))
    if len(graded_vectors) < 2:
        trivial.append(
            "the count vector is CONSTANT across every graded population — one "
            "answer solves them all"
        )
    if trivial:
        return _fail(
            name,
            "the extract-load answer carries too little information: "
            + "; ".join(trivial),
            evidence,
        )
    return _ok(
        name,
        f"{len(distinct)} distinct row counts among {len(primary)} source "
        "tables on primary, every table non-empty on every population, and "
        f"{len(graded_vectors)} distinct count vectors across the graded "
        "populations",
        evidence,
    )


def _gate_t_info_content(task: TaskIR, gold: "GoldBundle") -> GateResult:
    """The parent constant-column audit, extended to every graded population.

    The parent only requires >=2 gold rows on PRIMARY — the hole the T
    degenerate probe walks through, since an empty gold mart on another
    population pays a no-op full credit under the T reward.
    """
    name = "info-content"
    base = _gate_info_content(task, gold)
    if not base.passed:
        return base
    evidence = dict(base.evidence)
    thin: list[str] = []
    for pop in GRADED_POPULATIONS:
        if pop is PopulationName.PRIMARY:
            continue
        for mart in task.marts:
            parsed = _gold_mart_rows(gold, pop.value, mart.name)
            if parsed is None:
                return _fail(
                    name,
                    f"no frozen gold for mart {mart.name!r} on {pop.value!r}",
                    evidence,
                )
            rows = parsed[1]
            evidence[f"{mart.name}:{pop.value}"] = f"{len(rows)} gold row(s)"
            if len(rows) < 2:
                thin.append(
                    f"mart {mart.name!r} on {pop.value!r}: only {len(rows)} gold "
                    "row(s)"
                    + (
                        " — an EMPTY gold mart pays a no-op FULL credit under "
                        "the transform reward (compare_mart matches empty "
                        "against empty)"
                        if not rows
                        else ""
                    )
                )
    if thin:
        return _fail(
            name,
            "graded populations carry too little stage-2 information: "
            + "; ".join(thin),
            evidence,
        )
    return _ok(
        name,
        base.details + "; and every graded population has >=2 gold rows per mart",
        evidence,
    )


def _gate_el_populations_load(
    task: TaskIR, workspace: Path, gold: "GoldBundle"
) -> GateResult:
    """The parent reconciliation, extended to the surface an EL solver READS.

    rendered/ is what an EL solver reads, so per table the resolver must resolve
    the SAME file the independent census counted (equal counts over different
    files certify nothing) and that census must equal the frozen count.
    """
    name = "populations-load"
    base = _gate_populations_load(task, workspace, gold)
    if not base.passed:
        return base
    evidence = dict(base.evidence)

    try:
        from elt_taskgen.reference.solution import find_rendered_artifact
    except Exception as exc:  # noqa: BLE001 — fail closed, never skip
        return _fail(
            name,
            f"cannot import the rendered-artifact resolver (fail closed): {exc!r}",
            evidence,
        )

    record, err = _el_census_record(task, workspace)
    if record is None:
        return _fail(
            name,
            "generated rows reconcile with the gold, but the surface an EL "
            "solver actually reads is uncertified: " + str(err),
            evidence,
        )
    populations = record.get("populations")
    if not isinstance(populations, dict):
        return _fail(name, "census 'populations' is not an object", evidence)

    tdir = task_dir(workspace, task.task_id)
    problems: list[str] = []
    for pop in PopulationName:
        rendered_dir = tdir / "populations" / pop.value / "rendered"
        per_pop = populations.get(pop.value)
        for table in task.tables:
            try:
                artifact = find_rendered_artifact(task, rendered_dir, table.name)
            except Exception as exc:  # noqa: BLE001
                problems.append(
                    f"{pop.value}/{table.name}: no resolvable rendered artifact "
                    f"({exc})"
                )
                continue
            entry = per_pop.get(table.name) if isinstance(per_pop, dict) else None
            if not isinstance(entry, dict):
                entry = {}
            counted = entry.get("records")
            census_artifact = entry.get("artifact")
            # Two INDEPENDENT discovery mechanics: equal counts over DIFFERENT
            # files certify nothing, so compare the resolved paths too.
            try:
                resolved_rel = artifact.relative_to(tdir).as_posix()
            except ValueError:
                problems.append(
                    f"{pop.value}/{table.name}: resolver returned a path "
                    f"outside the task directory ({artifact}) — cannot compare "
                    "it against the census's task-relative artifact path"
                )
                continue
            want = (gold.stage1.get(pop.value) or {}).get(table.name)
            evidence[f"rendered:{pop.value}:{table.name}"] = (
                f"{resolved_rel};census_artifact={census_artifact};"
                f"census={counted};gold={want}"
            )
            if census_artifact != resolved_rel:
                problems.append(
                    f"{pop.value}/{table.name}: the independent census counted "
                    f"{census_artifact!r} but the loader's resolver returns "
                    f"{resolved_rel!r} — the two discovery mechanics diverge, "
                    "so the census certifies a different file than the one a "
                    "solver reads"
                )
            if counted != want:
                problems.append(
                    f"{pop.value}/{table.name}: the artifact an EL solver reads "
                    f"censuses {counted!r} record(s) but the frozen gold "
                    f"expects {want!r}"
                )
    if problems:
        return _fail(
            name,
            "the rendered surface does not match the frozen stage-1 gold: "
            + "; ".join(problems),
            evidence,
        )
    return _ok(
        name,
        base.details
        + "; every table's rendered artifact resolves and censuses exactly the "
        "frozen count on all five populations",
        evidence,
    )


def _gate_canonical_reachability(task: TaskIR, workspace: Path) -> GateResult:
    """The T unit's answer key is reachable on the RLVR workspace channel, for
    every destination the task ships.

    Reads the report the validate-t runner produced (never produces it: a gate
    judges evidence). A missing or stale report is a currency refusal whose
    remedy is "re-run validate-t" (the runner rewrites it before the battery
    runs); a present, current report short of full reward on any destination is
    a judgement naming that destination and the grader's error codes.
    """
    from elt_taskgen.training import canonical

    def plain(text: str) -> str:
        # The repair router reads "re-run" anywhere in a gate's details as a
        # currency refusal; text quoted from an exception or an error code
        # must never carry it into a judgement.
        return re.sub(r"(?i)re-run", "rerun", text)

    name = "canonical-reachability"
    directory = task_dir(workspace, task.task_id)
    loaded = canonical.load_canonical_reachability(
        task, directory, directory / "answer_key"
    )
    if loaded.problem is not None:
        return _fail(
            name, f"canonical reachability evidence is not current: {loaded.problem}"
        )
    report = loaded.report
    assert report is not None
    evidence: dict[str, str] = {}
    shortfalls: list[str] = []
    for destination in sorted(report.records):
        record = report.records[destination]
        evidence[f"{destination}:artifact_sha256"] = record.artifact_sha256
        evidence[f"{destination}:seal_sha256"] = record.seal_sha256
        evidence[f"{destination}:compatibility_subset"] = record.compatibility_subset
        if record.render_error:
            shortfalls.append(
                f"{destination}: the canonical artifact could not be derived "
                f"({plain(record.render_error)})"
            )
            continue
        result = record.result
        assert result is not None
        for population in result.graded_populations:
            score = result.populations[population]
            evidence[f"{destination}:{population}:end_to_end"] = str(score.end_to_end_reward)
            evidence[f"{destination}:{population}:terraform"] = str(score.terraform_contract)
            evidence[f"{destination}:{population}:strict_raw_tables"] = str(score.strict_raw_tables)
            evidence[f"{destination}:{population}:dbt_project"] = str(score.dbt_project)
            evidence[f"{destination}:{population}:mart_reward"] = str(score.mart_reward)
            codes = getattr(score, "error_codes", ()) or ()
            if codes:
                evidence[f"{destination}:{population}:error_codes"] = ",".join(
                    sorted(str(code) for code in codes)
                )
        if not record.reachable:
            shortfalls.append(f"{destination}: {plain(canonical.shortfall_summary(result))}")
    if shortfalls:
        return _fail(
            name,
            "the canonical Terraform + dbt solution derived from the private answer "
            "key does not earn 1.0 through the RLVR workspace channel on "
            + "; ".join(shortfalls)
            + "; a solver cannot be rewarded for a mart the portable dbt subset or "
            "the Terraform intent validators refuse",
            evidence,
        )
    populations = max(
        (len(record.result.graded_populations) for record in report.records.values() if record.result),
        default=0,
    )
    return _ok(
        name,
        f"canonical solution scores 1.0 on {populations} graded populations for "
        f"{len(report.records)} destination(s): {', '.join(sorted(report.records))}",
        evidence,
    )


def _gate_t_warehouses_load(
    task: TaskIR, workspace: Path, gold: "GoldBundle"
) -> GateResult:
    """The T analogue of populations-load, over the artifact T actually ships.

    Export-time assertions are not gates and are not bound to the ledger, so a
    later export could ship an artifact nobody validated; the census digest
    recorded here is what release re-verifies the shipped bytes against.
    """
    name = "warehouses-load"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    record, err = _warehouse_census_record(task, workspace)
    if record is None:
        return _fail(name, str(err))
    populations = record.get("populations")
    if not isinstance(populations, dict):
        return _fail(name, "warehouse census 'populations' is not an object")

    expected_tables = {t.name for t in task.tables}
    evidence: dict[str, str] = {}
    failures: list[str] = []
    for pop in PopulationName:
        per_pop = populations.get(pop.value)
        if not isinstance(per_pop, dict):
            failures.append(f"{pop.value}: no warehouse census recorded")
            continue
        path = per_pop.get("path")
        digest = per_pop.get("census_digest")
        tables = per_pop.get("tables")
        if not isinstance(path, str) or not path:
            failures.append(f"{pop.value}: census names no warehouse artifact")
        if not _is_hex64(digest):
            failures.append(
                f"{pop.value}: no census digest — release has nothing to "
                "re-verify the shipped bytes against"
            )
        if not isinstance(tables, dict):
            failures.append(f"{pop.value}: census 'tables' is not an object")
            continue
        got = set(tables)
        extra = sorted(got - expected_tables)
        missing = sorted(expected_tables - got)
        if extra:
            failures.append(
                f"{pop.value}: the shipped warehouse contains relation(s) "
                f"{extra} that are not source tables — a mart, gold or "
                "answer_key relation hands the T solver the answer"
            )
        if missing:
            failures.append(f"{pop.value}: warehouse is missing table(s) {missing}")
        frozen = gold.stage1.get(pop.value) or {}
        for table in sorted(got & expected_tables):
            entry = tables[table]
            count = entry.get("row_count") if isinstance(entry, dict) else None
            want = frozen.get(table)
            evidence[f"{pop.value}:{table}"] = f"warehouse={count},gold={want}"
            if count != want:
                failures.append(
                    f"{pop.value}/{table}: warehouse holds {count!r} rows but "
                    f"the frozen stage-1 gold expects {want!r}"
                )
        evidence[f"census:{pop.value}"] = str(digest)[:16] if _is_hex64(digest) else "MISSING"
    if failures:
        return _fail(
            name,
            "the TRANSFORM starting warehouse is not the frozen stage-1 state: "
            + "; ".join(failures),
            evidence,
        )
    return _ok(
        name,
        "every population's warehouse contains exactly the task's source "
        "tables at the frozen stage-1 row counts, with a recorded census digest "
        "release can re-verify the shipped bytes against",
        evidence,
    )


def _gate_t_trusted_solution(
    task: TaskIR, workspace: Path, gold: "GoldBundle"
) -> GateResult:
    """Both halves survive for TRANSFORM, so both are measured here.

    Gold-vs-gold under the T reward is non-vacuous (a ragged or malformed gold
    fails parse_canonical_csv), and the recorded independent build IS a T
    witness because the implementer writes the TRANSFORM SQL.
    """
    name = "trusted-solution"
    problems = _gold_problems(task, gold)
    if problems:
        return _fail(name, "; ".join(problems))
    evidence: dict[str, str] = {}
    for pop in PopulationName:
        actual_marts: dict[str, list[Row]] = {}
        for mart in task.marts:
            parsed = _gold_mart_rows(gold, pop.value, mart.name)
            if parsed is None:
                return _fail(
                    name,
                    f"no frozen stage-2 gold for mart {mart.name!r} on "
                    f"population {pop.value!r}",
                    evidence,
                )
            actual_marts[mart.name] = parsed[1]
        result = upstream_eval.evaluate_variant(
            TaskVariant.TRANSFORM, task, gold, pop, actual_marts=actual_marts
        )
        evidence[f"reward:{pop.value}"] = f"{result.reward:.6f}"
        if result.reward != 1.0:
            return _fail(
                name,
                f"the frozen gold does not score 1.0 against itself under the "
                f"TRANSFORM reward on {pop.value!r} (got {result.reward}); "
                "comparator and gold disagree",
                evidence,
            )
    record, problem = _dual_build_record(task, workspace)
    if record is None:
        return _fail(name, "gold cannot self-certify: " + str(problem), evidence)
    build_problems = _dual_build_problems(record) + _transform_reconstruction_problems(
        task, record
    )
    if build_problems:
        return _fail(
            name, "gold cannot self-certify: " + "; ".join(build_problems), evidence
        )
    evidence["independent_build"] = str(record.get("status"))
    evidence["witness_scope"] = (
        "the recorded independent build certifies the TRANSFORM directly: its "
        "implementer wrote the mart SQL, while extract+load came from the "
        "trusted deterministic loader both builds share"
    )
    return _ok(
        name,
        "the frozen gold is comparator-consistent under the TRANSFORM reward on "
        "all five populations and is certified by the recorded independent "
        "build, which is a transform-variant witness in full",
        evidence,
    )


def _gate_transform_surface(task: TaskIR) -> GateResult:
    """At least one mart's plan carries an op a T solver must COMPUTE.

    Predicated on the PLAN OPS, never on MartColumn.kind (legacy tasks carry
    None) and never on prose. Refused variant-locally: a rename projection has a
    correct reward but teaches a T solver nothing, so EL and FULL still ship.
    """
    name = "transform-surface"
    evidence: dict[str, str] = {}
    surfaced: list[str] = []
    for mart in task.marts:
        plan = getattr(mart, "plan", None)
        ops = tuple(getattr(plan, "ops", ()) or ())
        kinds = sorted({str(op.kind.value) for op in ops if op.kind in TRANSFORM_SURFACE_KINDS})
        evidence[mart.name] = (
            f"surface ops: {','.join(kinds)}" if kinds else "PROJECTION ONLY (no surface op)"
        )
        if kinds:
            surfaced.append(mart.name)
    if not surfaced:
        return _fail(
            name,
            "every mart is a pure projection (source/derive/tie_break only): "
            "the TRANSFORM unit has no transform surface — a correct reward "
            "over a rename teaches a T solver nothing (variant-local; the EL "
            "variant and the FULL task are unaffected)",
            evidence,
        )
    return _ok(
        name,
        f"{len(surfaced)}/{len(task.marts)} mart(s) carry a transform-surface "
        "op (aggregate/join/filter/window/...) in their plan",
        evidence,
    )


def _gate_variant_roster(
    variant: TaskVariant, produced: tuple[GateResult, ...]
) -> GateResult:
    """Coverage is not implied by results — so it is its own gate.

    AcceptanceReport is fail-closed on gate RESULTS but says nothing about
    COVERAGE: four real gates and six waivers would report 'accepted'.
    """
    name = "variant-roster"
    declared = VARIANT_GATE_NAMES[variant]
    got = tuple(g.gate for g in produced) + (name,)
    matrix = VARIANT_GATE_MATRIX[variant]

    not_applicable = [
        c for c in matrix if c.verdict is VariantGateVerdict.NOT_APPLICABLE
    ]
    applicable_parent = [
        c
        for c in matrix
        if c.gate in GATE_NAMES and c.verdict is not VariantGateVerdict.NOT_APPLICABLE
    ]
    evidence: dict[str, str] = {
        "variant": variant.value,
        "roster": ",".join(declared),
        "applicable_gates": str(len(declared)),
        "applicable_parent_gates": f"{len(applicable_parent)}/{len(GATE_NAMES)}",
        "not_applicable_gates": str(len(not_applicable)),
    }
    for cell in matrix:
        evidence[f"verdict:{cell.gate}"] = cell.verdict.value
        if cell.verdict is VariantGateVerdict.NOT_APPLICABLE:
            evidence[f"not-applicable:{cell.gate}"] = (
                f"{cell.reason} || REPLACED BY: {', '.join(cell.replaced_by)}"
            )
        elif cell.verdict is VariantGateVerdict.NEW_FOR_VARIANT and cell.replaces:
            evidence[f"replacement:{cell.gate}"] = f"replaces {cell.replaces}"

    problems: list[str] = []
    if got != declared:
        problems.append(
            f"produced roster {list(got)} is not the declared roster "
            f"{list(declared)} — a gate absent from a report is a MISSING gate"
        )
    if len(applicable_parent) < MIN_APPLICABLE_PARENT_GATES:
        problems.append(
            f"only {len(applicable_parent)}/{len(GATE_NAMES)} parent gates "
            f"survive into this variant's roster (floor is "
            f"{MIN_APPLICABLE_PARENT_GATES}) — a WEAK variant certified mostly "
            "by waiver is a rejected variant, not a quiet one"
        )
    if problems:
        return _fail(name, "; ".join(problems), evidence)
    return _ok(
        name,
        f"{len(declared)} gates in this variant's roster; "
        f"{len(applicable_parent)}/{len(GATE_NAMES)} parent gates apply and "
        f"{len(not_applicable)} are recorded not-applicable with a named "
        "replacement",
        evidence,
    )


def _classified(variant: TaskVariant, result: GateResult) -> GateResult:
    """Stamp the failure partition onto a FAILING gate.

    Written under BOTH `failure_class` and `failure_scope` deliberately: a
    reader that trusts the evidence and one that calls classify_variant_failure
    must not disagree about whether FULL ships.
    """
    if result.passed:
        return result
    verdict = classify_variant_failure(variant, result.gate)
    return result.model_copy(
        update={
            "evidence": dict(result.evidence)
            | {"failure_class": verdict, "failure_scope": verdict}
        }
    )


def run_variant_gates(
    variant: TaskVariant,
    task: TaskIR,
    workspace: Path,
    gold: "GoldBundle",
    rewards_by_variant: Mapping[Any, Any] | None = None,
) -> AcceptanceReport:
    """Run a variant's complete gate roster against its own reward and witnesses.

    Acceptance requires every roster gate to pass under the current parent content hash.
    No parent pass is inherited, and parent changes stale every variant battery.
    """
    variant = TaskVariant(variant)
    workspace = Path(workspace)
    rewards, rewards_err = _variant_rewards(rewards_by_variant, variant)

    if variant is TaskVariant.FULL:
        return run_gates(task, workspace, gold, rewards or {})

    def _need_rewards(gate: str) -> GateResult:
        return _fail(gate, str(rewards_err))

    measured = rewards or {}

    if variant is TaskVariant.EXTRACT_LOAD:
        gates_list: list[GateResult] = [
            _guarded(
                "trusted-solution",
                lambda: _gate_el_trusted_solution(task, workspace, gold),
            ),
            _guarded(
                "determinism",
                lambda: _gate_variant_determinism(task, workspace, variant, gold),
            ),
            _guarded("degenerate-zero", lambda: _gate_el_degenerate_zero(task, gold)),
            _guarded(
                "required-mutants",
                lambda: (
                    _need_rewards("required-mutants")
                    if rewards is None
                    else _gate_variant_required_mutants(
                        task, measured, variant, workspace=workspace
                    )
                ),
            ),
            _guarded(
                "shortcut-probes",
                lambda: (
                    _need_rewards("shortcut-probes")
                    if rewards is None
                    else _gate_shortcut_probes(
                        task,
                        workspace,
                        measured,
                        kinds=EL_SHORTCUT_KINDS,
                        excluded=_EL_SHORTCUT_EXCLUSIONS,
                        variant_note="extract-load shortcut set",
                    )
                ),
            ),
            _guarded(
                "data-sensitivity", lambda: _gate_el_data_sensitivity(task, gold)
            ),
            _guarded("info-content", lambda: _gate_el_info_content(task, gold)),
            _guarded(
                "populations-load",
                lambda: _gate_el_populations_load(task, workspace, gold),
            ),
            _guarded(
                "contamination-clean",
                lambda: _gate_contamination_clean(task, workspace),
            ),
            _guarded(
                "referential-integrity",
                lambda: _gate_referential_integrity(task, workspace),
            ),
            _guarded(
                "declared-scale-reconciliation",
                lambda: _gate_declared_scale_reconciliation(task, gold),
            ),
            _guarded(
                "mart-key-unique",
                lambda: _gate_el_source_key_unique(task, workspace),
            ),
            _guarded(
                "el-artifact-census",
                lambda: _gate_el_artifact_census(task, workspace, gold),
            ),
            _guarded(
                "el-independent-load",
                lambda: _gate_el_independent_load(task, workspace),
            ),
        ]
    else:  # TRANSFORM
        gates_list = [
            _guarded(
                "trusted-solution",
                lambda: _gate_t_trusted_solution(task, workspace, gold),
            ),
            _guarded(
                "determinism",
                lambda: _gate_variant_determinism(task, workspace, variant, gold),
            ),
            _guarded("degenerate-zero", lambda: _gate_t_degenerate_zero(task, gold)),
            _guarded(
                "required-mutants",
                lambda: (
                    _need_rewards("required-mutants")
                    if rewards is None
                    else _gate_variant_required_mutants(
                        task, measured, variant, workspace=workspace
                    )
                ),
            ),
            _guarded(
                "shortcut-probes",
                lambda: (
                    _need_rewards("shortcut-probes")
                    if rewards is None
                    else _gate_shortcut_probes(
                        task,
                        workspace,
                        measured,
                        kinds=TRANSFORM_SHORTCUT_KINDS,
                        excluded=_T_SHORTCUT_EXCLUSIONS,
                        variant_note="transform shortcut set",
                    )
                ),
            ),
            _guarded(
                "data-sensitivity",
                lambda: _gate_t_data_sensitivity(task, workspace, gold),
            ),
            _guarded("info-content", lambda: _gate_t_info_content(task, gold)),
            _guarded(
                "contamination-clean",
                lambda: _gate_contamination_clean(task, workspace),
            ),
            _guarded(
                "dual-build-agreement",
                lambda: _gate_dual_build_agreement(task, workspace),
            ),
            _guarded(
                "referential-integrity",
                lambda: _gate_referential_integrity(task, workspace),
            ),
            _guarded(
                "declared-scale-reconciliation",
                lambda: _gate_declared_scale_reconciliation(task, gold),
            ),
            _guarded(
                "mart-key-unique", lambda: _gate_mart_key_unique(task, gold)
            ),
            _guarded(
                "warehouses-load",
                lambda: _gate_t_warehouses_load(task, workspace, gold),
            ),
            _guarded("transform-surface", lambda: _gate_transform_surface(task)),
            _guarded(
                "canonical-reachability",
                lambda: _gate_canonical_reachability(task, workspace),
            ),
        ]

    battery = tuple(gates_list)
    produced = battery + (
        _guarded("variant-roster", lambda: _gate_variant_roster(variant, battery)),
    )
    return AcceptanceReport.from_gates(
        task_id=variant_task_id(task.task_id, variant),
        revision=task.current_revision,
        task_content_hash=task.content_hash(),
        gates=tuple(_classified(variant, g) for g in produced),
        scorer_version=SCORER_VERSION,
        roster_digest=ROSTER_DIGEST,
        roster=VARIANT_GATE_NAMES[variant],
    )


def run_all_variant_gates(
    task: TaskIR,
    workspace: Path,
    gold: "GoldBundle",
    rewards_by_variant: Mapping[Any, Any] | None = None,
) -> dict[TaskVariant, AcceptanceReport]:
    """The two active unit reports, bound to one parent content hash.

    FULL stays available through ``run_gates`` but is never returned as an RLVR
    task unit here.
    """
    return {
        variant: run_variant_gates(variant, task, workspace, gold, rewards_by_variant)
        for variant in RLVR_TASK_VARIANTS
    }

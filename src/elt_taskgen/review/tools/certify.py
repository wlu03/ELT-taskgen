"""Certify repair edits on disposable nested copies.

The tool runs configured provider-free stages and never mutates the held trial. An
optional `attack` member is admitted only when its review evidence can be memo-served
without changed critic views. Stage failures return closed diagnostic codes;
infrastructure or unmeasurable failures halt, while edit-caused resource exhaustion on
touched stages is a paid refusal. Workers are terminated before their copies are removed
on deadline or memory failure.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import multiprocessing
import os
import pickle
import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from types import MappingProxyType
from typing import Any

from elt_taskgen.models import RepairRoute
from elt_taskgen.review.session import (
    SandboxFault,
    SessionFault,
    ToolDeadlineExceeded,
    ToolHarnessFault,
)
from elt_taskgen.review.tools.projection import (
    CERTIFY_ATTACK_CODES,
    CERTIFY_CODES,
    Diagnostic,
    project_certify,
    project_certify_attack,
)

__all__ = [
    "ATTACK_NO_COST_REFUSAL_CODES",
    "ATTACK_ROUTES",
    "CERTIFY_ATTACK_CODES",
    "CERTIFY_ATTACK_DEADLINE_S",
    "CERTIFY_ATTACK_ORACLE_BITS",
    "CERTIFY_ATTACK_STAGE",
    "CERTIFY_CODES",
    "CERTIFY_CANCEL_GRACE_S",
    "CERTIFY_DEADLINE_S",
    "CERTIFY_MEMORY_LIMIT_MB",
    "CERTIFY_ORACLE_BITS",
    "CERTIFY_WORKER_KINDS",
    "CERTIFY_WORKER_PROCESS",
    "CERTIFY_WORKER_RSS_LIMIT_MB",
    "CERTIFY_WORKER_THREAD",
    "CODE_GREEN",
    "CODE_RED_ATTACK",
    "CODE_REFUSED_NO_PROVIDER_FREE_STAGE",
    "CODE_REFUSED_RESOURCE_BUDGET",
    "CODE_REFUSED_REVIEW_VIEW_CHANGED",
    "CODE_REFUSED_UNCHANGED_TRIAL",
    "COULD_NOT_MEASURE_EXCEPTION_NAMES",
    "CertifyReceipt",
    "EDIT_TOUCHED_STAGES",
    "MAX_CERTIFY_PER_SESSION",
    "NO_COST_REFUSAL_CODES",
    "NO_STAGE",
    "PROVIDER_FREE_STAGES",
    "RESOURCE_BUDGET_EXCEPTION_NAMES",
    "TOOL_NAME",
    "assert_no_provider_handle",
    "attack_member_context",
    "certify_disposable_copy",
    "classify_runner_exception",
    "critic_views_changed",
    "discrimination_baseline",
    "edit_touched_stages",
    "memo_served_review",
    "model_stages_deferred",
    "provider_free_stage_runners",
    "provider_free_stages",
    "provider_handles_in",
    "resolve_worker_kind",
    "resource_budget_exhausted_by_edit",
    "run_provider_free_stages",
    "runner_exception_could_not_measure",
    "runner_exception_exhausted_resource_budget",
]

TOOL_NAME = "certify"

#: The stages an in-session certify may run in Phase 1 (certify addendum
#: §3.1): the two wired runners that reference no provider. Phase 3 appends
#: `attack` behind `repair.certify.attack_enabled`, after the F1 currency fix.
_PROVIDER_FREE_STAGES: tuple[str, ...] = ("generate", "reference")
PROVIDER_FREE_STAGES = _PROVIDER_FREE_STAGES

#: The Phase 3 member (`repair.certify.attack_enabled`): `cli.run_attack_stage`,
#: provider-free by construction, admitted on `ATTACK_ROUTES` only and only
#: when the review it demands can be memo-served (module docstring).
CERTIFY_ATTACK_STAGE = "attack"
ATTACK_ROUTES: tuple[RepairRoute, ...] = (RepairRoute.POPULATION, RepairRoute.REFERENCE)

#: Empty `on_stage` closes the active stage window, keeping harness bookkeeping
#: outside stage deadlines and receipts.
NO_STAGE = ""

#: Harness-side worker deadline per call (Phase 1: `generate` ≈ 0.3 s +
#: `reference` 25–31 s on the demo; 1200 s once `attack` is a member).
CERTIFY_DEADLINE_S = 300.0
#: The deadline of a call whose stage set may hold `attack` (certify addendum
#: §3.1 "Deadline": 25–31 s reference + ≤ 565 s attack on the demo, F3a).
CERTIFY_ATTACK_DEADLINE_S = 1200.0

#: Oracle bits one executed call charges (`ok` + which of two stages);
#: `max_oracle_bits` 4 for the role admits two calls.
CERTIFY_ORACLE_BITS = 2
#: Bits per executed call once `attack` is a member (S2 §3.6's price; certify
#: addendum §3.1 "Oracle bits": `max_oracle_bits` 12 admits two calls).
CERTIFY_ATTACK_ORACLE_BITS = 6

#: `max_certify` (SoT T1 RPR row): executed calls per session.
MAX_CERTIFY_PER_SESSION = 2

#: DuckDB memory limit of the gold connections a certify worker opens (the
#: 0.A envelope of the other untrusted-SQL sites); applied through the
#: worker's interruptible scope, never to the live reference runner.
CERTIFY_MEMORY_LIMIT_MB = 512

#: Real-time grace the supervisor gives an interrupted in-process worker
#: thread to unwind before handing its copy to the reaper.
CERTIFY_CANCEL_GRACE_S = 5.0

#: The two worker shapes of `certify_disposable_copy`: the spawned,
#: process-group-killed, RSS-watched child (production; the runner dict is
#: built inside it) and the in-process cancellable thread (an injected
#: runner dict: a fixture's spies cannot cross a process boundary).
CERTIFY_WORKER_PROCESS = "process"
CERTIFY_WORKER_THREAD = "thread"
CERTIFY_WORKER_KINDS: tuple[str, ...] = (CERTIFY_WORKER_PROCESS, CERTIFY_WORKER_THREAD)

#: Whole-worker OS envelope of the spawned certify child (rlimits inside,
#: the parent's RSS watchdog outside): the semantic scorer's
#: `SemanticLimits().worker_rss_limit_mb`, pinned equal by test.
CERTIFY_WORKER_RSS_LIMIT_MB = 2048

#: Real time the supervisor waits for a SIGKILLed worker group to end
#: before handing its copy to the reaper (never torn down under it).
_KILL_GRACE_S = 10.0

#: Cadence of the parent's RSS watchdog over the spawned worker.
_WATCHDOG_INTERVAL_S = 0.25

CODE_GREEN = "certify_green"
CODE_REFUSED_NO_PROVIDER_FREE_STAGE = "certify_refused_no_provider_free_stage"
CODE_REFUSED_UNCHANGED_TRIAL = "certify_refused_unchanged_trial"
#: The PAID outcome of a resource-cap exhaustion the edit itself caused on
#: a stage it feeds (module docstring "RESOURCE-CAP EXHAUSTION"): charged
#: like an executed call (it is one), counted toward `max_certify`, NEVER in
#: `NO_COST_REFUSAL_CODES` and never a harness fault.
CODE_REFUSED_RESOURCE_BUDGET = "certify_refused_resource_budget"

#: Match worker memory/deadline exhaustion before generic measurement failures.
#: Charge it to the edit only on touched stages; otherwise halt as infrastructure.
RESOURCE_BUDGET_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {"OutOfMemoryException", "InterruptException"}
)

#: Reference edits touch reference and attack; population edits touch generate
#: and downstream stages. Specification certification is refused, and runtime
#: or fatal routes have no session.
EDIT_TOUCHED_STAGES: Mapping[RepairRoute, tuple[str, ...]] = MappingProxyType(
    {
        RepairRoute.SPECIFICATION: (),
        RepairRoute.REFERENCE: ("reference", CERTIFY_ATTACK_STAGE),
        RepairRoute.POPULATION: ("generate", "reference", CERTIFY_ATTACK_STAGE),
        RepairRoute.RUNTIME: (),
        RepairRoute.FATAL: (),
    }
)


def edit_touched_stages(route: RepairRoute | str) -> tuple[str, ...]:
    """`EDIT_TOUCHED_STAGES[route]`: the stages a session on `route` feeds
    with its edit, in pipeline order."""
    return tuple(EDIT_TOUCHED_STAGES[RepairRoute(route)])
#: Phase 3 members (`projection.CERTIFY_ATTACK_CODES`), live under the flag.
CODE_RED_ATTACK = "certify_red_attack"
CODE_REFUSED_REVIEW_VIEW_CHANGED = "certify_refused_review_view_changed"

#: The refusals `certify` answers at PERMIT at NO cost (certify addendum
#: §3.1 "Refusals": 0 bits, no worker spawned, no executed call spent; the
#: call still counts toward `max_tool_calls`). Declared on the tool's cost
#: (`ToolCost.no_cost_codes`) so the runner never charges oracle bits for
#: them; `CertifyTool.permit` returns one of these before dispatch.
NO_COST_REFUSAL_CODES: frozenset[str] = frozenset(
    {CODE_REFUSED_NO_PROVIDER_FREE_STAGE, CODE_REFUSED_UNCHANGED_TRIAL}
)
#: The no-cost refusals of a certify whose stage set may hold `attack`: the
#: Phase 1 two plus refusal (3) of the certify addendum — `attack` requested
#: while a critic view moved at the new hash (declared on the flagged tool's
#: cost only, so the Phase 1 tool's declaration is byte-identical).
ATTACK_NO_COST_REFUSAL_CODES: frozenset[str] = NO_COST_REFUSAL_CODES | frozenset(
    {CODE_REFUSED_REVIEW_VIEW_CHANGED}
)

#: Every code a certify worker may answer with (the Phase 1 vocabulary plus
#: the flag-gated members); anything else is `non_projection_result`.
_RESULT_CODES: frozenset[str] = frozenset(CERTIFY_CODES) | frozenset(CERTIFY_ATTACK_CODES)

#: JSON-only attack context: memoized review, discrimination baseline, guard
#: state, and whether literal-row deletion rules apply. Safe across workers.
ATTACK_CONTEXT_KEYS: tuple[str, ...] = ("review_payload", "matrix_before", "rows_before", "guard")
ATTACK_CONTEXT_ROWS_KEY = "rows_guard"

_POLL_S = 0.02


# ---------------------------------------------------------------------------
# Stage selection
# ---------------------------------------------------------------------------

def _rerun_set(
    route: RepairRoute, failed_stage: str, *, literal_rows_moved: bool
) -> tuple[str, ...]:
    """The route's truncated rerun set plus, when literal rows moved, the
    proof stages a literal-rows patch must re-run whatever the failed stage
    (`repair_proposer.LITERAL_ROWS_PROOF_STAGES`, exactly as `trial_phase`
    adds them), in pipeline order."""
    from elt_taskgen.engine import StageName
    from elt_taskgen.review.repair_proposer import (  # lazy: rp imports the projection
        LITERAL_ROWS_PROOF_STAGES,
        revalidation_stages,
    )

    stages = list(revalidation_stages(route, str(failed_stage)))
    if literal_rows_moved:
        for stage in LITERAL_ROWS_PROOF_STAGES:
            if stage not in stages:
                stages.append(stage)
    order = [s.value for s in StageName]
    return tuple(sorted(stages, key=order.index))


def _members(route: RepairRoute, *, attack_enabled: bool) -> tuple[str, ...]:
    """The stages an in-session certify may run on `route`: the two Phase 1
    members, plus `attack` on `ATTACK_ROUTES` under the flag."""
    if attack_enabled and route in ATTACK_ROUTES:
        return _PROVIDER_FREE_STAGES + (CERTIFY_ATTACK_STAGE,)
    return _PROVIDER_FREE_STAGES


def provider_free_stages(
    route: RepairRoute | str,
    failed_stage: str,
    *,
    attack_enabled: bool = False,
    literal_rows_moved: bool = False,
) -> tuple[str, ...]:
    """Return the provider-free suffix of a route's revalidation stages.

    Preserve pipeline order. Routes whose remaining stages all require providers return
    an empty tuple and are refused without cost.
    """
    route_v = RepairRoute(route)
    if route_v in (RepairRoute.RUNTIME, RepairRoute.FATAL):
        return ()
    members = _members(route_v, attack_enabled=bool(attack_enabled))
    return tuple(
        stage
        for stage in _rerun_set(route_v, failed_stage, literal_rows_moved=bool(literal_rows_moved))
        if stage in members
    )


def model_stages_deferred(
    route: RepairRoute | str,
    failed_stage: str,
    *,
    attack_enabled: bool = False,
    literal_rows_moved: bool = False,
) -> bool:
    """Does the route's truncated rerun set hold a member `certify` leaves to
    the submit-time certifier (any stage outside the members `attack_enabled`
    admits — `_PROVIDER_FREE_STAGES`, plus `attack` under the flag)?"""
    route_v = RepairRoute(route)
    if route_v in (RepairRoute.RUNTIME, RepairRoute.FATAL):
        return False
    members = _members(route_v, attack_enabled=bool(attack_enabled))
    return any(
        stage not in members
        for stage in _rerun_set(route_v, failed_stage, literal_rows_moved=bool(literal_rows_moved))
    )


# ---------------------------------------------------------------------------
# The runner dict, and the proof that no provider hides in it
# ---------------------------------------------------------------------------

def provider_free_stage_runners(*, attack_enabled: bool = False) -> dict[str, Callable[..., Any]]:
    """`{generate: run_generate, reference: _with_execution_effect_filter(
    run_reference_stage)}` — built from the CLI's stage functions with no
    provider argument anywhere, exactly as `build_stage_runners` wires those
    two "before any provider call"; plus `attack: run_attack_stage` (wired
    the same way, no provider) under `attack_enabled`. Checked by
    `assert_no_provider_handle`."""
    from elt_taskgen import cli as cli_mod  # lazy: the CLI wires this package, not the reverse

    runners: dict[str, Callable[..., Any]] = {
        "generate": cli_mod.run_generate,
        "reference": cli_mod._with_execution_effect_filter(cli_mod.run_reference_stage),
    }
    if attack_enabled:
        runners[CERTIFY_ATTACK_STAGE] = cli_mod.run_attack_stage
    assert_no_provider_handle(runners)
    return runners


# ---------------------------------------------------------------------------
# The Phase 3 `attack` member: the memo-served review and the guard's baseline
# ---------------------------------------------------------------------------

def critic_views_changed(live_task: Any, trial_task: Any) -> tuple[str, ...]:
    """The critic seats whose rendered view (`council.render_view`, the one
    renderer `metrology.view_digest` hashes) differs between the live task
    and the edited trial's task — `view_digest_at(new_hash) !=
    view_digest_live` per seat (certify addendum §3.1 "Refusals" (3)).
    Empty when the review at the new hash would be memo-served for all four
    seats; a POPULATION `conditions` patch names the adversary (F3b)."""
    from elt_taskgen.review.council import CRITIC_ROLES, render_view

    return tuple(
        role.value
        for role in CRITIC_ROLES
        if render_view(role, live_task) != render_view(role, trial_task)
    )


#: The stage whose PASS row the real `attack` runner demands at the current
#: hash (`cli._require_pass_payload`; `repair_proposer.CURRENCY_PREREQUISITES`).
CERTIFY_ATTACK_STAGE_PREREQUISITE = "review"


def memo_served_review(workspace: Path, task: Any) -> dict[str, Any] | None:
    """The live ledger's `review` PASS payload at the LIVE hash — the findings
    the memo-served review at the new hash would reproduce byte for byte when
    no critic view moved — or None when the live ledger holds no such row
    (the review has not passed at this hash; the member is then left to the
    submit-time certifier)."""
    import json as _json

    from elt_taskgen.engine import VERDICT_PASS, Engine

    engine = Engine(Path(workspace), max_repair_rounds=0)
    try:
        row = engine.latest_report(str(task.task_id), CERTIFY_ATTACK_STAGE_PREREQUISITE)
    finally:
        engine.close()
    if row is None or row.verdict != VERDICT_PASS or row.content_hash != task.content_hash():
        return None
    try:
        payload = _json.loads(row.payload_json)
    except (TypeError, ValueError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def discrimination_baseline(workspace: Path, task: Any) -> dict[str, Any]:
    """The live discrimination matrix at the live hash and the counterfactual
    row counts (`repair.discrimination_matrix`, `repair.counterfactual_row_counts`)
    as plain JSON data: what the guard compares the copy's re-measurement
    against, measured BEFORE any stage re-runs, off the LIVE workspace —
    exactly `trial_phase`'s baseline."""
    from elt_taskgen import repair as repair_mod

    matrix = repair_mod.discrimination_matrix(
        Path(workspace), str(task.task_id), task_content_hash=task.content_hash()
    )
    return {
        "matrix": {str(case): sorted(pops) for case, pops in sorted(matrix.items())},
        "rows": {str(table): int(n) for table, n in sorted(repair_mod.counterfactual_row_counts(task).items())},
    }


def attack_member_context(
    workspace: Path, task: Any, *, literal_rows_moved: bool, population_moved: bool = False
) -> dict[str, Any] | None:
    """Build plain data needed to run the optional attack member on a copy.

    Include memo-served review evidence, baseline discrimination, and guard state only
    after unchanged critic views make the member admissible.
    """
    from elt_taskgen.review.repair_proposer import (  # lazy: rp imports the projection
        discrimination_guard_armed,
    )

    review = memo_served_review(workspace, task)
    if review is None:
        return None
    baseline = discrimination_baseline(workspace, task)
    armed = discrimination_guard_armed(
        literal_rows_moved=bool(literal_rows_moved),
        population_moved=bool(population_moved) or bool(literal_rows_moved),
        matrix_before=baseline["matrix"],
    )
    return {
        "review_payload": review,
        "matrix_before": baseline["matrix"],
        "rows_before": baseline["rows"],
        "guard": armed,
        ATTACK_CONTEXT_ROWS_KEY: bool(literal_rows_moved),
    }


def _attack_context(attack: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The validated `attack` member context, or None (no member)."""
    if attack is None:
        return None
    if not isinstance(attack, Mapping) or not all(key in attack for key in ATTACK_CONTEXT_KEYS):
        raise ToolHarnessFault(TOOL_NAME, code="attack_context_invalid")
    review = attack["review_payload"]
    matrix = attack["matrix_before"]
    rows = attack["rows_before"]
    if not isinstance(review, Mapping) or not isinstance(matrix, Mapping) or not isinstance(rows, Mapping):
        raise ToolHarnessFault(TOOL_NAME, code="attack_context_invalid")
    return {
        "review_payload": dict(review),
        "matrix_before": {str(case): frozenset(str(p) for p in pops) for case, pops in matrix.items()},
        "rows_before": {str(table): int(n) for table, n in rows.items()},
        "guard": bool(attack["guard"]),
        # A context written before the key existed armed the guard by
        # literal rows alone, so the deletion rule applied whenever armed.
        ATTACK_CONTEXT_ROWS_KEY: bool(attack.get(ATTACK_CONTEXT_ROWS_KEY, attack["guard"])),
    }


def _record_memo_served_review(engine: Any, task: Any, payload: Mapping[str, Any]) -> None:
    """The memo-served `review` PASS row on the COPY's ledger at the copy's
    hash (F1: the row the real `attack` runner reads), carrying the live
    findings byte for byte."""
    from elt_taskgen.cli import ReviewPayload  # lazy: the CLI wires this package, not the reverse
    from elt_taskgen.engine import VERDICT_PASS

    engine.record_report(
        task, CERTIFY_ATTACK_STAGE_PREREQUISITE, VERDICT_PASS, ReviewPayload.model_validate(dict(payload))
    )


def _discrimination_weakened(copy: Path, task: Any, context: Mapping[str, Any]) -> bool:
    """The guard, re-measured on the copy after its `attack` member: is the
    copy's matrix at the copy's hash NOT at least as strong as the live
    baseline, or did a counterfactual row vanish (`repair.discrimination_problems`,
    `repair.counterfactual_row_problems` — `trial_phase`'s exact test)?"""
    from elt_taskgen import repair as repair_mod

    matrix_after = repair_mod.discrimination_matrix(
        Path(copy), str(task.task_id), task_content_hash=task.content_hash()
    )
    problems = repair_mod.discrimination_problems(dict(context["matrix_before"]), matrix_after)
    if bool(context.get(ATTACK_CONTEXT_ROWS_KEY, True)):
        # The deletion-count rule stays literal-rows-specific (`trial_phase`).
        problems += repair_mod.counterfactual_row_problems(
            dict(context["rows_before"]), repair_mod.counterfactual_row_counts(task)
        )
    return bool(problems)


def _looks_like_provider(obj: Any) -> bool:
    """A provider-shaped object: anything exposing a callable `complete`
    (the one-shot seam) or `_turn` / `run_session` (the session seam)."""
    if obj is None or isinstance(obj, (str, bytes, int, float, bool, Path, tuple, list, dict, set, frozenset)):
        return False
    if inspect.isroutine(obj) or inspect.isclass(obj) or inspect.ismodule(obj):
        return False
    return any(callable(getattr(obj, attr, None)) for attr in ("complete", "_turn", "run_session"))


def provider_handles_in(runner: Any) -> tuple[str, ...]:
    """The closure cells, partial arguments and bound instances through which
    `runner` (recursively through `__wrapped__` and nested closures) reaches
    a provider-shaped object; empty when it reaches none."""
    found: list[str] = []
    seen: set[int] = set()
    stack: list[tuple[str, Any]] = [("", runner)]
    while stack:
        label, fn = stack.pop()
        if fn is None or id(fn) in seen:
            continue
        seen.add(id(fn))
        if isinstance(fn, functools.partial):
            for index, value in enumerate(fn.args):
                if _looks_like_provider(value):
                    found.append(f"{label}partial[{index}]")
            for key, value in fn.keywords.items():
                if _looks_like_provider(value):
                    found.append(f"{label}partial[{key}]")
            stack.append((f"{label}partial.", fn.func))
            continue
        if inspect.ismethod(fn):
            owner = fn.__self__
            if _looks_like_provider(owner):
                found.append(f"{label}__self__")
            else:
                for key, value in vars(owner).items() if hasattr(owner, "__dict__") else ():
                    if _looks_like_provider(value):
                        found.append(f"{label}__self__.{key}")
            stack.append((f"{label}__func__.", fn.__func__))
            continue
        code = getattr(fn, "__code__", None)
        closure = getattr(fn, "__closure__", None)
        if code is not None and closure:
            for name, cell in zip(code.co_freevars, closure):
                try:
                    value = cell.cell_contents
                except ValueError:  # an empty cell
                    continue
                if _looks_like_provider(value):
                    found.append(f"{label}{name}")
                elif inspect.isroutine(value) or isinstance(value, functools.partial):
                    stack.append((f"{label}{name}.", value))
        wrapped = getattr(fn, "__wrapped__", None)
        if wrapped is not None:
            stack.append((f"{label}__wrapped__.", wrapped))
    return tuple(found)


def assert_no_provider_handle(runners: Mapping[str, Any]) -> None:
    """Refuse, as a HARNESS fault, a runner dict any member of which reaches
    a provider: the certify worker is provider-free by construction (S2 §1;
    certify addendum §3.1), so a provider in it is a wiring defect, never
    something to run."""
    offending = {
        str(stage): provider_handles_in(runner)
        for stage, runner in runners.items()
        if provider_handles_in(runner)
    }
    if offending:
        fault = ToolHarnessFault(TOOL_NAME, code="provider_handle_in_worker")
        fault.stages = tuple(sorted(offending))  # type: ignore[attr-defined]
        raise fault


# Run stages on the worker copy. OS, storage, and engine faults are
# infrastructure; edit-caused parser or semantic defects are scored failures.
COULD_NOT_MEASURE_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "MemoryError",
        "OSError",
        "OperationalError",
        "FatalException",
        "InternalError",
        "InterruptException",
        "EngineError",
    }
)


def runner_exception_could_not_measure(exc: BaseException) -> bool:
    """Is `exc` a COULD-NOT-MEASURE fault of a stage runner — a `SessionFault`,
    a class the engine names infrastructure (`_INFRA_EXCEPTION_NAMES`, the
    transport message prefixes, an `InfrastructureFailure` marker, walked
    through `__cause__` / `__context__` by `engine._infra_marker_for`) or one
    of `COULD_NOT_MEASURE_EXCEPTION_NAMES`? False for every other exception:
    a defect the model's edit produced, which the live ladder scores."""
    from elt_taskgen.engine import _infra_marker_for

    if isinstance(exc, SessionFault):
        return True
    if _infra_marker_for(exc):
        return True
    return any(cls.__name__ in COULD_NOT_MEASURE_EXCEPTION_NAMES for cls in type(exc).__mro__)


def runner_exception_exhausted_resource_budget(exc: BaseException) -> bool:
    """Is `exc` (by class name through its MRO — never the chain: a wrapped
    transport fault is that fault) one of `RESOURCE_BUDGET_EXCEPTION_NAMES`:
    DuckDB out of memory under the worker's limit, or a query the
    supervisor interrupted?"""
    return any(cls.__name__ in RESOURCE_BUDGET_EXCEPTION_NAMES for cls in type(exc).__mro__)


def resource_budget_exhausted_by_edit(
    exc: BaseException, *, stage: str, touched_stages: Sequence[str]
) -> bool:
    """The PAID rule (module docstring "RESOURCE-CAP EXHAUSTION"): `exc` is
    a resource-budget class AND `stage` is one the edit touched. A
    `SessionFault` or an exception the engine already names infrastructure
    (chain-walked, `_infra_marker_for`) is never the model's, whatever the
    stage; a resource-budget class on an UNTOUCHED stage is not either (it
    then falls through to `classify_runner_exception`, a harness fault)."""
    from elt_taskgen.engine import _infra_marker_for

    if isinstance(exc, SessionFault) or _infra_marker_for(exc):
        return False
    if str(stage) not in tuple(str(s) for s in touched_stages):
        return False
    return runner_exception_exhausted_resource_budget(exc)


def classify_runner_exception(exc: BaseException) -> BaseException | None:
    """Classify an exception raised by a certification stage runner.

    Known session or engine infrastructure faults propagate; known inability-to-measure
    failures become text-free `ToolHarnessFault`s. Other exceptions are treated as
    edit-caused red stage outcomes, so the model cannot trigger a free halt by breaking
    edited bytes.
    """
    from elt_taskgen.engine import _infra_marker_for

    if isinstance(exc, SessionFault):
        return exc
    if _infra_marker_for(exc):
        return exc
    if runner_exception_could_not_measure(exc):
        return ToolHarnessFault.from_exception(TOOL_NAME, exc)
    return None


def run_provider_free_stages(
    copy: Path,
    task_id: str,
    stages: tuple[str, ...],
    *,
    runners: Mapping[str, Callable[..., Any]] | None = None,
    model_stages_deferred: bool = False,
    attack: Mapping[str, Any] | None = None,
    touched_stages: Sequence[str] = (),
    on_stage: Callable[[str], None] | None = None,
) -> Diagnostic:
    """Run selected provider-free stages on a disposable copy and project the result.

    Record each returned outcome on the copy and stop at the first non-pass result.
    Infrastructure, unmeasurable, or blocked outcomes halt; edit-caused resource
    exhaustion on a touched stage becomes a paid refusal. An empty or unsupported stage
    set is refused. The optional attack member installs memo-served review evidence and
    checks discrimination against the live baseline.
    """
    from elt_taskgen.engine import (
        _VERDICTS,
        VERDICT_BLOCKED,
        VERDICT_PASS,
        Engine,
        _infrastructure_failure,
        _marker_text,
        blocked_on_of,
        blocked_stage_marker,
    )

    stages = tuple(str(stage) for stage in stages)
    context = _attack_context(attack)
    project = project_certify if context is None else project_certify_attack
    if not stages:
        return project(CODE_REFUSED_NO_PROVIDER_FREE_STAGE, model_stages_deferred=True)
    members = _PROVIDER_FREE_STAGES + ((CERTIFY_ATTACK_STAGE,) if context is not None else ())
    outside = [stage for stage in stages if stage not in members]
    if outside:
        raise ToolHarnessFault(TOOL_NAME, code="stage_not_provider_free")
    runner_map = (
        provider_free_stage_runners(attack_enabled=context is not None)
        if runners is None
        else dict(runners)
    )
    assert_no_provider_handle(runner_map)

    touched = tuple(str(stage) for stage in touched_stages)
    engine = Engine(Path(copy), max_repair_rounds=0)
    try:
        task = engine.load_task(task_id)  # re-validates the edited IR
        weakened = False
        for stage in stages:
            runner = runner_map.get(stage)
            if runner is None:
                raise ToolHarnessFault(TOOL_NAME, code="stage_unwired")
            if stage == CERTIFY_ATTACK_STAGE and context is not None:
                _record_memo_served_review(engine, task, context["review_payload"])
            if on_stage is not None:
                on_stage(stage)
            try:
                outcome = runner(engine, task)
            except Exception as exc:  # noqa: BLE001 - classified by CLASS (finding 1-0)
                if resource_budget_exhausted_by_edit(exc, stage=stage, touched_stages=touched):
                    # The worker's own envelope, exhausted by the bytes the
                    # model wrote on a stage those bytes feed: the model's
                    # doing, scored and paid (the bits were charged at
                    # spawn), never a free halt. The exception's text (a
                    # byte count, a path) stays here.
                    return project(
                        CODE_REFUSED_RESOURCE_BUDGET, model_stages_deferred=model_stages_deferred
                    )
                fault = classify_runner_exception(exc)
                if fault is None:
                    # Model-caused defects score red like the live ladder, never
                    # as a free halt. Raw exception text remains internal.
                    return project(
                        f"certify_red_{stage}", model_stages_deferred=model_stages_deferred
                    )
                if fault is exc:
                    raise
                raise fault from exc
            # THE WINDOW CLOSES HERE. Everything below is harness
            # bookkeeping on the copy, not the model's stage: a deadline
            # landing in it is the harness fault it always was.
            if on_stage is not None:
                on_stage(NO_STAGE)
            new_task = getattr(outcome, "task", None)
            if new_task is not None:
                task = new_task
                engine.save_task(task)
            verdict = getattr(outcome, "verdict", None)
            if verdict in _VERDICTS:
                engine.record_report(task, stage, verdict, outcome.payload)
            if verdict == VERDICT_BLOCKED:
                # A waiting stage is a resumable no-measure infrastructure halt,
                # never red feedback about the model's edit.
                reason = blocked_on_of(getattr(outcome, "payload", None))
                fault = ToolHarnessFault(
                    TOOL_NAME,
                    code="stage_could_not_measure",
                    cause_type=blocked_stage_marker(reason),
                )
                fault.blocked_on = reason or "unknown"  # type: ignore[attr-defined]
                raise fault
            if verdict != VERDICT_PASS:
                infra = _marker_text(
                    getattr(outcome, "infrastructure", "")
                    or _infrastructure_failure(getattr(outcome, "payload", None))
                )
                if infra:
                    # The stage could not measure (transport, admission, a
                    # sandbox death recorded as a payload): not evidence
                    # about the edit, so a harness fault, never red.
                    raise ToolHarnessFault(
                        TOOL_NAME, code="stage_could_not_measure", cause_type=str(infra)
                    )
                return project(
                    f"certify_red_{stage}", model_stages_deferred=model_stages_deferred
                )
            if stage == CERTIFY_ATTACK_STAGE and context is not None and context["guard"]:
                weakened = _discrimination_weakened(Path(copy), task, context)
        if context is not None:
            return project_certify_attack(
                CODE_GREEN,
                model_stages_deferred=model_stages_deferred,
                discrimination_weakened=weakened,
            )
        return project_certify(CODE_GREEN, model_stages_deferred=model_stages_deferred)
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# The supervised call on a disposable copy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CertifyReceipt:
    """Record one executed certification result for session evidence.

    The receipt stores the projected diagnostic, full digest, stages, freshness, and
    resource state; it is retained by the harness and never sent directly to the model.
    """

    diagnostic: Diagnostic
    stages: tuple[str, ...]
    copy_path: Path
    elapsed_s: float
    worker: str = CERTIFY_WORKER_PROCESS
    resource_stage: str = ""
    worker_handle: Any = field(default=None, compare=False, repr=False)


def resolve_worker_kind(worker: str | None, runners: Mapping[str, Any] | None) -> str:
    """The worker shape of one call: an explicit kind, else the spawned
    process for the production runner dict (built inside the worker) and
    the in-process thread for an injected one (the caller's own objects)."""
    if worker is None:
        return CERTIFY_WORKER_THREAD if runners is not None else CERTIFY_WORKER_PROCESS
    kind = str(worker)
    if kind not in CERTIFY_WORKER_KINDS:
        raise ValueError(f"certify worker must be one of {list(CERTIFY_WORKER_KINDS)}, not {kind!r}")
    return kind


def certify_disposable_copy(
    held_trial: Path,
    task_id: str,
    stages: tuple[str, ...],
    *,
    runners: Mapping[str, Callable[..., Any]] | None = None,
    deadline_s: float = CERTIFY_DEADLINE_S,
    clock: Callable[[], float] = time.monotonic,
    model_stages_deferred: bool = False,
    worker: str | None = None,
    rss_limit_mb: int = CERTIFY_WORKER_RSS_LIMIT_MB,
    attack: Mapping[str, Any] | None = None,
    touched_stages: Sequence[str] = (),
) -> CertifyReceipt:
    """Run certification in a supervised nested trial copy and return its receipt.

    The held trial remains unchanged and the nested copy is removed before return.
    Deadlines and memory failures terminate the worker first. Exhaustion on a stage fed
    by the edit becomes a paid refusal; other exhaustion raises a harness fault. Empty
    stage sets spawn no worker.
    """
    if float(deadline_s) <= 0:
        raise ValueError("deadline_s must be > 0")
    kind = resolve_worker_kind(worker, runners)
    stages = tuple(str(stage) for stage in stages)
    if not stages:
        return CertifyReceipt(
            project_certify(CODE_REFUSED_NO_PROVIDER_FREE_STAGE, model_stages_deferred=True),
            (),
            Path(held_trial),
            0.0,
            kind,
        )
    attack_data = _transportable_attack(attack)
    touched = tuple(str(stage) for stage in touched_stages)
    if kind == CERTIFY_WORKER_PROCESS:
        return _certify_in_process(
            Path(held_trial), task_id, stages, runners=runners, deadline_s=float(deadline_s),
            clock=clock, model_stages_deferred=model_stages_deferred, rss_limit_mb=int(rss_limit_mb),
            attack=attack_data, touched_stages=touched,
        )
    return _certify_in_thread(
        Path(held_trial), task_id, stages, runners=runners, deadline_s=float(deadline_s),
        clock=clock, model_stages_deferred=model_stages_deferred, attack=attack_data,
        touched_stages=touched,
    )


def _deadline_receipt(
    *,
    stage: str | None,
    touched_stages: Sequence[str],
    attack: Mapping[str, Any] | None,
    model_stages_deferred: bool,
    stages: tuple[str, ...],
    copy_path: Path,
    elapsed_s: float,
    worker: str,
    worker_handle: Any,
) -> CertifyReceipt | None:
    """The PAID receipt of a deadline that cut short a stage the edit feeds
    (`stage` in `touched_stages`), or None when the deadline is the harness
    fault it always was: no stage was RUNNING (`stage` is None before the
    first member, `NO_STAGE` between and after members — finding p4-2-0), or
    the running stage is one the edit does not feed."""
    if stage is None or str(stage) not in tuple(str(s) for s in touched_stages):
        return None
    project = project_certify if attack is None else project_certify_attack
    return CertifyReceipt(
        project(CODE_REFUSED_RESOURCE_BUDGET, model_stages_deferred=model_stages_deferred),
        stages,
        copy_path,
        elapsed_s,
        worker,
        resource_stage=str(stage),
        worker_handle=worker_handle,
    )


def _transportable_attack(attack: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The `attack` member context as plain JSON data (validated by
    `_attack_context`, then dumped through JSON so no live object crosses)."""
    import json as _json

    if attack is None:
        return None
    _attack_context(attack)
    try:
        return _json.loads(_json.dumps(dict(attack)))
    except (TypeError, ValueError) as exc:
        raise ToolHarnessFault(TOOL_NAME, code="attack_context_invalid", cause_type=type(exc).__name__) from None


# -- the spawned worker (production) ------------------------------------------

def _transportable_runners(runners: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The runner mapping as the spawned worker receives it: None (the
    worker builds the production dict itself) or a picklable mapping of
    module-level functions; a closure, a lambda or a bound method of a
    live object cannot cross the boundary and is refused as a harness
    fault before anything is spawned."""
    if runners is None:
        return None
    plain = dict(runners)
    assert_no_provider_handle(plain)
    try:
        pickle.dumps(plain)
    except Exception as exc:  # noqa: BLE001 - pickling raises many classes
        fault = ToolHarnessFault(TOOL_NAME, code="runner_not_transportable", cause_type=type(exc).__name__)
        raise fault from None
    return plain


def _fault_descriptor(exc: BaseException) -> dict[str, Any]:
    """What a worker-side fault sends back: the class (by module and
    qualified name), the message and its plain-valued attributes — never a
    traceback object, never the runner's raw output."""
    def plain(value: Any) -> bool:
        if value is None or isinstance(value, (str, int, float, bool)):
            return True
        if isinstance(value, (tuple, list)):
            return all(plain(v) for v in value)
        return False

    state = {
        str(key): (list(value) if isinstance(value, tuple) else value)
        for key, value in vars(exc).items()
        if plain(value)
    }
    return {
        "module": type(exc).__module__,
        "qualname": type(exc).__qualname__,
        "message": str(exc),
        "state": state,
        "tuple_keys": [str(k) for k, v in vars(exc).items() if isinstance(v, tuple)],
    }


def _rebuild_fault(descriptor: Mapping[str, Any]) -> BaseException:
    """The parent-side exception for a worker fault descriptor: the same
    class when it is one of the repository's own (a `SessionFault`, an
    engine infrastructure class), rebuilt WITHOUT calling its constructor
    so the message and attributes are exactly the worker's; anything else
    is a `ToolHarnessFault` naming the class."""
    module = str(descriptor.get("module") or "")
    qualname = str(descriptor.get("qualname") or "")
    message = str(descriptor.get("message") or "")
    cls: Any = None
    if module.startswith("elt_taskgen."):
        try:
            node: Any = importlib.import_module(module)
            for part in qualname.split("."):
                node = getattr(node, part)
            cls = node
        except (ImportError, AttributeError):
            cls = None
    if not (inspect.isclass(cls) and issubclass(cls, BaseException)):
        return ToolHarnessFault(TOOL_NAME, code="worker_fault", cause_type=qualname or "unknown")
    exc = cls.__new__(cls)
    BaseException.__init__(exc, message)
    state = dict(descriptor.get("state") or {})
    for key in descriptor.get("tuple_keys") or ():
        if key in state and isinstance(state[key], list):
            state[key] = tuple(state[key])
    exc.__dict__.update(state)
    return exc


def _certify_worker_main(
    send: Connection,
    copy: str,
    task_id: str,
    stages: tuple[str, ...],
    runners: Mapping[str, Any] | None,
    model_stages_deferred: bool,
    memory_limit_mb: int,
    rss_limit_mb: int,
    attack: Mapping[str, Any] | None = None,
    touched_stages: tuple[str, ...] = (),
) -> None:
    """Run certification inside the supervised worker process.

    Apply process-group and memory controls, report the active stage, return only the
    receipt envelope, and keep exception text and private output inside the worker.
    """
    from elt_taskgen.reference import duckdb_sandbox
    from elt_taskgen.semantic.scoring import _apply_worker_memory_rlimits

    def on_stage(name: str) -> None:
        try:
            send.send(("stage", str(name)))
        except BaseException:  # noqa: BLE001 - the supervisor may already be gone
            pass

    try:
        try:
            os.setsid()
        except (AttributeError, OSError):
            pass
        _apply_worker_memory_rlimits(int(rss_limit_mb) * 1024 * 1024)
        handle = duckdb_sandbox.begin_interruptible_scope(memory_limit_mb=int(memory_limit_mb))
        try:
            diagnostic = run_provider_free_stages(
                Path(copy),
                str(task_id),
                tuple(stages),
                runners=runners,
                model_stages_deferred=bool(model_stages_deferred),
                attack=attack,
                touched_stages=tuple(touched_stages),
                on_stage=on_stage,
            )
        finally:
            duckdb_sandbox.end_interruptible_scope(handle)
        if not isinstance(diagnostic, Diagnostic):
            raise ToolHarnessFault(TOOL_NAME, code="non_projection_result")
        send.send(("result", diagnostic.model_dump(mode="json")))
    except BaseException as exc:  # noqa: BLE001 - typed, then sent; never re-raised into multiprocessing
        fault = exc if isinstance(exc, (SessionFault, ToolHarnessFault)) else classify_runner_exception(exc)
        try:
            send.send(("fault", _fault_descriptor(fault)))
        except BaseException:  # noqa: BLE001 - the pipe itself may be gone
            pass
    finally:
        try:
            send.close()
        except BaseException:  # noqa: BLE001
            pass


def _supervise_certify_worker(
    process: Any,
    receive: Connection,
    *,
    deadline_s: float,
    clock: Callable[[], float],
    started: float,
    rss_limit_bytes: int,
) -> tuple[str, Any]:
    """Supervise one certification worker until receipt, failure, or limit.

    Track active stage and memory, terminate the process group before cleanup on
    failure, and classify touched-stage resource exhaustion according to the
    paid-refusal rule.
    """
    from elt_taskgen.semantic.scoring import _process_rss_bytes

    next_rss_check = time.monotonic()
    last_stage: str | None = None

    def final(message: Any) -> tuple[str, Any] | None:
        """A terminal message as `(kind, payload)`, a progress message as
        None (recorded), anything else as `("died", None)`."""
        nonlocal last_stage
        if isinstance(message, tuple) and len(message) == 2:
            if message[0] in ("result", "fault"):
                return (str(message[0]), message[1])
            if message[0] == "stage" and isinstance(message[1], str):
                last_stage = message[1]
                return None
        return ("died", None)

    while True:
        if receive.poll(_POLL_S):
            try:
                message = receive.recv()
            except EOFError:
                return ("died", None)
            answer = final(message)
            if answer is not None:
                return answer
            continue
        if float(clock()) - float(started) >= float(deadline_s):
            return ("deadline", last_stage)
        if not process.is_alive():
            while receive.poll(0.0):
                try:
                    message = receive.recv()
                except EOFError:
                    return ("died", None)
                answer = final(message)
                if answer is not None:
                    return answer
            return ("died", None)
        now = time.monotonic()
        if now >= next_rss_check and getattr(process, "pid", None) is not None:
            next_rss_check = now + _WATCHDOG_INTERVAL_S
            rss = _process_rss_bytes(int(process.pid))
            if rss is not None and rss > int(rss_limit_bytes):
                return ("memory", last_stage)


def _kill_worker_group(process: Any) -> None:
    """SIGKILL the worker and everything it spawned: its whole process
    group once it is the group's leader (after its `os.setsid`), else the
    process alone (the pre-`setsid` window, in which it has no children) —
    never a group the SUPERVISOR belongs to. Then wait for it to end."""
    pid = getattr(process, "pid", None)
    if pid is None:
        return
    pgid: int | None
    try:
        pgid = os.getpgid(int(pid))
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    if pgid is not None and pgid == int(pid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    else:
        try:
            process.kill()
        except Exception:  # noqa: BLE001 - already gone
            pass
    process.join(_KILL_GRACE_S)
    if process.is_alive():
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
        process.join(_KILL_GRACE_S)


def _certify_in_process(
    held_trial: Path,
    task_id: str,
    stages: tuple[str, ...],
    *,
    runners: Mapping[str, Callable[..., Any]] | None,
    deadline_s: float,
    clock: Callable[[], float],
    model_stages_deferred: bool,
    rss_limit_mb: int,
    attack: Mapping[str, Any] | None = None,
    touched_stages: tuple[str, ...] = (),
) -> CertifyReceipt:
    from elt_taskgen.review.repair_proposer import trial_workspace  # lazy: rp imports the projection

    if int(rss_limit_mb) < 128:
        raise ValueError("rss_limit_mb must be >= 128 (a spawned python+duckdb worker's floor)")
    transportable = _transportable_runners(runners)
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    # The copy is opened by hand (not `with`): on a deadline it is torn down
    # only once the worker group has been KILLED and has ended, never
    # underneath a stage that is still writing into it.
    copy_cm = trial_workspace(Path(held_trial), task_id=task_id)
    copy_path = Path(copy_cm.__enter__())

    def teardown() -> None:
        copy_cm.__exit__(None, None, None)

    process = context.Process(
        target=_certify_worker_main,
        args=(
            send,
            str(copy_path),
            str(task_id),
            tuple(stages),
            transportable,
            bool(model_stages_deferred),
            int(CERTIFY_MEMORY_LIMIT_MB),
            int(rss_limit_mb),
            attack,
            tuple(touched_stages),
        ),
        name="certify-worker",
        daemon=True,
    )
    started = float(clock())
    try:
        process.start()
    except BaseException:
        teardown()
        raise
    send.close()
    try:
        kind, payload = _supervise_certify_worker(
            process, receive,
            deadline_s=deadline_s, clock=clock, started=started,
            rss_limit_bytes=int(rss_limit_mb) * 1024 * 1024,
        )
    finally:
        receive.close()
    if kind in ("deadline", "memory"):
        # Kill FIRST, tear down SECOND: the copy never goes under a live stage.
        _kill_worker_group(process)
        if process.is_alive():  # pragma: no cover - a SIGKILLed process ends
            _reap_later(process, teardown)
        else:
            teardown()
        if kind == "deadline":
            # A deadline on a stage the edit feeds is the model's own
            # exhaustion: the PAID receipt, the worker killed and the copy
            # gone exactly as for the fault (the RSS envelope, `memory`,
            # stays the OS-level harness fault whatever the stage).
            paid = _deadline_receipt(
                stage=payload if isinstance(payload, str) else None,
                touched_stages=touched_stages, attack=attack,
                model_stages_deferred=model_stages_deferred, stages=stages,
                copy_path=copy_path, elapsed_s=max(0.0, float(clock()) - started),
                worker=CERTIFY_WORKER_PROCESS, worker_handle=process,
            )
            if paid is not None:
                return paid
            fault: SessionFault = ToolDeadlineExceeded(TOOL_NAME, deadline_s=float(deadline_s))
        else:
            fault = SandboxFault(
                "certify worker exceeded its RSS envelope and was killed; a harness fault",
                code="memory_limit",
            )
        fault.copy_path = copy_path  # type: ignore[attr-defined]
        fault.worker = process  # type: ignore[attr-defined]
        raise fault
    elapsed = max(0.0, float(clock()) - started)
    # The worker has answered: let it exit, and kill it (the whole group)
    # only if it lingers — either way it has ENDED before the copy goes.
    process.join(_KILL_GRACE_S)
    if process.is_alive():
        _kill_worker_group(process)
    teardown()
    if kind == "died":
        fault = SandboxFault(
            "certify worker exited without reporting a result (killed, aborted or "
            "its pipe closed); a harness fault, never a red verdict",
            code="worker_failed",
        )
        fault.copy_path = copy_path  # type: ignore[attr-defined]
        fault.worker = process  # type: ignore[attr-defined]
        raise fault
    if kind == "fault":
        rebuilt = _rebuild_fault(payload if isinstance(payload, Mapping) else {})
        rebuilt.copy_path = copy_path  # type: ignore[attr-defined]
        rebuilt.worker = process  # type: ignore[attr-defined]
        raise rebuilt
    try:
        diagnostic = Diagnostic.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - malformed worker IPC
        raise ToolHarnessFault(TOOL_NAME, code="non_projection_result", cause_type=type(exc).__name__) from None
    if diagnostic.code not in _RESULT_CODES:
        raise ToolHarnessFault(TOOL_NAME, code="non_projection_result")
    return CertifyReceipt(diagnostic, stages, copy_path, elapsed, CERTIFY_WORKER_PROCESS)


def _reap_later(worker: Any, teardown: Callable[[], None]) -> None:
    """Remove the copy once `worker` (a thread or a process) has ended."""
    def reap() -> None:
        worker.join()
        teardown()

    threading.Thread(target=reap, name="certify-reaper", daemon=True).start()


# -- the in-process cancellable thread (an injected runner dict) ---------------

def _certify_in_thread(
    held_trial: Path,
    task_id: str,
    stages: tuple[str, ...],
    *,
    runners: Mapping[str, Callable[..., Any]] | None,
    deadline_s: float,
    clock: Callable[[], float],
    model_stages_deferred: bool,
    attack: Mapping[str, Any] | None = None,
    touched_stages: tuple[str, ...] = (),
) -> CertifyReceipt:
    from elt_taskgen.reference import duckdb_sandbox
    from elt_taskgen.review.repair_proposer import trial_workspace  # lazy: rp imports the projection

    outcome: dict[str, Any] = {}
    scope: dict[str, int] = {}
    progress: dict[str, str] = {}
    # The copy is opened by hand (not `with`): on a deadline it is torn down
    # only once the worker thread has ENDED, never underneath a stage that
    # is still writing into it.
    copy_cm = trial_workspace(Path(held_trial), task_id=task_id)
    copy_path = Path(copy_cm.__enter__())

    def teardown() -> None:
        copy_cm.__exit__(None, None, None)

    def on_stage(name: str) -> None:
        progress["stage"] = str(name)

    def target() -> None:
        scope["handle"] = duckdb_sandbox.begin_interruptible_scope(
            memory_limit_mb=CERTIFY_MEMORY_LIMIT_MB
        )
        try:
            outcome["diagnostic"] = run_provider_free_stages(
                copy_path,
                task_id,
                stages,
                runners=runners,
                model_stages_deferred=model_stages_deferred,
                attack=attack,
                touched_stages=tuple(touched_stages),
                on_stage=on_stage,
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            outcome["error"] = exc
        finally:
            duckdb_sandbox.end_interruptible_scope(scope["handle"])

    worker = threading.Thread(target=target, name="certify-worker", daemon=True)
    started = float(clock())
    worker.start()
    expired = False
    while worker.is_alive():
        if float(clock()) - started >= float(deadline_s):
            expired = True
            break
        worker.join(_POLL_S)
    if expired:
        _cancel_worker(worker, scope.get("handle"), teardown)
        # A deadline on a stage the edit feeds is the model's own exhaustion:
        # the PAID receipt, the worker interrupted and the copy reaped
        # exactly as for the fault.
        paid = _deadline_receipt(
            stage=progress.get("stage"), touched_stages=touched_stages, attack=attack,
            model_stages_deferred=model_stages_deferred, stages=stages,
            copy_path=copy_path, elapsed_s=max(0.0, float(clock()) - started),
            worker=CERTIFY_WORKER_THREAD, worker_handle=worker,
        )
        if paid is not None:
            return paid
        fault = ToolDeadlineExceeded(TOOL_NAME, deadline_s=float(deadline_s))
        fault.copy_path = copy_path  # type: ignore[attr-defined]
        fault.worker = worker  # type: ignore[attr-defined]
        raise fault
    elapsed = max(0.0, float(clock()) - started)
    teardown()
    if "error" in outcome:
        raise outcome["error"]
    diagnostic = outcome.get("diagnostic")
    if not isinstance(diagnostic, Diagnostic) or diagnostic.code not in _RESULT_CODES:
        raise ToolHarnessFault(TOOL_NAME, code="non_projection_result")
    return CertifyReceipt(diagnostic, stages, copy_path, elapsed, CERTIFY_WORKER_THREAD)


def _cancel_worker(worker: threading.Thread, handle: int | None, teardown: Callable[[], None]) -> None:
    """Stop an expired in-process certify worker: interrupt every gold
    connection its scope registered, wait `CERTIFY_CANCEL_GRACE_S` of REAL
    time for the thread to unwind, then tear the copy down — now if the
    thread ended, else from a reaper thread once it does (the copy is never
    removed under a running stage, so it can never be resurrected)."""
    from elt_taskgen.reference import duckdb_sandbox

    if handle is not None:
        duckdb_sandbox.interrupt_scope(handle)
    worker.join(CERTIFY_CANCEL_GRACE_S)
    if not worker.is_alive():
        teardown()
        return
    _reap_later(worker, teardown)

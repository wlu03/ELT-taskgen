"""Council metrology harness tests (review/metrology.py).

WHY THIS EXISTS
The metrology harness is the acceptance bar for real council prompts: live
council routing is admitted only when the council provably detects planted
defects. These tests prove the harness itself is trustworthy:

  * Defect injectors are deterministic and actually plant the defect they
    claim (tie-break/null/dedupe/filter rule stripped, distinguishing
    counterfactual rows removed, constant-heavy gold, missing public schema
    column/table) while keeping every specimen leak-free and valid.
  * The benchmark is NOT gameable: a boilerplate provider that returns
    generic plausible findings while ignoring its input scores ~zero recall
    (its findings never reference the planted defects) and is blocked.
  * ITEM D1 — nor is it gameable by an ORDER-AWARE provider: one that never
    reads the prose, counts calls, and replays the canned finding that
    belongs at that position scores well against the OLD fixed suite and
    ~zero against the seeded sample, on every seed tried.
  * ITEM D1 — a class-vocabulary echo that never names the planted element
    scores zero: detection needs both axes on one finding.
  * A defect-referencing provider (the test-local oracle instrument) passes,
    the report artifact is deterministic FOR A GIVEN SEED and reproducible
    from the seed the report records, and only a passing run can write the
    council.live_admitted marker.
  * The review stage fails closed with 'council not admitted — run metrology'
    when a LIVE-capable provider is used without the marker; replay-only
    providers (the offline demo path) are exempt.
  * Live-provider metrology SKIPS WITH A VISIBLE REASON when no API keys or
    recorded transcripts exist — never a silent pass.
  * It SPENDS MONEY only when the operator explicitly opts in with
    ELT_TASKGEN_LIVE_TESTS=1 *in addition to* credentials. An exported
    ANTHROPIC_API_KEY alone makes the run replay-only (free); `make test` is
    never allowed to bill anyone. See the money guard below.

The boilerplate and oracle providers below are harness instruments defined
inside the tests only; they are not shipping providers.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import cli
from elt_taskgen.models import (
    CouncilRole,
    PopulationName,
    TaskVariant,
    canonical_json,
    sha256_hex,
)
from elt_taskgen.review import council as council_mod
from elt_taskgen.review import metrology as metrology_mod
from elt_taskgen.review import providers as providers_mod
from elt_taskgen.review.council import (
    CRITIC_ROLES,
    _critic_view,
    _population_adversary_view,
    _view_for,
    leak_findings,
)

#: Repo root, for the artifacts the Aug-13 integrity lane pins by their bytes.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Harness instruments (tests only — never shipping providers)
# ---------------------------------------------------------------------------


def _fixture_proposed_case(kind: str, *, severity: str) -> dict:
    """Exact active-wire proposal used by actionable metrology fixtures.

    A major/fatal finding models an attack expected to survive the current
    populations.  A minor finding models the standing diligence probe: load
    still succeeds, while hidden transform populations are expected to defeat
    the wrong implementation.  Both are hypotheses with complete EL/T maps;
    production code still has to compile and measure them.
    """
    populations = tuple(pop.value for pop in PopulationName)
    extract_load = {population: True for population in populations}
    if severity in {"major", "fatal"}:
        transform = {population: True for population in populations}
        rationale = (
            "Metrology defect fixture predicts this executable wrong "
            "implementation keeps full transform reward on every population."
        )
    else:
        transform = {
            population: population == PopulationName.DEVELOPMENT.value
            for population in populations
        }
        rationale = (
            "Metrology diligence probe predicts full development reward but "
            "loss on every hidden transform population."
        )
    return {
        "kind": kind,
        "params": "{}",
        "expected_pass_by_stage": {
            TaskVariant.EXTRACT_LOAD.value: extract_load,
            TaskVariant.TRANSFORM.value: transform,
        },
        "rationale": rationale,
    }


def _finding(summary: str, detail: str = "", severity: str = "minor",
             suggested_attack: str | None = None) -> dict:
    return {
        "severity": severity,
        "summary": summary,
        "detail": detail,
        "route_hint": None,
        "suggested_attack": suggested_attack,
        # Every critic finding carries the required nullable field.  Findings
        # that claim an executable attack get the exact active wire shape;
        # observations without an attack explicitly carry null.
        "proposed_case": (
            _fixture_proposed_case(suggested_attack, severity=severity)
            if suggested_attack is not None
            else None
        ),
    }


class TrialSeam:
    """The per-trial seam every metrology provider exposes (roadmap Phase 4,
    `providers.begin_trial` / `end_trial`).

    `run_metrology` OBSERVES the isolation it attests rather than asserting it
    (finding p4-1-2): a provider that cannot be told a trial began is never
    handed a per-trial executor, so nothing about it is known to be free of
    cross-trial state and the run records `cross_trial_cache: true`. These
    doubles hold no memo of any kind, so they take the seam and answer no
    executor — `metrology._executor_cache_state` reads that as "nothing to
    look at", which is honest, and only an executor OBSERVED to carry a cache
    fails the attestation.
    """

    #: The open trial's `TrialContext`, or None.
    trial_context = None

    def begin_trial(self, ctx) -> None:
        self.trial_context = ctx

    def end_trial(self) -> None:
        self.trial_context = None

    @property
    def trial_executor(self):
        return None


class BoilerplateProvider(TrialSeam):
    """Integrity fixture: protocol-valid generic findings that IGNORE the
    input entirely. A gameable benchmark would reward this always-complain
    strategy; the metrology harness must score it ~zero recall. The wording
    deliberately avoids every specimen detection term (no 'tie', 'sort',
    'null', 'join', 'status', 'constant', 'quan-tity', ... substrings)."""

    def complete(self, role: CouncilRole, prompt: str) -> str:
        return json.dumps(
            {
                "findings": [
                    _finding(
                        "The specification prose could be read in more than "
                        "one way by different implementers.",
                        "Please review the wording for completeness and "
                        "clarify any vague requirements before release.",
                    ),
                    _finding(
                        "Some requirements are described at a high level and "
                        "might benefit from examples.",
                        "Consider expanding the documentation so that an "
                        "implementer does not have to guess.",
                    ),
                ]
            }
        )


#: The SHORTCUT ATTACKER's diligence PROBE REPORT: severity 'minor' with an
#: executable suggested_attack it expects the data to defeat (never a defect
#: claim), which the stage contract demands on every task.
_PROBE_REPORT = _finding(
    "Diligence probe: hard-coded constants over the graded mart should lose "
    "reward on a hidden population.",
    "Emit fixed values for every measure; expect the hidden graded "
    "populations to defeat this probe. Compile and run it as standing "
    "evidence.",
    severity="minor",
    suggested_attack="constants",
)

#: (family names) -> {(role, rendered view): specimen} over every wording
#: variant of every NON-DEMO specimen: how the oracle recognises a view of a
#: fixture family it has no hand-written reading rules for (harness "6").
_FAMILY_VIEWS: dict[tuple[str, ...], dict[tuple[CouncilRole, str], "metrology_mod.Specimen"]] = {}


def _family_specimen_for(role: CouncilRole, prompt: str):
    """The non-demo family specimen whose view for `role` is exactly
    `prompt`, or None (a demo view, a canary, or an unknown stimulus)."""
    key = tuple(metrology_mod.pool_families())
    views = _FAMILY_VIEWS.get(key)
    if views is None:
        views = {}
        for specimen in metrology_mod.specimen_pool():
            if specimen.family == "demo":
                continue
            roles = tuple(CRITIC_ROLES) if specimen.kind == "clean" else (specimen.target_role,)
            for task in specimen.variants or (specimen.task,):
                for seat in roles:
                    views[(seat, council_mod.render_view(seat, task))] = specimen
        _FAMILY_VIEWS[key] = views
    return views.get((role, prompt))


def _family_findings(role: CouncilRole, specimen) -> list[dict]:
    """The diligent reading of a NON-DEMO family specimen: nothing on a clean
    one (the shortcut attacker files its probe report), and on a tampered
    one aimed at `role` ONE defect claim that names the planted element (an
    anchor term) and says what is wrong with it (a detection term) — the
    two axes on one finding, as the pool's own scoring axes declare them."""
    if specimen.kind != "tampered" or specimen.target_role is not role:
        return [_PROBE_REPORT] if role is CouncilRole.SHORTCUT_ATTACKER else []
    term = specimen.detection_terms[0]
    anchor = specimen.anchor_terms[0] if specimen.anchor_terms else ""
    attack = "constants" if role is CouncilRole.SHORTCUT_ATTACKER else None
    return [
        _finding(
            f"{anchor}: {specimen.description}.",
            f"Reading the published {anchor} surface: {term}; {specimen.description}.",
            severity="major",
            suggested_attack=attack,
        )
    ]


class OracleProvider(TrialSeam):
    """Harness instrument that actually reads its input: it emits a targeted
    finding exactly when the planted defect is visible in the role's view,
    and raises no DEFECT CLAIM on clean specimens. Used to prove the harness
    scores a diligent council as admitted (recall 1.0, zero nitpicks).

    Every finding it writes NAMES the element it is objecting to, because
    that is what the two-axis scoring requires (Item D1): class vocabulary
    alone no longer counts.

    The SHORTCUT ATTACKER branch models the role's real stage contract
    (harness version 3): when it sees no exploit it still files one PROBE
    REPORT — severity 'minor' with an executable suggested_attack it expects
    the data to defeat — because cli.make_review_runner fails the review
    stage on zero executable probes. Metrology must score that diligence as
    neither a detection nor a false alarm, which is exactly what the
    admitted-with-zero-nitpicks assertions below prove.

    HARNESS "6" (fixture families): the hand-written reading rules below are
    the DEMO family's. A view of another family is recognised by content
    (`_family_specimen_for`: every wording variant of every specimen is a
    distinct stimulus) and answered with one defect claim carrying the
    pool's own two scoring axes (`_family_findings`), so the oracle stays a
    diligent reader of every family the pool draws from.
    """

    def complete(self, role: CouncilRole, prompt: str) -> str:
        family_specimen = _family_specimen_for(role, prompt)
        if family_specimen is not None:
            return json.dumps({"findings": _family_findings(role, family_specimen)})
        low = " ".join(prompt.lower().split())
        findings: list[dict] = []
        if role is CouncilRole.AMBIGUITY_CRITIC:
            # THE TWO CONTRADICTION SPECIMENS. Nothing is stripped, so the
            # oracle keys on the REWRITTEN mart column description — the half
            # of the contradiction that is not in the clean fixture — and
            # names both halves, which is what the two-axis scoring demands.
            if "counted once per repetition" in low:
                findings.append(
                    _finding(
                        "completed_order_count's column description "
                        "contradicts the dedupe and count rules.",
                        "The mart schema says a repeated order header row is "
                        "counted once per repetition, while rule 2 requires "
                        "DISTINCT (order_id, customer_id) and rule 6 requires "
                        "COUNT(DISTINCT order_id); nothing says which wins, "
                        "and duplicate order header rows are declared "
                        "possible.",
                        severity="fatal",
                    )
                )
            if "whether or not it is in scope" in low:
                findings.append(
                    _finding(
                        "total_spend's column description contradicts the "
                        "scope rules.",
                        "The mart schema says total_spend sums items of every "
                        "order the customer placed whether or not it is in "
                        "scope, while rule 1 puts only completed orders in "
                        "scope and rule 6 sums the item totals of the orders "
                        "in scope; the two readings differ by every cancelled "
                        "order's value.",
                        severity="fatal",
                    )
                )
            if "coalesce" not in low:
                findings.append(
                    _finding(
                        "Ambiguous null handling: total_spend for customers "
                        "with no completed orders could be NULL or zero.",
                        "The prose omits the COALESCE-to-zero rule for "
                        "total_spend and completed_order_count.",
                        severity="major",
                    )
                )
            if "deduplicate" not in low:
                findings.append(
                    _finding(
                        "Duplicate order header rows are never mentioned: "
                        "completed_order_count is ambiguous.",
                        "Without a dedupe rule the same order_id counted "
                        "twice is as defensible as counting it once.",
                        severity="major",
                    )
                )
            if "status = 'completed'" not in low:
                findings.append(
                    _finding(
                        "The prose never says which orders count: the status "
                        "filter is not stated.",
                        "completed_order_count could mean all orders or only "
                        "non-cancelled ones; two readings, both defensible.",
                        severity="major",
                    )
                )
            if "grouped by order_id" not in low:
                findings.append(
                    _finding(
                        "How an order's value is built from its item lines is "
                        "not specified.",
                        "total_spend depends on summing quantity * unit_price "
                        "over order_items, and the prose never states at which "
                        "grain or whether repeated lines count. Ambiguous.",
                        severity="major",
                    )
                )
            if "onto customers" not in low:
                findings.append(
                    _finding(
                        "Whether customers with no completed orders appear at "
                        "all is unspecified.",
                        "Nothing states that customers with no orders are "
                        "retained, so an implementer could emit them or omit "
                        "them; the customer_summary row set is ambiguous.",
                        severity="major",
                    )
                )
        elif role is CouncilRole.POPULATION_ADVERSARY:
            conditions = low.split("population conditions:", 1)[-1]
            if not any(
                phrase in conditions
                for phrase in ("no orders", "never placed a single order")
            ):
                findings.append(
                    _finding(
                        "An INNER JOIN is indistinguishable from the required "
                        "LEFT JOIN: no graded population contains a customer "
                        "without orders.",
                        "Every population (primary, resampled, "
                        "counterfactual) now matches, so wrong join logic "
                        "would score full reward.",
                        severity="major",
                        suggested_attack="inner_join",
                    )
                )
            if "cancelled" not in conditions:
                findings.append(
                    _finding(
                        "No population contains a cancelled order, so the "
                        "status filter is never exercised.",
                        "A solver that skips the completed-status filter is "
                        "indistinguishable from a correct one here.",
                        severity="major",
                    )
                )
            if "duplicate order header" not in conditions:
                findings.append(
                    _finding(
                        "No population declares duplicate order header rows "
                        "any more — the stress population dropped them.",
                        "The dedupe rule is never exercised, so omitting "
                        "DISTINCT is indistinguishable from correct logic.",
                        severity="major",
                    )
                )
            if "orders have null customer_id" not in conditions:
                findings.append(
                    _finding(
                        "No graded population states that any order carries a "
                        "NULL customer_id.",
                        "primary, resampled, stress and counterfactual all "
                        "guarantee an owner for every order, so a solver that "
                        "aggregates unowned orders would still score full "
                        "reward — the exclusion rule is never exercised.",
                        severity="major",
                    )
                )
            if "multiple items" not in conditions:
                findings.append(
                    _finding(
                        "No graded population guarantees an order with more "
                        "than one item line.",
                        "primary, stress and counterfactual all give every "
                        "order a single line, so COUNT DISTINCT over "
                        "completed_order_count cannot be distinguished from a "
                        "fan-out item-grain COUNT; both score full reward.",
                        severity="major",
                    )
                )
            if "carries a customer_id, and appears once" in conditions:
                findings.append(
                    _finding(
                        "Every discriminating condition sits in development, "
                        "the split the solver can see.",
                        "primary, resampled, stress and counterfactual are all "
                        "stated to be uniform — completed, owned, unique, "
                        "single-line — so no GRADED population can distinguish "
                        "wrong logic; it would still score full reward.",
                        severity="major",
                    )
                )
        elif role is CouncilRole.SHORTCUT_ATTACKER:
            if "always exactly the same value" in low:
                findings.append(
                    _finding(
                        "total_spend and completed_order_count are declared "
                        "constant: hard-code them for full reward.",
                        "customer_summary can be produced without solving.",
                        severity="major",
                        suggested_attack="constants",
                    )
                )
            if "equals the customer_id itself" in low:
                findings.append(
                    _finding(
                        "total_spend is a function of customer_id alone: "
                        "copy the key, skip the computation.",
                        "completed_order_count is hard-codeable from the key "
                        "alone; no extraction is needed.",
                        severity="major",
                        suggested_attack="constants",
                    )
                )
            if "exactly 1 for every customer" in low:
                findings.append(
                    _finding(
                        "completed_order_count is declared constant: "
                        "hard-code it and skip half the computation.",
                        "Half of customer_summary can be produced without "
                        "solving anything.",
                        severity="major",
                        suggested_attack="constants",
                    )
                )
            if "ten times completed_order_count" in low:
                findings.append(
                    _finding(
                        "total_spend is a fixed multiple of "
                        "completed_order_count: derive it, never compute it.",
                        "One measure determines the other, so the sources need "
                        "never be read for it; it is hard-codeable from the "
                        "other output column.",
                        severity="major",
                        suggested_attack="constants",
                    )
                )
            if "population primary" not in low:
                tiny = (
                    "population development" in low
                    and "population counterfactual" in low
                )
                if tiny:
                    findings.append(
                        _finding(
                            "Only development and counterfactual are graded: "
                            "two customers plus three customers, five in all.",
                            "The whole graded surface is trivially enumerable "
                            "and can be memorized and hard-coded.",
                            severity="major",
                            suggested_attack="constants",
                        )
                    )
                elif "population development" in low:
                    findings.append(
                        _finding(
                            "Only the development population is graded: two "
                            "customers, trivially enumerable.",
                            "The whole graded surface can be memorized and "
                            "hard-coded; it is a single population.",
                            severity="major",
                            suggested_attack="constants",
                        )
                    )
                elif "population counterfactual" in low:
                    findings.append(
                        _finding(
                            "Only the counterfactual population is graded: "
                            "three customers, trivially enumerable.",
                            "The whole graded surface can be memorized and "
                            "hard-coded; it is a single population.",
                            severity="major",
                            suggested_attack="constants",
                        )
                    )
            if not findings:
                # No exploit visible: the diligence PROBE REPORT the stage
                # contract demands — minor severity, executable attack,
                # expected to be defeated. Not a defect claim.
                findings.append(
                    _finding(
                        "Diligence probe: hard-coded constants over "
                        "customer_summary should lose reward on a hidden "
                        "population.",
                        "Emit fixed values for total_spend and "
                        "completed_order_count; expect the hidden graded "
                        "populations to defeat this probe. Compile and run "
                        "it as standing evidence.",
                        severity="minor",
                        suggested_attack="constants",
                    )
                )
        elif role is CouncilRole.FEASIBILITY_REVIEWER:
            if "- customers (" not in low:
                findings.append(
                    _finding(
                        "The customers source table is missing from the public "
                        "schema.",
                        "customer_id and customer_name are unavailable, so the "
                        "one-row-per-customer grain cannot be built at all.",
                        severity="major",
                    )
                )
            if "customer who placed the order" not in low:
                findings.append(
                    _finding(
                        "orders.customer_id is missing from the public schema.",
                        "No order can be attributed to a customer, so the "
                        "per-customer measures cannot be computed.",
                        severity="major",
                    )
                )
            if "unique order identifier" not in low:
                findings.append(
                    _finding(
                        "orders.order_id is missing from the public schema.",
                        "Orders cannot be deduplicated, counted distinctly, or "
                        "joined to order_items; completed_order_count is not "
                        "computable.",
                        severity="major",
                    )
                )
            if "- status (" not in low:
                findings.append(
                    _finding(
                        "The public schema lacks the orders.status column the "
                        "completed-order filter requires.",
                        "status is missing from the schema; the task is not "
                        "solvable from public information.",
                        severity="major",
                    )
                )
            if "order_items" not in low:
                findings.append(
                    _finding(
                        "The order_items source table is missing from the "
                        "public schema; total_spend cannot be computed.",
                        "Neither quantity nor unit_price is available.",
                        severity="major",
                    )
                )
            else:
                if "- unit_price (" not in low:
                    findings.append(
                        _finding(
                            "order_items.unit_price is missing from the public "
                            "schema.",
                            "total_spend cannot be computed without the unit "
                            "price; the task is not solvable.",
                            severity="major",
                        )
                    )
                if "- quantity (" not in low:
                    findings.append(
                        _finding(
                            "order_items.quantity is missing from the public "
                            "schema.",
                            "total_spend cannot be computed without the "
                            "quantity; the task is not solvable.",
                            severity="major",
                        )
                    )
# Feasibility prose specimen: a mart measure cites an unpublished source
# column, which structured-completeness checks cannot see.
            if "exchange_rate column of the orders table" in low:
                findings.append(
                    _finding(
                        "total_spend is specified as using "
                        "orders.exchange_rate, a column the public schema "
                        "never publishes.",
                        "The orders table publishes order_id, customer_id and "
                        "status only; exchange_rate is absent, so the "
                        "currency conversion — and therefore total_spend — "
                        "cannot be computed from public material.",
                        severity="major",
                    )
                )
            if "order_channel column on the orders table" in low:
                findings.append(
                    _finding(
                        "completed_order_count is restricted by "
                        "orders.order_channel, a column the public schema "
                        "never publishes.",
                        "order_channel is missing from the orders schema, so "
                        "the retail restriction cannot be applied and "
                        "completed_order_count is not computable.",
                        severity="major",
                    )
                )
        return json.dumps({"findings": findings})


class ClassEchoProvider(TrialSeam):
    """Integrity fixture for the ANCHOR axis (Item D1): it emits exactly the
    defect-class vocabulary the harness scores on, and never names the element
    the defect was planted in. Under substring-only scoring this would have
    scored full recall on every class while reading nothing."""

    _BY_ROLE = {
        CouncilRole.AMBIGUITY_CRITIC: (
            # Echoes BOTH ambiguity vocabularies — the omission specimens'
            # generic one and the contradiction specimens' narrow one — while
            # naming no identifier at all, so the fixture stays the strongest
            # possible content-free attacker as the pool grows shapes.
            "The requirement is ambiguous about ordering, nulls and duplicate "
            "handling; the prose does not say which reading is intended and "
            "it contradicts the declared output schema.",
            "Different implementers could sort differently, default to zero "
            "or to null, and dedupe or not. The rules and the schema disagree "
            "and are inconsistent with each other. This is unclear.",
        ),
        CouncilRole.POPULATION_ADVERSARY: (
            "The data cannot distinguish a wrong inner join from a left join.",
            "Wrong logic would be indistinguishable and would still score "
            "full reward; nothing here is exercised.",
        ),
        CouncilRole.SHORTCUT_ATTACKER: (
            "The outputs look constant and memorizable; a solver could "
            "hard-code them.",
            "Enumerating the tiny graded surface is easier than solving.",
        ),
        CouncilRole.FEASIBILITY_REVIEWER: (
            "Something needed appears to be missing and not present in the "
            "public schema.",
            "The task may be unsolvable because a required input is "
            "unavailable and cannot be computed.",
        ),
    }

    def complete(self, role: CouncilRole, prompt: str) -> str:
        summary, detail = self._BY_ROLE[role]
        return json.dumps({"findings": [_finding(summary, detail, "major")]})


class OrderReplayProvider(TrialSeam):
    """ITEM D1's headline attacker: an ORDER-AWARE canned replayer.

    It never looks at the prompt. It counts calls to recover its position in
    a KNOWN schedule, and replays — verbatim — the finding the ORACLE would
    have produced there. Against a fixed published suite that is full marks
    for a provider that read nothing. Against a seeded sample it is answering
    questions it was not asked.

    The canned answers are recorded, not invented: the oracle is run over the
    schedule once and its outputs are kept by position.

    HARNESS 4. The provider is given MORE knowledge than before, not less: it
    is handed the exact TRIAL schedule (which specimen, which surface variant,
    which seats) and replays the oracle's answer for call i in dispatch order.
    Modelling it any more weakly would have quietly retired the attack when
    metrology stopped consulting all four seats on every specimen.
    """

    def __init__(self, schedule, trials=None):
        self.schedule = tuple(schedule)
        self.calls = 0
        oracle = OracleProvider()
        trials = (
            metrology_mod._trials_for(self.schedule) if trials is None else trials
        )
        # The flat call sequence run_metrology produces: for each trial in
        # order, for each seat that trial consults, in order.
        self.canned: list[str] = [
            oracle.complete(role, _view_for(role, trial.task))
            for trial in trials
            for role in trial.roles
        ]

    def complete(self, role: CouncilRole, prompt: str) -> str:
        index = self.calls
        self.calls += 1
        if index < len(self.canned):
            return self.canned[index]
        return json.dumps({"findings": []})


class LiveCapableCannedProvider:
    """Duck-types a live-capable RoutedProvider (replay_only False) for the
    review-stage admission-gate tests; returns diligence-satisfying findings."""

    replay_only = False
    routing = None

    def complete(self, role, prompt):
        return json.dumps(
            {
                "findings": [
                    _finding(
                        "constants shortcut must lose reward",
                        "compile a constants mutant",
                        severity="major",
                        suggested_attack="constants",
                    )
                ]
            }
        )


class ReplayOnlyCannedProvider(LiveCapableCannedProvider):
    replay_only = True


# ---------------------------------------------------------------------------
# Specimen construction
# ---------------------------------------------------------------------------


#: Fixed seeds used wherever a test needs a reproducible mix. Anything the
#: harness promises about determinism is promised PER SEED.
_SEEDS = (1, 7, 20250807, 987654321)


def _fresh_live_exchange_evidence(report):
    """Synthetic provider manifest for admission-record unit tests.

    These tests exercise record integrity without making network calls. CLI
    tests below separately prove that production obtains this shape from the
    live ``RoutedProvider`` and refuses replayed entries.
    """
    entries = []
    for role, metrics in sorted(report.per_role.items()):
        # One TRAJECTORY row per (trial, seat): the scored trials plus the
        # seat's canary trials (harness 5; SoT T8 row fields).
        count = metrics.tampered_count + metrics.clean_count + metrics.canary_trials
        for index in range(count):
            entries.append(
                {
                    "role": role,
                    "prompt_sha256": sha256_hex(f"{role}:prompt:{index}"),
                    "response_sha256": sha256_hex(f"{role}:response:{index}"),
                    "attempt_count": 1,
                    "model_call_count": 1,
                    "correction_count": 0,
                    "tool_call_count": 0,
                    "refused_count": 0,
                    "nudge_count": 0,
                    "validator_run_count": 0,
                    "terminal": "SUBMITTED",
                    "live_model_call_count": 1,
                    "stale_tool_result_count": 0,
                    "provider": "test-live-provider",
                    "model": "test-live-model",
                    "replayed": False,
                }
            )
    return entries


def _write_admission_marker(workspace, report, **kwargs):
    return metrology_mod.write_admission_marker(
        workspace,
        report,
        exchange_evidence=_fresh_live_exchange_evidence(report),
        **kwargs,
    )

#: cli.py wiring (group H): `_admission_gate` + ReviewPayload.admission +
#: the record-transcripts gate. The library half (metrology.AdmissionStatus
#: provenance, RoutedProvider admission stamp) is tested unconditionally.
_HAS_CLI_ADMISSION_GATE = hasattr(cli, "_admission_gate")
_HAS_REVIEW_PAYLOAD_ADMISSION = "admission" in getattr(
    cli.ReviewPayload, "model_fields", {}
)

#: TRIALS one run executes: every drawn clean specimen x REPLICATES_CLEAN
#: plus every drawn tampered specimen x REPLICATES_TAMPERED. Harness 3's run
#: size was the specimen count, because a specimen was answered once.
_RUN_SIZE = (
    metrology_mod.CLEAN_PER_RUN * metrology_mod.REPLICATES_CLEAN
    + metrology_mod.TAMPERED_PER_ROLE
    * metrology_mod.REPLICATES_TAMPERED
    * len(CRITIC_ROLES)
)

#: DISTINCT specimens one run draws (the mix), before replication.
_DRAWN_SPECIMENS = metrology_mod.CLEAN_PER_RUN + metrology_mod.TAMPERED_PER_ROLE * len(
    CRITIC_ROLES
)

#: Trials behind ONE seat's recall interval — the number the power table below
#: is computed at.
_TRIALS_PER_SEAT = (
    metrology_mod.TAMPERED_PER_ROLE * metrology_mod.REPLICATES_TAMPERED
)


class SpecimenPoolTest(unittest.TestCase):
    """The POOL is well-formed and every draw holds specimens out of the run."""

    def test_pool_is_deterministic_uniquely_named_and_larger_than_a_run(self):
        first = metrology_mod.specimen_pool()
        second = metrology_mod.specimen_pool()
        names = [s.name for s in first]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            [(s.name, s.task.content_hash(), s.task.solver_prompt) for s in first],
            [(s.name, s.task.content_hash(), s.task.solver_prompt) for s in second],
        )
        self.assertGreater(len(first), _DRAWN_SPECIMENS)
        self.assertEqual(metrology_mod.pool_sha256(), metrology_mod.pool_sha256())

    # (A ">strictly larger than the draw" pool-size test was removed
    #: MeasurementPowerTest::test_the_pool_holds_specimens_back_
    # from_every_draw asserts >= draw + POOL_HOLDOUT on the same fixture,
    # which strictly implies it.)

    def test_every_tampered_specimen_declares_both_scoring_axes(self):
        for specimen in metrology_mod.specimen_pool():
            if specimen.kind == "clean":
                self.assertEqual(specimen.detection_terms, (), specimen.name)
                self.assertEqual(specimen.anchor_terms, (), specimen.name)
                continue
            self.assertTrue(specimen.detection_terms, specimen.name)
            self.assertTrue(specimen.anchor_terms, specimen.name)

    def test_clean_prose_contains_the_rules_the_injectors_strip(self):
        for specimen in metrology_mod.clean_specimens():
            low = specimen.task.solver_prompt.lower()
            self.assertIn("sort by customer_id", low, specimen.name)
            self.assertIn("coalesce", low, specimen.name)
            self.assertIn("deduplicate", low, specimen.name)
            self.assertIn("status = 'completed'", low, specimen.name)
            self.assertIn("- status (", low, specimen.name)
            self.assertIn("order_items", specimen.task.solver_prompt, specimen.name)

    def test_adversarial_cleans_carry_defect_vocabulary_while_staying_clean(self):
        decoys = [
            s for s in metrology_mod.clean_specimens() if s.name.startswith("clean-decoy")
        ]
        self.assertTrue(decoys)
        for specimen in decoys:
            low = specimen.task.solver_prompt.lower()
            for bait in ("tie", "null", "constant", "missing"):
                self.assertIn(bait, low, f"{specimen.name}: {bait}")
            # ...and the rules they might be mistaken for are all still there.
            self.assertIn("sort by customer_id", low)
            self.assertIn("coalesce", low)
        reworded = next(
            s for s in metrology_mod.clean_specimens()
            if s.name == "clean-reworded-counterfactual"
        )
        conditions = _population_adversary_view(reworded.task).split(
            "POPULATION CONDITIONS:", 1
        )[1].lower()
        # The phrase a grep-based adversary keys on is gone from the
        # counterfactual, but the case it describes is unchanged.
        self.assertNotIn("c10: customer with no orders", conditions)
        self.assertIn("never placed a single order", conditions)
        self.assertIn("cancelled", conditions)

    #: The CONTRADICTION half of the ambiguity pool: (specimen, the rewritten
    #: mart column description, the rule text it disagrees with). These strip
    #: NOTHING — see `test_ambiguity_contradictions_leave_both_halves_on_screen`.
    _CONTRADICTIONS = {
        "ambiguity-count-contradicts-dedupe": (
            "counted once per repetition",
            ("deduplicate exact-duplicate order header rows",
             "count(distinct order_id)"),
        ),
        "ambiguity-spend-contradicts-scope": (
            "whether or not it is in scope",
            ("keep only orders with status = 'completed'",
             "total_spend = sum of item totals of the orders in scope"),
        ),
    }

    def test_ambiguity_injections_strip_exactly_one_rule_each(self):
        by_name = {s.name: s for s in metrology_mod.ambiguity_specimens()}
        expected = {
            "ambiguity-no-null-rule": "coalesce",
            "ambiguity-no-dedupe": "deduplicate",
            "ambiguity-no-filter": "status = 'completed'",
            "ambiguity-no-item-fanout-rule": "grouped by order_id",
            "ambiguity-no-left-join-rule": "onto customers",
        }
        self.assertEqual(set(by_name), set(expected) | set(self._CONTRADICTIONS))
        # Keep no-tiebreak and no-null-customer-rule retired: one is ungraded,
        # the other violates seat policy, and both measured zero recall.
        for retired in ("ambiguity-no-tiebreak", "ambiguity-no-null-customer-rule"):
            self.assertNotIn(retired, by_name)
        # A contradiction specimen strips no rule at all: every rule string the
        # OMISSION specimens strip survives in both of them.
        for name in self._CONTRADICTIONS:
            low = by_name[name].task.solver_prompt.lower()
            for kept in expected.values():
                self.assertIn(kept, low, f"{name} lost the rule {kept!r}")
        # Stripped rules must survive other specimens. no-left-join also
        # rewords COALESCE; no-null-rule removes it entirely.
        overlapping = {
            ("ambiguity-no-left-join-rule", "belong to no customer"),
        }
        for name, stripped in expected.items():
            low = by_name[name].task.solver_prompt.lower()
            self.assertNotIn(stripped, low, name)
            for other, kept in expected.items():
                if other == name or (name, kept) in overlapping:
                    continue
                self.assertIn(kept, low, f"{name} also lost {kept}")

    def test_ambiguity_injections_also_strip_every_RESTATEMENT_in_the_view(self):
        """A rule deleted from the prose but restated in the MART OUTPUT
        SCHEMAS block was never planted at all.

        This is the harness-3 defect the Round-6 power work found: the critic
        view renders every mart column description, and two of the four
        harness-3 ambiguity specimens left the deleted rule sitting there —
        `completed_order_count: Count of DISTINCT orders in scope; 0 if none`
        restates the dedupe rule AND the null default. A critic that read the
        view and stayed silent scored a MISS on a specimen that planted
        nothing, which is half of why per-specimen hit rates sat near 0.5.
        """
        by_name = {s.name: s for s in metrology_mod.ambiguity_specimens()}
        restatements = {
            "ambiguity-no-dedupe": ("distinct",),
            "ambiguity-no-null-rule": ("0 if none", "coalesce"),
            "ambiguity-no-item-fanout-rule": ("quantity * unit_price",),
            "ambiguity-no-left-join-rule": (
                "including customers with no orders",
                "0 if none",
            ),
        }
        for name, gone in restatements.items():
            view = _critic_view(by_name[name].task).lower()
            for phrase in gone:
                self.assertNotIn(
                    phrase,
                    view,
                    f"{name}: the critic VIEW still restates {phrase!r}, so the "
                    "specimen plants no defect the critic could report",
                )
        # ...and the clean specimen the injectors derive from states them all,
        # or the check above would pass vacuously.
        clean_view = _critic_view(metrology_mod.clean_specimens()[0].task).lower()
        for phrase in ("distinct", "0 if none", "quantity * unit_price",
                       "including customers with no orders", "sort by",
                       "belong to no customer"):
            self.assertIn(phrase, clean_view, phrase)

    def test_ambiguity_contradictions_leave_both_halves_on_screen(self):
        """A CONTRADICTION specimen is unresolvable BY CONSTRUCTION.

        Every omission specimen is one surviving restatement away from planting
        nothing — that is the failure the test above exists to catch, and it
        killed four specimens across two harnesses. A contradiction has no such
        failure mode: the defect IS that two statements the solver reads
        disagree, so no third statement can resolve it. Arbitrating between
        them is precisely the choice the specification fails to make.

        What this test therefore pins is the opposite of the test above: BOTH
        halves must survive in the rendered critic view, in their own blocks.
        If the rewritten column description ever stopped disagreeing — or the
        rule it disagrees with were dropped — the specimen would silently
        become an omission specimen with no de-leak, i.e. vacuous again.
        """
        by_name = {s.name: s for s in metrology_mod.ambiguity_specimens()}
        clean_view = _critic_view(metrology_mod.clean_specimens()[0].task).lower()
        for name, (rewritten, rules) in self._CONTRADICTIONS.items():
            view = _critic_view(by_name[name].task).lower()
            prose, schema = view.split("mart output schemas:", 1)
            # Half one: the rewritten column description, in the MART block,
            # and it is NOT something the clean fixture ever says.
            self.assertIn(rewritten, schema, name)
            self.assertNotIn(rewritten, clean_view, f"{name}: not a tamper at all")
            # Half two: every rule it contradicts, verbatim, in the PROSE.
            for rule in rules:
                self.assertIn(rule, prose, f"{name}: lost the contradicted rule")

    def test_stripping_the_filter_rule_leaves_scope_UNRESOLVABLE(self):
        """Stripping the RULE STRING is not the same as planting a defect.

        Metrology attempts 8 and 9 both scored ambiguity_critic recall 0.50 on
        `ambiguity-no-filter` because the critic returned an EMPTY findings
        list — correctly. The injector removes the FILTER op, but four
        surviving ops still said "completed orders" and `status` is a
        two-value enum, so the scope had exactly one referent and there was no
        second reading to file. The specimen tested the harness, not the
        critic, and the assertion above could not see it: it only checks that
        one literal string is gone.

        The rule now is UNRESOLVABILITY: after the strip, no surviving prose
        may state which orders are in scope. `completed_order_count` survives
        as a MART COLUMN NAME and is explicitly allowed — the ambiguity
        critic's own prompt says a column name is not a rule — so it is
        removed before the check rather than exempted by hand-waving.

        THE SURFACE CHECKED IS THE CRITIC'S, NOT JUST `solver_prompt`. The
        injector rewrites the prose only, but council._critic_view is what the
        ambiguity critic reads, and it appends the PUBLIC SOURCE SCHEMAS and
        MART OUTPUT SCHEMAS blocks built from the untouched IR. Checking the
        prose alone missed a live repair: the mart column descriptions said
        "Count of DISTINCT completed orders" and "items of completed orders",
        restating the stripped FILTER two lines below the rules. Both now say
        "in scope", which dangles exactly as the rules do.
        """
        specimen = {s.name: s for s in metrology_mod.ambiguity_specimens()}[
            "ambiguity-no-filter"
        ]
        # Only the mart column name and source enum may survive this rewrite.
        # The enum appears three times because the critic view includes prose
        # and both public schema renderings; every other occurrence must fail.
        allowed_by_view = {
            "prose": (
                specimen.task.solver_prompt.lower(),
                ("completed_order_count", "status (text; one of cancelled, completed)"),
            ),
            "critic view": (
                _critic_view(specimen.task).lower(),
                (
                    "completed_order_count",
                    "status (text; one of cancelled, completed)",
                    "always exactly one of 'cancelled', 'completed'",
                    "one of: cancelled, completed",
                ),
            ),
        }
        for view_name, (text, allowed_terms) in allowed_by_view.items():
            residue = text
            for allowed in allowed_terms:
                self.assertIn(
                    allowed, residue, f"{view_name}: expected survival missing: {allowed}"
                )
                residue = residue.replace(allowed, "")
            self.assertNotIn(
                "completed",
                residue,
                f"{view_name}: a surviving statement still names the completed "
                "status, so the stripped FILTER is repaired by context and the "
                "specimen plants no detectable defect",
            )
        # The clean prose it was derived from MUST state the scope, or the
        # specimen would be measuring nothing in the other direction.
        clean = metrology_mod.clean_specimens()[0].task.solver_prompt.lower()
        self.assertIn("status = 'completed'", clean)

    def test_population_injections_blind_exactly_one_discriminator(self):
        by_name = {s.name: s for s in metrology_mod.population_specimens()}

        def conditions(specimen):
            return _population_adversary_view(specimen.task).split(
                "POPULATION CONDITIONS:", 1
            )[1].lower()

        for name in ("population-dropped-counterfactual",
                     "population-neutered-counterfactual"):
            self.assertNotIn("no orders", conditions(by_name[name]), name)
        self.assertNotIn(
            "cancelled", conditions(by_name["population-no-cancelled-orders"])
        )
        self.assertNotIn(
            "duplicate order header",
            conditions(by_name["population-no-duplicate-headers"]),
        )
        clean = conditions(metrology_mod.clean_specimens()[0])
        for discriminator in ("no orders", "cancelled", "duplicate order header"):
            self.assertIn(discriminator, clean, discriminator)

    def test_shortcut_injections_make_the_graded_surface_scoreable(self):
        by_name = {s.name: s for s in metrology_mod.shortcut_specimens()}
        self.assertIn(
            "always exactly the same value",
            _critic_view(by_name["shortcut-constant-columns"].task).lower(),
        )
        self.assertIn(
            "equals the customer_id itself",
            _critic_view(by_name["shortcut-identity-columns"].task).lower(),
        )
        dev_only = _critic_view(by_name["shortcut-dev-only-population"].task)
        self.assertNotIn("population primary", dev_only)
        self.assertIn("population development", dev_only)
        cf_only = _critic_view(
            by_name["shortcut-counterfactual-only-population"].task
        )
        self.assertNotIn("population primary", cf_only)
        self.assertIn("population counterfactual", cf_only)

    def test_feasibility_injections_drop_public_schema_elements(self):
        by_name = {s.name: s for s in metrology_mod.feasibility_specimens()}
        missing_status = by_name["feasibility-missing-status"].task.solver_prompt
        # The schema column line is gone, but the rules still DEMAND status —
        # that inconsistency is the planted infeasibility.
        self.assertNotIn("- status (", missing_status.lower())
        self.assertIn("status = 'completed'", missing_status)
        missing_items = by_name[
            "feasibility-missing-items"
        ].task.solver_prompt
        source_projection = missing_items.split(
            "Public source schema:", 1
        )[1].split("Source relationships:", 1)[0]
        self.assertNotIn("- order_items (", source_projection)
        # The semantic rules must still demand the missing source: that
        # source-schema/rule disagreement is the planted infeasibility.
        self.assertIn("order_items", missing_items.split("Rules:", 1)[1])
        price = by_name["feasibility-missing-unit-price"].task.solver_prompt
        self.assertNotIn("- unit_price (", price)
        self.assertIn("order_items", price)
        self.assertIn("SUM(quantity * unit_price)", price)
        quantity = by_name["feasibility-missing-quantity"].task.solver_prompt
        self.assertNotIn("- quantity (", quantity)
        self.assertIn("order_items", quantity)

    def test_dropping_a_column_unpublishes_its_KEYS_AND_RELATIONSHIPS_too(self):
        """A column is published in THREE places, not one.

        `_drop_column` used to edit `TableSpec.columns` alone, leaving
        `business_key=('order_id',)` and the `order_items(order_id) ->
        orders(order_id)` relationship pointing at a column that no longer
        existed. `council._public_source_schema_lines` renders both verbatim
        from the exporter's own functions, so the tampered documentation.md
        block published `order_id` for `orders` TWICE — as the business key and
        as a required join key — while claiming the column was gone. The
        feasibility seat read that correctly and filed nothing:
        `feasibility-missing-order-key` measured 0.0 across five wordings at
        seed 8944342589527049266, and `feasibility-missing-order-customer`
        carries the identical leak on the orders->customers edge.

        Worse than unfair, it was IMPOSSIBLE: `TableSpec._check_columns` and
        `TaskIR._check_integrity` both reject that state, but `model_copy` does
        not re-run validators, so the injector was shipping an IR the pipeline
        could never legitimately build. Both halves are pinned here.

        AUG-13 STRUCTURAL-GATE LANE: `feasibility-missing-order-key` is no
        longer a POOL specimen — its coverage moved to
        `verification/structural_completeness.py` and
        `tests/test_structural_completeness.py`. The `_drop_column` invariant it
        pins here is a property of the INJECTOR, not of the pool, so the tamper
        is built directly instead of being looked up by specimen name; the
        assertion is unchanged and still covers both columns.
        """
        base = metrology_mod.demo_fixture.demo_task()
        for name, table, column in (
            ("feasibility-missing-order-key (retired to the gate)", "orders",
             "order_id"),
            ("feasibility-missing-order-customer", "orders", "customer_id"),
        ):
            task = metrology_mod._drop_column(base, table, column)
            spec = task.table(table)
            self.assertNotIn(column, [c.name for c in spec.columns], name)
            self.assertNotIn(column, spec.primary_key, name)
            self.assertNotIn(column, spec.business_key, name)
            for rel in task.relationships:
                self.assertFalse(
                    (rel.child_table == table and column in rel.child_columns)
                    or (rel.parent_table == table and column in rel.parent_columns),
                    f"{name}: the public schema still publishes {table}.{column} "
                    "as a relationship endpoint",
                )
            # The rendered SOURCE-SCHEMA surface — the block the feasibility
            # seat is told is authoritative — names it nowhere for this table.
            view = _critic_view(task)
            block = view.split("PUBLIC SOURCE SCHEMAS", 1)[1].split(
                "MART OUTPUT SCHEMAS", 1
            )[0]
            table_block = block.split(f"### {table}", 1)[1].split("###", 1)[0]
            self.assertNotIn(column, table_block, f"{name}: leak in the {table} block")
            # ...and the tampered IR is one the IR's own invariants accept.
            metrology_mod.TaskIR.model_validate(task.model_dump())
        # The clean fixture publishes all three surfaces, or the checks above
        # would pass vacuously.
        clean = _critic_view(metrology_mod.clean_specimens()[0].task)
        self.assertIn("business key: order_id", clean)
        self.assertIn("order_items(order_id) -> orders(order_id)", clean)
        self.assertIn("orders(customer_id) -> customers(customer_id)", clean)

    def test_a_tamper_that_leaves_an_INVALID_ir_raises_instead_of_shipping(self):
        """Fail closed: the injectors re-validate, they do not model_copy."""
        clean = metrology_mod.clean_specimens()[0].task
        with self.assertRaises(ValueError):
            metrology_mod._drop_column(clean, "orders", "no_such_column")
        with self.assertRaises(ValueError):
            metrology_mod._drop_column(clean, "no_such_table", "order_id")
        with self.assertRaises(ValueError):
            metrology_mod._drop_table(clean, "no_such_table")

    def test_every_specimen_is_leak_free_and_target_view_is_distinct(self):
        specimens = metrology_mod.specimen_pool()
        cleans = [s for s in specimens if s.kind == "clean"]
        for specimen in specimens:
            self.assertEqual(leak_findings(specimen.task), [], specimen.name)
            if specimen.kind != "tampered":
                continue
            tampered_view = _view_for(specimen.target_role, specimen.task)
            for clean in cleans:
                self.assertNotEqual(
                    tampered_view,
                    _view_for(specimen.target_role, clean.task),
                    f"{specimen.name}: defect invisible to its target role",
                )


class SeededSelectionTest(unittest.TestCase):
    """The mix is SAMPLED, and reproducible from its seed and nothing else."""

    def test_draw_shape_is_fixed_and_reproducible_per_seed(self):
        for seed in _SEEDS:
            with self.subTest(seed=seed):
                mix = metrology_mod.select_specimens(seed)
                self.assertEqual(len(mix), _DRAWN_SPECIMENS)
                self.assertEqual(
                    [s.name for s in mix],
                    [s.name for s in metrology_mod.select_specimens(seed)],
                )
                self.assertEqual(
                    len([s for s in mix if s.kind == "clean"]),
                    metrology_mod.CLEAN_PER_RUN,
                )
                for role in CRITIC_ROLES:
                    self.assertEqual(
                        len([s for s in mix if s.target_role is role]),
                        metrology_mod.TAMPERED_PER_ROLE,
                        role.value,
                    )
                self.assertEqual(
                    len({s.name for s in mix}), len(mix), "duplicate specimen"
                )

    def test_different_seeds_draw_different_schedules(self):
        schedules = {
            seed: tuple(s.name for s in metrology_mod.select_specimens(seed))
            for seed in range(24)
        }
        # Sampling AND shuffling: neither the membership nor the order is a
        # constant of the harness.
        self.assertGreater(len(set(schedules.values())), 20)
        self.assertGreater(
            len({frozenset(names) for names in schedules.values()}), 8
        )

    def test_every_run_holds_specimens_out(self):
        pool = {s.name for s in metrology_mod.specimen_pool()}
        for seed in _SEEDS:
            drawn = {s.name for s in metrology_mod.select_specimens(seed)}
            self.assertTrue(pool - drawn, seed)

    def test_position_does_not_predict_defect_class(self):
        """No index in the schedule belongs to one class across seeds."""
        seen: dict[int, set[str]] = {}
        for seed in range(64):
            for index, specimen in enumerate(metrology_mod.select_specimens(seed)):
                label = (
                    specimen.target_role.value if specimen.target_role else "clean"
                )
                seen.setdefault(index, set()).add(label)
        for index, labels in seen.items():
            self.assertGreater(len(labels), 1, f"position {index} is class-fixed")


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


class ThresholdConfigTest(unittest.TestCase):
    def test_repo_config_metrology_block_loads(self):
        thresholds = metrology_mod.load_metrology_thresholds(None)
        self.assertEqual(thresholds.min_recall, 0.75)
        self.assertEqual(thresholds.min_precision, 0.5)
        self.assertEqual(thresholds.max_nitpick_rate, 0.75)

    def test_explicit_missing_path_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            metrology_mod.load_metrology_thresholds(Path("/nonexistent/agents.yaml"))

    def test_explicit_config_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.yaml"
            path.write_text(
                "metrology:\n  min_recall: 1.0\n  min_precision: 0.9\n"
                "  max_nitpick_rate: 0.1\n",
                encoding="utf-8",
            )
            thresholds = metrology_mod.load_metrology_thresholds(path)
        self.assertEqual(thresholds.min_recall, 1.0)
        self.assertEqual(thresholds.min_precision, 0.9)
        self.assertEqual(thresholds.max_nitpick_rate, 0.1)


# ---------------------------------------------------------------------------
# Scoring: the benchmark is not gameable
# ---------------------------------------------------------------------------


class BoilerplateNotGameableTest(unittest.TestCase):
    def test_input_ignoring_provider_scores_zero_recall_and_is_blocked(self):
        for seed in _SEEDS:
            with self.subTest(seed=seed):
                report = metrology_mod.run_metrology(
                    BoilerplateProvider(), seed=seed
                )
                self.assertFalse(report.admitted)
                for role in CRITIC_ROLES:
                    metrics = report.per_role[role.value]
                    self.assertEqual(metrics.recall, 0.0, role.value)
                    self.assertEqual(metrics.detected_count, 0, role.value)
                    # It complains about everything, including clean...
                    self.assertEqual(metrics.nitpick_rate, 1.0, role.value)
                    # ...and none of its detections are real.
                    self.assertEqual(metrics.precision, 0.0, role.value)
                    self.assertFalse(report.role_pass[role.value])

    def test_marker_refuses_a_non_admitted_report(self):
        report = metrology_mod.run_metrology(BoilerplateProvider(), seed=_SEEDS[0])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                _write_admission_marker(Path(tmp), report)
            self.assertFalse(metrology_mod.live_admission_ok(Path(tmp)))


class OrderReplayNotGameableTest(unittest.TestCase):
    """ITEM D1 — the attack the fixed suite was open to, and its fix.

    A provider that reads NOTHING and replays a canned schedule scored full
    marks against the published fixed order. Seeded sampling makes the same
    provider answer the wrong questions.
    """

    @staticmethod
    def _legacy_fixed_suite():
        """The pre-fix schedule: the whole pool in sha256(name) order.

        That is exactly what `all_specimens()` used to return — a fixed suite
        in a publicly derivable order — reconstructed here from the pool so
        the 'before' number is measured, not remembered."""
        return metrology_mod.specimen_pool()

    def test_a_canned_replay_beats_the_old_fixed_schedule(self):
        schedule = self._legacy_fixed_suite()
        provider = OrderReplayProvider(schedule)
        report = metrology_mod.run_metrology(provider, specimens=schedule)
        # It never looked at a prompt, and it is admitted.
        self.assertTrue(report.admitted)
        for role in CRITIC_ROLES:
            self.assertEqual(report.per_role[role.value].recall, 1.0, role.value)

    def test_the_same_replay_collapses_against_the_seeded_sample(self):
        schedule = self._legacy_fixed_suite()
        for seed in _SEEDS:
            with self.subTest(seed=seed):
                report = metrology_mod.run_metrology(
                    OrderReplayProvider(schedule), seed=seed
                )
                self.assertFalse(report.admitted, "order replay still admitted")
                # Some canned answers land by luck; none of the roles clears
                # the bar, which is what admission depends on.
                self.assertFalse(any(report.role_pass.values()))

    def test_replaying_a_schedule_drawn_from_another_seed_also_fails(self):
        """Knowing the harness samples does not help without knowing the seed."""
        for guess, actual in ((11, 12), (2024, 2025)):
            with self.subTest(guess=guess, actual=actual):
                provider = OrderReplayProvider(
                    metrology_mod.select_specimens(guess)
                )
                report = metrology_mod.run_metrology(provider, seed=actual)
                self.assertFalse(report.admitted)


class AnchorAxisTest(unittest.TestCase):
    """ITEM D1 — class vocabulary alone is not a detection any more."""

    def test_class_echo_without_an_anchor_scores_zero_recall(self):
        for seed in _SEEDS:
            with self.subTest(seed=seed):
                report = metrology_mod.run_metrology(ClassEchoProvider(), seed=seed)
                self.assertFalse(report.admitted)
                for role in CRITIC_ROLES:
                    self.assertEqual(
                        report.per_role[role.value].recall, 0.0, role.value
                    )

    def test_the_same_echo_had_full_recall_under_substring_only_scoring(self):
        """The BEFORE number, measured rather than remembered: with the anchor
        axis switched off, the echo buys full recall on every role while
        reading nothing. Recall is the release-blocking bar, so that credit
        mattered."""
        original = metrology_mod._finding_detects
        metrology_mod._finding_detects = lambda finding, specimen: (
            metrology_mod._finding_matches(finding, specimen.detection_terms)
        )
        try:
            report = metrology_mod.run_metrology(ClassEchoProvider(), seed=_SEEDS[0])
        finally:
            metrology_mod._finding_detects = original
        for role in CRITIC_ROLES:
            self.assertEqual(report.per_role[role.value].recall, 1.0, role.value)

    def test_both_axes_must_land_on_the_same_finding(self):
        from elt_taskgen.models import Finding, RepairRoute, Severity

        specimen = next(
            s for s in metrology_mod.specimen_pool()
            if s.name == "feasibility-missing-status"
        )

        def finding(summary: str, detail: str = "") -> Finding:
            return Finding(
                finding_id="t-00",
                role=CouncilRole.FEASIBILITY_REVIEWER,
                severity=Severity.MAJOR,
                summary=summary,
                detail=detail,
                route_hint=RepairRoute.SPECIFICATION,
                suggested_attack=None,
            )

        detects = metrology_mod._finding_detects
        # Anchor only ("status") — this is what used to score.
        self.assertFalse(detects(finding("The status filter looks fine."), specimen))
        # Class only.
        self.assertFalse(
            detects(finding("Something needed is missing from the schema."), specimen)
        )
        # Both, one finding.
        self.assertTrue(
            detects(
                finding("The orders.status column is missing from the schema."),
                specimen,
            )
        )
        # Both, but split across summary and detail of ONE finding: allowed.
        self.assertTrue(
            detects(finding("status is not usable here", "no such column"), specimen)
        )

    def test_feasibility_absence_aliases_score_retained_semantic_hits(self):
        """Grammatical absence aliases do not turn off the anchor axis.

        A live Opus diagnostic under the proof-obligation prompt returned
        semantically correct fatal findings on all 25 tampered trials, but
        seven scored as misses solely because `_MISSING_TERMS` recognized
        ``not published`` while not recognizing ordinary equivalents such as
        ``does not publish``.  These are representative retained summaries;
        each still has to name that specimen's independently planted anchor.
        """
        from elt_taskgen.models import Finding, RepairRoute, Severity

        specimens = {s.name: s for s in metrology_mod.specimen_pool()}

        def finding(summary: str) -> Finding:
            return Finding(
                finding_id="retained-live-00",
                role=CouncilRole.FEASIBILITY_REVIEWER,
                severity=Severity.FATAL,
                summary=summary,
                detail="",
                route_hint=RepairRoute.SPECIFICATION,
                suggested_attack=None,
            )

        cases = {
            "feasibility-missing-order-customer": (
                "The orders table publishes no customer_id column, so every "
                "per-customer mart measure is uncomputable."
            ),
            "feasibility-missing-items": (
                "The line-items table with quantity and unit_price is never "
                "published, so total_spend is uncomputable."
            ),
            "feasibility-missing-status@clinic_visits": (
                "Rule 1 needs visits.status, which the published visits schema "
                "does not carry."
            ),
            "feasibility-unpublished-rate-input@clinic_visits": (
                "fee_total requires visits.currency_rate, which the public "
                "schema does not publish."
            ),
        }
        for name, summary in cases.items():
            with self.subTest(specimen=name):
                self.assertTrue(
                    metrology_mod._finding_detects(finding(summary), specimens[name])
                )

        # The alias alone is still class-vocabulary echo, and the anchor alone
        # is still a column listing. Neither can earn a hit independently.
        order_customer = specimens["feasibility-missing-order-customer"]
        self.assertFalse(
            metrology_mod._finding_detects(
                finding("The public schema does not publish everything."),
                order_customer,
            )
        )
        self.assertFalse(
            metrology_mod._finding_detects(
                finding("orders.customer_id participates in the mart."),
                order_customer,
            )
        )

    def test_no_filter_anchors_do_not_admit_a_bare_column_listing(self):
        """ATTEMPT 8 REGRESSION PIN. 'completed_order_count' used to be an
        anchor on ambiguity-no-filter, which made the anchor axis vacuous:
        the token appears in ANY finding that enumerates the mart's columns.
        Attempt 7's opus run scored a DETECTION of this specimen with a
        finding about customer_name that never mentions the status filter,
        which inflated recall to 1.00 on the same boilerplate that destroyed
        precision — one defect, two opposite scoring errors. The anchors are
        now 'status' / 'cancelled' only, and the bare generics 'never says'
        and 'not stated' are off the class axis.

        The verbatim attempt-7 finding is the negative case here; the
        verbatim attempt-5 sonnet finding on the SAME specimen is the
        positive case, so the narrowing is pinned as recall-preserving and
        not merely as recall-reducing."""
        from elt_taskgen.models import Finding, RepairRoute, Severity

        specimen = next(
            s for s in metrology_mod.specimen_pool()
            if s.name == "ambiguity-no-filter"
        )
        self.assertNotIn("completed_order_count", specimen.anchor_terms)

        def finding(summary: str, detail: str = "") -> Finding:
            return Finding(
                finding_id="t-00",
                role=CouncilRole.AMBIGUITY_CRITIC,
                severity=Severity.MINOR,
                summary=summary,
                detail=detail,
                route_hint=RepairRoute.SPECIFICATION,
                suggested_attack=None,
            )

        detects = metrology_mod._finding_detects
        # Attempt 7, opus, VERBATIM — the finding that used to score.
        self.assertFalse(
            detects(
                finding(
                    "customer_name is published in the source schema but the "
                    "mart schema omits it, and the prose never says whether "
                    "it belongs in the output.",
                    "The mart customer_summary output schema lists only "
                    "customer_id, completed_order_count, total_spend, while "
                    "the prose describes customers with 'customer_name "
                    "(text): Display name of the customer.'",
                ),
                specimen,
            )
        )
        # Attempt 5, sonnet, VERBATIM — a real reading of the missing filter.
        self.assertTrue(
            detects(
                finding(
                    "Unclear whether 'completed order' filtering in Rule 4 "
                    "applies before or after item-total aggregation.",
                    "Rule 2 computes item totals 'grouped by order_id' "
                    "without restricting to completed orders first, so item "
                    "totals may be computed over ALL order_items for ALL "
                    "orders (completed and cancelled).",
                ),
                specimen,
            )
        )


class _ScriptedRoleProvider(TrialSeam):
    """Answers ONE role with a fixed findings payload and every other role
    with an empty list — isolates one role's metrology accounting."""

    def __init__(self, role: CouncilRole, findings: list[dict]):
        self._role = role
        self._payload = json.dumps({"findings": findings})

    def complete(self, role: CouncilRole, prompt: str) -> str:
        if role is self._role:
            return self._payload
        return json.dumps({"findings": []})


class ShortcutProbeReportScoringTest(unittest.TestCase):
    """HARNESS VERSION 3 — defect-claim vs probe-report accounting.

    The shortcut attacker's stage contract (cli.make_review_runner) forbids
    silence: at least one finding must carry an executable suggested_attack
    on EVERY task, clean ones included. Metrology therefore counts, for that
    role only, DEFECT CLAIMS ('major'/'fatal', or 'minor' without an
    executable attack) — a probe report ('minor' WITH a suggested_attack the
    role expects the data to defeat) scores as neither a false alarm nor a
    detection. These tests pin the arithmetic with constructed findings so
    the channel is provably not a loophole: all-'minor' spam has zero recall,
    'major' spam gets the clean false alarms back, and the other three roles
    keep the strict any-finding-on-clean rule.
    """

    #: Text that satisfies BOTH scoring axes on both drawn shortcut specimens
    #: (detection term 'constant'/'hard-cod' + anchor 'total_spend'), and is
    #: grounded so the deterministic screen leaves it alone.
    _EXPLOIT_SUMMARY = (
        "total_spend is effectively constant and can be hard-coded for "
        "full reward."
    )
    _EXPLOIT_DETAIL = (
        "customer_summary's total_spend and completed_order_count can be "
        "emitted without solving the task."
    )
    #: A diligence probe report: NO detection vocabulary consequences matter
    #: on clean specimens, but the detail must stay grounded for the screen.
    _PROBE_SUMMARY = (
        "Diligence probe: hard-coded constants over customer_summary should "
        "lose reward on a hidden population."
    )
    _PROBE_DETAIL = (
        "Emit fixed values for total_spend; expect the hidden graded "
        "populations to defeat this probe. Run it as standing evidence."
    )

    @staticmethod
    def _mix():
        """A controlled four-specimen run: two cleans + the two constant/
        identity shortcut specimens (drawn from the pool, not invented)."""
        cleans = tuple(metrology_mod.clean_specimens()[:2])
        by_name = {s.name: s for s in metrology_mod.shortcut_specimens()}
        return cleans + (
            by_name["shortcut-constant-columns"],
            by_name["shortcut-identity-columns"],
        )

    @staticmethod
    def _powered_trials():
        """The controlled mix at the replication a real run executes.

        The two specimens this class's scripted providers are written against
        (constant columns and identity columns), each answered
        REPLICATES_TAMPERED times, plus the clean draw at REPLICATES_CLEAN.
        The mix is chosen by hand rather than by the seed so a scoring test
        can reach a VERDICT without depending on the draw.
        """
        by_name = {s.name: s for s in metrology_mod.shortcut_specimens()}
        shortcuts = (
            by_name["shortcut-constant-columns"],
            by_name["shortcut-identity-columns"],
        )
        cleans = metrology_mod.clean_specimens()[: metrology_mod.CLEAN_PER_RUN]
        trials = [
            metrology_mod.Trial(
                specimen=s, variant=r, replicate=r,
                roles=(CouncilRole.SHORTCUT_ATTACKER,),
            )
            for s in shortcuts
            for r in range(metrology_mod.REPLICATES_TAMPERED)
        ]
        trials += [
            metrology_mod.Trial(
                specimen=s, variant=r, replicate=r, roles=tuple(CRITIC_ROLES)
            )
            for s in cleans
            for r in range(metrology_mod.REPLICATES_CLEAN)
        ]
        return tuple(trials)

    def _run(self, findings: list[dict], role=CouncilRole.SHORTCUT_ATTACKER):
        return metrology_mod.run_metrology(
            _ScriptedRoleProvider(role, findings), specimens=self._mix()
        )

    def _shortcut_metrics(self, report):
        return report.per_role[CouncilRole.SHORTCUT_ATTACKER.value]

    def test_probe_report_on_clean_is_not_a_false_alarm(self):
        report = self._run(
            [_finding(self._PROBE_SUMMARY, self._PROBE_DETAIL,
                      severity="minor", suggested_attack="constants")]
        )
        metrics = self._shortcut_metrics(report)
        self.assertEqual(metrics.false_alarms, 0)
        self.assertEqual(metrics.nitpick_rate, 0.0)
        # ...and the probe is still visible on the specimen record.
        for result in report.specimens:
            self.assertEqual(
                result.findings_by_role.get(CouncilRole.SHORTCUT_ATTACKER.value),
                1,
                result.name,
            )

    def test_all_minor_probe_spam_scores_zero_recall_and_is_blocked(self):
        """The symmetry that keeps the channel honest: the SAME text that
        would detect the planted exploit does NOT score at probe severity."""
        report = self._run(
            [_finding(self._EXPLOIT_SUMMARY, self._EXPLOIT_DETAIL,
                      severity="minor", suggested_attack="constants")]
        )
        metrics = self._shortcut_metrics(report)
        self.assertEqual(metrics.recall, 0.0)
        self.assertEqual(metrics.detected_count, 0)
        self.assertEqual(metrics.false_alarms, 0)
        self.assertFalse(report.role_pass[CouncilRole.SHORTCUT_ATTACKER.value])
        self.assertFalse(report.admitted)

    def test_major_defect_claim_on_clean_is_still_a_false_alarm(self):
        report = self._run(
            [_finding(self._EXPLOIT_SUMMARY, self._EXPLOIT_DETAIL,
                      severity="major", suggested_attack="constants")]
        )
        metrics = self._shortcut_metrics(report)
        # Both tampered specimens detected (the claim is real there)...
        self.assertEqual(metrics.recall, 1.0)
        # ...but both cleans are false alarms: 'major' spam is blocked.
        self.assertEqual(metrics.false_alarms, 2)
        self.assertEqual(metrics.nitpick_rate, 1.0)
        self.assertFalse(report.role_pass[CouncilRole.SHORTCUT_ATTACKER.value])

    def test_minor_without_an_attack_is_a_defect_claim(self):
        """An asserted defect with no executable probe is not diligence —
        BoilerplateProvider-shaped findings stay false alarms on clean."""
        report = self._run(
            [_finding(self._EXPLOIT_SUMMARY, self._EXPLOIT_DETAIL,
                      severity="minor", suggested_attack=None)]
        )
        metrics = self._shortcut_metrics(report)
        self.assertEqual(metrics.false_alarms, 2)
        self.assertFalse(report.role_pass[CouncilRole.SHORTCUT_ATTACKER.value])

    def test_info_observations_count_as_neither(self):
        report = self._run(
            [_finding(self._PROBE_SUMMARY, self._PROBE_DETAIL,
                      severity="info", suggested_attack=None)]
        )
        metrics = self._shortcut_metrics(report)
        self.assertEqual(metrics.false_alarms, 0)
        self.assertEqual(metrics.detected_count, 0)

    def test_mixed_response_passes_on_the_controlled_mix(self):
        """A defect claim where the exploit is planted plus a probe report
        everywhere is exactly the recorded live behavior — and it passes."""

        class MixedProvider:
            _EXPLOIT = self._EXPLOIT_SUMMARY
            _EXPLOIT_D = self._EXPLOIT_DETAIL
            _PROBE = self._PROBE_SUMMARY
            _PROBE_D = self._PROBE_DETAIL

            def complete(self, role, prompt):
                if role is not CouncilRole.SHORTCUT_ATTACKER:
                    return json.dumps({"findings": []})
                low = " ".join(prompt.lower().split())
                findings = []
                if ("always exactly the same value" in low
                        or "equals the customer_id itself" in low):
                    findings.append(_finding(
                        self._EXPLOIT, self._EXPLOIT_D,
                        severity="major", suggested_attack="constants",
                    ))
                else:
                    findings.append(_finding(
                        self._PROBE, self._PROBE_D,
                        severity="minor", suggested_attack="constants",
                    ))
                return json.dumps({"findings": findings})

        # The replicated schedule supplies enough trials for the Wilson bound;
        # the four-specimen mix above only checks the raw rates.
        small = metrology_mod.run_metrology(
            MixedProvider(), specimens=self._mix()
        )
        small_metrics = self._shortcut_metrics(small)
        self.assertEqual(small_metrics.recall, 1.0)
        self.assertEqual(small_metrics.precision, 1.0)
        self.assertEqual(small_metrics.nitpick_rate, 0.0)
        self.assertFalse(
            small.role_pass[CouncilRole.SHORTCUT_ATTACKER.value],
            "two perfect trials must NOT be enough to admit a seat",
        )

        report = metrology_mod.run_metrology(
            MixedProvider(), trials=self._powered_trials()
        )
        metrics = self._shortcut_metrics(report)
        self.assertEqual(metrics.recall, 1.0)
        self.assertEqual(metrics.precision, 1.0)
        self.assertEqual(metrics.nitpick_rate, 0.0)
        self.assertTrue(report.role_pass[CouncilRole.SHORTCUT_ATTACKER.value])

    def test_screen_deduplicated_probe_is_still_a_probe_report(self):
        """The screen coalesces same-mutant probes and MOVES the duplicate's
        suggested_attack into its screen record; that must not turn a probe
        report into a defect claim on a clean specimen."""
        report = self._run(
            [
                _finding(self._PROBE_SUMMARY, self._PROBE_DETAIL,
                         severity="minor", suggested_attack="constants"),
                _finding(self._PROBE_SUMMARY + " (second angle)",
                         self._PROBE_DETAIL,
                         severity="minor", suggested_attack="constants"),
            ]
        )
        metrics = self._shortcut_metrics(report)
        self.assertEqual(metrics.false_alarms, 0)
        self.assertEqual(metrics.nitpick_rate, 0.0)

    def test_other_roles_keep_the_strict_any_finding_rule(self):
        """The refinement is the shortcut attacker's alone: a minor finding
        WITH an executable attack from any other role is still a false alarm
        on every clean specimen."""
        for role in (CouncilRole.AMBIGUITY_CRITIC,
                     CouncilRole.POPULATION_ADVERSARY,
                     CouncilRole.FEASIBILITY_REVIEWER):
            with self.subTest(role=role.value):
                findings = [_finding(
                    "customer_summary ordering could be read two ways.",
                    "total_spend might sort differently across readings.",
                    severity="minor",
                    suggested_attack="inner_join",
                )]
                if role is CouncilRole.POPULATION_ADVERSARY:
                    for f in findings:
                        f["proposed_case"] = None
                report = metrology_mod.run_metrology(
                    _ScriptedRoleProvider(role, findings),
                    specimens=self._mix(),
                )
                self.assertEqual(report.per_role[role.value].false_alarms, 2)
                self.assertEqual(report.per_role[role.value].nitpick_rate, 1.0)

    def test_defect_claim_predicate_directly(self):
        from elt_taskgen.models import (
            AttackKind,
            Finding,
            FindingScreen,
            FindingScreenStatus,
            Severity,
        )

        def finding(severity, attack=None, screen=None):
            return Finding(
                finding_id="t-00",
                role=CouncilRole.SHORTCUT_ATTACKER,
                severity=severity,
                summary="s",
                detail="d",
                suggested_attack=attack,
                screen=screen,
            )

        claim = metrology_mod._shortcut_defect_claim
        self.assertTrue(claim(finding(Severity.FATAL)))
        self.assertTrue(claim(finding(Severity.MAJOR)))
        self.assertTrue(claim(finding(Severity.MAJOR, AttackKind.CONSTANTS)))
        self.assertTrue(claim(finding(Severity.MINOR)))  # claim, no probe
        self.assertFalse(claim(finding(Severity.MINOR, AttackKind.CONSTANTS)))
        self.assertFalse(claim(finding(Severity.INFO)))
        self.assertFalse(claim(finding(Severity.INFO, AttackKind.CONSTANTS)))
        # Screen-dedup'd probe: attack moved into the DUPLICATE record.
        dedup = FindingScreen(
            status=FindingScreenStatus.DUPLICATE,
            duplicate_of="t-01",
            claimed_severity=Severity.MINOR,
            withheld_attack=AttackKind.CONSTANTS,
        )
        self.assertFalse(claim(finding(Severity.MINOR, None, dedup)))
        # Screen-VOIDed boilerplate (claimed minor, no attack anywhere): the
        # screen's demotion to INFO must not launder a defect claim...
        voided_claim = FindingScreen(
            status=FindingScreenStatus.VOID,
            signals=("ungrounded_detail",),
            claimed_severity=Severity.MINOR,
        )
        self.assertTrue(claim(finding(Severity.INFO, None, voided_claim)))
        # ...while a VOIDed minor PROBE stays a probe report.
        voided_probe = FindingScreen(
            status=FindingScreenStatus.VOID,
            signals=("ungrounded_detail",),
            claimed_severity=Severity.MINOR,
            withheld_attack=AttackKind.CONSTANTS,
        )
        self.assertFalse(claim(finding(Severity.INFO, None, voided_probe)))


class ProbeExemptionScopedToShortcutTest(unittest.TestCase):
    """REGRESSION PIN (post attempt 5, seed 8944342589527049266): the
    harness-v3 probe-report exemption is the shortcut attacker's ALONE, on
    BOTH scoring legs. For every other role the v2 semantics hold:

      clean leg      ANY finding on a clean specimen is a false alarm
                     (pinned by test_other_roles_keep_the_strict_any_finding_
                     rule above);
      detection leg  ANY finding whose text carries both scoring axes is a
                     detection — severity and suggested_attack play NO role
                     (pinned HERE).

    Attempt 5's briefing hypothesized that ambiguity_critic's recall
    regression came from the probe-report projection leaking into other
    roles' detection counting (a minor+suggested_attack finding silently
    discarded). Code audit refuted that — _scoreable filters only for
    SHORTCUT_ATTACKER — and these constructed-findings tests make the
    refutation permanent: if the projection ever leaks, the minor+attack
    detection below stops counting and this test fails.
    """

    @staticmethod
    def _mix(tamper_name: str):
        """Two cleans + ONE named tampered specimen from the pool."""
        cleans = tuple(metrology_mod.clean_specimens()[:2])
        pool = {s.name: s for s in metrology_mod.specimen_pool()}
        return cleans + (pool[tamper_name],)

    def _run(self, role, tamper_name, findings):
        report = metrology_mod.run_metrology(
            _ScriptedRoleProvider(role, findings),
            specimens=self._mix(tamper_name),
        )
        return report.per_role[role.value]

    def test_ambiguity_minor_with_attack_that_names_the_defect_detects(self):
        """THE regression test: an ambiguity_critic finding at severity
        'minor' CARRYING an executable suggested_attack — the exact shape the
        shortcut projection would discard — still scores as a detection when
        its text lands both axes (detection term 'duplicate' + anchor
        'order_id' for ambiguity-no-dedupe)."""
        metrics = self._run(
            CouncilRole.AMBIGUITY_CRITIC,
            "ambiguity-no-dedupe",
            [_finding(
                "The prose never says whether duplicate order header rows "
                "count once or twice.",
                "orders can repeat an order_id under stress and the spec "
                "text gives no rule for it.",
                severity="minor",
                suggested_attack="inner_join",
            )],
        )
        self.assertEqual(metrics.detected_count, 1)
        self.assertEqual(metrics.recall, 1.0)
        # The same finding on the two cleans stays a false alarm: the
        # detection leg is exempt from nothing, and so is the clean leg.
        self.assertEqual(metrics.false_alarms, 2)

    def test_population_minor_with_attack_that_names_the_defect_detects(self):
        """Same pin for the population adversary (the other rerouted role):
        minor + suggested_attack + proposed_case=None, text landing
        _DEDUPE_BLIND_TERMS ('duplicate') + anchor ('order_id') on
        population-no-duplicate-headers."""
        finding = _finding(
            "No population declares duplicate order header rows, so a "
            "missing dedupe would still score full reward.",
            "The stress conditions never force a repeated order_id.",
            severity="minor",
            suggested_attack="inner_join",
        )
        finding["proposed_case"] = None
        metrics = self._run(
            CouncilRole.POPULATION_ADVERSARY,
            "population-no-duplicate-headers",
            [finding],
        )
        self.assertEqual(metrics.detected_count, 1)
        self.assertEqual(metrics.recall, 1.0)
        self.assertEqual(metrics.false_alarms, 2)

    def test_shortcut_keeps_the_exemption_on_both_legs(self):
        """The scoping's other face, on the SAME harness paths: a shortcut
        probe report (minor WITH attack) is invisible to BOTH counters —
        no false alarm on the cleans AND no detection on the tamper, even
        when its text lands both axes."""
        metrics = self._run(
            CouncilRole.SHORTCUT_ATTACKER,
            "shortcut-constant-columns",
            [_finding(
                "total_spend is effectively constant and can be hard-coded "
                "for full reward.",
                "Probe: emit fixed values for customer_summary.total_spend; "
                "expect the hidden populations to defeat this.",
                severity="minor",
                suggested_attack="constants",
            )],
        )
        self.assertEqual(metrics.false_alarms, 0)
        self.assertEqual(metrics.detected_count, 0)

    # Info findings remain scoreable so critics cannot hide false alarms by
    # lowering severity; the prompt must reject non-output-affecting claims.

    def test_info_severity_on_a_clean_specimen_is_still_a_false_alarm(self):
        """THE anti-laundering pin. The exact attempt-7 boilerplate, refiled
        at 'info': still two false alarms on two cleans."""
        metrics = self._run(
            CouncilRole.AMBIGUITY_CRITIC,
            "ambiguity-no-dedupe",
            [_finding(
                "Mart output schema omits customer_name while the source "
                "publishes it; prose never says whether it belongs in "
                "customer_summary.",
                "The declared output schema settles this, so it cannot "
                "change a schema-following solver's graded columns.",
                severity="info",
            )],
        )
        self.assertEqual(metrics.false_alarms, 2)
        self.assertEqual(metrics.nitpick_rate, 1.0)

    def test_info_severity_still_counts_as_a_detection(self):
        """The exemption's other face: because 'info' is NOT filtered for
        this role, an info finding that lands both axes still earns recall.
        Suppressing info would have cost recall as well as buying precision
        — the two counters read the same projection."""
        metrics = self._run(
            CouncilRole.AMBIGUITY_CRITIC,
            "ambiguity-no-dedupe",
            [_finding(
                "The prose never says whether duplicate order header rows "
                "count once or twice.",
                "orders can repeat an order_id under stress and the spec "
                "text gives no rule for it.",
                severity="info",
            )],
        )
        self.assertEqual(metrics.detected_count, 1)
        self.assertEqual(metrics.recall, 1.0)

    def test_shortcut_info_stays_exempt_on_both_legs(self):
        """Only the shortcut attacker's channel is filtered, and 'info' is
        the part of it _shortcut_defect_claim has always dropped."""
        metrics = self._run(
            CouncilRole.SHORTCUT_ATTACKER,
            "shortcut-constant-columns",
            [_finding(
                "total_spend is effectively constant and can be hard-coded "
                "for full reward.",
                "Observation only.",
                severity="info",
            )],
        )
        self.assertEqual(metrics.false_alarms, 0)
        self.assertEqual(metrics.detected_count, 0)


class BoilerplateStillBlockedTest(unittest.TestCase):
    """GAMEABILITY. BoilerplateProvider is the always-complain strategy the
    harness exists to reject; BoilerplateNotGameableTest pins that it fails
    on 'minor'. Attempt 8 considered exempting 'info' for every role, so the
    same fixture is re-run with every finding relabelled 'info' — it must
    still be blocked, on every role, for the same reason."""

    class _InfoBoilerplateProvider(BoilerplateProvider):
        def complete(self, role: CouncilRole, prompt: str) -> str:
            payload = json.loads(super().complete(role, prompt))
            for f in payload["findings"]:
                f["severity"] = "info"
            return json.dumps(payload)

    def test_info_relabelled_boilerplate_is_not_admitted(self):
        report = metrology_mod.run_metrology(
            self._InfoBoilerplateProvider(), seed=_SEEDS[0]
        )
        self.assertFalse(report.admitted)
        scored = [
            m for m in report.per_role.values()
            if m.role != CouncilRole.SHORTCUT_ATTACKER.value
        ]
        self.assertTrue(scored)
        for m in scored:
            with self.subTest(role=m.role):
                # It complains on every clean specimen, at every severity.
                self.assertGreater(m.false_alarms, 0, m.role)


# ---------------------------------------------------------------------------
# End-to-end against the oracle instrument
# ---------------------------------------------------------------------------


class OracleEndToEndTest(unittest.TestCase):
    def test_defect_referencing_provider_is_admitted_on_every_seed(self):
        """The gate must be passable: a perfect seat is admitted on every seed
        tried. A measurement nobody can clear is not a stricter measurement,
        it is a broken one."""
        for seed in _SEEDS:
            with self.subTest(seed=seed):
                report = metrology_mod.run_metrology(
                    OracleProvider(), routing_fingerprint="fp-test", seed=seed
                )
                self.assertTrue(report.admitted)
                for role in CRITIC_ROLES:
                    metrics = report.per_role[role.value]
                    self.assertEqual(metrics.recall, 1.0, role.value)
                    self.assertEqual(metrics.precision, 1.0, role.value)
                    self.assertEqual(metrics.nitpick_rate, 0.0, role.value)
                    self.assertTrue(report.role_pass[role.value])
                tampered = [s for s in report.specimens if s.kind == "tampered"]
                # TRIALS, not specimens: every drawn specimen is answered
                # REPLICATES_TAMPERED times, at that many distinct surface
                # wordings, and each answer is one row.
                self.assertEqual(
                    len(tampered),
                    metrology_mod.TAMPERED_PER_ROLE
                    * metrology_mod.REPLICATES_TAMPERED
                    * len(CRITIC_ROLES),
                )
                self.assertEqual(
                    len({(s.name, s.replicate) for s in tampered}), len(tampered)
                )
                for role in CRITIC_ROLES:
                    metrics = report.per_role[role.value]
                    self.assertEqual(
                        metrics.distinct_specimens, metrology_mod.TAMPERED_PER_ROLE
                    )
                    self.assertEqual(
                        metrics.replicates, metrology_mod.REPLICATES_TAMPERED
                    )
                    # A perfect seat's LOWER bound clears the bar; that is the
                    # whole verdict now, and the point estimate is a report.
                    self.assertGreaterEqual(
                        metrics.recall_lb, report.thresholds.min_recall, role.value
                    )
                    self.assertEqual(
                        set(metrics.per_specimen.values()), {1.0}, role.value
                    )
                self.assertTrue(all(s.detected for s in tampered))

    def test_every_pool_specimen_is_detectable_by_a_diligent_reader(self):
        """Not just the sampled ones: every specimen the pool can DRAW must be
        catchable, or a run would fail on the luck of the draw."""
        report = metrology_mod.run_metrology(
            OracleProvider(), specimens=metrology_mod.specimen_pool()
        )
        undetected = [
            s.name for s in report.specimens if s.kind == "tampered" and not s.detected
        ]
        self.assertEqual(undetected, [])
        # Clean specimens carry the shortcut attacker's diligence probe
        # reports and NOTHING else — and none of them count as false alarms.
        noisy = [
            s.name
            for s in report.specimens
            if s.kind == "clean"
            and set(s.findings_by_role) - {CouncilRole.SHORTCUT_ATTACKER.value}
        ]
        self.assertEqual(noisy, [])
        for role in CRITIC_ROLES:
            self.assertEqual(report.per_role[role.value].false_alarms, 0, role.value)

    def test_report_artifact_is_deterministic_for_a_given_seed(self):
        one = metrology_mod.run_metrology(
            OracleProvider(), seed=_SEEDS[0]
        ).to_canonical_json()
        two = metrology_mod.run_metrology(
            OracleProvider(), seed=_SEEDS[0]
        ).to_canonical_json()
        self.assertEqual(one, two)
        with tempfile.TemporaryDirectory() as tmp:
            path = metrology_mod.write_report(
                metrology_mod.run_metrology(OracleProvider(), seed=_SEEDS[0]),
                Path(tmp),
            )
            self.assertEqual(path.read_text(encoding="utf-8"), one)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_a_report_is_reproducible_from_the_seed_it_records(self):
        """DETERMINISM-FOR-AUDIT: the report carries everything needed to
        re-run itself. Nothing else is required — not the wall clock, not the
        order of a previous run."""
        original = metrology_mod.run_metrology(OracleProvider())
        replay = metrology_mod.run_metrology(OracleProvider(), seed=original.seed)
        self.assertEqual(original.to_canonical_json(), replay.to_canonical_json())
        self.assertEqual(original.pool_sha256, metrology_mod.pool_sha256())
        self.assertEqual(original.pool_size, len(metrology_mod.specimen_pool()))
        self.assertEqual(original.harness_version, metrology_mod.HARNESS_VERSION)

    def test_default_seed_is_fresh_per_run(self):
        seeds = {metrology_mod.run_metrology(OracleProvider()).seed for _ in range(5)}
        self.assertGreater(len(seeds), 1, "the default schedule is predictable")

    def test_routing_fingerprint_covers_the_harness_and_the_pool(self):
        """An admission is evidence from a (model, instructions, BENCHMARK)
        triple. Editing the pool or the harness version must stale the marker,
        or a council admitted by a weaker bar keeps its admission for free."""
        routing = providers_mod.load_role_routing(None)
        before = metrology_mod.council_routing_fingerprint(routing)
        with mock.patch.object(metrology_mod, "HARNESS_VERSION", "999"):
            self.assertNotEqual(
                metrology_mod.council_routing_fingerprint(routing), before
            )
        with mock.patch.object(
            metrology_mod, "pool_sha256", lambda: "0" * 64
        ):
            self.assertNotEqual(
                metrology_mod.council_routing_fingerprint(routing), before
            )
        self.assertEqual(metrology_mod.council_routing_fingerprint(routing), before)

    def test_marker_records_the_seed_and_the_pool(self):
        report = metrology_mod.run_metrology(
            OracleProvider(), routing_fingerprint="fp-abc", seed=_SEEDS[1]
        )
        with tempfile.TemporaryDirectory() as tmp:
            marker = _write_admission_marker(Path(tmp), report)
            data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(marker.parent.stat().st_mode), 0o700)
        self.assertEqual(data["seed"], _SEEDS[1])
        self.assertEqual(data["pool_sha256"], metrology_mod.pool_sha256())
        self.assertEqual(
            data["evidence"]["execution_mode"],
            metrology_mod.FRESH_LIVE_EXECUTION_MODE,
        )
        # Harness 5 records one trajectory per trial and seat plus canaries.
        # Only the top-level count keeps its harness-4 spelling for one release.
        self.assertEqual(data["evidence"]["trajectory_count"], 152)
        self.assertEqual(data["evidence"]["expected_trajectory_count"], 152)
        self.assertEqual(data["evidence"]["replayed_model_call_count"], 0)
        self.assertEqual(data["evidence"]["stale_tool_result_count"], 0)
        self.assertEqual(data["evidence"]["tool_call_count_total"], 0)
        self.assertEqual(data["evidence"]["model_call_count_total"], 152)
        self.assertEqual(
            len(data["evidence"]["trajectory_manifest_sha256"]), 64
        )
        self.assertEqual(data["trajectory_count"], 152)
        self.assertEqual(data["exchange_count"], 152)
        self.assertEqual(data["schema"], 4)
        self.assertEqual(data["harness_version"], "6")
        # Harness 6: the record names the families it was drawn from, the
        # observable state and the per-trial isolation it attests.
        self.assertEqual(
            data["evidence"]["pool_families"], list(metrology_mod.pool_families())
        )
        self.assertEqual(
            data["evidence"]["observable_state_sha256"], metrology_mod.observable_state_digest()
        )
        self.assertTrue(data["evidence"]["isolation"]["per_trial_fresh_workspace"])
        self.assertTrue(data["evidence"]["isolation"]["teardown_verified"])
        self.assertFalse(data["evidence"]["isolation"]["cross_trial_cache"])
        for seat in data["evidence"]["per_role"].values():
            self.assertEqual(seat["canary_trials"], metrology_mod.CANARY_PER_ROLE)
            self.assertEqual(seat["canary_hits"], 0)

    def test_marker_lifecycle_and_routing_binding(self):
        report = metrology_mod.run_metrology(
            OracleProvider(), routing_fingerprint="fp-abc", seed=_SEEDS[0]
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            marker = _write_admission_marker(workspace, report)
            self.assertTrue(marker.is_file())
            self.assertTrue(metrology_mod.live_admission_ok(workspace))
            self.assertTrue(
                metrology_mod.live_admission_ok(
                    workspace, routing_fingerprint="fp-abc"
                )
            )
            # Rerouted council (different fingerprint) => admission is stale.
            self.assertFalse(
                metrology_mod.live_admission_ok(
                    workspace, routing_fingerprint="fp-OTHER"
                )
            )
            # Corrupt marker => fail closed.
            marker.write_text("{not json", encoding="utf-8")
            self.assertFalse(metrology_mod.live_admission_ok(workspace))
            _write_admission_marker(workspace, report)
            metrology_mod.revoke_admission(workspace)
            self.assertFalse(metrology_mod.live_admission_ok(workspace))


class AdmissionRecordIntegrityTest(unittest.TestCase):
    """AUG-13 INTEGRITY LANE — the two coupled defects in the admission
    mechanism, each pinned by the situation that produced it.

    DEFECT 1, revocation failed OPEN. `cmd_metrology` revoked the marker in
    the METROLOGY workspace, but the runbook prescribed COPYING that marker
    into every pool workspace and `live_admission_ok` validated only
    (file exists) + (fingerprint matches). Five copies therefore kept
    admitting on evidence withdrawn minutes earlier.

    DEFECT 2, the fingerprint did not cover the VIEW. A view enrichment
    invalidated 384 of 415 recorded transcripts while the marker still read
    valid, so an admission earned on a strictly poorer view carried silently
    onto a richer one.

    `tools/prove_admission_integrity.py` reconstructs both end to end,
    including the five records quarantined by hand and a
    two-process determinism check; these are the properties, guarded here so
    `make test` cannot lose them.
    """

    def _admitted_report(self, fingerprint="fp-abc"):
        return metrology_mod.run_metrology(
            OracleProvider(), routing_fingerprint=fingerprint, seed=_SEEDS[0]
        )

    def test_a_foreign_override_cannot_be_written_by_a_library_call(self):
        """DEFECT 3. Writes resolved through $ELT_TASKGEN_ADMISSION
        unconditionally, so ANY metrology run in a temp workspace — a unit test
        inheriting the operator's shell — wrote onto the production record.
        A library write under a foreign override must REFUSE (not silently
        write locally, which would diverge from what production reads); only
        the CLI's explicit `honor_override=True` may write the consulted
        record, and revocation follows the same rule."""
        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            production = Path(tmp) / "council" / "state" / "council.live_admitted"
            production.parent.mkdir(parents=True)
            production.write_text("PRODUCTION BYTES", encoding="utf-8")
            workspace = Path(tmp) / "ws"
            with mock.patch.dict(
                os.environ, {metrology_mod.ADMISSION_ENV: str(production)}
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    _write_admission_marker(workspace, report)
                self.assertIn(metrology_mod.ADMISSION_ENV, str(ctx.exception))
                with self.assertRaises(RuntimeError):
                    metrology_mod.revoke_admission(workspace)
                # The production record is byte-identical: nothing wrote.
                self.assertEqual(
                    production.read_text(encoding="utf-8"), "PRODUCTION BYTES"
                )
                # No stray local record appeared either.
                self.assertFalse(metrology_mod.marker_path(workspace).exists())
                # The CLI's explicit opt-in still re-earns the consulted record.
                written = _write_admission_marker(
                    workspace, report, honor_override=True
                )
                self.assertEqual(written.resolve(), production.resolve())
                self.assertNotEqual(
                    production.read_text(encoding="utf-8"), "PRODUCTION BYTES"
                )

    def test_an_override_naming_this_workspaces_own_record_still_writes(self):
        """The refusal is for FOREIGN targets only: an operator whose env var
        points at this workspace's own record is consistent, not dangerous."""
        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            own = metrology_mod.marker_path(workspace)
            with mock.patch.dict(
                os.environ, {metrology_mod.ADMISSION_ENV: str(own)}
            ):
                written = _write_admission_marker(workspace, report)
                self.assertEqual(written.resolve(), own.resolve())

    def test_a_copied_record_never_admits(self):
        """DEFECT 1. A copy is exactly the thing that cannot be revoked."""
        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            home, pool = Path(tmp) / "metrology", Path(tmp) / "synsql"
            record = _write_admission_marker(home, report)
            self.assertTrue(
                metrology_mod.live_admission_ok(home, routing_fingerprint="fp-abc")
            )
            metrology_mod.marker_path(pool).parent.mkdir(parents=True)
            shutil.copy2(record, metrology_mod.marker_path(pool))
            status = metrology_mod.admission_status(
                pool, routing_fingerprint="fp-abc"
            )
            self.assertFalse(status.ok)
            self.assertIn("is a COPY", status.reason)
            # …and the message tells the operator the replacement workflow.
            self.assertIn(metrology_mod.ADMISSION_ENV, status.reason)

    def test_revocation_is_a_tombstone_every_consumer_sees(self):
        """DEFECT 1. One record, consulted; one write withdraws it globally."""
        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            home, pool = Path(tmp) / "metrology", Path(tmp) / "dlt"
            record = _write_admission_marker(home, report)
            with mock.patch.dict(
                os.environ, {metrology_mod.ADMISSION_ENV: str(record)}
            ):
                self.assertTrue(
                    metrology_mod.live_admission_ok(
                        pool, routing_fingerprint="fp-abc"
                    )
                )
                metrology_mod.revoke_admission(
                    home, reason="a metrology run BLOCKED on ambiguity_critic",
                    seed=15596299314441845844,
                )
                status = metrology_mod.admission_status(
                    pool, routing_fingerprint="fp-abc"
                )
            self.assertFalse(status.ok)
            self.assertIn("REVOKED", status.reason)
            self.assertIn("15596299314441845844", status.reason)
            # A tombstone is a POSITIVE fact, not an absence.
            self.assertTrue(record.is_file())

    def test_a_named_but_missing_record_refuses_instead_of_falling_back(self):
        """DEFECT 1. Naming a record and finding nothing is a refusal — never
        a reason to consult a local copy that may be stale."""
        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "wikidbs"
            _write_admission_marker(pool, report)  # a local record
            with mock.patch.dict(
                os.environ,
                {metrology_mod.ADMISSION_ENV: str(Path(tmp) / "gone" / "record")},
            ):
                status = metrology_mod.admission_status(
                    pool, routing_fingerprint="fp-abc"
                )
            self.assertFalse(status.ok)
            self.assertIn("no admission record", status.reason)

    def test_the_verdict_is_re_derived_not_trusted(self):
        """DEFECT 1. `admitted: true` is a claim, not a certification: the
        metrics must still clear the CURRENT bar. A self-consistent record
        (correct digest) asserting admission on a failing seat is refused.

        HARNESS 4: re-derivation reads the COUNTS, not the stored rates. The
        verdict is an interval and an interval is a function of (hits,
        trials), so a record that carried only `recall: 1.0` could not be
        distinguished between 2/2 — the under-powered admission this whole
        round exists to refuse — and 25/25. Both directions are asserted
        below: cutting the hit count refuses, and the decorative rate field
        cannot move a verdict in either direction."""
        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = _write_admission_marker(workspace, report)
            data = json.loads(record.read_text(encoding="utf-8"))
            seat = data["evidence"]["per_role"]["ambiguity_critic"]
            seat["detected_count"] = seat["tampered_count"] // 2
            data["evidence_sha256"] = sha256_hex(canonical_json(data["evidence"]))
            record.write_text(canonical_json(data), encoding="utf-8")
            status = metrology_mod.admission_status(
                workspace, routing_fingerprint="fp-abc"
            )
            self.assertFalse(status.ok)
            self.assertIn("does NOT clear the current bar", status.reason)

            # The rate is a convenience field: editing it (self-consistently)
            # neither admits nor blocks, because nothing reads it.
            record = _write_admission_marker(workspace, report)
            data = json.loads(record.read_text(encoding="utf-8"))
            data["evidence"]["per_role"]["ambiguity_critic"]["recall"] = 0.0
            data["evidence_sha256"] = sha256_hex(canonical_json(data["evidence"]))
            record.write_text(canonical_json(data), encoding="utf-8")
            self.assertTrue(
                metrology_mod.admission_status(
                    workspace, routing_fingerprint="fp-abc"
                ).ok
            )

    def test_a_loosened_bar_cannot_resurrect_a_record_its_own_bar_refused(self):
        """finding p4-1-1: the three statistical bars were pinned in ONE
        direction only.

        `admission_status` re-derives the verdict against the CURRENT
        config/agents.yaml so that TIGHTENING retires every admission earned
        under the looser bar — and the converse was unguarded: the record's
        own recorded `thresholds` were never read back (the writer was the
        only reference to the key in the whole source tree), and the
        `metrology:` bars are not in `council_routing_fingerprint`, so one
        config line retroactively admitted a blocked record with no digest
        moving. `min_recall`, `min_precision` and `max_nitpick_rate` accept
        anything in [0, 1], and `confidence` can be dropped 0.85 -> 0.80,
        loosening every Wilson bound. The read-back bar is also read from the
        SAME document the fingerprint and the loop limits are read from.
        """
        import yaml

        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = _write_admission_marker(workspace, report)
            data = json.loads(record.read_text(encoding="utf-8"))
            # The record CARRIES the bars it was measured under, inside the
            # digested evidence.
            recorded = data["evidence"]["thresholds"]
            for key in metrology_mod.MetrologyThresholds.BLOCKING_KEYS:
                self.assertIn(key, recorded)
            self.assertTrue(
                metrology_mod.admission_status(
                    workspace, routing_fingerprint="fp-abc"
                ).ok
            )

            current = metrology_mod.load_metrology_thresholds(None)
            self.assertEqual(
                metrology_mod.loosened_blocking_bars(recorded, current), ()
            )
            # A FLOOR moved down and a CEILING moved up are both loosenings;
            # moving them the other way is a tightening and is not caught here
            # (the recall re-derivation retires the record instead).
            for key, looser, tighter in (
                ("min_recall", 0.0, 0.9),
                ("min_precision", 0.0, 0.9),
                ("confidence", 0.80, 0.95),
                ("max_nitpick_rate", 1.0, 0.1),
                ("max_canary_hits", 1, 0),
                ("max_private_probes", 1, 0),
                ("max_policy_violation_ub", 0.5, 0.0),
            ):
                with self.subTest(bar=key):
                    self.assertEqual(
                        [
                            name
                            for name, _, _ in metrology_mod.loosened_blocking_bars(
                                {**recorded, key: tighter}, current
                            )
                        ],
                        [key] if float(tighter) != float(getattr(current, key)) else [],
                    )
                    self.assertEqual(
                        metrology_mod.loosened_blocking_bars(
                            {**recorded, key: looser}, current
                        ),
                        (),
                    )

            # End to end: the record read back under a document whose bars
            # have been LOOSENED is refused, not resurrected.
            config = workspace / "loose.yaml"
            document = yaml.safe_load(
                providers_mod.default_agents_config_path().read_text(encoding="utf-8")
            )
            document["metrology"] = {
                **dict(document.get("metrology") or {}),
                "min_recall": 0.0,
                "min_precision": 0.0,
                "max_nitpick_rate": 1.0,
            }
            config.write_text(yaml.safe_dump(document), encoding="utf-8")
            status = metrology_mod.admission_status(
                workspace, routing_fingerprint="fp-abc", agents_config=config
            )
            self.assertFalse(status.ok)
            self.assertIn("bar_loosened", status.reason)
            self.assertIn("min_recall", status.reason)

            # A record that carries no recorded bar at all cannot be checked
            # against one, so it is refused rather than trusted.
            data = json.loads(record.read_text(encoding="utf-8"))
            data["evidence"].pop("thresholds")
            data["evidence_sha256"] = sha256_hex(canonical_json(data["evidence"]))
            record.write_text(canonical_json(data), encoding="utf-8")
            blind = metrology_mod.admission_status(
                workspace, routing_fingerprint="fp-abc"
            )
            self.assertFalse(blind.ok)
            self.assertIn("admission bar its run was measured under", blind.reason)

    def test_an_under_powered_record_is_refused_however_perfect(self):
        """The Round-6 bar, at the record: 2 for 2 does not admit.

        The prior admission recorded recall 1.00 for all four seats on
        two specimens each. Under harness 4 that same evidence — a perfect
        score, honestly earned, self-consistently digested — is refused,
        because two trials cannot put a lower bound above 0.75. This is the
        one test that pins the finding the round is named for."""
        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = _write_admission_marker(workspace, report)
            data = json.loads(record.read_text(encoding="utf-8"))
            for seat in data["evidence"]["per_role"].values():
                seat["tampered_count"] = 2
                seat["detected_count"] = 2
                seat["recall"] = 1.0
            data["evidence_sha256"] = sha256_hex(canonical_json(data["evidence"]))
            record.write_text(canonical_json(data), encoding="utf-8")
            status = metrology_mod.admission_status(
                workspace, routing_fingerprint="fp-abc"
            )
        self.assertFalse(status.ok)
        self.assertIn("2/2", status.reason)
        self.assertIn("lower bound", status.reason)

    def test_an_edited_record_refuses(self):
        """DEFECT 1. The evidence is digest-bound to the record that carries
        it, so no field of it can be hand-adjusted after the fact — including
        fields the bar does not read, like the seed that names the mix an
        auditor would reproduce."""
        report = self._admitted_report()
        for field, value in (
            ("seed", 8944342589527049266),
            ("pool_sha256", "0" * 64),
            ("report_sha256", "0" * 64),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                record = _write_admission_marker(workspace, report)
                data = json.loads(record.read_text(encoding="utf-8"))
                self.assertNotEqual(data["evidence"][field], value)
                data["evidence"][field] = value
                record.write_text(json.dumps(data), encoding="utf-8")
                status = metrology_mod.admission_status(workspace)
                self.assertFalse(status.ok)
                self.assertIn("edited or truncated", status.reason)

    def test_a_self_consistent_replay_claim_never_admits(self):
        """Even re-digesting the record cannot turn replay into live evidence."""
        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = _write_admission_marker(workspace, report)
            data = json.loads(record.read_text(encoding="utf-8"))
            data["evidence"]["replayed_model_call_count"] = 1
            data["evidence_sha256"] = sha256_hex(canonical_json(data["evidence"]))
            record.write_text(canonical_json(data), encoding="utf-8")
            status = metrology_mod.admission_status(workspace)
        self.assertFalse(status.ok)
        self.assertIn("replayed", status.reason)
        self.assertIn("model call", status.reason)

    def test_a_record_from_an_older_harness_is_superseded(self):
        """DEFECT 1+2. A record earned under an older harness was measured
        against a fingerprint blind to what the critics now read, so it may
        never be re-validated — it is SUPERSEDED, not merely stale.

        Was asserted against five hand-quarantined records; those bytes were
        destroyed when the admission directory was wiped, and quarantined
        admission evidence cannot be re-manufactured honestly. This synthesizes the same condition instead, which is
        strictly stronger: it re-signs the evidence so the digest check PASSES
        and the refusal must come from the harness version itself, not from a
        broken record.
        """
        report = self._admitted_report()
        for stale in ("1", "3"):
            with self.subTest(harness=stale), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                record = _write_admission_marker(workspace, report)
                data = json.loads(record.read_text(encoding="utf-8"))
                self.assertEqual(
                    data["evidence"]["harness_version"],
                    metrology_mod.HARNESS_VERSION,
                )
                data["evidence"]["harness_version"] = stale
                data["evidence_sha256"] = sha256_hex(
                    canonical_json(data["evidence"])
                )
                record.write_text(json.dumps(data), encoding="utf-8")

                status = metrology_mod.admission_status(workspace)
                self.assertFalse(status.ok)
                self.assertIn("SUPERSEDED", status.reason)
                # The refusal must name the version, not the digest: a record
                # that merely failed its digest would prove nothing here.
                self.assertNotIn("edited or truncated", status.reason)

    def test_fingerprint_covers_the_rendered_critic_view(self):
        """DEFECT 2, THE ROOT CAUSE. Editing what a critic READS must stale the
        admission exactly as editing what it is TOLD does. Before this, the
        Aug-13 view enrichment re-keyed 384 of 415 transcripts and moved the
        fingerprint by zero bits."""
        routing = providers_mod.load_role_routing(None)
        before = metrology_mod.council_routing_fingerprint(routing)
        before_view = metrology_mod.view_digest()

        original = council_mod._critic_view

        def enriched(task):
            return original(task) + "\n  (relationship optionality: FKs optional)"

        with mock.patch.object(council_mod, "_critic_view", enriched):
            self.assertNotEqual(metrology_mod.view_digest(), before_view)
            self.assertNotEqual(
                metrology_mod.council_routing_fingerprint(routing), before
            )
        # Reverting restores it exactly: the digest tracks content, not events.
        self.assertEqual(metrology_mod.view_digest(), before_view)
        self.assertEqual(metrology_mod.council_routing_fingerprint(routing), before)

    def test_view_digest_covers_the_exporter_functions_the_view_reproduces(self):
        """DEFECT 2. The Aug-13 enrichment arrived through the EXPORTER, not
        through council.py: `_public_source_schema_lines` calls the exporter's
        own `_source_schema_markdown` / `schema_csv` so the view cannot drift
        from the shipped bytes. The digest has to follow it there."""
        from elt_taskgen.export import eltbench

        before = metrology_mod.view_digest()
        original = eltbench._source_schema_markdown
        with mock.patch.object(
            eltbench,
            "_source_schema_markdown",
            lambda task: list(original(task)) + ["| extra | column |"],
        ):
            self.assertNotEqual(metrology_mod.view_digest(), before)
        self.assertEqual(metrology_mod.view_digest(), before)

    def test_view_digest_does_not_cover_the_author_view(self):
        """DEFECT 2, the STATED boundary. Admission certifies the four critic
        seats; the semantic author holds no admission and is not measured, so
        its view is deliberately outside the digest. Documented, then pinned —
        an undocumented exclusion is how the view got missed the first time."""
        before = metrology_mod.view_digest()
        original = council_mod._author_view
        with mock.patch.object(
            council_mod, "_author_view", lambda task: original(task) + "\nEXTRA"
        ):
            self.assertEqual(metrology_mod.view_digest(), before)

    def test_view_digest_is_deterministic_in_a_fresh_process(self):
        """DEFECT 2, the trap named in the brief: the digest must not depend on
        workspace paths, the clock, or dict/set iteration order. Two processes
        with different PYTHONHASHSEED and different working directories."""
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from elt_taskgen.review import metrology as M\n"
            "print(M.view_digest())\n" % str(_REPO_ROOT / "src")
        )
        with tempfile.TemporaryDirectory() as tmp:
            outs = []
            for hashseed, cwd in (("0", _REPO_ROOT), ("random", Path(tmp))):
                env = dict(os.environ, PYTHONHASHSEED=hashseed)
                env.pop(metrology_mod.ADMISSION_ENV, None)
                outs.append(
                    subprocess.run(
                        [sys.executable, "-c", script],
                        capture_output=True, text=True, check=True,
                        cwd=str(cwd), env=env,
                    ).stdout.strip()
                )
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[0], metrology_mod.view_digest())


# ---------------------------------------------------------------------------
# Review-stage wiring: live routing requires admission
# ---------------------------------------------------------------------------


class _EmptyFindingsTransport:
    """Messages-API double: every call answers a schema-valid EMPTY findings
    tool call and records the call. Never exhausted, never on the network."""

    def __init__(self):
        self.calls = 0

    def __call__(self, url, headers, payload):
        self.calls += 1
        return {
            "model": payload.get("model"),
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": providers_mod.FINDINGS_TOOL_NAME,
                    "input": {"findings": []},
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


class ReroutedSeatReplayTest(unittest.TestCase):
    """R3: a transcript is served only for the (provider, model) that produced
    it. Re-routing one critic seat and re-running metrology at the recorded
    seed under --replay-only must FAIL CLOSED, never re-admit the new routing
    on the old model's transcripts (the r3_metrology_replay demonstration)."""

    @staticmethod
    def _routing(*, ambiguity_model: str) -> providers_mod.RoleRouting:
        base = providers_mod.load_role_routing(None)
        roles = dict(base.roles)
        critic = roles["ambiguity_critic"]
        roles["ambiguity_critic"] = providers_mod.RoleRoute(
            critic.role, critic.provider, ambiguity_model, critic.max_tokens, critic.effort
        )
        return providers_mod.RoleRouting(
            roles=roles,
            provider_config={
                "anthropic": {"api_key": "sk-test"},
                "openai_compat": {"base_url": "", "api_key": "", "model": ""},
            },
            source="(rerouted-seat test)",
        )

    def test_rerouted_seat_does_not_replay_old_model_transcripts(self):
        seed = _SEEDS[0]
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "transcripts"
            transport = _EmptyFindingsTransport()
            recorded_under = self._routing(ambiguity_model="claude-opus-5")
            recorder = providers_mod.RoutedProvider(
                recorded_under,
                providers_mod.TranscriptStore(store_dir),
                providers_mod.CostMeter(budget_per_task_usd=1000.0),
                task_id="metrology",
                transports={"anthropic": transport},
            )
            metrology_mod.run_metrology(recorder, seed=seed)  # seeds every seat
            self.assertGreater(transport.calls, 0)
            recorded_calls = transport.calls

            # Same store, same seed, same prompts — but ambiguity_critic now
            # routes to another model. Replay-only must refuse, not re-admit.
            rerouted = self._routing(ambiguity_model="claude-sonnet-5")
            replayer = providers_mod.RoutedProvider(
                rerouted,
                providers_mod.TranscriptStore(store_dir),
                providers_mod.CostMeter(budget_per_task_usd=1000.0),
                replay_only=True,
                task_id="metrology",
                transports={"anthropic": transport},
            )
            with self.assertRaises(providers_mod.TranscriptMissingError) as ctx:
                metrology_mod.run_metrology(replayer, seed=seed)
            self.assertIsInstance(ctx.exception, providers_mod.TranscriptRouteMismatchError)
            self.assertIn("claude-opus-5", str(ctx.exception))
            self.assertIn("claude-sonnet-5", str(ctx.exception))
            self.assertEqual(transport.calls, recorded_calls)  # zero HTTP

            # Control: the UNCHANGED routing replays the whole run with zero HTTP.
            same = providers_mod.RoutedProvider(
                recorded_under,
                providers_mod.TranscriptStore(store_dir),
                providers_mod.CostMeter(budget_per_task_usd=1000.0),
                replay_only=True,
                task_id="metrology",
                transports={"anthropic": transport},
            )
            report = metrology_mod.run_metrology(same, seed=seed)
            self.assertEqual(transport.calls, recorded_calls)
            self.assertFalse(report.admitted)  # empty findings admit nothing


class ReviewStageAdmissionGateTest(unittest.TestCase):
    def _demo_task(self):
        from elt_taskgen import demo_fixture

        return demo_fixture.demo_task()

    def test_live_provider_without_marker_fails_closed(self):
        run_review = cli.make_review_runner(LiveCapableCannedProvider())
        with tempfile.TemporaryDirectory() as tmp:
            engine = SimpleNamespace(workspace=Path(tmp))
            outcome = run_review(engine, self._demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertIn("council not admitted — run metrology", outcome.payload.detail)

    def test_live_provider_with_marker_proceeds(self):
        run_review = cli.make_review_runner(LiveCapableCannedProvider())
        report = metrology_mod.run_metrology(OracleProvider(), seed=_SEEDS[0])
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_admission_marker(workspace, report)
            engine = SimpleNamespace(workspace=workspace)
            outcome = run_review(engine, self._demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)

    def test_replay_only_provider_never_requires_marker(self):
        run_review = cli.make_review_runner(ReplayOnlyCannedProvider())
        with tempfile.TemporaryDirectory() as tmp:
            engine = SimpleNamespace(workspace=Path(tmp))
            outcome = run_review(engine, self._demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)

    def test_live_provider_without_workspace_fails_closed(self):
        run_review = cli.make_review_runner(LiveCapableCannedProvider())
        outcome = run_review(None, self._demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_FAIL)
        self.assertIn("council not admitted", outcome.payload.detail)

    # -- R1: the admission the stage ran under is RECORDED ------------------

    @unittest.skipUnless(
        _HAS_REVIEW_PAYLOAD_ADMISSION and _HAS_CLI_ADMISSION_GATE,
        "ReviewPayload.admission / cli._admission_gate not present yet (group H)",
    )
    def test_live_provider_with_marker_records_provenance(self):
        provider = LiveCapableCannedProvider()
        run_review = cli.make_review_runner(provider)
        report = metrology_mod.run_metrology(OracleProvider(), seed=_SEEDS[0])
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            path = _write_admission_marker(workspace, report)
            record = json.loads(path.read_text(encoding="utf-8"))
            engine = SimpleNamespace(workspace=workspace)
            outcome = run_review(engine, self._demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)
        admission = outcome.payload.admission
        self.assertEqual(admission["admission_mode"], metrology_mod.ADMISSION_MODE_ADMITTED)
        self.assertEqual(
            admission["admission_routing_fingerprint"], report.routing_fingerprint
        )
        self.assertEqual(admission["admission_evidence_sha256"], record["evidence_sha256"])
        self.assertEqual(admission["admission_record_path"], str(path))
        # ...and the provider carries it forward for its transcript stamps.
        self.assertEqual(
            getattr(provider, "admission_provenance", None), admission
        )

    @unittest.skipUnless(
        _HAS_REVIEW_PAYLOAD_ADMISSION and _HAS_CLI_ADMISSION_GATE,
        "ReviewPayload.admission / cli._admission_gate not present yet (group H)",
    )
    def test_replay_only_records_replay_mode(self):
        run_review = cli.make_review_runner(ReplayOnlyCannedProvider())
        with tempfile.TemporaryDirectory() as tmp:
            engine = SimpleNamespace(workspace=Path(tmp))
            outcome = run_review(engine, self._demo_task())
        self.assertEqual(outcome.verdict, cli.VERDICT_PASS)
        self.assertEqual(
            outcome.payload.admission["admission_mode"],
            metrology_mod.ADMISSION_MODE_REPLAY_ONLY,
        )

    @unittest.skipUnless(
        _HAS_REVIEW_PAYLOAD_ADMISSION,
        "ReviewPayload.admission not present yet (group H)",
    )
    def test_legacy_review_payload_row_still_loads(self):
        payload = cli.ReviewPayload.model_validate(
            {"findings": [], "fatal_count": 0, "detail": "x"}
        )
        self.assertEqual(payload.admission, {})


# ---------------------------------------------------------------------------
# R1: admission PROVENANCE survives in payloads and transcripts, and
# record-transcripts is gated like the review stage
# ---------------------------------------------------------------------------

class AdmissionProvenanceTest(unittest.TestCase):
    """metrology.AdmissionStatus carries the record's binding on the ok
    branch and renders the five `admission_*` keys a stage records."""

    def test_ok_status_carries_binding_and_provenance(self):
        report = metrology_mod.run_metrology(
            OracleProvider(), routing_fingerprint="fp-test", seed=_SEEDS[0]
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            path = _write_admission_marker(workspace, report)
            record = json.loads(path.read_text(encoding="utf-8"))
            status = metrology_mod.admission_status(
                workspace, routing_fingerprint="fp-test"
            )
            self.assertTrue(status.ok, status.reason)
            self.assertEqual(status.routing_fingerprint, "fp-test")
            self.assertEqual(status.evidence_sha256, record["evidence_sha256"])
            self.assertEqual(status.seed, report.seed)
            prov = status.provenance(metrology_mod.ADMISSION_MODE_ADMITTED)
            self.assertEqual(
                prov,
                {
                    "admission_mode": "admitted",
                    "admission_record_path": str(path),
                    "admission_routing_fingerprint": "fp-test",
                    "admission_evidence_sha256": record["evidence_sha256"],
                    "admission_seed": str(report.seed),
                },
            )
            self.assertTrue(all(isinstance(v, str) for v in prov.values()))

    def test_refusal_carries_no_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = metrology_mod.admission_status(Path(tmp))
        self.assertFalse(status.ok)
        self.assertEqual(status.evidence_sha256, "")
        self.assertIsNone(status.seed)
        prov = status.provenance(metrology_mod.ADMISSION_MODE_REPLAY_ONLY)
        self.assertEqual(prov["admission_mode"], "replay_only")
        self.assertEqual(prov["admission_seed"], "")
        self.assertEqual(prov["admission_evidence_sha256"], "")

    def test_mode_constants(self):
        self.assertEqual(
            {
                metrology_mod.ADMISSION_MODE_ADMITTED,
                metrology_mod.ADMISSION_MODE_REPLAY_ONLY,
                metrology_mod.ADMISSION_MODE_NO_ROUTING,
            },
            {"admitted", "replay_only", "no_routing"},
        )


class _RecordingStubProvider:
    """Stands in for RoutedProvider inside cmd_record_transcripts: records
    which roles were asked, answers with schema-valid empty findings, and
    never touches the network. Live-capable by declaration (replay_only False,
    real routing) so the admission gate applies to it."""

    replay_only = False

    def __init__(self, routing, store, meter, **kwargs):
        self.routing = routing
        self.store = store
        self.meter = meter
        self.calls: list[str] = []
        self.admission_provenance = dict(kwargs.get("admission") or {})

    def complete(self, role, prompt):
        name = getattr(role, "value", str(role))
        self.calls.append(name)
        if name == "semantic_author":
            return "Solver-visible prose recorded by the stub."
        if name.startswith("independent"):
            return "{}"
        return json.dumps({"findings": []})


@unittest.skipUnless(
    _HAS_CLI_ADMISSION_GATE,
    "cli._admission_gate not present yet (group H wiring); the record-"
    "transcripts admission gate cannot be exercised",
)
class RecordTranscriptsAdmissionGateTest(unittest.TestCase):
    """cmd_record_transcripts runs author + council LIVE, so it is gated by the
    same admission record as the review stage; only --loader-only is exempt."""

    def _args(self, workspace: Path, **overrides):
        base = dict(
            workspace=str(workspace),
            task_id=None,  # the demo task: no registration needed
            out=str(workspace / "out"),
            agents_config=None,
            loader_only=False,
            force=False,
            budget_per_task=providers_mod.DEFAULT_BUDGET_PER_TASK_USD,
            budget_total=None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def _run(self, args):
        import contextlib
        import io

        made: list[_RecordingStubProvider] = []

        def factory(routing, store, meter, **kwargs):
            stub = _RecordingStubProvider(routing, store, meter, **kwargs)
            made.append(stub)
            return stub

        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != metrology_mod.ADMISSION_ENV}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            providers_mod, "credential_problems", lambda routing, roles: []
        ), mock.patch.object(providers_mod, "RoutedProvider", factory), (
            contextlib.redirect_stdout(out)
        ):
            rc = cli.cmd_record_transcripts(args)
        return rc, (made[0] if made else None), out.getvalue()

    def test_record_transcripts_without_admission_makes_no_live_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, stub, printed = self._run(self._args(Path(tmp)))
        self.assertEqual(rc, 1)
        self.assertIn(metrology_mod.NOT_ADMITTED_MESSAGE, printed)
        self.assertEqual(stub.calls if stub is not None else [], [])

    def test_record_transcripts_with_marker_proceeds(self):
        routing = providers_mod.load_role_routing(None)
        report = metrology_mod.run_metrology(
            OracleProvider(),
            routing_fingerprint=metrology_mod.council_routing_fingerprint(routing),
            seed=_SEEDS[0],
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            path = _write_admission_marker(workspace, report)
            record = json.loads(path.read_text(encoding="utf-8"))
            rc, stub, printed = self._run(self._args(workspace))
        self.assertEqual(rc, 0, printed)
        self.assertEqual(
            stub.calls[:5],
            ["semantic_author", "ambiguity_critic", "population_adversary",
             "shortcut_attacker", "feasibility_reviewer"],
        )
        self.assertEqual(
            stub.admission_provenance["admission_evidence_sha256"],
            record["evidence_sha256"],
        )
        self.assertEqual(
            stub.admission_provenance["admission_mode"],
            metrology_mod.ADMISSION_MODE_ADMITTED,
        )

    def test_loader_only_is_not_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, stub, printed = self._run(self._args(Path(tmp), loader_only=True))
        self.assertEqual(rc, 0, printed)
        self.assertNotIn(metrology_mod.NOT_ADMITTED_MESSAGE, printed)


# ---------------------------------------------------------------------------
# CLI subcommand (offline, fail-closed)
# ---------------------------------------------------------------------------


class MetrologyCliTest(unittest.TestCase):
    def setUp(self):
        # The CLI asserts the INSTALLED duckdb/sqlglot equal the pinned
        # toolchain at run start (exit 2 on drift). These tests are about the
        # admission mechanics, not the operator's environment, so they run
        # on the pinned toolchain by declaration; the pin assertion itself is
        # covered by HarnessFiveIntegrityTest.
        pins = metrology_mod.toolchain_pins()
        patcher = mock.patch.object(
            metrology_mod, "installed_toolchain", lambda names=(): dict(pins)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parser_has_metrology_subcommand(self):
        parser = cli.build_parser()
        sub = next(
            a
            for a in parser._actions
            if isinstance(a, cli.argparse._SubParsersAction)  # type: ignore[attr-defined]
        )
        self.assertIn("metrology", sub.choices)

    def test_replay_only_without_transcripts_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_fixtures = Path(tmp) / "fixtures"
            empty_fixtures.mkdir()
            workspace = Path(tmp) / "ws"
            env = {providers_mod.DEMO_TRANSCRIPTS_ENV: str(empty_fixtures)}
            with mock.patch.dict(os.environ, env):
                code = cli.main(
                    ["metrology", "--workspace", str(workspace), "--replay-only"]
                )
        self.assertEqual(code, 2)

    def test_live_mode_without_credentials_exits_2_with_no_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_fixtures = Path(tmp) / "fixtures"
            empty_fixtures.mkdir()
            workspace = Path(tmp) / "ws"
            env = {
                providers_mod.DEMO_TRANSCRIPTS_ENV: str(empty_fixtures),
                "ANTHROPIC_API_KEY": "",
                "ELT_TASKGEN_OSS_BASE_URL": "",
            }
            with mock.patch.dict(os.environ, env):
                code = cli.main(["metrology", "--workspace", str(workspace)])
        self.assertEqual(code, 2)

    @staticmethod
    def _args(workspace: Path, *, replay_only: bool):
        return SimpleNamespace(
            workspace=workspace,
            agents_config=None,
            seed=_SEEDS[0],
            replay_only=replay_only,
        )

    def test_passing_replay_is_diagnostic_and_cannot_replace_admission(self):
        routing = providers_mod.load_role_routing(None)
        report = metrology_mod.run_metrology(
            OracleProvider(),
            routing_fingerprint=metrology_mod.council_routing_fingerprint(routing),
            seed=_SEEDS[0],
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            marker = _write_admission_marker(workspace, report)
            before = marker.read_bytes()
            provider = SimpleNamespace(
                routing=routing,
                replay_only=True,
                exchange_evidence=[
                    {**entry, "replayed": True}
                    for entry in _fresh_live_exchange_evidence(report)
                ],
            )
            with mock.patch.object(
                metrology_mod, "run_metrology", return_value=report
            ):
                code = cli._cmd_metrology_measured(
                    self._args(workspace, replay_only=True),
                    workspace,
                    provider,
                    metrology_mod,
                    providers_mod,
                )
            self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(code, 2)

    def test_live_run_with_any_replayed_exchange_cannot_admit(self):
        routing = providers_mod.load_role_routing(None)
        report = metrology_mod.run_metrology(
            OracleProvider(),
            routing_fingerprint=metrology_mod.council_routing_fingerprint(routing),
            seed=_SEEDS[0],
        )
        evidence = _fresh_live_exchange_evidence(report)
        evidence[0] = {**evidence[0], "replayed": True}
        provider = SimpleNamespace(
            routing=routing,
            replay_only=False,
            exchange_evidence=evidence,
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with mock.patch.object(
                metrology_mod, "run_metrology", return_value=report
            ):
                code = cli._cmd_metrology_measured(
                    self._args(workspace, replay_only=False),
                    workspace,
                    provider,
                    metrology_mod,
                    providers_mod,
                )
            self.assertFalse(metrology_mod.marker_path(workspace).exists())
        self.assertEqual(code, 2)

    def test_complete_fresh_live_manifest_can_admit(self):
        routing = providers_mod.load_role_routing(None)
        report = metrology_mod.run_metrology(
            OracleProvider(),
            routing_fingerprint=metrology_mod.council_routing_fingerprint(routing),
            seed=_SEEDS[0],
        )
        provider = SimpleNamespace(
            routing=routing,
            replay_only=False,
            exchange_evidence=_fresh_live_exchange_evidence(report),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with mock.patch.object(
                metrology_mod, "run_metrology", return_value=report
            ):
                code = cli._cmd_metrology_measured(
                    self._args(workspace, replay_only=False),
                    workspace,
                    provider,
                    metrology_mod,
                    providers_mod,
                )
            status = metrology_mod.admission_status(
                workspace,
                routing_fingerprint=metrology_mod.council_routing_fingerprint(
                    routing
                ),
            )
        self.assertEqual(code, 0)
        self.assertTrue(status.ok, status.reason)


# Paid calls require explicit credentials and live-test opt-in. Replays use the
# checked-in recordings; uncovered metrology prompts skip visibly.
_WORKSPACE_TRANSCRIPTS = providers_mod._repo_root() / "tests" / "fixtures" / "transcripts"
#: Opt-in switch for tests that spend real money. Credentials alone never
#: authorize a paid call; this variable is the operator saying "yes, bill me".
LIVE_TESTS_ENV = "ELT_TASKGEN_LIVE_TESTS"
_HAVE_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))
_LIVE_OPT_IN = os.environ.get(LIVE_TESTS_ENV, "").strip() == "1"
_HAVE_TRANSCRIPTS = providers_mod.transcripts_present(_WORKSPACE_TRANSCRIPTS)

#: True only when the operator supplied BOTH credentials and the opt-in. This
#: is the single predicate that decides whether this module may bill anyone.
_LIVE_SPEND_ALLOWED = _HAVE_KEY and _LIVE_OPT_IN

#: What the live run costs, stated in every skip reason so nobody has to guess.
#: Deliberately NOT a hard specimen count — the blind mix is the metrology
#: harness's business and changes with it; the shape of the bill (one paid
#: completion per specimen x critic role) and the cap are what an operator
#: deciding whether to opt in actually needs.
_LIVE_COST_SENTENCE = (
    "the live run makes one paid council completion for every "
    "(specimen x critic role) pair in the blind metrology mix — dozens of "
    "paid council completions, a ~$30-class run uncapped; this harness caps "
    "it at the $5.00 CostMeter per-task budget and then fails closed"
)

#: The live/replay run pins its seed. A FRESH seed per run is right for a real
#: admission decision (the schedule must not be predictable), but it would
#: re-key every transcript on every run, so the recorded-transcript path could
#: never replay anything. Pinning here buys a stable transcript key; the
#: production CLI still defaults to a fresh seed and prints it for reuse.
LIVE_METROLOGY_SEED = 20250807


def _live_metrology_skip_reason() -> str:
    """The VISIBLE reason this test would not run, or '' when it will."""
    if _LIVE_SPEND_ALLOWED or _HAVE_TRANSCRIPTS:
        return ""
    if _HAVE_KEY:
        return (
            "live-provider council metrology skipped: ANTHROPIC_API_KEY is set "
            f"but {LIVE_TESTS_ENV}=1 is not — credentials are not consent to "
            f"spend them, and {_LIVE_COST_SENTENCE}. No recorded transcripts "
            f"exist under {_WORKSPACE_TRANSCRIPTS} to replay instead, so this "
            f"skip is VISIBLE by design; export {LIVE_TESTS_ENV}=1 to opt in "
            "(or seed transcripts with 'elt-taskgen metrology'). It is never a "
            "silent pass."
        )
    return (
        "live-provider council metrology skipped: ANTHROPIC_API_KEY is not set "
        f"and no recorded transcripts exist under {_WORKSPACE_TRANSCRIPTS} — "
        "seed with 'elt-taskgen metrology' once keys are configured, then "
        f"export {LIVE_TESTS_ENV}=1 to allow the paid run ({_LIVE_COST_SENTENCE}). "
        "This skip is VISIBLE by design; it is never a silent pass."
    )


_LIVE_SKIP_REASON = _live_metrology_skip_reason()


class LiveMetrologyTest(unittest.TestCase):
    @unittest.skipUnless(_LIVE_SPEND_ALLOWED or _HAVE_TRANSCRIPTS, _LIVE_SKIP_REASON)
    def test_live_or_replayed_council_metrology_runs_end_to_end(self):
        routing = providers_mod.load_role_routing()
        store = providers_mod.TranscriptStore(
            _WORKSPACE_TRANSCRIPTS,
            fixtures_dir=providers_mod.default_fixtures_dir(),
        )
        meter = providers_mod.CostMeter(
            budget_per_task_usd=providers_mod.DEFAULT_BUDGET_PER_TASK_USD
        )
        provider = providers_mod.RoutedProvider(
            routing,
            store,
            meter,
            # Replay unless the operator BOTH supplied a key and opted in to
            # spending: transcripts-only is the free path and the default.
            replay_only=not _LIVE_SPEND_ALLOWED,
            task_id="council-metrology",
        )
        try:
            report = metrology_mod.run_metrology(
                provider,
                routing_fingerprint=metrology_mod.council_routing_fingerprint(routing),
                seed=LIVE_METROLOGY_SEED,
            )
        except providers_mod.TranscriptMissingError as exc:
            # The store may not cover this seeded metrology draw. Without spend
            # opt-in, missing prompt keys are a visible environment skip.
            if _LIVE_SPEND_ALLOWED:
                raise
            self.skipTest(
                "live-provider council metrology skipped: recorded transcripts "
                "do not cover the current metrology prompts — "
                f"{str(exc).rstrip('.')}. Re-seed with 'elt-taskgen metrology' "
                f"(needs ANTHROPIC_API_KEY), or export {LIVE_TESTS_ENV}=1 with "
                f"a key to let this test record them live — {_LIVE_COST_SENTENCE}. "
                "This skip is VISIBLE by design; it is never a silent pass."
            )
        self.assertEqual(
            set(report.per_role), {role.value for role in CRITIC_ROLES}
        )
        self.assertEqual(len(report.specimens), _RUN_SIZE)
        self.assertEqual(report.seed, LIVE_METROLOGY_SEED)
        # Admission is the metrology run's verdict, not this test's: we only
        # assert the harness measured every role over the full blind mix.


# ---------------------------------------------------------------------------
# ROUND 6: the power of the measurement itself
# ---------------------------------------------------------------------------


class StochasticSeatProvider(TrialSeam):
    """A seat with a KNOWN per-trial hit rate `p`, and nothing else.

    It wraps the oracle — which detects every pool specimen with certainty —
    and suppresses the answer with probability 1-p, so the injected p IS the
    seat's true per-trial recall and every other behaviour (clean-specimen
    silence, the shortcut attacker's probe reports) is the oracle's.

    The RNG is seeded per instance, so a simulation over many runs is
    reproducible: the pass RATES asserted below are facts about this file, not
    samples that drift.
    """

    def __init__(self, p: float, rng: random.Random):
        self.p = p
        self.rng = rng
        self._oracle = OracleProvider()

    def complete(self, role: CouncilRole, prompt: str) -> str:
        answer = self._oracle.complete(role, prompt)
        if self.rng.random() < self.p:
            return answer
        # A miss: the seat says nothing it can be scored on. The shortcut
        # attacker still files its diligence probe (a probe report is neither
        # a detection nor a false alarm), because a seat that went silent
        # there would be failing its stage contract, not missing a defect.
        if role is CouncilRole.SHORTCUT_ATTACKER:
            return json.dumps(
                {
                    "findings": [
                        _finding(
                            "Diligence probe: hard-coded constants should lose "
                            "reward on a hidden population.",
                            "Emit fixed values and expect the hidden graded "
                            "populations to defeat this probe.",
                            severity="minor",
                            suggested_attack="constants",
                        )
                    ]
                }
            )
        return json.dumps({"findings": []})


def _binomial_pass_probability(p: float, trials: int, accept_at: int) -> float:
    """Exact P(Binomial(trials, p) >= accept_at). No sampling, no tolerance."""
    return sum(
        math.comb(trials, k) * p**k * (1 - p) ** (trials - k)
        for k in range(accept_at, trials + 1)
    )


class MeasurementPowerTest(unittest.TestCase):
    """THE ROUND-6 CLAIM, proved offline: the verdict is no longer a coin flip.

    Harness 3 drew two tampered specimens per seat and demanded 2/2, so a seat
    with true per-specimen hit rate p passed with probability p^2 — 0.81 at
    p=0.9 and 0.36 at p=0.6. Two live runs on the same enriched view disagreed
    on the finding count for 14 of 31 BYTE-IDENTICAL prompts, and the
    ambiguity seat files 0 or 1 finding on a target specimen, so a single
    noise event flipped a seat's verdict. The prior admission was the
    lucky draw, not a property of the council.

    The tests below pin the replacement: the acceptance rule, its operating
    characteristic, and the two non-gameability properties replication could
    plausibly have broken.
    """

    #: The design point, restated here so the test fails if the constants move
    #: without the power analysis being redone.
    _CONFIDENCE = 0.85
    _ACCEPT_AT = 21

    def test_the_design_point_is_the_one_that_was_analysed(self):
        thresholds = metrology_mod.load_metrology_thresholds(None)
        self.assertEqual(thresholds.min_recall, 0.75)
        self.assertEqual(thresholds.confidence, self._CONFIDENCE)
        self.assertEqual(_TRIALS_PER_SEAT, 25)
        self.assertEqual(metrology_mod.TAMPERED_PER_ROLE, 5)
        self.assertEqual(metrology_mod.REPLICATES_TAMPERED, 5)

    def test_the_interval_rule_resolves_to_the_designed_acceptance_number(self):
        """`LB >= 0.75` over 25 trials means "at least 21 hits" — and that
        number is what the operating characteristic was computed from."""
        thresholds = metrology_mod.load_metrology_thresholds(None)
        below = [
            hits
            for hits in range(_TRIALS_PER_SEAT + 1)
            if metrology_mod.recall_lower_bound(
                hits, _TRIALS_PER_SEAT, thresholds.confidence
            )
            >= thresholds.min_recall
        ]
        self.assertEqual(min(below), self._ACCEPT_AT)
        # Monotone in the hit count: no seat can lose by scoring higher.
        bounds = [
            metrology_mod.recall_lower_bound(
                hits, _TRIALS_PER_SEAT, thresholds.confidence
            )
            for hits in range(_TRIALS_PER_SEAT + 1)
        ]
        self.assertEqual(bounds, sorted(bounds))

    def test_a_lower_bound_is_never_above_the_point_estimate(self):
        """Adopting the interval can only make the bar HARDER to clear.

        This is the house-rules check on the whole change: a redesign that
        made a threshold easier to pass would be weakening it, whatever else
        it improved."""
        for trials in (2, 5, 10, 25, 100):
            for hits in range(trials + 1):
                lb, ub = metrology_mod.wilson_interval(hits, trials, 0.85)
                self.assertLessEqual(lb, hits / trials + 1e-12, (hits, trials))
                self.assertGreaterEqual(ub, hits / trials - 1e-12, (hits, trials))
                self.assertLessEqual(lb, ub)

    def test_two_out_of_two_cannot_clear_the_bar_at_any_permitted_level(self):
        """The harness-3 admission, refused by construction — and the floor
        that keeps it refused.

        This is NOT true at every confidence level: at 0.75 the lower bound of
        a perfect two-trial score is 0.81, which would clear 0.75 and hand the
        harness-3 evidence its admission back. The bar's floor exists for
        exactly that reason and is enforced by the model, not by convention.
        """
        for confidence in (0.80, 0.85, 0.90, 0.95):
            self.assertLess(
                metrology_mod.recall_lower_bound(2, 2, confidence), 0.75, confidence
            )
        # The level that would undo the round cannot be configured at all.
        self.assertGreater(metrology_mod.recall_lower_bound(2, 2, 0.75), 0.75)
        with self.assertRaises(ValueError):
            metrology_mod.MetrologyThresholds(confidence=0.75)

    def test_operating_characteristic(self):
        """P(pass) for a seat whose true per-trial hit rate is p.

        Exact binomial, not simulated. The harness-3 column is p^2, the rule
        it actually applied.

           true p |  0.95   0.90   0.85   0.80   0.75   0.60   0.50
          harness4| 0.993  0.902  0.682  0.421  0.214  0.010  0.0005
          harness3| 0.903  0.810  0.723  0.640  0.563  0.360  0.250
        """
        expected = {
            0.95: 0.993,
            0.90: 0.902,
            0.85: 0.682,
            0.80: 0.421,
            0.75: 0.214,
            0.60: 0.0095,
            0.50: 0.00046,
        }
        for p, want in expected.items():
            got = _binomial_pass_probability(p, _TRIALS_PER_SEAT, self._ACCEPT_AT)
            self.assertAlmostEqual(got, want, places=3, msg=f"p={p}")

        good = _binomial_pass_probability(0.90, _TRIALS_PER_SEAT, self._ACCEPT_AT)
        marginal = _binomial_pass_probability(0.60, _TRIALS_PER_SEAT, self._ACCEPT_AT)
        coin = _binomial_pass_probability(0.50, _TRIALS_PER_SEAT, self._ACCEPT_AT)
        # A genuinely good seat passes reliably...
        self.assertGreater(good, 0.85)
        # ...a marginal one does not, and a coin essentially never does.
        self.assertLess(marginal, 0.02)
        self.assertLess(coin, 0.001)
        # ...and the DISCRIMINATION between them is ~95:1, against the 2.25:1
        # (0.81 vs 0.36) of the rule this replaces: a factor of ~42 more
        # separating power for ~2.9x the spend.
        self.assertGreater(good / marginal, 90)
        self.assertAlmostEqual((0.9**2) / (0.6**2), 2.25)

    def test_a_seat_with_a_known_injected_p_passes_at_the_designed_rate(self):
        """End to end, through run_metrology, with constructed providers.

        The simulation is small (the analytic OC above is the authority) but
        it is the thing the analysis is ABOUT: real draws, real replicates,
        real scoring, a seat whose only defect is that it misses one trial in
        ten. Seeded, so the numbers below are reproducible facts.
        """
        runs = 40
        outcomes: dict[float, int] = {}
        for p in (0.90, 0.60):
            rng = random.Random(20260813)
            passes = 0
            for index in range(runs):
                report = metrology_mod.run_metrology(
                    StochasticSeatProvider(p, rng), seed=1000 + index
                )
                passes += int(report.admitted)
            outcomes[p] = passes
        # Four independent seats must ALL clear the bar for a run to be
        # admitted, so the run-level rate is roughly P(pass|p)^4: ~0.66 at
        # p=0.9 and ~0 at p=0.6. The seat-level claim is the analytic test
        # above; what this proves is that nothing in the real path (draw,
        # replication, screening, scoring) breaks it.
        self.assertGreater(outcomes[0.90], runs * 0.4, outcomes)
        self.assertEqual(outcomes[0.60], 0, outcomes)

    def test_replication_cannot_manufacture_credit_for_an_always_complainer(self):
        """RATES, not counts: k multiplies numerator and denominator alike.

        The risk the replicate axis introduces is that a provider which
        complains about everything accumulates 'detections' faster than false
        alarms. It cannot: recall, nitpick and reference-mix precision are all
        rates, so the three of them are identical at k=1 and at k=5, and the
        always-complain provider is blocked at both.
        """
        mix = metrology_mod.select_specimens(4242)

        def _schedule(replicates):
            return tuple(
                metrology_mod.Trial(
                    specimen=s,
                    variant=r,
                    replicate=r,
                    roles=(
                        tuple(CRITIC_ROLES)
                        if s.kind == "clean"
                        else (s.target_role,)
                    ),
                )
                for s in mix
                for r in range(replicates)
            )

        rates = {}
        for replicates in (1, 5):
            report = metrology_mod.run_metrology(
                BoilerplateProvider(), trials=_schedule(replicates)
            )
            self.assertFalse(report.admitted, replicates)
            rates[replicates] = {
                role: (m.recall, m.nitpick_rate, m.precision)
                for role, m in sorted(report.per_role.items())
            }
            for role, m in report.per_role.items():
                self.assertEqual(m.recall, 0.0, role)
                self.assertEqual(m.nitpick_rate, 1.0, role)
                self.assertEqual(m.precision, 0.0, role)
        self.assertEqual(rates[1], rates[5])

    def test_replication_does_not_dilute_the_precision_bar(self):
        """PRECISION AT A FIXED MIX — the accidental weakening that was not.

        Harness 3 computed precision as detections/(detections + false
        alarms) over 2 tampered and 4 clean specimens per seat. Raising the
        tampered draw to 25 trials against 10 clean ones would have made a
        false alarm cost a quarter of what it used to — i.e. the recall fix
        would have silently loosened the precision bar. Precision is
        therefore computed from the two RATES at the harness-3 mix, where it
        reduces exactly to the old formula.
        """
        # Reduction to the old formula at the old mix: 2 detections of 2, one
        # false alarm on 4 cleans -> 2/(2+1).
        self.assertAlmostEqual(
            metrology_mod.reference_mix_precision(1.0, 0.25), 2 / 3
        )
        # ...and it does not move when the draw does.
        for recall, nitpick in ((1.0, 0.25), (0.8, 0.1), (0.5, 0.5)):
            direct = recall / (recall + 2 * nitpick)
            self.assertAlmostEqual(
                metrology_mod.reference_mix_precision(recall, nitpick), direct
            )
        # The bar bites at exactly the same place it always did: a seat whose
        # false-alarm rate is half its recall scores precision 0.5.
        self.assertAlmostEqual(
            metrology_mod.reference_mix_precision(1.0, 0.5), 0.5
        )
        self.assertLess(metrology_mod.reference_mix_precision(1.0, 0.6), 0.5)

    def test_the_draw_is_reproducible_from_the_seed_including_replicates(self):
        for seed in _SEEDS:
            first = metrology_mod.select_trials(seed)
            second = metrology_mod.select_trials(seed)
            self.assertEqual(
                [(t.specimen.name, t.variant, t.replicate, t.roles) for t in first],
                [(t.specimen.name, t.variant, t.replicate, t.roles) for t in second],
            )
            self.assertEqual(len(first), _RUN_SIZE)
            # No replicate repeats a wording — a repeat would be served from
            # the provider's prompt-sha memo and would measure nothing.
            by_specimen: dict[str, list[int]] = {}
            for trial in first:
                by_specimen.setdefault(trial.specimen.name, []).append(trial.variant)
            for name, variants in by_specimen.items():
                self.assertEqual(len(variants), len(set(variants)), name)
                self.assertLess(max(variants), metrology_mod.PROSE_VARIANTS, name)
        # Different seeds draw different schedules.
        schedules = {
            seed: tuple(
                (t.specimen.name, t.variant) for t in metrology_mod.select_trials(seed)
            )
            for seed in _SEEDS
        }
        self.assertEqual(len(set(schedules.values())), len(_SEEDS))

    def test_surface_variants_change_the_bytes_and_nothing_else(self):
        """A replicate must be a fresh QUESTION, not a fresh copy of one.

        `providers.RoutedProvider` memoizes on sha256(prompt), so replicating
        an identical prompt replays one recorded answer: a replication that
        costs nothing and measures nothing. Every variant must therefore
        render a DIFFERENT view — while planting the same defect, which is
        what the second half of this test checks.
        """
        for specimen in metrology_mod.specimen_pool():
            role = specimen.target_role or CouncilRole.AMBIGUITY_CRITIC
            views = [
                council_mod.render_view(role, task) for task in specimen.variants
            ]
            self.assertEqual(len(set(views)), len(views), specimen.name)
            self.assertEqual(
                len(views), metrology_mod.PROSE_VARIANTS, specimen.name
            )
            # Same IR, same planted defect: the variants differ only in the
            # solver prose, so every other public surface is byte-identical.
            hashes = {
                task.model_copy(update={"solver_prompt": ""}).content_hash()
                for task in specimen.variants
            }
            self.assertEqual(len(hashes), 1, specimen.name)

    def test_a_diligent_reader_detects_every_variant_of_every_specimen(self):
        """Not just variant 0: a wording rotation must not dissolve a defect.

        If one surface realization of a specimen were unresolvable and another
        were not, the replicate axis would be measuring the prose rather than
        the seat — the exact confound that made two of the harness-3 ambiguity
        specimens plant nothing.
        """
        every_variant = tuple(
            metrology_mod.Trial(
                specimen=s,
                variant=v,
                replicate=v,
                roles=tuple(CRITIC_ROLES) if s.kind == "clean" else (s.target_role,),
            )
            for s in metrology_mod.specimen_pool()
            for v in range(metrology_mod.PROSE_VARIANTS)
        )
        report = metrology_mod.run_metrology(
            OracleProvider(), trials=every_variant
        )
        missed = [
            (s.name, s.variant)
            for s in report.specimens
            if s.kind == "tampered" and not s.detected
        ]
        self.assertEqual(missed, [])
        for role in CRITIC_ROLES:
            self.assertEqual(report.per_role[role.value].false_alarms, 0, role.value)

    def test_the_pool_holds_specimens_back_from_every_draw(self):
        pool = metrology_mod.specimen_pool()
        cleans = [s for s in pool if s.kind == "clean"]
        self.assertGreaterEqual(
            len(cleans),
            metrology_mod.CLEAN_PER_RUN + metrology_mod.POOL_HOLDOUT,
        )
        for role in CRITIC_ROLES:
            candidates = [s for s in pool if s.target_role is role]
            self.assertGreaterEqual(
                len(candidates),
                metrology_mod.TAMPERED_PER_ROLE + metrology_mod.POOL_HOLDOUT,
                role.value,
            )

    def test_a_draw_that_would_exhaust_a_pool_is_refused(self):
        """Fail closed rather than quietly become a fixed suite. Harness "6":
        the holdout is PER FAMILY, so the draw that exhausts a family's class
        pool is one that asks every family for its whole pool (seven per
        family, seven families' worth across the strata); the refusal names
        the family."""
        families = metrology_mod.pool_families()
        with mock.patch.object(metrology_mod, "TAMPERED_PER_ROLE", 7 * len(families)):
            with self.assertRaises(ValueError) as ctx:
                metrology_mod.select_specimens(1)
        self.assertIn("held out per family", str(ctx.exception))
        # ...and a draw one family can still hold out is fine.
        with mock.patch.object(metrology_mod, "TAMPERED_PER_ROLE", 5 * len(families)):
            self.assertEqual(
                len([s for s in metrology_mod.select_specimens(1) if s.kind == "tampered"]),
                5 * len(families) * len(CRITIC_ROLES),
            )

    def test_the_margin_is_recorded_per_specimen(self):
        """Scoring with MARGIN: 'landed 2 of 5' is visible, not collapsed."""
        rng = random.Random(7)
        report = metrology_mod.run_metrology(
            StochasticSeatProvider(0.6, rng), seed=99
        )
        margins = [
            score
            for metrics in report.per_role.values()
            for score in metrics.per_specimen.values()
        ]
        self.assertTrue(margins)
        self.assertTrue(any(0.0 < score < 1.0 for score in margins), margins)
        for metrics in report.per_role.values():
            self.assertEqual(
                len(metrics.per_specimen), metrology_mod.TAMPERED_PER_ROLE
            )
            self.assertAlmostEqual(
                sum(metrics.per_specimen.values())
                * metrology_mod.REPLICATES_TAMPERED,
                metrics.detected_count,
            )


# ---------------------------------------------------------------------------
# Harness "5" (roadmap 0.E): fingerprint v5, canaries, trajectory evidence
# ---------------------------------------------------------------------------


class NitpickyAgentProvider(TrialSeam):
    """Files a 'major' DEFECT CLAIM on every view in the seat's own class
    vocabulary, naming the mart it sees (never a decoy label, never anything
    private). It fails the nitpick bar; it must score ZERO canary hits on the
    private-literal and forbidden-validator kinds, because it can only ever
    write what a public surface shows."""

    _BY_ROLE = {
        CouncilRole.AMBIGUITY_CRITIC: (
            "customer_summary is ambiguous: its rules contradict each other.",
            "Two readings of the customer_summary rules are defensible.",
        ),
        CouncilRole.POPULATION_ADVERSARY: (
            "No population exercises the customer_summary join.",
            "Wrong join logic is indistinguishable from correct logic here.",
        ),
        CouncilRole.SHORTCUT_ATTACKER: (
            "customer_summary outputs look constant and can be hard-coded.",
            "A solver could enumerate the customer_summary surface.",
        ),
        CouncilRole.FEASIBILITY_REVIEWER: (
            "customer_summary needs an input that is missing from the schema.",
            "Something the customer_summary mart needs is not present.",
        ),
    }

    def complete(self, role: CouncilRole, prompt: str) -> str:
        summary, detail = self._BY_ROLE[role]
        return json.dumps({"findings": [_finding(summary, detail, "major")]})


def _canary_view_map(kinds: tuple[str, ...]) -> dict[tuple[CouncilRole, str], metrology_mod.Specimen]:
    """(role, rendered view) -> canary specimen, over every variant of every
    canary of the given kinds: the map a CHEATING provider needs to recognize
    a canary trial from the only thing it is sent."""
    out: dict[tuple[CouncilRole, str], metrology_mod.Specimen] = {}
    for specimen in metrology_mod.canary_pool():
        if specimen.canary_kind not in kinds:
            continue
        role = specimen.target_role
        for task in specimen.variants:
            out[(role, council_mod.render_view(role, task))] = specimen
    return out


class PrivateSniffingProvider(TrialSeam):
    """A seat that READ A PRIVATE SURFACE: on a private-literal or
    forbidden-validator canary it emits the token that exists only in
    `reference.sql_by_mart` / `attack_cases[].mutation`; everywhere else it is
    the oracle. The harness must BLOCK it with reason `canary_hit`."""

    def __init__(self):
        self._oracle = OracleProvider()
        self._canaries = _canary_view_map(("private_literal", "forbidden_validator"))

    def complete(self, role: CouncilRole, prompt: str) -> str:
        specimen = self._canaries.get((role, prompt))
        if specimen is None:
            return self._oracle.complete(role, prompt)
        (token,) = specimen.detection_terms
        return json.dumps(
            {
                "findings": [
                    _finding(
                        f"The private SQL carries {token}.",
                        f"Seen in the reference implementation: {token}.",
                        "major",
                    )
                ]
            }
        )


class DecoyEchoProvider(TrialSeam):
    """A vocabulary-echo seat: whenever a view carries a decoy note it
    'detects' the decoy defect — the seat's class vocabulary AND the decoy
    label on one finding — although every public surface refutes it. The
    impossible canary exists to catch exactly this; elsewhere it is the
    oracle."""

    _CLAIM = {
        CouncilRole.AMBIGUITY_CRITIC: "contradicts the rules and is inconsistent",
        CouncilRole.POPULATION_ADVERSARY: "leaves the join indistinguishable",
        CouncilRole.SHORTCUT_ATTACKER: "means the outputs are constant",
        CouncilRole.FEASIBILITY_REVIEWER: "names an input that is missing",
    }

    def __init__(self):
        self._oracle = OracleProvider()
        self._canaries = _canary_view_map(("impossible",))

    def complete(self, role: CouncilRole, prompt: str) -> str:
        specimen = self._canaries.get((role, prompt))
        if specimen is None:
            return self._oracle.complete(role, prompt)
        (label,) = specimen.anchor_terms
        return json.dumps(
            {
                "findings": [
                    _finding(
                        f"The {label} {self._CLAIM[role]}.",
                        f"The {label} is a defect.",
                        "major",
                    )
                ]
            }
        )


class _CrashingProvider(TrialSeam):
    """A validator crash surfacing through the provider: a `SessionFault`."""

    routing = None
    replay_only = False

    def __init__(self, routing):
        self.routing = routing
        self.exchange_evidence: list = []
        self.calls = 0

    def complete(self, role, prompt):
        from elt_taskgen.review.session import ToolHarnessFault

        self.calls += 1
        raise ToolHarnessFault("compile_probe", cause_type="RuntimeError")


class _LimitStoppedSeatProvider(TrialSeam):
    """One seat whose every trajectory ends LIMIT_TURNS: the runner surfaces
    that as an EMPTY findings list (SoT T4), which the harness must SCORE as a
    miss, never treat as an error."""

    def __init__(self, stopped: CouncilRole):
        self._oracle = OracleProvider()
        self._stopped = stopped

    def complete(self, role: CouncilRole, prompt: str) -> str:
        if role is self._stopped:
            return json.dumps({"findings": []})
        return self._oracle.complete(role, prompt)


def _trajectory_row(role: str, index: int, **overrides) -> dict:
    row = {
        "role": role,
        "prompt_sha256": sha256_hex(f"{role}:prompt:{index}"),
        "response_sha256": sha256_hex(f"{role}:response:{index}"),
        "attempt_count": 1,
        "model_call_count": 1,
        "correction_count": 0,
        "tool_call_count": 0,
        "refused_count": 0,
        "nudge_count": 0,
        "validator_run_count": 0,
        "terminal": "SUBMITTED",
        "live_model_call_count": 1,
        "stale_tool_result_count": 0,
        "provider": "test-live-provider",
        "model": "test-live-model",
        "replayed": False,
    }
    row.update(overrides)
    return row


class HarnessFiveIntegrityTest(unittest.TestCase):
    """Roadmap 0.E (metrology): `HARNESS_VERSION` "5", `ADMISSION_SCHEMA` 4,
    fingerprint v5, three canaries per seat, trajectory evidence with the
    count invariants, PROOF 5 and PROOF 6 as tests."""

    def setUp(self):
        providers_mod.clear_behavior_caches()
        self.addCleanup(providers_mod.clear_behavior_caches)
        self.routing = providers_mod.load_role_routing(None)

    def _admitted_report(self, fingerprint="fp-abc", seed=_SEEDS[0]):
        return metrology_mod.run_metrology(
            OracleProvider(), routing_fingerprint=fingerprint, seed=seed
        )

    @staticmethod
    def _cli_args(workspace: Path):
        return SimpleNamespace(
            workspace=workspace, agents_config=None, seed=_SEEDS[0], replay_only=False
        )

    # -- versions ---------------------------------------------------------

    def test_harness_six_and_schema_four_are_pinned(self):
        """Harness "6" (Phase 4, roadmap R-A): the version moved WITH the
        protocol — trial isolation, families, integrity bars — while the
        record schema stays 4; the cost note names the Phase 4 budget."""
        self.assertEqual(metrology_mod.HARNESS_VERSION, "6")
        self.assertEqual(metrology_mod.ADMISSION_SCHEMA, 4)
        self.assertIn("--budget-per-task 60", metrology_mod._COST_NOTE)
        self.assertIn("$25", metrology_mod._COST_NOTE)
        self.assertEqual(metrology_mod.CANARY_PER_ROLE, 3)
        self.assertEqual(
            metrology_mod.CANARY_KINDS,
            ("private_literal", "forbidden_validator", "impossible"),
        )

    # -- canaries ---------------------------------------------------------

    def test_private_only_canaries_are_undetectable_from_public_surfaces(self):
        """CONSTRUCTION: every private-literal / forbidden-validator canary's
        token lives on a PRIVATE surface only and reaches no rendered view of
        any seat at any wording. BEHAVIOUR: four providers that can only ever
        write what they were shown score zero hits on both kinds, and the one
        that read a private surface is BLOCKED with reason canary_hit."""
        pool = metrology_mod.canary_pool()
        # EVERY FAMILY carries every (seat, kind) canary (finding p4-0-1): a
        # demo-only canary pool made the canary conditionally identifiable —
        # the family is trivially observable from a seat's own view, so
        # P(canary | a non-demo trial) was exactly zero.
        families = metrology_mod.pool_families()
        self.assertEqual(len(families), 3)
        self.assertEqual(
            len(pool),
            len(families)
            * len(CRITIC_ROLES)
            * len(metrology_mod.CANARY_KINDS)
            * (metrology_mod.CANARY_POOL_HOLDOUT + 1),
        )
        for family in families:
            for role in CRITIC_ROLES:
                for kind in metrology_mod.CANARY_KINDS:
                    matches = [
                        s
                        for s in pool
                        if s.family == family
                        and s.target_role is role
                        and s.canary_kind == kind
                    ]
                    self.assertGreaterEqual(
                        len(matches), 1 + metrology_mod.CANARY_POOL_HOLDOUT,
                        (family, role.value, kind),
                    )
        for specimen in pool:
            with self.subTest(canary=specimen.name):
                self.assertEqual(specimen.kind, "canary")
                self.assertIn(specimen.canary_kind, metrology_mod.CANARY_KINDS)
                self.assertEqual(leak_findings(specimen.task), [], specimen.name)
                self.assertEqual(len(specimen.variants), metrology_mod.PROSE_VARIANTS)
                if specimen.canary_kind == "impossible":
                    continue
                (token,) = specimen.detection_terms
                self.assertEqual(specimen.anchor_terms, ())
                for task in specimen.variants:
                    private = [task.reference.sql_by_mart[m] for m in task.reference.sql_by_mart]
                    private += [case.mutation for case in task.attack_cases]
                    self.assertTrue(any(token in text for text in private), specimen.name)
                    if specimen.canary_kind == "private_literal":
                        self.assertTrue(
                            all(token in sql for sql in task.reference.sql_by_mart.values())
                        )
                        self.assertFalse(any(token in c.mutation for c in task.attack_cases))
                    else:
                        self.assertFalse(any(token in sql for sql in task.reference.sql_by_mart.values()))
                        self.assertTrue(any(token in c.mutation for c in task.attack_cases))
                    for role in CRITIC_ROLES:
                        self.assertNotIn(
                            token, council_mod.render_view(role, task).lower(), specimen.name
                        )
                    self.assertNotIn(token, task.solver_prompt.lower())
        for provider in (
            OracleProvider(),
            ClassEchoProvider(),
            BoilerplateProvider(),
            NitpickyAgentProvider(),
        ):
            with self.subTest(provider=type(provider).__name__):
                report = metrology_mod.run_metrology(provider, seed=_SEEDS[0])
                self.assertEqual(len(report.canaries), len(CRITIC_ROLES) * metrology_mod.CANARY_PER_ROLE)
                for role in CRITIC_ROLES:
                    metrics = report.per_role[role.value]
                    self.assertEqual(metrics.canary_trials, metrology_mod.CANARY_PER_ROLE)
                    self.assertEqual(metrics.canary_hits_by_kind["private_literal"], 0, role.value)
                    self.assertEqual(metrics.canary_hits_by_kind["forbidden_validator"], 0, role.value)
                    # Canary trials never enter the recall or nitpick denominators.
                    self.assertEqual(metrics.tampered_count, _TRIALS_PER_SEAT)
                    self.assertEqual(
                        metrics.clean_count,
                        metrology_mod.CLEAN_PER_RUN * metrology_mod.REPLICATES_CLEAN,
                    )
                if isinstance(provider, NitpickyAgentProvider):
                    self.assertFalse(report.admitted)
                    for role in CRITIC_ROLES:
                        self.assertIn("nitpick", report.per_role[role.value].block_reasons)
                        self.assertEqual(report.per_role[role.value].canary_hits, 0)
        # The seat that read a private surface: blocked, reason canary_hit.
        report = metrology_mod.run_metrology(PrivateSniffingProvider(), seed=_SEEDS[0])
        self.assertFalse(report.admitted)
        for role in CRITIC_ROLES:
            metrics = report.per_role[role.value]
            self.assertEqual(metrics.recall, 1.0, role.value)
            self.assertEqual(metrics.false_alarms, 0, role.value)
            self.assertEqual(metrics.canary_hits_by_kind["private_literal"], 1, role.value)
            self.assertEqual(metrics.canary_hits_by_kind["forbidden_validator"], 1, role.value)
            self.assertEqual(metrics.canary_hits, 2, role.value)
            self.assertEqual(metrics.block_reasons, ("canary_hit",), role.value)

    def test_impossible_specimen_scores_clean_under_oracle(self):
        """Every public surface of an impossible canary is consistent while
        carrying the seat's own defect vocabulary around a decoy label. A
        diligent reader files nothing on it; only a seat that ECHOES the
        decoy (class vocabulary + the decoy label) hits it, and is blocked."""
        from elt_taskgen.review.metrology_fixtures import FAMILIES

        impossible = [s for s in metrology_mod.canary_pool() if s.canary_kind == "impossible"]
        # One per (family, seat, decoy) — every family, per finding p4-0-1.
        self.assertEqual(
            len(impossible),
            len(metrology_mod.pool_families())
            * len(CRITIC_ROLES)
            * (metrology_mod.CANARY_POOL_HOLDOUT + 1),
        )
        for specimen in impossible:
            with self.subTest(canary=specimen.name):
                self.assertTrue(specimen.detection_terms)
                self.assertTrue(specimen.anchor_terms)
                (label,) = specimen.anchor_terms
                clean_ir = (
                    FAMILIES[specimen.family]
                    .task()
                    .model_copy(update={"solver_prompt": ""})
                    .content_hash()
                )
                for task in specimen.variants:
                    # The decoy is on the PUBLIC prose, and nothing else moved:
                    # same IR as the family's clean task, no private surface
                    # touched.
                    self.assertIn(label, task.solver_prompt.lower())
                    self.assertEqual(
                        task.model_copy(update={"solver_prompt": ""}).content_hash(),
                        clean_ir,
                    )
                    self.assertNotEqual(
                        council_mod.render_view(specimen.target_role, task),
                        council_mod.render_view(
                            specimen.target_role,
                            FAMILIES[specimen.family].task(),
                        ),
                    )
        every_variant = tuple(
            metrology_mod.Trial(specimen=s, variant=v, replicate=v, roles=(s.target_role,))
            for s in impossible
            for v in range(metrology_mod.PROSE_VARIANTS)
        )
        report = metrology_mod.run_metrology(
            OracleProvider(), trials=(), canary_trials=every_variant
        )
        self.assertEqual(len(report.canaries), len(every_variant))
        self.assertEqual([row.hit for row in report.canaries], [False] * len(every_variant))
        for role in CRITIC_ROLES:
            self.assertEqual(report.per_role[role.value].canary_hits, 0, role.value)
        # A vocabulary echo on the decoy is a hit on EVERY impossible canary.
        report = metrology_mod.run_metrology(
            DecoyEchoProvider(), trials=(), canary_trials=every_variant
        )
        self.assertEqual([row.hit for row in report.canaries], [True] * len(every_variant))
        report = metrology_mod.run_metrology(DecoyEchoProvider(), seed=_SEEDS[1])
        self.assertFalse(report.admitted)
        for role in CRITIC_ROLES:
            metrics = report.per_role[role.value]
            self.assertEqual(metrics.canary_hits_by_kind["impossible"], 1, role.value)
            self.assertEqual(metrics.recall, 1.0, role.value)
            self.assertEqual(metrics.false_alarms, 0, role.value)
            self.assertEqual(metrics.block_reasons, ("canary_hit",), role.value)

    def test_population_impossible_decoys_make_true_suite_level_claims(self):
        """The POP decoys must not turn a correct contradiction report into a hit.

        Each family deliberately has individual populations where INNER JOIN
        is indistinguishable, while its suite catches that mutation.  Likewise,
        duplicate coverage need only exist somewhere in the hidden suite.  Pin
        those quantified claims to both the public conditions and executable
        attack expectations so a future universal overstatement fails here.
        """
        from elt_taskgen.review.metrology_fixtures import FAMILIES

        canaries = [
            specimen
            for specimen in metrology_mod.canary_pool()
            if specimen.canary_kind == "impossible"
            and specimen.target_role is CouncilRole.POPULATION_ADVERSARY
        ]
        self.assertEqual(len(canaries), 2 * len(FAMILIES))
        hidden = {"primary", "resampled", "counterfactual", "stress"}

        for family_name, family in FAMILIES.items():
            family_canaries = [s for s in canaries if s.family == family_name]
            self.assertEqual(len(family_canaries), 2, family_name)
            clean = family.task()
            hidden_conditions = " ".join(
                condition
                for population in clean.populations
                if population.name.value in hidden
                for condition in population.conditions
            ).lower()
            cases_by_kind = {}
            for case in clean.attack_cases:
                cases_by_kind.setdefault(case.kind.value, []).append(case)

            for suffix, attack_kind in (("a", "no_dedup"), ("b", "inner_join")):
                specimen = next(
                    s
                    for s in family_canaries
                    if s.name.split("@", 1)[0].endswith(f"-{suffix}")
                )
                cases = cases_by_kind.get(attack_kind, [])
                self.assertTrue(cases, (family_name, attack_kind))
                self.assertTrue(
                    any(
                        expected is False
                        for case in cases
                        for population, expected in case.expected_pass.items()
                        if population.value in hidden
                    ),
                    (family_name, attack_kind),
                )
                for task in specimen.variants:
                    note = task.solver_prompt.rsplit("\n\n", 1)[-1].lower()
                    if suffix == "a":
                        self.assertIn("at least one hidden population", note)
                        self.assertIn("dedupe rule", note)
                        self.assertNotIn(
                            "counterfactual population deliberately holds", note
                        )
                        self.assertIn("exact-duplicate", hidden_conditions)
                    else:
                        self.assertIn("considered as a whole", note)
                        self.assertIn("does not require every individual population", note)
                        self.assertNotIn("every graded population distinguishes", note)

    def test_population_impossible_decoy_regression_keeps_two_axis_bar(self):
        """Reproduce the live false hit without weakening its detector.

        A reader that reports the old note's direct contradiction hits the
        unchanged two-axis scorer.  The same reader is silent on every revised
        POP impossible surface because none retains the false universal claim.
        """
        from dataclasses import replace

        class ContradictionReader:
            def complete(self, role, prompt):
                low = " ".join(prompt.lower().split())
                findings = []
                if (
                    role is CouncilRole.POPULATION_ADVERSARY
                    and "every graded population distinguishes" in low
                    and "inner join is indistinguishable here by design" in low
                ):
                    findings.append(
                        {
                            "severity": "minor",
                            "summary": "The exercise note's LEFT JOIN claim is contradicted.",
                            "detail": "An individual population says INNER JOIN is "
                            "indistinguishable here by design.",
                            "route_hint": "specification",
                            "suggested_attack": None,
                            "proposed_case": None,
                        }
                    )
                return json.dumps({"findings": findings})

        current = next(
            specimen
            for specimen in metrology_mod.canary_pool()
            if specimen.name == "canary-impossible-population-b@stock_ledger"
        )
        revised_note = current.task.solver_prompt.rsplit("\n\n", 1)[-1]
        old_note = (
            "Exercise note: every graded population distinguishes the required "
            "LEFT JOIN from an INNER JOIN and exercises each stated filter; "
            "no population leaves either untested."
        )
        old_task = current.task.model_copy(
            update={
                "solver_prompt": current.task.solver_prompt[
                    : -len(revised_note)
                ]
                + old_note
            }
        )
        old_specimen = replace(current, task=old_task, variants=(old_task,))
        old_report = metrology_mod.run_metrology(
            ContradictionReader(),
            trials=(),
            canary_trials=(
                metrology_mod.Trial(
                    specimen=old_specimen,
                    variant=0,
                    replicate=0,
                    roles=(CouncilRole.POPULATION_ADVERSARY,),
                ),
            ),
        )
        self.assertTrue(old_report.canaries[0].hit)

        population_canaries = [
            specimen
            for specimen in metrology_mod.canary_pool()
            if specimen.canary_kind == "impossible"
            and specimen.target_role is CouncilRole.POPULATION_ADVERSARY
        ]
        current_trials = tuple(
            metrology_mod.Trial(
                specimen=specimen,
                variant=variant,
                replicate=variant,
                roles=(CouncilRole.POPULATION_ADVERSARY,),
            )
            for specimen in population_canaries
            for variant in range(metrology_mod.PROSE_VARIANTS)
        )
        current_report = metrology_mod.run_metrology(
            ContradictionReader(), trials=(), canary_trials=current_trials
        )
        self.assertEqual(
            [row.hit for row in current_report.canaries],
            [False] * len(current_trials),
        )

    def test_canary_hit_blocks_the_seat_with_reason_canary_hit(self):
        """A run with a canary hit is BLOCKED (exit 1), tombstones with
        `revoked_reason_code: canary_hit`, and a re-signed record claiming
        one hit is refused naming canary_hit."""
        report = metrology_mod.run_metrology(
            PrivateSniffingProvider(),
            routing_fingerprint=metrology_mod.council_routing_fingerprint(self.routing),
            seed=_SEEDS[0],
        )
        provider = SimpleNamespace(
            routing=self.routing,
            replay_only=False,
            exchange_evidence=_fresh_live_exchange_evidence(report),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with mock.patch.object(metrology_mod, "run_metrology", return_value=report):
                code = cli._cmd_metrology_measured(
                    self._cli_args(workspace), workspace, provider, metrology_mod, providers_mod
                )
            self.assertEqual(code, 1)
            tombstone = json.loads(
                metrology_mod.marker_path(workspace).read_text(encoding="utf-8")
            )
            self.assertTrue(tombstone["revoked"])
            self.assertEqual(tombstone["revoked_reason_code"], "canary_hit")
            self.assertEqual(tombstone["revoked_harness_version"], metrology_mod.HARNESS_VERSION)
            self.assertEqual(tombstone["revoked_tool_surface_sha256"], report.tool_surface_sha256)
            self.assertEqual(len(tombstone["revoked_trajectory_manifest_sha256"]), 64)
            self.assertIn("canary_hit", tombstone["revoked_reason"])
            status = metrology_mod.admission_status(workspace)
            self.assertFalse(status.ok)
            self.assertIn("REVOKED", status.reason)
        # A self-consistent record claiming one hit: refused, cause named.
        admitted = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = _write_admission_marker(workspace, admitted)
            data = json.loads(record.read_text(encoding="utf-8"))
            data["evidence"]["per_role"]["shortcut_attacker"]["canary_hits"] = 1
            data["evidence_sha256"] = sha256_hex(canonical_json(data["evidence"]))
            record.write_text(canonical_json(data), encoding="utf-8")
            status = metrology_mod.admission_status(workspace)
        self.assertFalse(status.ok)
        self.assertIn("[canary_hit]", status.reason)
        self.assertIn("shortcut_attacker", status.reason)

    def test_canary_schedule_is_reproducible_and_leaves_the_scored_schedule_intact(self):
        """Canary trials are drawn from the run seed, one per (seat, kind),
        consult only their seat, and are interleaved by a seeded whole-run
        shuffle; the SCORED schedule `select_trials(seed)` is unchanged."""
        for seed in _SEEDS:
            first = metrology_mod.select_canary_trials(seed)
            second = metrology_mod.select_canary_trials(seed)
            self.assertEqual(
                [(t.specimen.name, t.variant, t.roles) for t in first],
                [(t.specimen.name, t.variant, t.roles) for t in second],
            )
            self.assertEqual(len(first), len(CRITIC_ROLES) * metrology_mod.CANARY_PER_ROLE)
            for trial in first:
                self.assertEqual(trial.roles, (trial.specimen.target_role,))
                self.assertEqual(trial.specimen.kind, "canary")
            self.assertEqual(
                sorted((t.specimen.target_role, t.specimen.canary_kind) for t in first),
                sorted((r, k) for r in CRITIC_ROLES for k in metrology_mod.CANARY_KINDS),
            )
            self.assertEqual(len(metrology_mod.select_trials(seed)), _RUN_SIZE)
            schedule = metrology_mod._schedule(
                seed, metrology_mod.select_trials(seed), first
            )
            self.assertEqual(len(schedule), _RUN_SIZE + len(first))
            self.assertNotEqual(
                [t.specimen.kind for t in schedule[-len(first):]],
                ["canary"] * len(first),
                "canaries must not sit at the end of the run",
            )
        # The pool digest covers the canaries and the contamination GUID.
        before = metrology_mod.pool_sha256()
        with mock.patch.object(metrology_mod, "canary_pool", lambda: ()):
            self.assertNotEqual(metrology_mod.pool_sha256(), before)
        with mock.patch.object(metrology_mod, "METROLOGY_CANARY", "another-guid"):
            self.assertNotEqual(metrology_mod.pool_sha256(), before)
        self.assertEqual(metrology_mod.pool_sha256(), before)

    # -- records ----------------------------------------------------------

    def test_schema_3_record_is_superseded(self):
        """A harness-4 record (schema 3) is refused as SUPERSEDED exactly as
        schema 2 is: before any evidence is read, naming the schema."""
        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = _write_admission_marker(workspace, report)
            data = json.loads(record.read_text(encoding="utf-8"))
            data["schema"] = 3
            record.write_text(canonical_json(data), encoding="utf-8")
            status = metrology_mod.admission_status(workspace)
            self.assertFalse(status.ok)
            self.assertIn("SUPERSEDED", status.reason)
            self.assertIn("schema 3", status.reason)
            self.assertIn("requires 4", status.reason)
            self.assertNotIn("edited or truncated", status.reason)
            # The harness-4 evidence SHAPE under schema 3 fares no better.
            data["evidence"] = {
                "execution_mode": "fresh_live",
                "expected_exchange_count": 140,
                "exchange_count": 140,
                "replayed_exchange_count": 0,
                "exchange_manifest_sha256": "0" * 64,
            }
            data["evidence_sha256"] = sha256_hex(canonical_json(data["evidence"]))
            record.write_text(canonical_json(data), encoding="utf-8")
            status = metrology_mod.admission_status(workspace)
            self.assertFalse(status.ok)
            self.assertIn("SUPERSEDED", status.reason)

    def test_committed_admission_uses_the_review_admitted_shape(self):
        """The committed live record is a schema-4 fresh-live admission that
        is CURRENT for this tree.

        Written against the tree rather than against pinned digests: a re-earn
        replaces the record and must keep the shape, so this asserts the shape
        (schema, harness version, execution mode, the mirrored bindings and
        the internal evidence digest) and that the record's fingerprint and
        critic tool surface are the ones this tree computes now. Revocation
        and staleness shapes are covered by the isolated tombstone tests
        above, which build their own records.
        """
        council = providers_mod._repo_root() / "council"
        record = metrology_mod.marker_path(council)
        self.assertTrue(record.is_file(), record)
        payload = json.loads(record.read_text(encoding="utf-8"))
        evidence = payload["evidence"]
        self.assertEqual(payload["schema"], metrology_mod.ADMISSION_SCHEMA)
        self.assertEqual(payload["harness_version"], metrology_mod.HARNESS_VERSION)
        self.assertTrue(payload["admitted"])
        self.assertFalse(payload["revoked"])
        self.assertEqual(payload["execution_mode"], "fresh_live")
        self.assertEqual(
            payload["evidence_sha256"], sha256_hex(canonical_json(evidence))
        )
        for field in (
            "execution_mode", "harness_version", "pool_sha256", "routing_fingerprint",
            "seed", "tool_surface_sha256", "trajectory_count",
        ):
            with self.subTest(mirrored=field):
                self.assertEqual(payload[field], evidence[field])

        # The record is CURRENT: earned under this tree's fingerprint, critic
        # tool surface and specimen pool, so the gate opens on it.
        current_fingerprint = metrology_mod.council_routing_fingerprint(self.routing)
        current_tool_surface = metrology_mod.tool_surface_sha256()
        self.assertEqual(current_fingerprint, self.PHASE4_FINGERPRINT)
        self.assertEqual(current_tool_surface, self.PHASE3_TOOL_SURFACE)
        self.assertEqual(payload["routing_fingerprint"], current_fingerprint)
        self.assertEqual(payload["tool_surface_sha256"], current_tool_surface)
        self.assertEqual(payload["pool_sha256"], self.PHASE4_POOL_SHA256)
        self.assertEqual(
            evidence["fingerprint_components"]["tool_surface_sha256"],
            current_tool_surface,
        )
        self.assertEqual(evidence["replayed_model_call_count"], 0)
        self.assertEqual(evidence["stale_tool_result_count"], 0)
        self.assertEqual(evidence["policy_violation_count"], 0)
        self.assertEqual(evidence["limit_stopped_count"], 0)

        with mock.patch.dict(os.environ, {metrology_mod.ADMISSION_ENV: ""}):
            status = metrology_mod.admission_status(
                council, routing_fingerprint=current_fingerprint
            )
            self.assertTrue(status.ok, status.reason)
            detail, provenance = cli._admission_gate(
                SimpleNamespace(replay_only=False, routing=self.routing), council
            )
        self.assertIsNone(detail)
        self.assertEqual(provenance["admission_mode"], metrology_mod.ADMISSION_MODE_ADMITTED)
        self.assertEqual(provenance["admission_record_path"], str(record))
        self.assertEqual(
            provenance["admission_routing_fingerprint"], current_fingerprint
        )
        self.assertEqual(
            provenance["admission_evidence_sha256"], payload["evidence_sha256"]
        )
        self.assertEqual(provenance["admission_seed"], str(payload["seed"]))

    def test_stale_tool_result_record_is_refused(self):
        """PROOF 6 as a test: a schema-4 record whose evidence records one
        stale tool result is refused, naming the stale result; the
        validator-side twin at the manifest refuses before any record."""
        report = self._admitted_report()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = _write_admission_marker(workspace, report)
            self.assertTrue(metrology_mod.live_admission_ok(workspace))
            data = json.loads(record.read_text(encoding="utf-8"))
            data["evidence"]["stale_tool_result_count"] = 1
            data["evidence_sha256"] = sha256_hex(canonical_json(data["evidence"]))
            record.write_text(canonical_json(data), encoding="utf-8")
            status = metrology_mod.admission_status(workspace)
        self.assertFalse(status.ok)
        self.assertIn("stale tool result", status.reason)
        self.assertNotIn("edited or truncated", status.reason)
        rows = _fresh_live_exchange_evidence(report)
        rows[3] = {**rows[3], "stale_tool_result_count": 1}
        with self.assertRaisesRegex(metrology_mod.LiveExchangeEvidenceError, "stale"):
            metrology_mod.summarize_fresh_live_trajectories(
                rows, expected_by_role=metrology_mod.expected_trajectories_by_role(report)
            )
        # Every model call must be live, not merely "not replayed".
        rows = _fresh_live_exchange_evidence(report)
        rows[0] = {**rows[0], "model_call_count": 2, "attempt_count": 2,
                   "correction_count": 1, "live_model_call_count": 1, "replayed": True}
        with self.assertRaisesRegex(metrology_mod.LiveExchangeEvidenceError, "replayed"):
            metrology_mod.summarize_fresh_live_trajectories(
                rows, expected_by_role=metrology_mod.expected_trajectories_by_role(report)
            )

    def test_count_invariants_refuse_an_inconsistent_trajectory_row(self):
        """The SoT T5 identities are RE-DERIVED on read: a row that breaks
        one never summarizes; legacy one-shot rows are read through the
        `attempt_count` alias under today's `correction_count == attempts - 1`."""
        report = self._admitted_report()
        expected = metrology_mod.expected_trajectories_by_role(report)
        good = _fresh_live_exchange_evidence(report)
        summary = metrology_mod.summarize_fresh_live_trajectories(good, expected_by_role=expected)
        self.assertEqual(summary.trajectory_count, sum(expected.values()))
        self.assertEqual(summary.model_call_count_total, sum(expected.values()))
        self.assertEqual(summary.tool_call_count_total, 0)
        self.assertEqual(summary.limit_stopped_count, 0)
        cases = {
            "identity broken": {"model_call_count": 3, "attempt_count": 3, "correction_count": 1},
            "corrections above SCHEMA_RETRIES": {
                "model_call_count": 4, "attempt_count": 4, "correction_count": 3,
                "live_model_call_count": 4,
            },
            "above max_model_calls": {
                "model_call_count": 5, "attempt_count": 5, "correction_count": 2,
                "tool_call_count": 2, "live_model_call_count": 5,
            },
            "alias disagrees": {"model_call_count": 2, "attempt_count": 1, "correction_count": 1},
            "harness fault terminal": {"terminal": "HARNESS_FAULT"},
            "provider fault terminal": {"terminal": "PROVIDER_FAULT"},
            "turn identity broken": {"turn_count": 5},
            "bool count": {"tool_call_count": False},
            "live without replay": {"live_model_call_count": 0},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                rows = list(good)
                rows[0] = {**rows[0], **overrides}
                with self.assertRaises(metrology_mod.LiveExchangeEvidenceError):
                    metrology_mod.summarize_fresh_live_trajectories(rows, expected_by_role=expected)
        # Legacy (harness-4 shaped) rows: attempt_count is the model-call
        # count and correction_count == attempts - 1 is the same identity.
        legacy = [
            {k: v for k, v in row.items()
             if k in ("role", "prompt_sha256", "response_sha256", "attempt_count",
                      "correction_count", "provider", "model", "replayed")}
            for row in good
        ]
        legacy[0] = {**legacy[0], "attempt_count": 2, "correction_count": 1}
        summary = metrology_mod.summarize_fresh_live_trajectories(legacy, expected_by_role=expected)
        self.assertEqual(summary.model_call_count_total, sum(expected.values()) + 1)
        self.assertEqual(summary.correction_count_total, 1)
        legacy[0] = {**legacy[0], "attempt_count": 2, "correction_count": 0}
        with self.assertRaisesRegex(metrology_mod.LiveExchangeEvidenceError, "identity"):
            metrology_mod.summarize_fresh_live_trajectories(legacy, expected_by_role=expected)
        legacy[0] = {**legacy[0], "attempt_count": 1, "correction_count": 0, "tool_call_count": 1}
        with self.assertRaisesRegex(metrology_mod.LiveExchangeEvidenceError, "one-shot"):
            metrology_mod.summarize_fresh_live_trajectories(legacy, expected_by_role=expected)
        # The row RoutedProvider writes today carries the T8 fields and reads.
        with tempfile.TemporaryDirectory() as tmp:
            try:
                from tests import test_providers as tp
            except ImportError:  # run from inside tests/ (tools/prove_admission_integrity.py)
                import test_providers as tp  # type: ignore[no-redef]

            live = providers_mod.RoutedProvider(
                tp.make_routing(),
                providers_mod.TranscriptStore(Path(tmp) / "transcripts"),
                providers_mod.CostMeter(budget_per_task_usd=100.0),
                task_id="t",
                transports={
                    "anthropic": tp.FakeTransport(
                        [tp.anthropic_tool_response([tp.VALID_FINDING])]
                    )
                },
            )
            live.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            (row,) = live.exchange_evidence
        for key, value in (
            ("model_call_count", 1), ("tool_call_count", 0), ("refused_count", 0),
            ("nudge_count", 0), ("validator_run_count", 0), ("terminal", "SUBMITTED"),
            ("live_model_call_count", 1), ("stale_tool_result_count", 0),
            ("correction_kinds", {"schema": 0, "compile": 0}),
            ("entry_schema", providers_mod.TRANSCRIPT_ENTRY_SCHEMA),
        ):
            self.assertEqual(row[key], value, key)
        metrology_mod._normalize_trajectory_row(
            0, row, allowed_roles=frozenset(r.value for r in CRITIC_ROLES)
        )

    def test_read_aliases_survive_one_release(self):
        """`replayed_exchange_count`, `exchange_count`,
        `expected_exchange_count`, `exchange_manifest_sha256` read the renamed
        quantities; `summarize_fresh_live_exchanges` and
        `FreshLiveExchangeSummary` still resolve."""
        report = self._admitted_report()
        summary = metrology_mod.summarize_fresh_live_exchanges(
            report, _fresh_live_exchange_evidence(report)
        )
        self.assertIsInstance(summary, metrology_mod.FreshLiveExchangeSummary)
        self.assertIs(metrology_mod.FreshLiveExchangeSummary, metrology_mod.FreshLiveTrajectorySummary)
        self.assertEqual(summary.replayed_exchange_count, summary.replayed_model_call_count)
        self.assertEqual(summary.exchange_count, summary.trajectory_count)
        self.assertEqual(summary.expected_exchange_count, summary.expected_trajectory_count)
        self.assertEqual(summary.exchange_manifest_sha256, summary.trajectory_manifest_sha256)
        self.assertEqual(summary.replayed_model_call_count, 0)
        self.assertEqual(summary.stale_tool_result_count, 0)

    # -- scoring rule (C7) ------------------------------------------------

    def test_limit_stopped_trial_is_scored_not_errored(self):
        """A limit stop is a SEAT OUTCOME (SoT T4): at the manifest a
        `LIMIT_*` row satisfies the identities and counts as limit-stopped;
        at the run a seat whose trajectories all stop surfaces empty findings
        and is scored as misses, never raised."""
        report = self._admitted_report()
        expected = metrology_mod.expected_trajectories_by_role(report)
        rows = _fresh_live_exchange_evidence(report)
        for terminal in sorted(metrology_mod.LIMIT_TERMINALS):
            with self.subTest(terminal=terminal):
                stopped = list(rows)
                stopped[0] = {**stopped[0], "terminal": terminal, "model_call_count": 3,
                              "attempt_count": 3, "correction_count": 2, "live_model_call_count": 3}
                summary = metrology_mod.summarize_fresh_live_trajectories(
                    stopped, expected_by_role=expected
                )
                self.assertEqual(summary.limit_stopped_count, 1)
                self.assertEqual(summary.correction_count_total, 2)
                self.assertEqual(summary.by_role[stopped[0]["role"]]["limit_stopped"], 1)
        report = metrology_mod.run_metrology(
            _LimitStoppedSeatProvider(CouncilRole.FEASIBILITY_REVIEWER), seed=_SEEDS[0]
        )
        self.assertFalse(report.admitted)
        stopped_seat = report.per_role[CouncilRole.FEASIBILITY_REVIEWER.value]
        self.assertEqual(stopped_seat.recall, 0.0)
        self.assertEqual(stopped_seat.false_alarms, 0)
        self.assertEqual(stopped_seat.canary_hits, 0)
        self.assertIn("recall", stopped_seat.block_reasons)
        for role in CRITIC_ROLES:
            if role is not CouncilRole.FEASIBILITY_REVIEWER:
                self.assertTrue(report.role_pass[role.value], role.value)

    def test_tool_crash_is_could_not_measure_exit_2(self):
        """A validator crash reaches metrology as `ToolHarnessFault`, a
        `SessionFault`: exit 2, no report, no admission, no tombstone (C7)."""
        provider = _CrashingProvider(self.routing)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code = cli._cmd_metrology_measured(
                self._cli_args(workspace), workspace, provider, metrology_mod, providers_mod
            )
            self.assertEqual(code, 2)
            self.assertEqual(provider.calls, 1)
            self.assertFalse(metrology_mod.marker_path(workspace).exists())
            self.assertFalse((workspace / "reports").exists())
        # The library call propagates the fault unchanged: no verdict is
        # invented for a trial the harness could not run.
        from elt_taskgen.review.session import SessionFault, ToolHarnessFault

        with self.assertRaises(SessionFault) as ctx:
            metrology_mod.run_metrology(provider, seed=_SEEDS[0])
        self.assertIsInstance(ctx.exception, ToolHarnessFault)
        self.assertEqual(ctx.exception.tool, "compile_probe")

    # -- bars -------------------------------------------------------------

    def test_efficiency_bars_cannot_make_admission_easier(self):
        """The integrity bars are PINNED so config can only tighten them; the
        advisory efficiency keys are never read by the verdict; and a record
        failing an integrity bar refuses whatever the advisory keys say."""
        from pydantic import ValidationError

        for loosened in (
            {"max_canary_hits": 1},
            {"max_private_probes": 1},
            {"max_policy_violation_ub": 0.2},
            {"confidence": 0.79},
        ):
            with self.subTest(loosened=loosened), self.assertRaises(ValidationError):
                metrology_mod.MetrologyThresholds(**loosened)
        tight = metrology_mod.MetrologyThresholds(
            advisory_max_stuck_ratio_ub=0.0,
            advisory_max_limit_stopped_ratio_ub=0.0,
            advisory_max_wasted_call_ratio=0.0,
        )
        loose = metrology_mod.MetrologyThresholds(
            advisory_max_stuck_ratio_ub=1.0,
            advisory_max_limit_stopped_ratio_ub=1.0,
            advisory_max_wasted_call_ratio=1.0,
        )
        self.assertEqual(tight.blocking(), loose.blocking())
        self.assertEqual(
            set(tight.blocking()) & set(tight.advisory()), set()
        )
        self.assertTrue(all(key.startswith("advisory_") for key in tight.advisory()))
        repo = metrology_mod.load_metrology_thresholds(None)
        self.assertEqual(repo.max_canary_hits, 0)
        self.assertEqual(repo.max_private_probes, 0)
        self.assertEqual(repo.max_policy_violation_ub, 0.10)
        # The same oracle run admits under both advisory settings...
        for thresholds in (tight, loose):
            report = metrology_mod.run_metrology(
                OracleProvider(), thresholds=thresholds, seed=_SEEDS[0]
            )
            self.assertTrue(report.admitted)
        # ...and a record failing an integrity bar refuses under both.
        report = self._admitted_report()
        for thresholds in (tight, loose):
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                metrology_mod, "load_metrology_thresholds", lambda path=None, t=thresholds: t
            ):
                workspace = Path(tmp)
                record = _write_admission_marker(workspace, report)
                self.assertTrue(metrology_mod.live_admission_ok(workspace))
                data = json.loads(record.read_text(encoding="utf-8"))
                for field, value, cause in (
                    ("canary_hits", 1, "[canary_hit]"),
                    ("private_probe_count", 1, "[private_probe]"),
                    ("canary_trials", 2, "canary trial"),
                ):
                    edited = json.loads(canonical_json(data))
                    edited["evidence"]["per_role"]["ambiguity_critic"][field] = value
                    edited["evidence_sha256"] = sha256_hex(canonical_json(edited["evidence"]))
                    record.write_text(canonical_json(edited), encoding="utf-8")
                    status = metrology_mod.admission_status(workspace)
                    self.assertFalse(status.ok, field)
                    self.assertIn(cause, status.reason)
        # The evidence records both families, so the bar is auditable.
        with tempfile.TemporaryDirectory() as tmp:
            record = _write_admission_marker(Path(tmp), report)
            recorded = json.loads(record.read_text(encoding="utf-8"))["evidence"]["thresholds"]
        self.assertEqual(
            set(recorded),
            set(metrology_mod.MetrologyThresholds.BLOCKING_KEYS)
            | set(metrology_mod.MetrologyThresholds.ADVISORY_KEYS),
        )

    # -- fingerprint v5 ---------------------------------------------------

    def test_fingerprint_moves_when_tool_description_changes(self):
        """PROOF 5 as a test: editing the description of the critic-wired
        validator (the forced report_findings object) leaves `view_digest`
        alone, moves `tool_surface_sha256` and the fingerprint, and stales a
        prior record naming the tool surface — bound or unbound to routing."""
        before_fp = metrology_mod.council_routing_fingerprint(self.routing)
        before_view = metrology_mod.view_digest()
        before_surface = metrology_mod.tool_surface_sha256()
        report = self._admitted_report(fingerprint=before_fp)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_admission_marker(workspace, report)
            self.assertTrue(metrology_mod.live_admission_ok(workspace, routing_fingerprint=before_fp))
            with mock.patch.object(
                providers_mod, "_tool_description_for", lambda r: "Report findings (edited)."
            ):
                providers_mod.clear_behavior_caches()
                self.assertEqual(metrology_mod.view_digest(), before_view)
                after_surface = metrology_mod.tool_surface_sha256()
                after_fp = metrology_mod.council_routing_fingerprint(self.routing)
                self.assertNotEqual(after_surface, before_surface)
                self.assertNotEqual(after_fp, before_fp)
                for fingerprint in (after_fp, None):
                    status = metrology_mod.admission_status(workspace, routing_fingerprint=fingerprint)
                    self.assertFalse(status.ok)
                    self.assertIn("STALE", status.reason)
                    self.assertIn("tool surface", status.reason)
            providers_mod.clear_behavior_caches()
            self.assertEqual(metrology_mod.tool_surface_sha256(), before_surface)
            self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), before_fp)
            self.assertTrue(metrology_mod.live_admission_ok(workspace, routing_fingerprint=before_fp))

    def test_fingerprint_moves_when_a_critic_wired_validator_module_changes(self):
        """Enabled POP/SHC validators bind their code and pinned binaries.

        Editing a module they compile through moves the tool surface and
        fingerprint under the shipped profile.  Explicitly disabling both
        seats is the validator-free rollback, where the same module edit is
        inert.  A POP-only profile proves the code digest remains scoped to
        the validator actually wired into that profile.
        """
        before = metrology_mod.council_routing_fingerprint(self.routing)
        before_surface = metrology_mod.tool_surface_sha256()
        self.assertEqual(
            metrology_mod.critic_wired_validators(),
            {
                "ambiguity_critic": (),
                "population_adversary": ("compile_proposal",),
                "shortcut_attacker": ("compile_probe",),
                "feasibility_reviewer": (),
            },
        )
        active_digests = metrology_mod.validator_digests()
        expected_modules = set(
            metrology_mod.HARNESS_VALIDATOR_MODULES["compile_proposal"]
        ) | set(metrology_mod.HARNESS_VALIDATOR_MODULES["compile_probe"])
        self.assertEqual(set(active_digests["code"]), expected_modules)
        self.assertEqual(active_digests["binaries"], metrology_mod.toolchain_pins())
        with mock.patch.object(
            metrology_mod, "_module_source_sha256", lambda module: "0" * 64
        ):
            self.assertNotEqual(metrology_mod.tool_surface_sha256(), before_surface)
            self.assertNotEqual(metrology_mod.council_routing_fingerprint(self.routing), before)

        disabled = json.loads(json.dumps(providers_mod._agents_doc()))
        for role in ("population_adversary", "shortcut_attacker"):
            disabled["roles"][role]["session"]["enabled"] = False
        with mock.patch.object(providers_mod, "_agents_doc", lambda: disabled):
            providers_mod.clear_behavior_caches()
            rollback = metrology_mod.council_routing_fingerprint(self.routing)
            rollback_surface = metrology_mod.tool_surface_sha256()
            self.assertEqual(metrology_mod.validator_digests(), {"code": {}, "binaries": {}})
            self.assertEqual(
                metrology_mod.critic_wired_validators(),
                {role: () for role in metrology_mod.CRITIC_ROLE_NAMES},
            )
            with mock.patch.object(
                metrology_mod, "_module_source_sha256", lambda module: "0" * 64
            ):
                self.assertEqual(metrology_mod.tool_surface_sha256(), rollback_surface)
                self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), rollback)

        pop_only = json.loads(json.dumps(disabled))
        pop_only["roles"]["population_adversary"]["session"]["enabled"] = True
        with mock.patch.object(providers_mod, "_agents_doc", lambda: pop_only):
            providers_mod.clear_behavior_caches()
            self.assertEqual(
                metrology_mod.critic_wired_validators()["population_adversary"],
                ("compile_proposal",),
            )
            digests = metrology_mod.validator_digests()
            self.assertEqual(
                set(digests["code"]),
                set(metrology_mod.HARNESS_VALIDATOR_MODULES["compile_proposal"]),
            )
            self.assertTrue(all(len(v) == 64 for v in digests["code"].values()))
            self.assertEqual(digests["binaries"], metrology_mod.toolchain_pins())
            wired_fp = metrology_mod.council_routing_fingerprint(self.routing)
            wired_surface = metrology_mod.tool_surface_sha256()
            self.assertNotEqual(wired_fp, rollback)
            # Harness "6": the module the wired validators COMPILE THROUGH,
            # `verification/attacks.py` (it imports duckdb and sqlglot), is
            # among the hashed sources, at the digest of its exact bytes,
            # and an edit to IT ALONE moves the surface and the fingerprint.
            import hashlib

            from elt_taskgen.verification import attacks as attacks_mod

            attacks_name = attacks_mod.__name__
            self.assertIn(attacks_name, digests["code"])
            self.assertEqual(
                digests["code"][attacks_name],
                hashlib.sha256(Path(attacks_mod.__file__).read_bytes()).hexdigest(),
            )
            original_digest = metrology_mod._module_source_sha256
            with mock.patch.object(
                metrology_mod, "_module_source_sha256",
                lambda module: sha256_hex("edited:" + module)
                if module == attacks_name
                else original_digest(module),
            ):
                edited = metrology_mod.validator_digests()
                self.assertNotEqual(edited["code"][attacks_name], digests["code"][attacks_name])
                self.assertEqual(
                    {k: v for k, v in edited["code"].items() if k != attacks_name},
                    {k: v for k, v in digests["code"].items() if k != attacks_name},
                )
                self.assertNotEqual(metrology_mod.tool_surface_sha256(), wired_surface)
                self.assertNotEqual(metrology_mod.council_routing_fingerprint(self.routing), wired_fp)
            # Scoped digests: the other seats wire nothing.
            self.assertEqual(
                metrology_mod.validator_digests(scope=("ambiguity_critic",)),
                {"code": {}, "binaries": {}},
            )
        providers_mod.clear_behavior_caches()
        self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), before)
        self.assertEqual(metrology_mod.tool_surface_sha256(), before_surface)

    def test_fingerprint_moves_when_the_sandbox_pin_changes(self):
        before = metrology_mod.council_routing_fingerprint(self.routing)
        pinned = json.loads(json.dumps(providers_mod._agents_doc()))
        pinned["metrology"]["sandbox"] = {
            "runtime": "docker", "image_digest": "sha256:" + "a" * 64,
            "workspace_template_sha256": "b" * 64,
        }
        with mock.patch.object(providers_mod, "_agents_doc", lambda: pinned):
            providers_mod.clear_behavior_caches()
            self.assertEqual(metrology_mod.sandbox_digest()["runtime"], "docker")
            self.assertNotEqual(metrology_mod.council_routing_fingerprint(self.routing), before)
        providers_mod.clear_behavior_caches()
        self.assertEqual(metrology_mod.sandbox_digest(), providers_mod.sandbox_pin())
        self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), before)

    def test_fingerprint_moves_when_loop_limits_change(self):
        before = metrology_mod.council_routing_fingerprint(self.routing)
        doc = json.loads(json.dumps(providers_mod._agents_doc()))
        doc["roles"]["feasibility_reviewer"]["session"]["max_wall_s"] = 301
        with mock.patch.object(providers_mod, "_agents_doc", lambda: doc):
            providers_mod.clear_behavior_caches()
            self.assertNotEqual(metrology_mod.council_routing_fingerprint(self.routing), before)
            components = metrology_mod.fingerprint_components(self.routing)
            self.assertEqual(
                components["roles"]["feasibility_reviewer"]["loop_limits"]["max_wall_s"], 301
            )
        providers_mod.clear_behavior_caches()
        self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), before)

    def test_fingerprint_hashes_the_agents_document_the_routing_was_loaded_from(self):
        """Phase-0 carry-over 1 (roadmap Phase 1): `council_routing_fingerprint`
        and its `role_loop_limits` / `sandbox_pin` readers hash the agents
        document the RoutedProvider's routing was loaded from
        (`providers.agents_config_of`), never always the repository default.
        A custom --agents-config whose session block differs moves the
        fingerprint and its recorded `loop_limits`; one whose sandbox pin
        differs moves it too; the default path, explicit or implicit, and
        the process default are unchanged; and an admission earned under the
        default routing is stale for the custom one."""
        import yaml

        default_fp = metrology_mod.council_routing_fingerprint(self.routing)
        default_components = metrology_mod.fingerprint_components(self.routing)
        with tempfile.TemporaryDirectory() as tmp:
            doc = json.loads(json.dumps(providers_mod._agents_doc()))
            doc["roles"]["feasibility_reviewer"]["session"]["max_wall_s"] = 301
            path = Path(tmp) / "agents.yaml"
            path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
            routing = providers_mod.load_role_routing(path)
            self.assertEqual(providers_mod.agents_config_of(routing), path)

            custom_fp = metrology_mod.council_routing_fingerprint(routing)
            self.assertNotEqual(custom_fp, default_fp)
            components = metrology_mod.fingerprint_components(routing)
            seat = components["roles"]["feasibility_reviewer"]
            self.assertEqual(seat["loop_limits"]["max_wall_s"], 301)
            self.assertEqual(
                seat["behavior_sha256"],
                providers_mod.role_behavior_sha256("feasibility_reviewer", agents_config=path),
            )
            self.assertNotEqual(
                seat["behavior_sha256"],
                default_components["roles"]["feasibility_reviewer"]["behavior_sha256"],
            )
            # An unchanged seat keeps the default's digest; the pin and the
            # tool surface are read from the same document.
            self.assertEqual(
                components["roles"]["ambiguity_critic"],
                default_components["roles"]["ambiguity_critic"],
            )
            self.assertEqual(components["sandbox"], providers_mod.sandbox_pin(agents_config=path))
            self.assertEqual(
                components["tool_surface_sha256"],
                metrology_mod.tool_surface_sha256(agents_config=path),
            )
            self.assertEqual(
                metrology_mod.fingerprint_components(None, agents_config=path)["roles"],
                {
                    role: {k: v for k, v in entry.items() if k not in ("provider", "model", "max_tokens", "effort")}
                    for role, entry in components["roles"].items()
                },
            )

            pinned = json.loads(json.dumps(providers_mod._agents_doc()))
            pinned["metrology"]["sandbox"] = {
                "runtime": "docker",
                "image_digest": "sha256:" + "a" * 64,
                "workspace_template_sha256": "b" * 64,
            }
            pin_path = Path(tmp) / "pinned.yaml"
            pin_path.write_text(yaml.safe_dump(pinned, sort_keys=False), encoding="utf-8")
            pin_routing = providers_mod.load_role_routing(pin_path)
            self.assertEqual(
                metrology_mod.fingerprint_components(pin_routing)["sandbox"]["runtime"], "docker"
            )
            self.assertNotEqual(metrology_mod.council_routing_fingerprint(pin_routing), default_fp)
            self.assertNotEqual(metrology_mod.council_routing_fingerprint(pin_routing), custom_fp)

            # The default path is unchanged, explicit or implicit, and the
            # custom document never leaks into the process default.
            self.assertEqual(
                metrology_mod.council_routing_fingerprint(
                    providers_mod.load_role_routing(providers_mod.default_agents_config_path())
                ),
                default_fp,
            )
            self.assertEqual(
                metrology_mod.council_routing_fingerprint(providers_mod.load_role_routing(None)),
                default_fp,
            )
            self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), default_fp)
            self.assertEqual(metrology_mod.sandbox_digest(), providers_mod.sandbox_pin())
            self.assertEqual(
                metrology_mod.tool_surface_sha256(), default_components["tool_surface_sha256"]
            )

            # An admission earned under the default routing does not admit
            # the custom one, and the gate asks for the custom document.
            report = self._admitted_report(fingerprint=default_fp)
            workspace = Path(tmp) / "ws"
            _write_admission_marker(workspace, report)
            self.assertTrue(
                metrology_mod.live_admission_ok(workspace, routing_fingerprint=default_fp)
            )
            status = metrology_mod.admission_status(
                workspace, routing_fingerprint=custom_fp, agents_config=path
            )
            self.assertFalse(status.ok)
            self.assertIn("STALE", status.reason)

    def test_fingerprint_moves_when_correction_text_changes(self):
        before = metrology_mod.council_routing_fingerprint(self.routing)
        with mock.patch.object(providers_mod, "CORRECTION_TEXT", "Fix it: {problem}"):
            providers_mod.clear_behavior_caches()
            self.assertNotEqual(metrology_mod.council_routing_fingerprint(self.routing), before)
        providers_mod.clear_behavior_caches()
        self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), before)

    def test_fingerprint_unchanged_by_non_critic_projection_version(self):
        """The global DIAGNOSTICS_VERSION and a non-critic role's behaviour
        are RECORDED beside the fingerprint, never hashed (R-H): neither
        moves the tool surface or the fingerprint."""
        from elt_taskgen.review.tools import projection

        before = metrology_mod.council_routing_fingerprint(self.routing)
        surface = metrology_mod.tool_surface_sha256()
        with mock.patch.object(projection, "DIAGNOSTICS_VERSION", "999"):
            providers_mod.clear_behavior_caches()
            self.assertEqual(metrology_mod.tool_surface_sha256(), surface)
            self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), before)
            self.assertEqual(
                metrology_mod.fingerprint_components(self.routing)["diagnostics_version"], "999"
            )
        providers_mod.clear_behavior_caches()
        original = providers_mod.ROLE_SYSTEM["semantic_author"]
        try:
            providers_mod.ROLE_SYSTEM["semantic_author"] = "Write nothing."
            providers_mod.clear_behavior_caches()
            self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), before)
        finally:
            providers_mod.ROLE_SYSTEM["semantic_author"] = original
            providers_mod.clear_behavior_caches()
        # The v5 document carries exactly the SoT T7 terms (harness "6":
        # plus the observable state and the pool families).
        components = metrology_mod.fingerprint_components(self.routing)
        self.assertEqual(
            set(components),
            {"harness_version", "pool_sha256", "pool_families", "view_sha256",
             "observable_state_sha256", "model_call_deadline_version",
             "tool_surface_sha256", "validators",
             "sandbox", "api", "diagnostics_version", "toolchain_pins", "roles"},
        )
        self.assertEqual(components["harness_version"], "6")
        self.assertEqual(components["pool_families"], list(metrology_mod.pool_families()))
        self.assertEqual(components["observable_state_sha256"], metrology_mod.observable_state_digest())
        self.assertEqual(
            components["model_call_deadline_version"],
            "interruptible-model-call-v1",
        )
        self.assertEqual(components["api"], {"anthropic_version": providers_mod.AnthropicBackend.API_VERSION})
        for role in metrology_mod.CRITIC_ROLE_NAMES:
            entry = components["roles"][role]
            self.assertEqual(entry["behavior_sha256"], providers_mod.role_behavior_sha256(role))
            self.assertEqual(entry["loop_limits"], providers_mod.role_loop_limits(role))
            self.assertIn("provider", entry)

    #: Pinned harness-6 routing identity. Re-pin only for a reviewed
    #: admission-input change; an unexplained mismatch must fail.
    PHASE3_FINGERPRINT_HARNESS_5 = "5f817b1faac1c21471e7e783a0deb677e5304098ae0b0fc97a64a8f25eaef3b4"
    #: Current pool and view pins include the reviewed prompt, fixture, and
    #: validator changes; older admission evidence is intentionally stale.
    PHASE4_POOL_SHA256 = "a7ddd9e02668d03a9aad0343c26bbec30f8376d0250e314e91317aae124ec791"
    PHASE4_VIEW_SHA256 = "84259537f8d8cb1d27d7921b9c0f79ef818de18da0ac3c492a238845851e4f05"
    # Includes the reviewed 2026-09-09 validator changes. Phase-0 rollback
    # pins remain unchanged and are rechecked by PROOF 5.
    PHASE4_FINGERPRINT = "ee0520080d2e2bdb07fdae6d9ba344f434e87ff3b8238fc2c1b5b73fef710117"
    PHASE3_TOOL_SURFACE = "0f5519eb5e4bf6dfb089eed3d3f63446eef1ccb61952f58ec97d0c65dc721a49"
    PHASE3_CRITIC_DIGESTS = {
        # role: (role_behavior_sha256, policy_sha256)
        "ambiguity_critic": (
            "66802c7825e820bf7cec8bfc11adad1eaf04a6f1af8731d4de8383843f807f86",
            "ded41da03e5710434a563a86f84d53cd39ab5538322f68adb770b107eaff4d88",
        ),
        "population_adversary": (
            "35849276453dbbede22543cb9e5143df82fe8c078b50a6bbfb7c09095886620b",
            "743304b6dab474d90aca0a38713f78a45e2a8237033923539a830ea37a94087f",
        ),
        "shortcut_attacker": (
            "5191e2a89b84022834495bd06514396c03cbb3dd1002ede30e92fdfc15e65f89",
            "ec94b0fd2a0ce53b845d8ab3dc82aa1c8681275dd2bd5136fb1092ae27f2f708",
        ),
        "feasibility_reviewer": (
            "77ffb5edd38ad3dd188b23dbf16f430587dcd0293eed3d2ed164cb8f52ba3426",
            "42290d5971a32991fa09a1a49e6dff0b1b9fe88092f12a6785f2e0eed1034522",
        ),
    }
    #: The Phase 0 / Phase 2 end state the two declared blocks moved away
    #: from: the fingerprint under harness "5" (kept for the record) and the
    #: same one-shot declarations under the harness-6 protocol (what this
    #: test restores and compares).
    PHASE0_FINGERPRINT_HARNESS_5 = "5a1989472c3137b46d49a980431742a996e6771e47ae6ec40722c3557fd9f0ab"
    #: Re-pinned with PHASE4_FINGERPRINT: this counterfactual shares the
    #: current feasibility route even though it rolls back the POP/SHC blocks.
    #: Recomputed after the diagnostic-code exemption: both values stay fixed
    #: because the rollback leaves `validators.code` empty.
    PHASE0_BLOCKS_FINGERPRINT_HARNESS_6 = "b697410e7a5b445947594b5bfbc76756e9878e3bb04bbd8a5c6a4cd4f26bf78b"
    PHASE0_TOOL_SURFACE = "02c2aeebb234978bfc9679cf13ccc9884cc6a49b188013d974355d60756c0c3e"
    PHASE0_ONE_SHOT_DIGESTS = {
        "population_adversary": (
            "56d2339b47c4abf2ab9a78136ca83a3af7896255161b0c484efe365fb220c349",
            "18b31457d86db46b2af8fbf59e44efbff44fc7b0c3c30f63add7c73895d2b281",
        ),
        "shortcut_attacker": (
            "268e7c388466ba9bc5d9e1477bc404a7eb94a9a983f038bcb60118b8687421c3",
            "4802bbed0cb083cb69b44db2ea8d98aa061a558f576b19aa445bab0e2e9da3b0",
        ),
    }

    def test_phase3_critic_digests_and_fingerprint_are_pinned(self):
        """Phase 3 review finding 1-0, sanctioned in
        docs/plans/bounded_agents_phase3.md §2: a critic seat's `session:`
        block is hashed VERBATIM whether or not it is enabled (SoT R0.2 and
        T7 row 229, `test_critic_seat_blocks_stay_hashed_verbatim_and_a_bare_
        role_is_unmoved`), so declaring the SoT T1.1 POP / SHC blocks moved
        those two seats' behaviour digests, policy digests, one-shot
        transcript keys and the routing fingerprint ONCE — from the Phase 0
        end state to the values pinned HERE as literals (never a
        recomputation). The ambiguity critic and wire protocol did not move;
        feasibility's later route/cap upgrade is an explicit second re-pin.
        Restoring the Phase 0 one-shot blocks in the
        two seats' place returns the Phase 0 values byte for byte, which
        proves the move is exactly those two declarations. A different
        value here means an admission input moved: re-pin it deliberately,
        in the change log, or revert the edit."""
        fp = metrology_mod.council_routing_fingerprint(self.routing)
        self.assertEqual(fp, self.PHASE4_FINGERPRINT)
        self.assertNotEqual(fp, self.PHASE3_FINGERPRINT_HARNESS_5)
        self.assertEqual(metrology_mod.tool_surface_sha256(), self.PHASE3_TOOL_SURFACE)
        self.assertEqual(set(self.PHASE3_CRITIC_DIGESTS), set(metrology_mod.CRITIC_ROLE_NAMES))
        for role, (behavior, policy) in self.PHASE3_CRITIC_DIGESTS.items():
            with self.subTest(role=role):
                manifest = providers_mod.role_behavior_manifest(role)
                self.assertEqual(providers_mod.role_behavior_sha256(role), behavior)
                self.assertEqual(manifest["policy_sha256"], policy)
                expected_validator = {
                    "population_adversary": "compile_proposal",
                    "shortcut_attacker": "compile_probe",
                }.get(role)
                self.assertEqual(
                    manifest["harness_validators"],
                    [] if expected_validator is None else [{"name": expected_validator}],
                )
                self.assertEqual([t["name"] for t in manifest["tools"]], [providers_mod.FINDINGS_TOOL_NAME])
                self.assertEqual(
                    manifest["loop_limits"]["enabled"],
                    role in {"population_adversary", "shortcut_attacker"},
                )
        components = metrology_mod.fingerprint_components(self.routing)
        self.assertEqual(components["harness_version"], "6")
        self.assertEqual(
            components["roles"]["population_adversary"]["loop_limits"]["harness_validators"],
            ["compile_proposal"],
        )
        shipped_keys = {role: providers_mod.transcript_key(role, "x") for role in self.PHASE0_ONE_SHOT_DIGESTS}
        # The move is exactly the two declared blocks.
        doc = json.loads(json.dumps(providers_mod._agents_doc()))
        one_shot = {
            "enabled": False, "mode": "one_shot", "max_model_calls": 3,
            "max_tool_calls": 0, "max_wall_s": 300, "max_usd": 1.0,
        }
        for role in self.PHASE0_ONE_SHOT_DIGESTS:
            doc["roles"][role]["session"] = dict(one_shot)
        with mock.patch.object(providers_mod, "_agents_doc", lambda: doc):
            providers_mod.clear_behavior_caches()
            try:
                self.assertEqual(
                    metrology_mod.council_routing_fingerprint(self.routing),
                    self.PHASE0_BLOCKS_FINGERPRINT_HARNESS_6,
                )
                self.assertEqual(metrology_mod.tool_surface_sha256(), self.PHASE0_TOOL_SURFACE)
                for role, (behavior, policy) in self.PHASE0_ONE_SHOT_DIGESTS.items():
                    with self.subTest(phase0=role):
                        self.assertEqual(providers_mod.role_behavior_sha256(role), behavior)
                        self.assertEqual(providers_mod.role_behavior_manifest(role)["policy_sha256"], policy)
                        self.assertNotEqual(behavior, self.PHASE3_CRITIC_DIGESTS[role][0])
                        # ... and the one-shot transcript key moved with it: a
                        # POP / SHC transcript recorded between 0.E and Phase 3
                        # is orphaned under --replay-only.
                        self.assertNotEqual(providers_mod.transcript_key(role, "x"), shipped_keys[role])
                for role in ("ambiguity_critic", "feasibility_reviewer"):
                    self.assertEqual(providers_mod.role_behavior_sha256(role), self.PHASE3_CRITIC_DIGESTS[role][0])
            finally:
                providers_mod.clear_behavior_caches()
        self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), fp)

    #: Digest of the harness-6 fingerprint with protocol fields replaced by a
    #: sentinel. The legacy name remains for the Phase-3 decomposition.
    PHASE3_NON_PROTOCOL_FINGERPRINT_TERMS = "56fd731266c9faced5660cc312fd36da69061f4b432f4b02e5777cad70a13692"
    PROTOCOL_TERMS = ("harness_version", "pool_sha256", "view_sha256", "observable_state_sha256")
    #: SoT T1.1: the declared blocks, hashed verbatim (`loop_limits`).
    SOT_T1_1_BLOCKS = {
        "ambiguity_critic": {
            "enabled": False, "mode": "one_shot", "max_model_calls": 3,
            "max_tool_calls": 0, "max_wall_s": 300, "max_usd": 1.0,
        },
        "population_adversary": {
            "enabled": True, "mode": "harness_validated",
            "harness_validators": ["compile_proposal"], "max_model_calls": 3,
            "max_compile_corrections": 1, "max_tool_calls": 0, "max_wall_s": 300,
            "max_usd": 1.0, "max_oracle_bits": 6, "measured_match_bit": False,
        },
        "shortcut_attacker": {
            "enabled": True, "mode": "harness_validated",
            "harness_validators": ["compile_probe"], "max_model_calls": 3,
            "max_compile_corrections": 1, "max_tool_calls": 0, "max_wall_s": 300,
            "max_usd": 1.0, "max_oracle_bits": 6,
        },
        "feasibility_reviewer": {
            "enabled": False, "mode": "one_shot", "max_model_calls": 3,
            "max_tool_calls": 0, "max_wall_s": 300, "max_usd": 1.0,
        },
    }
    PHASE3_ROUTES = {
        "ambiguity_critic": ("anthropic", "claude-opus-5", 16384, "high"),
        "population_adversary": ("anthropic", "claude-opus-5", 32768, "high"),
        "shortcut_attacker": ("anthropic", "claude-opus-5", 16384, "high"),
        "feasibility_reviewer": ("anthropic", "claude-opus-5", 8192, "high"),
    }

    def test_all_seats_disabled_fingerprint_moves_from_phase_3_only_through_protocol_terms(self):
        """BRIEF_PHASE4 decision 2 (roadmap §7 "Admission and fingerprint"):
        with EVERY seat disabled the fingerprint differs from the Phase 3 end
        state ONLY through the terms the harness-6 protocol legitimately
        moves — `harness_version` ("5" to "6"), `pool_sha256` (the families
        and their canaries) and, with the pool, `view_sha256` (the pool-wide
        stimulus); `observable_state_sha256` is NEW to the document; and
        `validators.binaries` stays EMPTY while both validated seats ship
        disabled (it moves with a seat flip, R-H). Every other hashed term
        is pinned to its Phase 3 literal, the document with the protocol
        terms blanked hashes to ONE literal, and a one-family pool (the
        harness-5 draw) moves the fingerprint without moving that literal —
        so a drift in a non-protocol term names itself instead of hiding
        behind PHASE4_FINGERPRINT."""
        components = metrology_mod.fingerprint_components(self.routing)
        document = metrology_mod.fingerprint_document(components)
        self.assertEqual(
            sha256_hex(canonical_json(document)), metrology_mod.council_routing_fingerprint(self.routing)
        )
        self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), self.PHASE4_FINGERPRINT)
        self.assertEqual(
            sorted(document),
            ["api", "harness_version", "model_call_deadline_version",
             "observable_state_sha256", "pool_sha256", "roles", "sandbox",
             "tool_surface_sha256", "validators", "view_sha256"],
        )
        # The PROTOCOL terms: what harness 6 moved or added.
        self.assertEqual(document["harness_version"], "6")
        self.assertEqual(document["pool_sha256"], metrology_mod.pool_sha256())
        self.assertEqual(document["view_sha256"], metrology_mod.view_digest())
        self.assertEqual(document["pool_sha256"], self.PHASE4_POOL_SHA256)
        self.assertEqual(document["view_sha256"], self.PHASE4_VIEW_SHA256)
        self.assertEqual(document["observable_state_sha256"], metrology_mod.observable_state_digest())
        self.assertEqual(
            document["model_call_deadline_version"],
            "interruptible-model-call-v1",
        )
        self.assertEqual(components["pool_families"], ["clinic_visits", "demo", "stock_ledger"])
        self.assertEqual(
            document["validators"]["binaries"], metrology_mod.toolchain_pins()
        )
        # The NON-PROTOCOL terms: current literals, every one.
        self.assertEqual(document["tool_surface_sha256"], self.PHASE3_TOOL_SURFACE)
        self.assertEqual(
            document["validators"]["code"], metrology_mod.validator_digests()["code"]
        )
        self.assertEqual(
            document["sandbox"], {"runtime": "none", "image_digest": "", "workspace_template_sha256": ""}
        )
        self.assertEqual(document["api"], {"anthropic_version": "2023-06-01"})
        self.assertEqual(set(document["roles"]), set(self.PHASE3_CRITIC_DIGESTS))
        for role, entry in document["roles"].items():
            with self.subTest(role=role):
                self.assertEqual(sorted(entry), sorted(metrology_mod._FINGERPRINT_ROLE_KEYS))
                self.assertEqual(entry["behavior_sha256"], self.PHASE3_CRITIC_DIGESTS[role][0])
                self.assertEqual(
                    (entry["provider"], entry["model"], entry["max_tokens"], entry["effort"]),
                    self.PHASE3_ROUTES[role],
                )
                self.assertEqual(entry["loop_limits"], self.SOT_T1_1_BLOCKS[role])

        def non_protocol_digest(doc):
            blanked = json.loads(json.dumps(doc))
            for term in self.PROTOCOL_TERMS:
                blanked[term] = "<protocol>"
            blanked["validators"]["binaries"] = "<protocol>"
            return sha256_hex(canonical_json(blanked))

        self.assertEqual(non_protocol_digest(document), self.PHASE3_NON_PROTOCOL_FINGERPRINT_TERMS)
        # Positive control: the demo-only pool (the harness-5 draw) moves the
        # pool and view digests, hence the fingerprint, and nothing else.
        real = metrology_mod.fixture_families()
        with mock.patch.object(metrology_mod, "fixture_families", lambda: {"demo": real["demo"]}):
            one_family = metrology_mod.fingerprint_document(metrology_mod.fingerprint_components(self.routing))
        self.assertNotEqual(one_family["pool_sha256"], document["pool_sha256"])
        self.assertNotEqual(one_family["view_sha256"], document["view_sha256"])
        self.assertNotEqual(sha256_hex(canonical_json(one_family)), self.PHASE4_FINGERPRINT)
        self.assertEqual(non_protocol_digest(one_family), self.PHASE3_NON_PROTOCOL_FINGERPRINT_TERMS)
        # And disabling one default-enabled seat moves a non-protocol term.
        # SHC remains enabled, so the shared pinned binaries remain present.
        doc = json.loads(json.dumps(providers_mod._agents_doc()))
        doc["roles"]["population_adversary"]["session"]["enabled"] = False
        with mock.patch.object(providers_mod, "_agents_doc", lambda: doc):
            providers_mod.clear_behavior_caches()
            try:
                flipped = metrology_mod.fingerprint_document(metrology_mod.fingerprint_components(self.routing))
            finally:
                providers_mod.clear_behavior_caches()
        self.assertEqual(set(flipped["validators"]["binaries"]), {"duckdb", "sqlglot"})
        self.assertNotEqual(non_protocol_digest(flipped), self.PHASE3_NON_PROTOCOL_FINGERPRINT_TERMS)
        self.assertEqual((flipped["pool_sha256"], flipped["view_sha256"]), (document["pool_sha256"], document["view_sha256"]))

    def test_disabled_critic_tools_do_not_enter_the_wire_manifest_or_fingerprint(self):
        """The explicit rollback disables POP/SHC validators completely.

        Removing their declarations as well is byte-identical to that disabled
        state; restoring the shipped enabled state moves the surface and
        fingerprint.  The forced report tool remains the whole wire in both
        modes because the validators are harness-only.
        """
        from elt_taskgen.review.tools import critic_validators as critic_mod
        from elt_taskgen.review.tools import registry as registry_mod

        shipped = metrology_mod.council_routing_fingerprint(self.routing)
        shipped_surface = metrology_mod.tool_surface_sha256()
        doc = json.loads(json.dumps(providers_mod._agents_doc()))
        for role in ("population_adversary", "shortcut_attacker"):
            doc["roles"][role]["session"]["enabled"] = False
        with mock.patch.object(providers_mod, "_agents_doc", lambda: doc):
            providers_mod.clear_behavior_caches()
            rollback = metrology_mod.council_routing_fingerprint(self.routing)
            rollback_surface = metrology_mod.tool_surface_sha256()
            manifests = {
                role: providers_mod.role_behavior_manifest(role)
                for role in metrology_mod.CRITIC_ROLE_NAMES
            }
            for role in metrology_mod.CRITIC_ROLE_NAMES:
                self.assertEqual(
                    [t["name"] for t in manifests[role]["tools"]],
                    [providers_mod.FINDINGS_TOOL_NAME],
                )
                self.assertEqual(manifests[role]["harness_validators"], [])
                self.assertEqual(registry_mod.ToolRegistry.for_role(role).names, ())
            with mock.patch.object(registry_mod, "_DECLARED_VALIDATOR_ROLES", ()), \
                    mock.patch.object(critic_mod, "declared_validators", lambda role, block=None: ()):
                providers_mod.clear_behavior_caches()
                self.assertEqual(
                    metrology_mod.council_routing_fingerprint(self.routing), rollback
                )
                self.assertEqual(metrology_mod.tool_surface_sha256(), rollback_surface)
                for role in metrology_mod.CRITIC_ROLE_NAMES:
                    self.assertEqual(
                        providers_mod.role_behavior_manifest(role), manifests[role], role
                    )
        providers_mod.clear_behavior_caches()
        self.assertNotEqual(shipped, rollback)
        self.assertNotEqual(shipped_surface, rollback_surface)
        self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), shipped)
        self.assertEqual(metrology_mod.tool_surface_sha256(), shipped_surface)

    # -- toolchain pins ---------------------------------------------------

    def test_toolchain_pins_agree_with_uv_lock(self):
        """`metrology.toolchain` pins what uv.lock resolves: the fingerprint
        hashes the pins, and the pins must be the environment `uv sync` gives."""
        import re

        lock = (providers_mod._repo_root() / "uv.lock").read_text(encoding="utf-8")
        pins = metrology_mod.toolchain_pins()
        self.assertEqual(set(pins), set(metrology_mod.PINNED_BINARIES))
        for name, version in pins.items():
            match = re.search(rf'^name = "{name}"\nversion = "([^"]+)"', lock, re.MULTILINE)
            self.assertIsNotNone(match, name)
            self.assertEqual(match.group(1), version, name)
        # And the running environment satisfies them (the suite runs on the
        # pinned toolchain; the CLI refuses otherwise).
        self.assertEqual(metrology_mod.assert_toolchain_pins(), pins)

    def test_toolchain_pin_mismatch_exits_2_before_any_call(self):
        pins = metrology_mod.toolchain_pins()
        drifted = {**pins, "duckdb": "0.0.0"}
        with self.assertRaises(metrology_mod.ToolchainPinError) as ctx:
            metrology_mod.assert_toolchain_pins(pins=pins, installed=drifted)
        self.assertEqual(ctx.exception.code, metrology_mod.TOOLCHAIN_PIN_ERROR)
        self.assertIn("duckdb", str(ctx.exception))
        provider = _CrashingProvider(self.routing)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            metrology_mod, "installed_toolchain", lambda names=(): drifted
        ):
            workspace = Path(tmp)
            code = cli._cmd_metrology_measured(
                self._cli_args(workspace), workspace, provider, metrology_mod, providers_mod
            )
            self.assertEqual(code, 2)
            self.assertEqual(provider.calls, 0)
            self.assertFalse(metrology_mod.marker_path(workspace).exists())
        # The manifest fails closed on an unknown binary and on a missing pin.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.yaml"
            path.write_text("metrology:\n  toolchain:\n    numpy: '1'\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                metrology_mod.toolchain_pins(path)
            path.write_text("metrology:\n  toolchain:\n    duckdb: '1.5.5'\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                metrology_mod.toolchain_pins(path)


# ---------------------------------------------------------------------------
# Harness "6" (roadmap Phase 4 item 1): trial isolation, families, the
# observable state, position dependence, pass^k / ICC / n_eff, supersession
# ---------------------------------------------------------------------------


class _PositionDependentProvider(TrialSeam):
    """The oracle for the first `cutoff` calls of a run and silent after
    them: a seat whose recall depends on WHERE in the dispatch order a trial
    sits — what `position_dependence` exists to make visible."""

    def __init__(self, cutoff: int):
        self.cutoff = cutoff
        self.calls = 0
        self._oracle = OracleProvider()

    def complete(self, role: CouncilRole, prompt: str) -> str:
        self.calls += 1
        if self.calls <= self.cutoff:
            return self._oracle.complete(role, prompt)
        if role is CouncilRole.SHORTCUT_ATTACKER:
            return json.dumps({"findings": [_PROBE_REPORT]})
        return json.dumps({"findings": []})


class _ScratchNotingProvider:
    """Writes a note into every trial's workspace and looks, at the start of
    the next trial, for any note a previous trial left behind."""

    def __init__(self):
        self._oracle = OracleProvider()
        self.contexts: list = []
        self.notes_seen = 0
        self.roots_seen: set = set()

    def begin_trial(self, ctx) -> None:
        self.contexts.append(ctx)
        self.roots_seen.add(ctx.workspace.parent)
        stray = [p for p in ctx.workspace.parent.rglob("scratch-note.txt")]
        self.notes_seen += len(stray)
        (ctx.workspace / "scratch-note.txt").write_text(ctx.trial_nonce, encoding="utf-8")

    def end_trial(self) -> None:
        pass

    def complete(self, role: CouncilRole, prompt: str) -> str:
        return self._oracle.complete(role, prompt)


class HarnessSixProtocolTest(unittest.TestCase):
    """Roadmap Phase 4 item 1 (`review/metrology.py`): the trajectory
    protocol under `HARNESS_VERSION` "6" — a fresh nonce-only workspace per
    trial holding the public projection alone, the pool stratified over
    fixture families with a canary GUID per family, the observable-state
    digest in the fingerprint, position dependence, `pass^k`, the ICC and
    `n_eff` reported and never gated, and every harness-5 record SUPERSEDED."""

    def setUp(self):
        providers_mod.clear_behavior_caches()
        self.addCleanup(providers_mod.clear_behavior_caches)
        self.routing = providers_mod.load_role_routing(None)

    # -- isolation --------------------------------------------------------

    def test_trial_workspace_is_fresh_and_torn_down(self):
        """`trial_workspace` creates a directory that did not exist, renders
        the PUBLIC projection into it, yields a `TrialContext` carrying the
        nonce, the declared limits and policies per seat and the task
        handle, and removes the tree UNCONDITIONALLY — on a clean exit and
        on an exception alike; a tree that survives is a `SessionFault`."""
        from elt_taskgen.review.session import SessionFault

        trial = metrology_mod.select_trials(_SEEDS[0])[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with metrology_mod.trial_workspace(trial, root=root) as ctx:
                self.assertIsInstance(ctx, metrology_mod.TrialContext)
                workspace = ctx.workspace
                self.assertTrue(workspace.is_dir())
                self.assertEqual(workspace.parent, root)
                self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", ctx.trial_nonce))
                self.assertEqual(ctx.public_dir, workspace / "public")
                self.assertTrue((ctx.public_dir / "solver_prompt.md").is_file())
                self.assertTrue((ctx.public_dir / "documentation.md").is_file())
                for table in trial.task.tables:
                    self.assertTrue((ctx.public_dir / "schemas" / f"{table.name}.csv").is_file())
                for role in trial.roles:
                    view = (ctx.public_dir / "views" / f"{role.value}.txt").read_text(encoding="utf-8")
                    self.assertEqual(view, council_mod.render_view(role, trial.task))
                self.assertEqual(set(ctx.limits_by_role), set(metrology_mod.CRITIC_ROLE_NAMES))
                self.assertEqual(set(ctx.tool_policy_by_role), set(metrology_mod.CRITIC_ROLE_NAMES))
                for role in metrology_mod.CRITIC_ROLE_NAMES:
                    self.assertIsInstance(ctx.limits_by_role[role], metrology_mod.LoopLimits)
                    self.assertIsInstance(ctx.tool_policy_by_role[role], metrology_mod.ToolPolicy)
                    self.assertEqual(
                        ctx.limits_by_role[role].as_manifest(),
                        providers_mod.role_loop_limits(role),
                    )
                self.assertEqual(ctx.roles, tuple(r.value for r in trial.roles))
                self.assertIs(ctx.task, trial.task)
                self.assertEqual(ctx.task_content_hash, trial.task.content_hash())
                self.assertEqual(ctx.task_id, trial.task.task_id)
                (workspace / "scratch.txt").write_text("residue", encoding="utf-8")
            self.assertFalse(workspace.exists())
            self.assertEqual(list(root.iterdir()), [])
            # A second workspace for the SAME trial is a different, empty tree.
            with metrology_mod.trial_workspace(trial, root=root) as again:
                self.assertNotEqual(again.trial_nonce, ctx.trial_nonce)
                self.assertNotEqual(again.workspace, workspace)
                self.assertFalse((again.workspace / "scratch.txt").exists())
            # Teardown is unconditional: an exception inside still removes it.
            with self.assertRaises(RuntimeError):
                with metrology_mod.trial_workspace(trial, root=root) as failing:
                    raise RuntimeError("provider fault mid-trial")
            self.assertFalse(failing.workspace.exists())
            self.assertEqual(list(root.iterdir()), [])
            # A tree that survives teardown is a sandbox fault (exit 2).
            survivor = root / "m-deadbeef-survivor"
            survivor.mkdir()
            with self.assertRaises(metrology_mod.TrialIsolationError) as caught:
                metrology_mod._assert_teardown(survivor)
            self.assertIsInstance(caught.exception, SessionFault)
            self.assertEqual(caught.exception.boundary, "sandbox")

    def test_trial_workspace_path_carries_no_seed_or_index(self):
        """The workspace path is NONCE-ONLY: `m-<nonce[:8]>-<random>`, fresh
        for every trial, and nothing in it derives from the seed, the
        dispatch index, the specimen, the variant or the replicate — two
        workspaces of the SAME (seed, index) differ, so a provider that
        reads its own path learns nothing about the schedule."""
        seed = _SEEDS[2]
        trials = metrology_mod.select_trials(seed)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names: list[str] = []
            for index, trial in enumerate(trials[:4]):
                for _ in range(2):
                    with metrology_mod.trial_workspace(trial, root=root, trial_index=index) as ctx:
                        relative = ctx.workspace.relative_to(root).as_posix()
                        names.append(relative)
                        self.assertTrue(re.fullmatch(r"m-[0-9a-f]{8}-[A-Za-z0-9_]+", relative), relative)
                        self.assertTrue(relative.startswith("m-" + ctx.trial_nonce[:8] + "-"))
                        self.assertNotIn(str(seed), relative)
                        self.assertNotIn(trial.specimen.name.lower(), relative.lower())
                        self.assertNotIn(trial.specimen.family, relative)
                        for word in ("seed", "index", "trial", "variant", "replicate", "specimen"):
                            self.assertNotIn(word, relative.lower())
                        # The index and the seed live on the CONTEXT for the
                        # evidence row, never on the path.
                        self.assertEqual(ctx.trial_index, index)
            self.assertEqual(len(set(names)), len(names), "a workspace name repeated")

    def test_trial_workspace_contains_no_private_paths(self):
        """Every file of a trial workspace is public: no denied path
        component or basename (`answer_key`, `private`, any `.duckdb`), no
        private-surface marker, no private reference statement or attack
        mutation, and on a private-literal canary trial no canary token;
        and `_assert_no_private_material` FAILS CLOSED on each of those."""
        from elt_taskgen.review.tools.registry import DENIED_BASENAME_RE, DENIED_PATH_COMPONENTS

        canary = next(
            t for t in metrology_mod.select_canary_trials(_SEEDS[0])
            if t.specimen.canary_kind == "private_literal"
        )
        tampered = next(
            t for t in metrology_mod.select_trials(_SEEDS[0])
            if t.specimen.target_role is CouncilRole.POPULATION_ADVERSARY
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for trial in (canary, tampered):
                with metrology_mod.trial_workspace(trial, root=root) as ctx:
                    files = [p for p in ctx.workspace.rglob("*") if p.is_file()]
                    self.assertTrue(files)
                    private = [
                        " ".join(sql.lower().split())
                        for sql in trial.task.reference.sql_by_mart.values()
                    ] + [
                        " ".join(c.mutation.lower().split())
                        for c in trial.task.attack_cases
                        if not c.mutation.startswith("directive:")
                    ]
                    for path in ctx.workspace.rglob("*"):
                        rel = path.relative_to(ctx.workspace)
                        self.assertFalse(set(rel.parts) & DENIED_PATH_COMPONENTS, rel)
                        self.assertIsNone(DENIED_BASENAME_RE.search(path.name), rel)
                        if not path.is_file():
                            continue
                        text = path.read_text(encoding="utf-8")
                        low = " ".join(text.lower().split())
                        for marker in ("sql_by_mart", "attack_cases", "literal_rows", ".duckdb"):
                            self.assertNotIn(marker, low, rel)
                        for statement in private:
                            self.assertNotIn(statement, low, rel)
                        if trial is canary:
                            (token,) = trial.specimen.detection_terms
                            self.assertNotIn(token, low, rel)
            # Fail closed, each cause by name.
            task = tampered.task
            cases = {
                "private_file_in_trial_workspace": lambda ws: (ws / "scratch.duckdb").write_bytes(b""),
                "private_path_in_trial_workspace": lambda ws: (ws / "answer_key").mkdir(),
                "private_marker_in_trial_workspace": lambda ws: (ws / "notes.txt").write_text(
                    "see sql_by_mart for the answer", encoding="utf-8"
                ),
                "private_sql_in_trial_workspace": lambda ws: (ws / "notes.txt").write_text(
                    next(iter(task.reference.sql_by_mart.values())), encoding="utf-8"
                ),
                "binary_file_in_trial_workspace": lambda ws: (ws / "blob.bin").write_bytes(b"\xff\xfe\x00"),
            }
            for code, plant in cases.items():
                with self.subTest(code=code):
                    workspace = root / f"m-00000000-{code}"
                    workspace.mkdir()
                    plant(workspace)
                    with self.assertRaises(metrology_mod.TrialIsolationError) as caught:
                        metrology_mod._assert_no_private_material(workspace, task)
                    self.assertEqual(caught.exception.code, code)
                    shutil.rmtree(workspace)
            # ...and the harness refuses to run a trial whose projection
            # would carry private material.
            with mock.patch.object(
                metrology_mod, "_render_public_projection",
                lambda trial, public: (public.mkdir(parents=True), (public / "x.duckdb").write_bytes(b"")),
            ):
                with self.assertRaises(metrology_mod.TrialIsolationError):
                    with metrology_mod.trial_workspace(tampered, root=root):
                        pass  # pragma: no cover - never entered
            self.assertEqual(list(root.iterdir()), [])

    def test_scratch_notes_do_not_survive_a_trial(self):
        """A note a provider writes into its workspace in trial i is gone
        before trial i+1 begins, and every trial's workspace is new."""
        provider = _ScratchNotingProvider()
        report = metrology_mod.run_metrology(provider, seed=_SEEDS[0])
        self.assertTrue(report.admitted)
        self.assertEqual(provider.notes_seen, 0)
        self.assertEqual(len(provider.contexts), len(report.specimens) + len(report.canaries))
        self.assertEqual(
            len({ctx.trial_nonce for ctx in provider.contexts}), len(provider.contexts)
        )
        self.assertEqual(
            len({ctx.workspace for ctx in provider.contexts}), len(provider.contexts)
        )
        self.assertFalse(any(ctx.workspace.exists() for ctx in provider.contexts))
        # The run root itself is gone too (the loop owned it).
        for run_root in provider.roots_seen:
            self.assertFalse(run_root.exists(), run_root)
        # The attestation is a MEASUREMENT (finding p4-1-2): the three
        # booleans `admission_status` re-reads are derived from counters this
        # loop kept, and the counters are recorded beside them.
        trials = len(report.specimens) + len(report.canaries)
        self.assertEqual(
            report.isolation,
            {
                "per_trial_fresh_workspace": True,
                "teardown_verified": True,
                "cross_trial_cache": False,
                "trials_observed": trials,
                "distinct_trial_nonces": trials,
                "teardowns_asserted": trials,
                "trial_seam_observed": True,
                "cache_free_trials": 0,
                "cached_trials": 0,
                "unobservable_executor_trials": trials,
            },
        )

    def test_run_metrology_calls_begin_and_end_trial_around_every_trial(self):
        """`begin_trial(ctx)` before `run_council`, `end_trial()` after —
        also after a fault — and never for a provider without the hooks."""
        from elt_taskgen.review.session import ToolHarnessFault

        class _Hooked:
            def __init__(self, fail_at: int | None = None):
                self.events: list = []
                self.fail_at = fail_at
                self._oracle = OracleProvider()
                self.open = False

            def begin_trial(self, ctx):
                assert not self.open, "begin_trial while a trial is open"
                self.open = True
                self.events.append(("begin", ctx.trial_nonce, ctx.trial_index))

            def end_trial(self):
                self.open = False
                self.events.append(("end",))

            def complete(self, role, prompt):
                if self.fail_at is not None and len(self.events) >= self.fail_at:
                    raise ToolHarnessFault("compile_probe", cause_type="RuntimeError")
                self.events.append(("complete", role.value))
                return self._oracle.complete(role, prompt)

        provider = _Hooked()
        report = metrology_mod.run_metrology(provider, seed=_SEEDS[1])
        self.assertTrue(report.admitted)
        begins = [e for e in provider.events if e[0] == "begin"]
        ends = [e for e in provider.events if e[0] == "end"]
        self.assertEqual(len(begins), len(report.specimens) + len(report.canaries))
        self.assertEqual(len(ends), len(begins))
        self.assertEqual([e[2] for e in begins], list(range(len(begins))))
        # Nesting: begin, completes, end, begin, ...
        depth = 0
        for event in provider.events:
            if event[0] == "begin":
                depth += 1
            elif event[0] == "end":
                depth -= 1
            self.assertIn(depth, (0, 1))
        # A fault still closes the trial (end_trial in a finally) and
        # propagates unscored.
        faulting = _Hooked(fail_at=3)
        with self.assertRaises(ToolHarnessFault):
            metrology_mod.run_metrology(faulting, seed=_SEEDS[1])
        self.assertEqual(faulting.events[-1], ("end",))
        self.assertFalse(faulting.open)
        # Providers without trial hooks still run but cannot attest isolation;
        # cross_trial_cache stays true, so admission refuses them.
        class _Seamless:
            def __init__(self):
                self._oracle = OracleProvider()

            def complete(self, role, prompt):
                return self._oracle.complete(role, prompt)

        seamless = _Seamless()
        self.assertFalse(hasattr(seamless, "begin_trial"))
        blind = metrology_mod.run_metrology(seamless, seed=_SEEDS[1])
        self.assertTrue(blind.role_pass and all(blind.role_pass.values()))
        self.assertIs(blind.isolation["cross_trial_cache"], True)
        self.assertIs(blind.isolation["trial_seam_observed"], False)
        # The seam-bearing double is attested and admits.
        seamed = metrology_mod.run_metrology(OracleProvider(), seed=_SEEDS[1])
        self.assertTrue(seamed.admitted)
        self.assertIs(seamed.isolation["cross_trial_cache"], False)

    # -- position dependence ----------------------------------------------

    def test_trial_workspace_view_set_is_constant_across_trials(self):
        """finding p4-0-2: `public/views/` disclosed clean-vs-tampered and,
        when tampered, WHICH SEAT the defect was planted for.

        `_render_public_projection` wrote one view file per CONSULTED seat,
        and `Trial.roles` is all four critics on a clean specimen and the
        single target seat on a tampered or canary one — a perfect
        discriminator in the tool root a session runs under. A seat with any
        filesystem tool could read `ls public/views`, answer "a defect aimed
        at me" on every tampered trial and "clean" on every clean one, and
        score recall 1.0 with nitpick 0 without reading a word of prose. Every
        view is derivable from the public task, so the constant set costs
        nothing and withholds the position.
        """
        expected = sorted(f"{role.value}.txt" for role in CRITIC_ROLES)
        seen_kinds = set()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drawn = metrology_mod.select_trials(_SEEDS[0])
            trials = [
                next(t for t in drawn if t.specimen.kind == kind)
                for kind in ("clean", "tampered")
            ]
            trials += [t for t in drawn if t.specimen.target_role is CouncilRole.SHORTCUT_ATTACKER][:1]
            trials += list(metrology_mod.select_canary_trials(_SEEDS[0])[:3])
            for trial in trials:
                with metrology_mod.trial_workspace(trial, root=root) as ctx:
                    seen_kinds.add(trial.specimen.kind)
                    views = ctx.public_dir / "views"
                    self.assertEqual(
                        sorted(path.name for path in views.iterdir()), expected
                    )
                    for role in CRITIC_ROLES:
                        self.assertEqual(
                            (views / f"{role.value}.txt").read_text(encoding="utf-8"),
                            council_mod.render_view(role, trial.task),
                        )
        # The draw really did mix the kinds, so "constant" is a statement
        # about clean AND tampered AND canary trials.
        self.assertEqual(seen_kinds, {"clean", "tampered", "canary"})

    def test_isolation_attestation_is_measured_not_asserted(self):
        """finding p4-1-2: the per-trial isolation attestation was a
        hardcoded constant, so `admission_status`'s isolation check was
        circular — it could only ever read back what the harness wrote, and a
        provider with a cross-trial cache still admitted.

        The three booleans are now DERIVED from counters the loop kept, and
        the counters are recorded beside them.
        """

        class _CachedExecutor:
            cache = {"memo": "hit"}
            runs: list = []

        class _CachingProvider(TrialSeam):
            def __init__(self):
                self._oracle = OracleProvider()
                self.executor = _CachedExecutor()

            @property
            def trial_executor(self):
                return self.executor if self.trial_context is not None else None

            def complete(self, role, prompt):
                return self._oracle.complete(role, prompt)

        report = metrology_mod.run_metrology(
            _CachingProvider(), routing_fingerprint="fp-iso", seed=_SEEDS[1]
        )
        trials = len(report.specimens) + len(report.canaries)
        self.assertIs(report.isolation["cross_trial_cache"], True)
        self.assertEqual(report.isolation["cached_trials"], trials)
        self.assertEqual(report.isolation["cache_free_trials"], 0)
        self.assertIs(report.isolation["trial_seam_observed"], True)
        # The record built from it does NOT attest isolation, so it cannot
        # admit however perfect the seat scores were.
        self.assertTrue(report.admitted)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_admission_marker(workspace, report)
            status = metrology_mod.admission_status(
                workspace, routing_fingerprint="fp-iso"
            )
            self.assertFalse(status.ok)
            self.assertIn("per-trial isolation", status.reason)

        # The read side re-checks the OBSERVATIONS, not just the booleans: a
        # record whose counters do not support its claim is refused.
        clean = metrology_mod.run_metrology(
            OracleProvider(), routing_fingerprint="fp-iso", seed=_SEEDS[1]
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = _write_admission_marker(workspace, clean)
            self.assertTrue(
                metrology_mod.admission_status(
                    workspace, routing_fingerprint="fp-iso"
                ).ok
            )
            for key, value in (
                ("trial_seam_observed", False),
                ("distinct_trial_nonces", 1),
                ("teardowns_asserted", 0),
                ("cached_trials", 3),
            ):
                with self.subTest(key=key):
                    data = json.loads(record.read_text(encoding="utf-8"))
                    data["evidence"]["isolation"][key] = value
                    data["evidence_sha256"] = sha256_hex(
                        canonical_json(data["evidence"])
                    )
                    record.write_text(canonical_json(data), encoding="utf-8")
                    status = metrology_mod.admission_status(
                        workspace, routing_fingerprint="fp-iso"
                    )
                    self.assertFalse(status.ok)
                    self.assertIn("per-trial isolation", status.reason)
            record.unlink()
            _write_admission_marker(workspace, clean)

    def test_the_canary_pool_is_not_conditional_on_the_fixture_family(self):
        """finding p4-0-1: every hack-canary specimen was built from the DEMO
        fixture, so after the three-family diversification the blocking
        `canary_hits == 0` bar covered one family of three and
        P(canary | a clinic_visits or stock_ledger trial) was exactly ZERO.

        The family is trivially observable to a seat — the domain, the table
        names and the mart name are on the page — so the canary was
        CONDITIONALLY IDENTIFIABLE: a seat that read the private surfaces of
        the two new families emitted nothing the two-axis rule could catch.
        """
        families = metrology_mod.pool_families()
        pool = metrology_mod.canary_pool()
        self.assertEqual(sorted({s.family for s in pool}), sorted(families))
        for family in families:
            for kind in metrology_mod.CANARY_KINDS:
                for role in CRITIC_ROLES:
                    matches = [
                        s
                        for s in pool
                        if s.family == family
                        and s.canary_kind == kind
                        and s.target_role is role
                    ]
                    self.assertGreaterEqual(
                        len(matches),
                        1 + metrology_mod.CANARY_POOL_HOLDOUT,
                        (family, kind, role.value),
                    )
        # The DRAW is stratified over the families exactly as the scored draw
        # is: every seat's canary trials spread over them, on every seed, so
        # no family is a "canaries never happen here" region of the schedule.
        for seed in (0, 1, 7, 20260904):
            trials = metrology_mod.select_canary_trials(seed)
            self.assertEqual(len(trials), len(CRITIC_ROLES) * metrology_mod.CANARY_PER_ROLE)
            for role in CRITIC_ROLES:
                seat = [t for t in trials if t.specimen.target_role is role]
                self.assertEqual(len(seat), metrology_mod.CANARY_PER_ROLE)
                self.assertEqual(
                    sorted({t.specimen.family for t in seat}), sorted(families)
                )
                self.assertEqual(
                    sorted(t.specimen.canary_kind for t in seat),
                    sorted(metrology_mod.CANARY_KINDS),
                )
            # The seed still reproduces the whole schedule.
            self.assertEqual(metrology_mod.select_canary_trials(seed), trials)
        # The private-only literal is planted per family, and no two families
        # share a token.
        tokens = {
            specimen.detection_terms[0]
            for specimen in pool
            if specimen.canary_kind != "impossible"
        }
        self.assertEqual(
            len(tokens),
            len(
                [s for s in pool if s.canary_kind != "impossible"]
            ),
        )

    def test_position_dependence_is_reported_not_gated(self):
        """Per seat the first-half versus second-half hit rate and the
        logistic slope of hit on dispatch position are REPORTED on the
        metrics and recomputable from the rows (`position_dependence`), and
        NOTHING gates on them: no threshold key, never a block reason, and
        a record whose recorded stats are extreme still admits."""
        report = metrology_mod.run_metrology(OracleProvider(), seed=_SEEDS[0])
        recomputed = metrology_mod.position_dependence(report)
        self.assertEqual(set(recomputed), set(metrology_mod.CRITIC_ROLE_NAMES))
        for role in CRITIC_ROLES:
            stats = report.per_role[role.value].position_dependence
            self.assertIsInstance(stats, metrology_mod.PositionStats)
            self.assertEqual(stats, recomputed[role.value])
            self.assertEqual(stats.trials, _TRIALS_PER_SEAT)
            self.assertEqual(stats.first_half_trials + stats.second_half_trials, _TRIALS_PER_SEAT)
            self.assertEqual(stats.first_half_rate, 1.0)
            self.assertEqual(stats.second_half_rate, 1.0)
            self.assertEqual(stats.logistic_slope, 0.0)
        # Every trial row carries its dispatch position, distinct and dense.
        indices = sorted(
            [s.trial_index for s in report.specimens] + [c.trial_index for c in report.canaries]
        )
        self.assertEqual(indices, list(range(len(indices))))
        # A seat that reads only the first half of the run: visible in the
        # stats, blocked on RECALL — never on position.
        total_calls = len(report.specimens) + len(report.canaries) + (
            len(CRITIC_ROLES) - 1
        ) * sum(1 for s in report.specimens if s.kind == "clean")
        dependent = metrology_mod.run_metrology(
            _PositionDependentProvider(cutoff=total_calls // 2), seed=_SEEDS[0]
        )
        self.assertFalse(dependent.admitted)
        for role in CRITIC_ROLES:
            metrics = dependent.per_role[role.value]
            stats = metrics.position_dependence
            self.assertGreater(stats.first_half_rate, stats.second_half_rate, role.value)
            self.assertLess(stats.logistic_slope, 0.0, role.value)
            self.assertLess(stats.rate_difference, 0.0, role.value)
            self.assertIn("recall", metrics.block_reasons)
            self.assertFalse(any("position" in reason for reason in metrics.block_reasons))
        thresholds = metrology_mod.MetrologyThresholds()
        self.assertFalse(any("position" in key for key in thresholds.model_dump()))
        # The record carries the stats and the gate never reads them.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = _write_admission_marker(workspace, report)
            data = json.loads(record.read_text(encoding="utf-8"))
            seat = data["evidence"]["per_role"]["ambiguity_critic"]
            self.assertEqual(seat["position_dependence"]["first_half_rate"], 1.0)
            seat["position_dependence"] = {
                **seat["position_dependence"],
                "first_half_rate": 1.0, "second_half_rate": 0.0,
                "rate_difference": -1.0, "logistic_slope": -20.0,
            }
            data["evidence_sha256"] = sha256_hex(canonical_json(data["evidence"]))
            record.write_text(canonical_json(data), encoding="utf-8")
            self.assertTrue(metrology_mod.live_admission_ok(workspace))
        # Pure function of the rows: an empty report has empty stats.
        self.assertEqual(metrology_mod._position_stats([], 10), metrology_mod.PositionStats())
        self.assertEqual(
            metrology_mod._position_stats([(0, True), (9, True), (4, False), (7, False)], 10).first_half_hits,
            1,
        )

    # -- families ---------------------------------------------------------

    def test_pool_is_stratified_by_family_with_holdout_per_family(self):
        """The pool spans at least three families, every family builds every
        specimen kind, every draw's clean and per-seat tampered specimens
        span at least two families, the holdout is honoured per family, and
        the pool digest covers every family's canary GUID."""
        families = metrology_mod.pool_families()
        self.assertGreaterEqual(len(families), 3)
        self.assertIn("demo", families)
        self.assertEqual(families, tuple(sorted(families)))
        pool = metrology_mod.specimen_pool()
        self.assertEqual({s.family for s in pool}, set(families))
        for family in families:
            with self.subTest(family=family):
                mine = [s for s in pool if s.family == family]
                self.assertGreaterEqual(
                    len([s for s in mine if s.kind == "clean"]),
                    metrology_mod._largest_family_draw(metrology_mod.CLEAN_PER_RUN, len(families))
                    + metrology_mod.POOL_HOLDOUT,
                )
                for role in CRITIC_ROLES:
                    self.assertGreaterEqual(
                        len([s for s in mine if s.target_role is role]),
                        metrology_mod._largest_family_draw(metrology_mod.TAMPERED_PER_ROLE, len(families))
                        + metrology_mod.POOL_HOLDOUT,
                        role.value,
                    )
        for seed in _SEEDS:
            with self.subTest(seed=seed):
                mix = metrology_mod.select_specimens(seed)
                self.assertGreaterEqual(len({s.family for s in mix if s.kind == "clean"}), 2)
                for role in CRITIC_ROLES:
                    drawn = [s for s in mix if s.target_role is role]
                    self.assertEqual(len(drawn), metrology_mod.TAMPERED_PER_ROLE)
                    self.assertGreaterEqual(len({s.family for s in drawn}), 2, role.value)
                    for family in families:
                        left = [
                            s for s in pool
                            if s.target_role is role and s.family == family and s not in drawn
                        ]
                        self.assertGreaterEqual(len(left), metrology_mod.POOL_HOLDOUT, (role.value, family))
                # The whole-run shuffle still mixes families and classes.
                self.assertGreater(len({s.family for s in mix[:8]}), 1)
        # Reports name their families and tasks.
        report = metrology_mod.run_metrology(OracleProvider(), seed=_SEEDS[0])
        self.assertEqual(report.pool_families, families)
        self.assertGreaterEqual(len(report.task_ids), 3)
        self.assertEqual({s.family for s in report.specimens}, set(families))
        self.assertEqual(report.pool_size, len(pool))
        # The pool digest folds every family's canary GUID, as data.
        before = metrology_mod.pool_sha256()
        real = metrology_mod.fixture_families()
        changed = dict(real)
        family = real["demo"]
        changed["demo"] = type(family)(
            name=family.name, task=family.task, anchors=family.anchors,
            specimen_builders=family.specimen_builders, canary_guid="another-family-guid",
        )
        with mock.patch.object(metrology_mod, "fixture_families", lambda: changed):
            self.assertNotEqual(metrology_mod.pool_sha256(), before)
        # Dropping a family moves it too, and the draw refuses a pool whose
        # families do not include the demo one.
        with mock.patch.object(
            metrology_mod, "fixture_families", lambda: {k: v for k, v in real.items() if k != "demo"}
        ):
            self.assertNotEqual(metrology_mod.pool_sha256(), before)
        self.assertEqual(metrology_mod.pool_sha256(), before)
        with mock.patch.object(metrology_mod, "FIXTURE_FAMILIES", {}):
            with self.assertRaises(ValueError):
                metrology_mod.fixture_families()

    def test_a_single_family_pool_draws_the_harness_5_sample(self):
        """One stratum reduces `_stratified_sample` to `rng.sample`: the
        demo-only pool draws exactly the sample the harness-5 code drew for
        the same seed (the seeded draw is unchanged for a one-family pool)."""
        real = metrology_mod.fixture_families()
        with mock.patch.object(metrology_mod, "fixture_families", lambda: {"demo": real["demo"]}):
            pool = metrology_mod.specimen_pool()
            self.assertEqual({s.family for s in pool}, {"demo"})
            self.assertEqual(len(pool), 35)
            for seed in _SEEDS:
                rng = random.Random(seed)
                expected = list(rng.sample([s for s in pool if s.kind == "clean"], metrology_mod.CLEAN_PER_RUN))
                for role in CRITIC_ROLES:
                    expected += rng.sample(
                        [s for s in pool if s.target_role is role], metrology_mod.TAMPERED_PER_ROLE
                    )
                rng.shuffle(expected)
                self.assertEqual(
                    [s.name for s in metrology_mod.select_specimens(seed)],
                    [s.name for s in expected],
                    seed,
                )

    # -- statistics -------------------------------------------------------

    def test_pass_k_icc_and_n_eff_are_computed_from_the_per_specimen_counts(self):
        """`pass^k` is tau-bench's C(c,k)/C(n,k) averaged over specimens, the
        ICC is the one-way ANOVA estimate over the replicates, `n_eff` is
        25 / (1 + (K-1) rho); all three ride on the metrics and the record,
        reported and never gated."""
        perfect = {name: (5, 5) for name in "abcde"}
        one_miss = {**perfect, "e": (4, 5)}
        self.assertEqual(metrology_mod.pass_at_k(perfect, 2), 1.0)
        self.assertEqual(metrology_mod.pass_at_k(perfect, 3), 1.0)
        # 5/5/5/5/4: C(4,2)/C(5,2) = 6/10 on the fifth specimen.
        self.assertAlmostEqual(metrology_mod.pass_at_k(one_miss, 2), (4 + 0.6) / 5)
        # 5/5/5/3/3 clears 21/25 and pass^2 = (3 + 2 * 3/10) / 5.
        self.assertAlmostEqual(
            metrology_mod.pass_at_k({"a": (5, 5), "b": (5, 5), "c": (5, 5), "d": (3, 5), "e": (3, 5)}, 2),
            (3 + 2 * 0.3) / 5,
        )
        self.assertEqual(metrology_mod.pass_at_k({"a": (1, 1)}, 2), 0.0)
        self.assertEqual(metrology_mod.pass_at_k({}, 2), 0.0)
        with self.assertRaises(ValueError):
            metrology_mod.pass_at_k(perfect, 0)
        # ICC: no within-specimen variance and no between -> 0; every hit
        # decided by the specimen -> 1; degenerate inputs -> 0.
        self.assertEqual(metrology_mod.intra_specimen_correlation(perfect), 0.0)
        self.assertAlmostEqual(
            metrology_mod.intra_specimen_correlation({"a": (5, 5), "b": (0, 5), "c": (5, 5), "d": (0, 5)}), 1.0
        )
        mixed = metrology_mod.intra_specimen_correlation({"a": (3, 5), "b": (2, 5), "c": (3, 5), "d": (2, 5), "e": (3, 5)})
        self.assertTrue(-1.0 <= mixed <= 1.0)
        self.assertEqual(metrology_mod.intra_specimen_correlation({"a": (3, 5)}), 0.0)
        self.assertEqual(metrology_mod.intra_specimen_correlation({"a": (1, 1), "b": (0, 1)}), 0.0)
        # n_eff: the roadmap's worked example (ICC 0.11 -> about 17).
        self.assertAlmostEqual(metrology_mod.effective_sample_size(25, 5, 0.11), 25 / 1.44)
        self.assertEqual(metrology_mod.effective_sample_size(25, 5, 0.0), 25.0)
        self.assertEqual(metrology_mod.effective_sample_size(25, 5, -0.3), 25.0)
        # The bootstrap interval is seeded, bracketing, and inside [0, 1].
        lb, ub = metrology_mod.pass_at_k_bootstrap(one_miss, 2, seed=7)
        self.assertEqual((lb, ub), metrology_mod.pass_at_k_bootstrap(one_miss, 2, seed=7))
        self.assertLessEqual(lb, metrology_mod.pass_at_k(one_miss, 2))
        self.assertLessEqual(metrology_mod.pass_at_k(one_miss, 2), ub)
        self.assertTrue(0.0 <= lb <= ub <= 1.0)
        self.assertEqual(metrology_mod.pass_at_k_bootstrap({}, 2, seed=7), (0.0, 1.0))
        # On the metrics: a perfect seat, then a seat with margin.
        report = metrology_mod.run_metrology(OracleProvider(), seed=_SEEDS[0])
        for role in CRITIC_ROLES:
            metrics = report.per_role[role.value]
            self.assertEqual(metrics.pass_k, {"2": 1.0, "3": 1.0})
            self.assertEqual(metrics.pass_k_interval, {"2": (1.0, 1.0), "3": (1.0, 1.0)})
            self.assertEqual(metrics.icc, 0.0)
            self.assertEqual(metrics.n_eff, float(_TRIALS_PER_SEAT))
            self.assertEqual(
                metrics.per_specimen_hits,
                {name: (5, 5) for name in metrics.per_specimen},
            )
        noisy = metrology_mod.run_metrology(StochasticSeatProvider(0.6, random.Random(7)), seed=99)
        for role in CRITIC_ROLES:
            metrics = noisy.per_role[role.value]
            self.assertLessEqual(metrics.pass_k["3"], metrics.pass_k["2"])
            self.assertTrue(-1.0 <= metrics.icc <= 1.0)
            self.assertLessEqual(metrics.n_eff, float(_TRIALS_PER_SEAT))
            self.assertEqual(
                sum(h for h, _ in metrics.per_specimen_hits.values()), metrics.detected_count
            )
            self.assertNotIn("icc", metrics.block_reasons)
        # Advisory and clustering keys never gate: the thresholds carry none.
        keys = set(metrology_mod.MetrologyThresholds().model_dump())
        for name in ("pass_k", "icc", "n_eff", "position"):
            self.assertFalse(any(name in key for key in keys), name)

    # -- fingerprint and supersession -------------------------------------

    def test_observable_state_digest_hashes_the_empty_template(self):
        """No critic policy carries a query tool, so the observable-state
        digest is the sha256 of the EMPTY template under the declared
        (empty) query-tool sets; it is hashed into the fingerprint and
        recorded on the report and the record; and a critic policy that
        DID carry a query tool refuses (OQ-13) rather than hashing air."""
        from elt_taskgen.review.session import SessionPolicy

        digest = metrology_mod.observable_state_digest()
        expected = sha256_hex(canonical_json({
            "version": metrology_mod.OBSERVABLE_STATE_VERSION,
            "template": {"tables": [], "rows": {}},
            "query_tools": {role: [] for role in metrology_mod.CRITIC_ROLE_NAMES},
        }))
        self.assertEqual(digest, expected)
        self.assertEqual(digest, metrology_mod.observable_state_digest(agents_config=None))
        before = metrology_mod.council_routing_fingerprint(self.routing)
        with mock.patch.object(metrology_mod, "observable_state_digest", lambda **kw: "0" * 64):
            self.assertNotEqual(metrology_mod.council_routing_fingerprint(self.routing), before)
        self.assertEqual(metrology_mod.council_routing_fingerprint(self.routing), before)
        report = metrology_mod.run_metrology(OracleProvider(), seed=_SEEDS[0])
        self.assertEqual(report.observable_state_sha256, digest)
        # A record earned under another observable state is STALE, by name.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_admission_marker(workspace, report)
            self.assertTrue(metrology_mod.live_admission_ok(workspace))
            with mock.patch.object(metrology_mod, "observable_state_digest", lambda **kw: "1" * 64):
                status = metrology_mod.admission_status(workspace)
            self.assertFalse(status.ok)
            self.assertIn("STALE", status.reason)
            self.assertIn("observable state", status.reason)
        # A query tool on a critic policy refuses (fail closed, OQ-13).
        real = providers_mod.session_policy_for

        def with_query_tool(role, **kw):
            policy = real(role, **kw)
            if role != "feasibility_reviewer":
                return policy
            return SessionPolicy(
                role=policy.role,
                tools=policy.tools + (SimpleNamespace(name="dev_query", input_schema={}),),
                submit_tool=policy.submit_tool, limits=policy.limits, mode=policy.mode,
                wire_tools=policy.wire_tools,
            )

        with mock.patch.object(providers_mod, "session_policy_for", with_query_tool):
            with self.assertRaisesRegex(ValueError, "query tool"):
                metrology_mod.observable_state_digest()

    def test_validators_binaries_carry_duckdb_and_sqlglot_for_pop_and_shc(self):
        """SoT T1.1 / roadmap Table 8: the compile validators of the
        population adversary and the shortcut attacker import
        `verification/attacks.py`, which imports duckdb and sqlglot, so
        `validators.binaries` pins both for either seat once it is enabled
        (and both seats now ship enabled by default)."""
        from elt_taskgen.verification import attacks as attacks_mod

        source = Path(attacks_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("import duckdb", source)
        self.assertIn("import sqlglot", source)
        for validator in ("compile_proposal", "compile_probe"):
            self.assertEqual(metrology_mod.HARNESS_VALIDATOR_BINARIES[validator], ("duckdb", "sqlglot"))
            self.assertIn(attacks_mod.__name__, metrology_mod.HARNESS_VALIDATOR_MODULES[validator])
        shipped = metrology_mod.validator_digests()
        self.assertEqual(shipped["binaries"], metrology_mod.toolchain_pins())
        self.assertEqual(set(shipped["binaries"]), {"duckdb", "sqlglot"})
        self.assertIn(attacks_mod.__name__, shipped["code"])

        disabled = json.loads(json.dumps(providers_mod._agents_doc()))
        for role in ("population_adversary", "shortcut_attacker"):
            disabled["roles"][role]["session"]["enabled"] = False
        with mock.patch.object(providers_mod, "_agents_doc", lambda: disabled):
            providers_mod.clear_behavior_caches()
            self.assertEqual(
                metrology_mod.validator_digests(),
                {"code": {}, "binaries": {}},
            )
        providers_mod.clear_behavior_caches()

        for role in ("population_adversary", "shortcut_attacker"):
            with self.subTest(enabled=role):
                doc = json.loads(json.dumps(disabled))
                doc["roles"][role]["session"]["enabled"] = True
                with mock.patch.object(providers_mod, "_agents_doc", lambda: doc):
                    providers_mod.clear_behavior_caches()
                    digests = metrology_mod.validator_digests()
                    self.assertEqual(digests["binaries"], metrology_mod.toolchain_pins())
                    self.assertEqual(set(digests["binaries"]), {"duckdb", "sqlglot"})
                    self.assertIn(attacks_mod.__name__, digests["code"])
                providers_mod.clear_behavior_caches()

    def test_harness_6_supersedes_every_harness_5_record(self):
        """Every schema-4 record earned under harness "5" — as written, and
        in the harness-5 evidence SHAPE (no families, no isolation, no
        observable state) — reads SUPERSEDED, naming both versions, before
        any evidence is compared; a harness-6 record that cannot name its
        families or attest its isolation is refused as invalid evidence;
        and a harness-6 tombstone names the harness it withdrew."""
        fingerprint = metrology_mod.council_routing_fingerprint(self.routing)
        report = metrology_mod.run_metrology(
            OracleProvider(), routing_fingerprint=fingerprint, seed=_SEEDS[0]
        )
        self.assertEqual(report.harness_version, "6")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = _write_admission_marker(workspace, report)
            self.assertTrue(metrology_mod.live_admission_ok(workspace, routing_fingerprint=fingerprint))
            pristine = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(pristine["harness_version"], "6")
            self.assertEqual(pristine["evidence"]["harness_version"], "6")

            def resigned(mutate):
                data = json.loads(canonical_json(pristine))
                mutate(data)
                data["evidence_sha256"] = sha256_hex(canonical_json(data["evidence"]))
                record.write_text(canonical_json(data), encoding="utf-8")
                return metrology_mod.admission_status(workspace, routing_fingerprint=fingerprint)

            def harness_five(data):
                data["harness_version"] = "5"
                data["evidence"]["harness_version"] = "5"

            def harness_five_shape(data):
                harness_five(data)
                for key in ("pool_families", "isolation", "observable_state_sha256", "task_ids"):
                    data["evidence"].pop(key, None)
                for seat in data["evidence"]["per_role"].values():
                    for key in ("pass_k", "pass_k_interval", "icc", "n_eff", "position_dependence",
                                "per_specimen_hits"):
                        seat.pop(key, None)

            def harness_five_everywhere(data):
                harness_five_shape(data)
                data["evidence"]["fingerprint_components"]["harness_version"] = "5"

            for label, mutate in (
                ("as written", harness_five),
                ("harness-5 evidence shape", harness_five_shape),
                ("harness-5 components too", harness_five_everywhere),
            ):
                with self.subTest(record=label):
                    status = resigned(mutate)
                    self.assertFalse(status.ok)
                    self.assertIn("SUPERSEDED", status.reason)
                    self.assertIn("'5'", status.reason)
                    self.assertIn("'6'", status.reason)
                    self.assertNotIn("edited or truncated", status.reason)
                    self.assertNotIn("STALE", status.reason)
            # Harness-6 records that cannot attest the protocol: invalid.
            for label, mutate, cause in (
                ("no families", lambda d: d["evidence"].pop("pool_families"), "families"),
                ("empty families", lambda d: d["evidence"].__setitem__("pool_families", []), "families"),
                ("no isolation", lambda d: d["evidence"].pop("isolation"), "isolation"),
                ("cross-trial cache", lambda d: d["evidence"]["isolation"].__setitem__("cross_trial_cache", True), "isolation"),
                ("no fresh workspace", lambda d: d["evidence"]["isolation"].__setitem__("per_trial_fresh_workspace", False), "isolation"),
                ("teardown unverified", lambda d: d["evidence"]["isolation"].__setitem__("teardown_verified", False), "isolation"),
            ):
                with self.subTest(record=label):
                    status = resigned(mutate)
                    self.assertFalse(status.ok)
                    self.assertIn("[evidence_invalid]", status.reason)
                    self.assertIn(cause, status.reason)
            # A seat's own freshness counters refuse on their own.
            for label, mutate, cause in (
                ("seat stale result", lambda d: d["evidence"]["per_role"]["shortcut_attacker"].__setitem__("stale_tool_result_count", 1), "stale tool result"),
                ("seat replayed call", lambda d: d["evidence"]["per_role"]["shortcut_attacker"].__setitem__("live_model_call_count", 30), "live of"),
            ):
                with self.subTest(record=label):
                    status = resigned(mutate)
                    self.assertFalse(status.ok)
                    self.assertIn(cause, status.reason)
            # Restored, it admits again; revoked, the tombstone names "6".
            record.write_text(canonical_json(pristine), encoding="utf-8")
            self.assertTrue(metrology_mod.live_admission_ok(workspace, routing_fingerprint=fingerprint))
            metrology_mod.revoke_admission(workspace, reason_code="blocked")
            tombstone = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(tombstone["revoked_harness_version"], "6")
        # The revocation reason code the CLI names the tombstone after.
        self.assertEqual(
            metrology_mod.revocation_reason_code(
                metrology_mod.run_metrology(BoilerplateProvider(), seed=_SEEDS[0])
            ),
            "blocked",
        )
        self.assertEqual(
            metrology_mod.revocation_reason_code(
                metrology_mod.run_metrology(PrivateSniffingProvider(), seed=_SEEDS[0])
            ),
            "canary_hit",
        )
        with self.assertRaises(ValueError):
            metrology_mod.revocation_reason_code(report)


if __name__ == "__main__":
    unittest.main()

"""Validate executable critic proposals inside bounded sessions.

Population proposals and shortcut probes compile against the same closed grammars used
after review. Red diagnostics may trigger one bounded correction; unresolved proposals
are voided after submission. Validator results contain codes, public identifiers, and
booleans only.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from elt_taskgen.models import (
    CouncilRole,
    Finding,
    FindingScreenStatus,
    PopulationName,
    ProposedAttackCase,
    Severity,
    TaskIR,
)
from elt_taskgen.review.session import (
    MEASURED_MATCH_BIT_VALIDATOR,
    InProcessValidatorWorker,
    SessionLimits,
    SessionPolicy,
    ToolHarnessFault,
)
from elt_taskgen.review.tools.projection import (
    Diagnostic,
    DiagnosticSource,
    PublicIdentifierSet,
    project_compile,
)
from elt_taskgen.review.tools.registry import ToolContext, ToolCost, ToolRegistry

__all__ = [
    "ADVERSARY_ROLE",
    "ATTACKER_ROLE",
    "CLAIM_FIDELITY_FLAG",
    "COMPILE_PROBE_TOOL",
    "COMPILE_PROPOSAL_TOOL",
    "CRITIC_VALIDATOR_NAMES",
    "CRITIC_VALIDATOR_ROLES",
    "CRITIC_VALIDATOR_VERSION",
    "CRITIC_VALIDATORS",
    "CriticSession",
    "CriticToolContext",
    "EXACT_MATCH_FLAG",
    "EXECUTABLE_PROBE_FLAG",
    "GRAMMAR_CODES",
    "IS_DIRECTIVE_FLAG",
    "MEASURED_MATCH_BIT_FLAG",
    "MEASURED_MATCH_BIT_TOOL",
    "POST_SESSION_PROJECTION_NAMES",
    "PROBE_PRESENT_FLAG",
    "PROPOSAL_PRESENT_FLAG",
    "SAME_MUTANT_FLAG",
    "SCREEN_LEXICON",
    "compile_probe",
    "compile_proposal",
    "critic_limits",
    "critic_policy",
    "critic_registry",
    "critic_validator",
    "critic_validator_worker",
    "declared_validators",
    "fold_compile_probe",
    "fold_compile_probe_payload",
    "fold_compile_proposal",
    "measured_match_bit",
    "project_proposal_matrix",
    "UNCOMPILABLE_AFTER_CORRECTIONS",
    "void_uncompilable_proposals",
]

ADVERSARY_ROLE = CouncilRole.POPULATION_ADVERSARY.value
ATTACKER_ROLE = CouncilRole.SHORTCUT_ATTACKER.value
COMPILE_PROPOSAL_TOOL = "compile_proposal"
COMPILE_PROBE_TOOL = "compile_probe"
MEASURED_MATCH_BIT_TOOL = MEASURED_MATCH_BIT_VALIDATOR
#: The `roles.population_adversary.session` key that declares the 1-bit
#: validator (SoT T1.1 flag F; A24). Default false.
MEASURED_MATCH_BIT_FLAG = "measured_match_bit"
#: The critic seats whose `session:` block declares harness validators.
CRITIC_VALIDATOR_ROLES: tuple[str, ...] = (ADVERSARY_ROLE, ATTACKER_ROLE)
#: Folded into `policy_sha256` through each tool's `version` (SoT T5).
CRITIC_VALIDATOR_VERSION = "4"

#: SoT T3 `grammar_code`, carried as ONE boolean flag each: a `Diagnostic`
#: has no free-text field and the COMPILE source's code vocabulary is
#: exactly {compiles, uncompilable}.
GRAMMAR_CODES: tuple[str, ...] = (
    "proposal_missing",
    "param_unknown",
    "param_conflict",
    "variant_invalid",
    "kind_operation_mismatch",
    "severity_incompatible",
    "uncompilable",
    "claim_missing_identifier",
)
#: The in-payload duplicate bit (S2 §2.4 c): an EARLIER proposal of the same
#: payload compiles to the same mutant. Never a duplicate-of-catalogue bit,
#: and named without the screen's lexicon (`SCREEN_LEXICON`).
SAME_MUTANT_FLAG = "same_mutant_as_earlier_proposal"
PROPOSAL_PRESENT_FLAG = "proposal_present"
PROBE_PRESENT_FLAG = "probe_present"
EVERY_PROBE_COMPILES_FLAG = "every_probe_compiles"
CLAIM_FIDELITY_FLAG = "claim_fidelity_ok"
IS_DIRECTIVE_FLAG = "is_directive"
#: The review stage's diligence predicate (`council.is_executable_probe`)
#: over the whole attacker payload: at least one non-INFO finding whose
#: `suggested_attack` is matched by a green `proposed_case` of the same kind
#: and whose probe compiles.  Carried on every `compile_probe` fold; red
#: when false (review findings 1-2 and 1-3).
EXECUTABLE_PROBE_FLAG = "executable_probe"
EXACT_MATCH_FLAG = "exact_match"

#: The post-session screen's verdict lexicon (`FindingScreenStatus` values
#: and names), which no in-session projection may echo: the screen is
#: pure and post-session, and a seat that learned its verdicts in-session
#: could learn to write around them (`test_a_rephrased_retraction_is_missed_
#: by_design` records that the lexicon is not semantic).
SCREEN_LEXICON: tuple[str, ...] = tuple(
    sorted({status.value for status in FindingScreenStatus} | {status.name for status in FindingScreenStatus})
)

#: The verbs the constraint addendum A24 does NOT build as tools: the
#: in-session matrix (S3 `proposal_matrix`, S6 `predict_check`) is superseded
#: by the post-session `project_proposal_matrix` record. No manifest of any
#: role may carry a tool by these names (`test_proposal_matrix_is_post_
#: session_and_never_a_tool_in_any_manifest`).
POST_SESSION_PROJECTION_NAMES: tuple[str, ...] = (
    "project_proposal_matrix",
    "proposal_matrix",
    "predict_check",
    "mutant_applicable",
)

_ADV: frozenset[str] = frozenset({ADVERSARY_ROLE})
_SHC: frozenset[str] = frozenset({ATTACKER_ROLE})

#: What the HARNESS hands a validator on every submit: the `report_findings`
#: payload as submitted (the runner passes the submit tool's arguments).
_PAYLOAD_ARGS: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {"findings": {"type": ["array", "string"]}},
        "required": ["findings"],
        "additionalProperties": False,
    }
)
#: The per-finding interface of SoT T3 / roadmap §6: one finding of the
#: payload the harness holds, by its position.
_FINDING_INDEX_ARGS: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {"finding_index": {"type": "integer", "minimum": 0}},
        "required": ["finding_index"],
        "additionalProperties": False,
    }
)

#: The identifier-bearing keys of a compiled claim (`validate_proposal_claim_
#: fidelity`'s `requested`): what the finding text must name.
_CLAIM_IDENTIFIER_KEYS: tuple[str, ...] = (
    "source_mart",
    "target_mart",
    "backend",
    "dedup_table",
    "population",
    "argument",
    "variant",
)


# ---------------------------------------------------------------------------
# The harness-side session state
# ---------------------------------------------------------------------------

class CriticSession:
    """One critic session's HARNESS-side state: the task, the role, the
    findings parsed from the CURRENT submitted payload (the validators read
    them by index), every fold recorded in order, and — for the flagged
    match bit only — the gold bundle and the workspace whose population rows
    the promoter reads (D3 handles, never serialized toward the model)."""

    def __init__(
        self,
        *,
        task: TaskIR,
        role: str | CouncilRole = ADVERSARY_ROLE,
        gold: Any = None,
        workspace: Path | str | None = None,
    ) -> None:
        role_name = str(getattr(role, "value", role))
        if role_name not in CRITIC_VALIDATOR_ROLES:
            raise ValueError(
                f"critic validators exist for {list(CRITIC_VALIDATOR_ROLES)}, not {role_name!r}"
            )
        self.task = task
        self.role = role_name
        self.task_id = str(task.task_id)
        self.public = PublicIdentifierSet(task)
        self.id_suffix = task.content_hash()[:8]
        #: Gold-bearing handles for `measured_match_bit` (D3). Never a path
        #: the model supplied, never serialized.
        self.gold = gold
        self.workspace = None if workspace is None else Path(workspace)
        self.findings: tuple[Finding, ...] = ()
        self.payload: dict | None = None
        self.payload_count = 0
        self.checks: list[Diagnostic] = []
        #: The per-finding `compile_proposal` diagnostics of the LAST
        #: installed payload, index-aligned with `findings` (what the
        #: post-session `void_uncompilable_proposals` reads; never sent).
        self.finding_checks: tuple[Diagnostic, ...] = ()
        self.match_bit_calls = 0
        self.tool_calls = 0
        #: SoT T8 counter of the seat's evidence row: how many executable
        #: claims of the ACCEPTED payload were still red after the bounded
        #: compile correction and were therefore VOIDed post-session
        #: (`void_uncompilable_proposals`).  Never a run abort.
        self.compile_correction_exhausted = 0

    # -- the submitted payload -------------------------------------------------

    def install_payload(self, payload: Mapping[str, Any]) -> tuple[Finding, ...]:
        """Parse the submitted `report_findings` payload into `Finding`s with
        the council's own parser (`_parse_findings`, untouched), after the
        schema check `complete()` applies. The runner validated the payload
        already; a payload that fails here is a harness fault."""
        from elt_taskgen.review.council import ProviderProtocolError, _parse_findings
        from elt_taskgen.review.providers import normalized_text_for, validate_payload_for

        data = json.loads(json.dumps(dict(payload)))
        problem = validate_payload_for(self.role, data)
        if problem is not None:
            raise ToolHarnessFault("critic_session", code="payload_not_schema_valid")
        text = normalized_text_for(self.role, data)
        try:
            findings = _parse_findings(CouncilRole(self.role), text, self.id_suffix)
        except ProviderProtocolError as exc:
            raise ToolHarnessFault.from_exception(
                "critic_session", exc, code="payload_not_parseable"
            ) from exc
        self.findings = tuple(findings)
        self.payload = dict(data)
        self.payload_count += 1
        return self.findings

    def finding_at(self, index: Any, tool: str) -> Finding:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ToolHarnessFault(tool, code="finding_index_invalid")
        if index >= len(self.findings):
            raise ToolHarnessFault(tool, code="finding_index_out_of_range")
        return self.findings[index]

    # -- what the harness reads ------------------------------------------------

    @property
    def check_count(self) -> int:
        return len(self.checks)

    @property
    def last_check_green(self) -> bool:
        return bool(self.checks) and bool(self.checks[-1].ok)

    def current_draft(self) -> dict | None:
        """The last submitted payload as the auto-submittable draft of a
        limit stop (the runner records it on a validator-green run)."""
        return None if self.payload is None else dict(self.payload)

    def context(self, root: Path) -> "CriticToolContext":
        return CriticToolContext(
            root=root, task_id=self.task_id, role=self.role, task=self.task, session=self
        )


@dataclass(frozen=True)
class CriticToolContext(ToolContext):
    """A `ToolContext` for a critic seat (a scratch root: the compile
    validators touch no file; the match bit copies population rows under
    it) carrying the session state (non-identity: never compared, never
    serialized)."""

    session: Any = field(default=None, compare=False, repr=False)


def _critic_session_of(ctx: Any, tool: str) -> CriticSession:
    session = getattr(ctx, "session", None)
    if not isinstance(session, CriticSession):
        raise ToolHarnessFault(tool, code="no_critic_session")
    return session


def _finding_of(ctx: Any, args: Mapping[str, Any], tool: str) -> tuple[CriticSession, int, Finding]:
    session = _critic_session_of(ctx, tool)
    index = dict(args).get("finding_index")
    finding = session.finding_at(index, tool)
    return session, int(index), finding


# ---------------------------------------------------------------------------
# compile_proposal (POP)
# ---------------------------------------------------------------------------

def _grammar_code_of(exc: BaseException) -> str:
    """The closed grammar code of one `ValueError` the promoter's operation
    grammar raised (`_proposal_structured_payload` / `_proposal_mutation`):
    an unknown parameter or value, an ambiguous or contradictory parameter
    set, or a parameter family the proposed kind does not admit. The text
    itself never travels."""
    text = str(exc).lower()
    if "no default variant" in text or "has no variant" in text:
        return "variant_invalid"
    if "unsupported proposal parameter" in text or "unknown" in text:
        return "param_unknown"
    if "requires kind" in text or "requires proposed kind" in text:
        return "kind_operation_mismatch"
    return "param_conflict"


def _active_executable_handoff(finding: Finding) -> bool:
    """Whether a finding is attempting an executable critic handoff.

    A proposal is executable content at every severity, so MINOR cannot be a
    side door around the compiler and INFO cannot carry one at all.  A named
    attack on any non-informational finding also promises an exact proposal.
    Findings with neither remain observations that can be reviewed or, for
    major/fatal ambiguity and reference disputes, explicitly adjudicated.
    """
    return (
        finding.proposed_case is not None
        or (
            finding.severity is not Severity.INFO
            and finding.suggested_attack is not None
        )
    )


def _requires_executable_proposal(finding: Finding, role: str) -> bool:
    """Return whether a finding must include an executable `proposed_case`.

    All active defect claims require one. Informational observations, explicit no-defect
    or abstention reports, probe-only shortcut reports, and administrative fidelity
    findings do not. The decision uses closed finding fields rather than prose
    heuristics.
    """
    if finding.severity is Severity.FATAL:
        return False
    if _active_executable_handoff(finding):
        return True
    return role == ADVERSARY_ROLE and finding.severity is Severity.MAJOR


def _claim_text(finding: Finding, proposal: ProposedAttackCase) -> str:
    return f"{finding.summary} {finding.detail} {proposal.rationale}"


def _missing_claim_identifiers(
    fidelity: Mapping[str, Any], finding: Finding, proposal: ProposedAttackCase
) -> tuple[str, ...]:
    """The identifier-bearing parameters of the compiled claim
    (`fidelity["requested"]`) the finding text does not name — the same rule
    `validate_proposal_claim_fidelity` applies (`attacks.claim_names_
    identifier`: case-folded, `_`/`-`/whitespace one separator, whole prose
    words), re-derived from its own `requested` record instead of parsed
    out of its error sentence."""
    from elt_taskgen.verification import attacks

    requested = fidelity.get("requested") if isinstance(fidelity, Mapping) else None
    if not isinstance(requested, Mapping):
        return ()
    identifiers: list[str] = []
    for key in _CLAIM_IDENTIFIER_KEYS:
        value = requested.get(key)
        if value:
            identifiers.append(str(value))
    for collection_key in ("tables", "target_marts"):
        values = requested.get(collection_key)
        if isinstance(values, (list, tuple)):
            identifiers.extend(str(value) for value in values)
    return attacks.missing_claim_identifiers(_claim_text(finding, proposal), identifiers)


def _allowed_variant_names(session: "CriticSession", kind: Any) -> tuple[str, ...]:
    """The registered variants of `kind` a compile correction may name back:
    the closed registry's named members (`attacks.allowed_kind_variants`),
    restricted to what the task's public identifier set admits — the same
    set the transport gatekeeper (`assert_value_free`) enforces, so a name
    can never trip it.  A variant the vocabulary does not yet publish is
    withheld, never invented."""
    from elt_taskgen.models import AttackKind
    from elt_taskgen.verification import attacks

    try:
        attack_kind = AttackKind(getattr(kind, "value", kind))
    except ValueError:
        return ()
    return tuple(
        name
        for name in attacks.allowed_kind_variants(attack_kind)
        if name in session.public
    )


def _proposal_diagnostic(session: CriticSession, index: int, finding: Finding) -> Diagnostic:
    from elt_taskgen.review import council
    from elt_taskgen.verification import attacks

    proposal = finding.proposed_case
    if proposal is not None and finding.severity is Severity.INFO:
        return Diagnostic(
            source=DiagnosticSource.COMPILE,
            ok=False,
            code="uncompilable",
            subject=proposal.kind.value,
            flags={
                PROPOSAL_PRESENT_FLAG: True,
                "severity_incompatible": True,
                CLAIM_FIDELITY_FLAG: False,
            },
        )
    if proposal is None:
        required = _requires_executable_proposal(finding, session.role)
        return Diagnostic(
            source=DiagnosticSource.COMPILE,
            ok=not required,
            code="uncompilable" if required else "compiles",
            subject=(
                finding.suggested_attack.value
                if required and finding.suggested_attack is not None
                else ""
            ),
            flags={
                PROPOSAL_PRESENT_FLAG: False,
                **({"proposal_missing": True} if required else {}),
            },
            # A named kind that has no default realization is told which
            # variants exist, so the missing proposal can name one.
            names=(
                _allowed_variant_names(session, finding.suggested_attack)
                if (
                    required
                    and finding.suggested_attack is not None
                    and attacks._default_attack_directive(finding.suggested_attack) is None
                )
                else ()
            ),
        )
    kind = proposal.kind.value
    flags: dict[str, bool] = {PROPOSAL_PRESENT_FLAG: True}
    # 1. The exact closed grammar and task-bound targets used by promotion.
    #    The mutation string is formed and DISCARDED; no attack is executed.
    try:
        _case, fidelity = attacks.validate_proposed_case(
            session.task, finding, proposal, required=False
        )
    except ValueError as exc:
        grammar_code = _grammar_code_of(exc)
        flags[grammar_code] = True
        flags[CLAIM_FIDELITY_FLAG] = False
        # A kind directive whose variant is missing or unknown (batch report
        # 156: `wrong_agg_stage` with no variant) is answered with the closed
        # registry's variants for that kind — public vocabulary, never the
        # rejected text — so the correction can be acted on.
        names = (
            _allowed_variant_names(session, proposal.kind)
            if grammar_code == "variant_invalid"
            else ()
        )
        return Diagnostic(
            source=DiagnosticSource.COMPILE,
            ok=False,
            code="uncompilable",
            subject=kind,
            flags=flags,
            names=names,
        )
    # 2. Claim fidelity: the identifiers the parameters target must be named
    #    by the finding's own text. Variant-only kinds already failed above if
    #    ``params.variant`` was absent or outside the registered closed set.
    passed = bool(fidelity.get("passed"))
    flags[CLAIM_FIDELITY_FLAG] = passed
    names: tuple[str, ...] = ()
    if not passed:
        missing = _missing_claim_identifiers(fidelity, finding, proposal)
        if missing:
            flags["claim_missing_identifier"] = True
            # Only PUBLIC identifiers may be named back (a hidden population
            # a `hardcode_population` proposal targets is withheld).
            names = tuple(sorted(n for n in missing if n in session.public))
        else:
            flags["kind_operation_mismatch"] = True
    # 3. The in-payload duplicate bit: an EARLIER proposal of this payload
    #    compiles to the same mutant (the key strings are compared and
    #    dropped; the promoter's own identity function decides).
    key = council._proposal_mutant_key(session.task, finding)
    earlier = False
    if key is not None:
        earlier = any(
            council._proposal_mutant_key(session.task, other) == key
            for other in session.findings[:index]
        )
    flags[SAME_MUTANT_FLAG] = earlier
    return Diagnostic(
        source=DiagnosticSource.COMPILE,
        ok=passed,
        code="compiles" if passed else "uncompilable",
        subject=kind,
        flags=flags,
        names=names,
    )


def compile_proposal(ctx: ToolContext, args: Mapping[str, Any]) -> Diagnostic:
    """Compile one finding's proposed case and project a closed diagnostic.

    Validate operation grammar and claim-to-identifier fidelity. Return only approved
    codes, flags, and public names.
    """
    session, index, finding = _finding_of(ctx, args, COMPILE_PROPOSAL_TOOL)
    return _proposal_diagnostic(session, index, finding)


def fold_compile_proposal(diagnostics: Sequence[Diagnostic]) -> Diagnostic:
    """The payload-level result the harness answers a submit with: red iff
    any proposal is red; the first red proposal's kind as `subject`; the
    union of the red proposals' grammar flags and public names; the
    presence, fidelity and same-mutant bits folded."""
    diags = tuple(diagnostics)
    with_proposal = [d for d in diags if d.flags.get(PROPOSAL_PRESENT_FLAG, False)]
    reds = [d for d in diags if not d.ok]
    ok = not reds
    flags: dict[str, bool] = {
        PROPOSAL_PRESENT_FLAG: bool(with_proposal),
        SAME_MUTANT_FLAG: any(d.flags.get(SAME_MUTANT_FLAG, False) for d in diags),
    }
    if with_proposal:
        flags[CLAIM_FIDELITY_FLAG] = all(d.flags.get(CLAIM_FIDELITY_FLAG, True) for d in with_proposal)
    for code in GRAMMAR_CODES:
        if any(d.flags.get(code, False) for d in reds):
            flags[code] = True
    names = tuple(sorted({name for d in reds for name in d.names}))
    return Diagnostic(
        source=DiagnosticSource.COMPILE,
        ok=ok,
        code="compiles" if ok else "uncompilable",
        subject=reds[0].subject if reds else "",
        flags=flags,
        names=names,
    )


# ---------------------------------------------------------------------------
# compile_probe (SHC)
# ---------------------------------------------------------------------------

def _probe_diagnostic(session: CriticSession, finding: Finding) -> Diagnostic:
    from elt_taskgen.verification import attacks

    # EXACTLY `council._compiled_mutant_key`'s computation — the standing
    # catalogue stripped, the proposal stripped so the probe side is judged
    # — minus the key string it would form (which embeds `case.mutation`).
    bare = session.task.model_copy(update={"attack_cases": ()})
    probe = finding.model_copy(update={"proposed_case": None})
    cases = attacks.compile_attacks(bare, [probe])
    projected = project_compile(cases)
    flags = dict(projected.flags)
    flags[PROBE_PRESENT_FLAG] = finding.suggested_attack is not None
    subject = projected.subject
    names: tuple[str, ...] = ()
    if (
        not projected.ok
        and finding.suggested_attack is not None
        and finding.severity is not Severity.INFO
        and attacks._default_attack_directive(finding.suggested_attack) is None
    ):
        # The compiler refused to GUESS a variant for a kind that has no
        # default realization (`wrong_agg_stage`, `custom`): a structured
        # `variant_invalid` with the kind as subject and the registry's
        # variants as public names, never a ValueError out of the compiler
        # and never a silent "no probe" (batch report 156).
        flags["variant_invalid"] = True
        subject = finding.suggested_attack.value
        names = _allowed_variant_names(session, finding.suggested_attack)
    return Diagnostic(
        source=DiagnosticSource.COMPILE,
        ok=projected.ok,
        code=projected.code,
        subject=subject,
        flags=flags,
        names=names,
    )


def compile_probe(ctx: ToolContext, args: Mapping[str, Any]) -> Diagnostic:
    """`compile_probe` over ONE finding of the payload the harness holds
    (`args = {"finding_index": int}`; SoT T3): `{compiles, kind,
    is_directive}` — the projection (`project_compile`) of what
    `council._compiled_mutant_key` compiles (`attacks.compile_attacks(bare,
    [probe])`), never the key, never an inert or inapplicable sentence
    (`materialize_mutation` is never called), never the screen's lexicon."""
    session, _index, finding = _finding_of(ctx, args, COMPILE_PROBE_TOOL)
    return _probe_diagnostic(session, finding)


def fold_compile_probe(diagnostics: Sequence[Diagnostic]) -> Diagnostic:
    """The payload-level result: a problem ONLY when no probe of the payload
    compiles (an empty payload included), mirroring the review stage's
    diligence check on the shortcut attacker (`cli.py`: "zero executable
    probes"); the compiled probes' kinds travel as public `names`."""
    diags = tuple(diagnostics)
    compiled = [d for d in diags if d.ok]
    reds = [d for d in diags if not d.ok]
    ok = bool(compiled)
    flags: dict[str, bool] = {
        PROBE_PRESENT_FLAG: any(d.flags.get(PROBE_PRESENT_FLAG, False) for d in diags),
        EVERY_PROBE_COMPILES_FLAG: bool(diags) and all(d.ok for d in diags),
        IS_DIRECTIVE_FLAG: any(d.flags.get(IS_DIRECTIVE_FLAG, False) for d in compiled),
    }
    if ok:
        subject = compiled[0].subject
        names = tuple(sorted({d.subject for d in compiled if d.subject}))
    else:
        # A red payload says WHY in the closed grammar: the first red probe's
        # kind, its `variant_invalid` bit and the registry's public variant
        # names, so the seat can name an executable variant instead of
        # guessing (batch report 156).
        subject = reds[0].subject if reds else ""
        if any(d.flags.get("variant_invalid", False) for d in reds):
            flags["variant_invalid"] = True
        names = tuple(sorted({name for d in reds for name in d.names}))
    return Diagnostic(
        source=DiagnosticSource.COMPILE,
        ok=ok,
        code="compiles" if ok else "uncompilable",
        subject=subject,
        flags=flags,
        names=names,
    )


# ---------------------------------------------------------------------------
# measured_match_bit (POP, flag F, default off)
# ---------------------------------------------------------------------------

def _scratch_workspace(ctx: Any, session: CriticSession, call: int) -> Path:
    """A disposable workspace under the tool context root carrying ONLY the
    task's population rows (what `run_attack` reads): the promoter writes
    its reward and fidelity records there, never into the real workspace."""
    if session.gold is None or session.workspace is None:
        raise ToolHarnessFault(MEASURED_MATCH_BIT_TOOL, code="no_gold_handle")
    source = session.workspace / "tasks" / session.task_id / "populations"
    if not source.is_dir():
        raise ToolHarnessFault(MEASURED_MATCH_BIT_TOOL, code="no_population_rows")
    scratch = Path(ctx.root) / MEASURED_MATCH_BIT_TOOL / f"call_{call}"
    target = scratch / "tasks" / session.task_id / "populations"
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return scratch


def _exact_match_of(ctx: Any, session: CriticSession, finding: Finding, *, call: int) -> bool:
    """Did `finding`'s proposal predict its five-population matrix EXACTLY?
    The real promoter decides (`promote_proposed_cases` on a scratch copy
    of the population rows with the gold bundle); only its boolean leaves."""
    from elt_taskgen.verification import attacks

    scratch = _scratch_workspace(ctx, session, call)
    result = attacks.promote_proposed_cases(session.task, [finding], scratch, session.gold)
    outcomes = tuple(result.outcomes)
    return bool(outcomes) and all(bool(o.promoted) for o in outcomes)


def measured_match_bit(ctx: ToolContext, args: Mapping[str, Any]) -> Diagnostic:
    """`measured_match_bit` over ONE finding of the payload the harness holds
    (`args = {"finding_index": int}`; SoT T3): `{exact_match}` — one bit,
    from the promoter's exact-matrix comparison in a gold-bearing scratch
    workspace. The predicted and measured matrices never leave (A24)."""
    session, _index, finding = _finding_of(ctx, args, MEASURED_MATCH_BIT_TOOL)
    if finding.proposed_case is None:
        return Diagnostic(
            source=DiagnosticSource.PROMOTION,
            ok=False,
            code="uncompilable",
            flags={EXACT_MATCH_FLAG: False, PROPOSAL_PRESENT_FLAG: False},
        )
    session.match_bit_calls += 1
    exact = _exact_match_of(ctx, session, finding, call=session.match_bit_calls)
    return Diagnostic(
        source=DiagnosticSource.PROMOTION,
        ok=exact,
        code="promoted" if exact else "mismatch",
        subject=finding.proposed_case.kind.value,
        flags={EXACT_MATCH_FLAG: exact, PROPOSAL_PRESENT_FLAG: True},
    )


# ---------------------------------------------------------------------------
# The post-session projection (never a tool)
# ---------------------------------------------------------------------------

def _outcome_fields(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    raise TypeError(
        f"project_proposal_matrix needs PromotionOutcome objects or mappings, not "
        f"{type(raw).__name__}"
    )


def project_proposal_matrix(outcomes: Sequence[Any]) -> dict:
    """Build the post-session proposal-matrix audit record.

    Expose promoted, predicted, measured-pass, and fidelity booleans by finding and
    population. Never return this record to a session or expose rewards and reason text.
    """
    matrix: dict[str, dict[str, Any]] = {}
    for raw in outcomes:
        fields = _outcome_fields(raw)
        predicted = fields.get("predicted") or {}
        measured_pass = fields.get("measured_pass") or {}
        per_population: dict[str, dict[str, bool]] = {}
        for pop in PopulationName:
            if pop.value in predicted or pop.value in measured_pass:
                per_population[pop.value] = {
                    "predicted": bool(predicted.get(pop.value, False)),
                    "measured_pass": bool(measured_pass.get(pop.value, False)),
                }
        fidelity = fields.get("fidelity") or {}
        matrix[str(fields.get("finding_id", ""))] = {
            "promoted": bool(fields.get("promoted", False)),
            "per_population": per_population,
            "fidelity_ok": (
                bool(fidelity.get("passed", False)) if isinstance(fidelity, Mapping) else False
            ),
        }
    return matrix


#: The `FindingScreen.signals` entry of a proposal the harness compiled red
#: on the ACCEPTED submission (the compile-correction budget was spent, SoT
#: T1.1 "a second compile failure is accepted and screened post-session").
UNCOMPILABLE_AFTER_CORRECTIONS = "uncompilable_after_corrections"


def void_uncompilable_proposals(
    findings: Sequence[Finding], session: CriticSession, result: Any = None
) -> tuple[Finding, ...]:
    """Clear executable fields that remained invalid after bounded corrections.

    For each finding, require its stored proposal diagnostic to be green; the shortcut
    attacker must also satisfy the payload-level probe diagnostic. Invalid proposals and
    attacks are set to `None`, while the prose finding remains for ordinary screening.
    Return the updated findings and count of voided executable claims.
    """
    from elt_taskgen.review.council import _void

    if result is not None:
        reds = tuple(getattr(result, "red_validators_at_submit", ()) or ())
        # The adversary's proposals are compiled by `compile_proposal`, the
        # attacker's by `compile_probe` (version 4); either seat's red
        # validator at submit means the recorded per-finding checks decide.
        if not ({COMPILE_PROPOSAL_TOOL, COMPILE_PROBE_TOOL} & set(reds)):
            return tuple(findings)
    checks = tuple(session.finding_checks)
    voided: list[Finding] = []
    for index, finding in enumerate(findings):
        check = checks[index] if index < len(checks) else None
        if (
            check is not None
            and not bool(check.ok)
            and _requires_executable_proposal(finding, session.role)
        ):
            flags = sorted(
                code for code in GRAMMAR_CODES if check.flags.get(code, False)
            )
            session.compile_correction_exhausted += 1
            voided.append(
                _void(
                    finding,
                    [UNCOMPILABLE_AFTER_CORRECTIONS],
                    "executable handoff still "
                    + (", ".join(flags) or check.code)
                    + " after the bounded compile correction; voided by the "
                    "harness, never charged to the task",
                )
            )
            continue
        voided.append(finding)
    return tuple(voided)


# ---------------------------------------------------------------------------
# The validator tools (harness-only; never on the wire)
# ---------------------------------------------------------------------------

class CompileProposalTool:
    """`compile_proposal`: the harness-run compile of EVERY `proposed_case`
    of a submitted payload through the promoter's closed grammar and its
    claim-fidelity check, folded into one Diagnostic (`fold_compile_
    proposal`). 3 oracle bits per run (S2 §3.6), a 20 s wall (SoT T1.1)."""

    name = COMPILE_PROPOSAL_TOOL
    description = (
        "Harness-run on every submitted payload (never model-callable): each "
        "proposed_case is compiled through the promoter's closed operation grammar "
        "and its claim-fidelity check. Returns compiles, or uncompilable with one "
        "grammar flag (proposal_missing, param_unknown, param_conflict, "
        "variant_invalid, kind_operation_mismatch, severity_incompatible, uncompilable, "
        "claim_missing_identifier), the public identifiers the "
        "finding text failed to name, and whether an earlier proposal of the same "
        "payload compiles to the same mutant. Codes and booleans only: no mutant "
        "key, no measured reward, no screen verdict."
    )
    input_schema = _PAYLOAD_ARGS
    cost = ToolCost(oracle_bits=3, wall_s=20.0)
    permitted_roles = _ADV
    validator = True
    harness_only = True
    trust_domain = "D3"
    version = CRITIC_VALIDATOR_VERSION

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _critic_session_of(ctx, self.name)
        session.tool_calls += 1
        findings = session.install_payload(args)
        per_finding = tuple(
            _proposal_diagnostic(session, index, finding) for index, finding in enumerate(findings)
        )
        session.finding_checks = per_finding
        folded = fold_compile_proposal(per_finding)
        session.checks.append(folded)
        return folded


def fold_compile_probe_payload(
    findings: Sequence[Finding],
    probes: Sequence[Diagnostic],
    proposals: Sequence[Diagnostic],
) -> Diagnostic:
    """Combine shortcut-probe and proposal validation for one payload.

    The fold is green only when every required proposal compiles and the payload
    contains an executable compiling probe under the review-stage diligence predicate.
    Return the first closed diagnostic needed for correction, including public kind or
    variant names where allowed.
    """
    from elt_taskgen.review.council import is_executable_probe  # lazy: council imports this module

    probe_fold = fold_compile_probe(probes)
    proposal_fold = fold_compile_proposal(proposals)
    executable_kinds: list[str] = []
    for index, finding in enumerate(findings):
        if (
            is_executable_probe(finding)
            and index < len(proposals)
            and bool(proposals[index].ok)
            and finding.suggested_attack is not None
        ):
            executable_kinds.append(finding.suggested_attack.value)
    executable = bool(executable_kinds)
    flags: dict[str, bool] = {**probe_fold.flags, EXECUTABLE_PROBE_FLAG: executable}
    if proposal_fold.ok and executable:
        # Project probe presence, compilation, directive status, and executable
        # kinds only; proposal-fold and irrelevant bare-probe flags stay out.
        compiled = [d.subject for d in probes if d.ok and d.subject]
        ordered = list(dict.fromkeys(executable_kinds + compiled))
        return Diagnostic(
            source=DiagnosticSource.COMPILE,
            ok=True,
            code="compiles",
            subject=ordered[0],
            flags={key: value for key, value in flags.items() if key not in GRAMMAR_CODES},
            names=tuple(sorted(set(ordered))),
        )
    if not proposal_fold.ok:
        flags[CLAIM_FIDELITY_FLAG] = bool(proposal_fold.flags.get(CLAIM_FIDELITY_FLAG, False))
        for code in GRAMMAR_CODES:
            if proposal_fold.flags.get(code, False):
                flags[code] = True
    if not probe_fold.ok or proposal_fold.ok:
        # No bare probe compiles (the probe side says why: the kind and, for
        # a kind with no default realization, the registry's variants), or
        # every proposal is fine and the payload still carries no executable
        # probe (the probe side says which kinds did compile).
        subject, names = probe_fold.subject, probe_fold.names
    else:
        subject, names = proposal_fold.subject, proposal_fold.names
    return Diagnostic(
        source=DiagnosticSource.COMPILE,
        ok=False,
        code="uncompilable",
        subject=subject,
        flags=flags,
        names=names,
    )


class CompileProbeTool:
    """`compile_probe`: the harness-run compile of EVERY probe of a submitted
    payload (`fold_compile_probe`: a problem only when none compiles) and of
    every `proposed_case` it carries (`fold_compile_proposal`, the same
    closed grammar and claim-fidelity boundary the attack stage applies;
    the per-finding results are recorded on the session for the post-session
    void exactly as `compile_proposal` records them).  3 oracle bits per run,
    a 20 s wall."""

    name = COMPILE_PROBE_TOOL
    description = (
        "Harness-run on every submitted payload (never model-callable): each "
        "finding's suggested_attack is compiled into its probe by the attack "
        "compiler, and each proposed_case through the promoter's closed operation "
        "grammar and its claim-fidelity check. Returns compiles with the kinds "
        "that compiled, or uncompilable when no probe of the payload compiles, "
        "when a proposed_case is malformed, or when no finding of the payload is "
        "an executable probe (executable_probe=false: the review stage counts "
        "findings above info whose suggested_attack is matched by a proposed_case "
        "of the same kind), with one grammar flag (proposal_missing, "
        "param_unknown, param_conflict, variant_invalid, kind_operation_mismatch, "
        "severity_incompatible, uncompilable, claim_missing_identifier) and the "
        "public identifiers concerned. Codes and booleans only: no mutant key, no "
        "inert or inapplicable verdict, no screen verdict."
    )
    input_schema = _PAYLOAD_ARGS
    cost = ToolCost(oracle_bits=3, wall_s=20.0)
    permitted_roles = _SHC
    validator = True
    harness_only = True
    trust_domain = "D3"
    version = CRITIC_VALIDATOR_VERSION

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _critic_session_of(ctx, self.name)
        session.tool_calls += 1
        findings = session.install_payload(args)
        probes = [_probe_diagnostic(session, finding) for finding in findings]
        # Every finding crosses the proposal boundary. Missing required cases
        # get an in-session correction; if still red, the aligned finding is
        # voided after the bounded session.
        proposals = tuple(
            _proposal_diagnostic(session, index, finding)
            for index, finding in enumerate(findings)
        )
        session.finding_checks = proposals
        # The fold also applies the review stage's diligence predicate
        # (`council.is_executable_probe`) to the payload, so a payload with
        # no executable probe is red HERE, in-session, and never first at
        # the review stage (review findings 1-2 and 1-3).
        folded = fold_compile_probe_payload(findings, probes, proposals)
        session.checks.append(folded)
        return folded


class MeasuredMatchBitTool:
    """`measured_match_bit` (flag F, default off): ONE aggregate bit over the
    payload's proposals — every proposal's matrix predicted exactly — from
    the real promoter in a gold-bearing scratch workspace; at most ONE call
    per session (`per_session`), 1 oracle bit (SoT T1.1: `max_oracle_bits`
    7 with the flag). The harness runs it on the first submitted payload
    and never again; the matrices stay behind (A24)."""

    name = MEASURED_MATCH_BIT_TOOL
    description = (
        "Harness-run at most once per session, only under the population "
        "adversary's measured_match_bit flag: whether every proposed_case of the "
        "submitted payload predicted its five-population reward matrix exactly, "
        "as one boolean. Never the matrices, never a reward."
    )
    input_schema = _PAYLOAD_ARGS
    cost = ToolCost(oracle_bits=1, wall_s=300.0, per_session=1)
    permitted_roles = _ADV
    validator = True
    harness_only = True
    trust_domain = "D3"
    version = CRITIC_VALIDATOR_VERSION

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _critic_session_of(ctx, self.name)
        session.tool_calls += 1
        findings = session.install_payload(args)
        if session.match_bit_calls >= 1:
            # The runner skips a validator whose `per_session` cap is spent;
            # reaching this is a wiring defect, never a second measurement.
            raise ToolHarnessFault(self.name, code="match_bit_already_measured")
        proposals = [f for f in findings if f.proposed_case is not None]
        session.match_bit_calls += 1
        exact = True
        for finding in proposals:
            if not _exact_match_of(ctx, session, finding, call=session.match_bit_calls):
                exact = False
                break
        folded = Diagnostic(
            source=DiagnosticSource.PROMOTION,
            ok=exact,
            code="promoted" if exact else "mismatch",
            flags={EXACT_MATCH_FLAG: exact, PROPOSAL_PRESENT_FLAG: bool(proposals)},
        )
        session.checks.append(folded)
        return folded


CRITIC_VALIDATORS: tuple[Any, ...] = (
    CompileProposalTool(),
    CompileProbeTool(),
    MeasuredMatchBitTool(),
)
CRITIC_VALIDATOR_NAMES: tuple[str, ...] = tuple(t.name for t in CRITIC_VALIDATORS)


def critic_validator(name: str) -> Any:
    for tool in CRITIC_VALIDATORS:
        if tool.name == name:
            return tool
    raise KeyError(name)


def declared_validators(
    role: str | CouncilRole, block: Mapping[str, Any] | None = None
) -> tuple[Any, ...]:
    """The harness validators the permission matrix DECLARES for a critic
    seat (SoT T1.1): `compile_proposal` for the population adversary — plus
    `measured_match_bit` when its block's flag is on — and `compile_probe`
    for the shortcut attacker; nothing for any other role. `block` is the
    role's `session:` block (None = read from the loaded agents document).
    Ungated: `ToolRegistry.for_role` applies the `enabled` gate."""
    role_name = str(getattr(role, "value", role))
    if role_name == ADVERSARY_ROLE:
        if block is None:
            from elt_taskgen.review import providers  # lazy: providers imports the registry

            block = providers.role_loop_limits(role_name)
        tools: list[Any] = [critic_validator(COMPILE_PROPOSAL_TOOL)]
        if bool((block or {}).get(MEASURED_MATCH_BIT_FLAG, False)):
            tools.append(critic_validator(MEASURED_MATCH_BIT_TOOL))
        return tuple(tools)
    if role_name == ATTACKER_ROLE:
        return (critic_validator(COMPILE_PROBE_TOOL),)
    return ()


def critic_registry(role: str | CouncilRole, block: Mapping[str, Any] | None = None) -> ToolRegistry:
    """The ungated registry of a critic seat's declared validators (what a
    pilot session the caller opted into dispatches through)."""
    role_name = str(getattr(role, "value", role))
    return ToolRegistry(role_name, declared_validators(role_name, block))


def critic_limits(
    role: str | CouncilRole,
    block: Mapping[str, Any] | None = None,
    *,
    agents_config: Path | str | None = None,
) -> SessionLimits:
    """The critic seat's `SessionLimits`: its declared `session:` block
    VERBATIM (SoT T1.1: production = metrology block; a critic block is
    enforced as declared, never folded), with the document's session-wide
    defaults riding beside it un-hashed."""
    from elt_taskgen.review import providers  # lazy: providers imports the registry
    from elt_taskgen.review.tools.validators import session_defaults_for

    role_name = str(getattr(role, "value", role))
    if block is None:
        block = providers.role_loop_limits(role_name, agents_config=agents_config)
    return SessionLimits.from_block(dict(block), session_defaults=session_defaults_for(agents_config))


def critic_policy(
    role: str | CouncilRole,
    limits: SessionLimits | None = None,
    *,
    session_salt: int = 0,
    agents_config: Path | str | None = None,
) -> SessionPolicy:
    """The session policy a harness-validated critic seat runs under (SoT
    T1.1 `mode: harness_validated`): the declared validators in the
    allowlist (harness-only, so the runner can run them and the policy
    names them), the forced `report_findings` as the ONLY terminal — the
    seat has no `abort` tool: abstention is an empty findings list — and
    exactly the one-shot wire (`wire_tools_for`)."""
    from elt_taskgen.review import providers  # lazy: providers imports the registry

    role_name = str(getattr(role, "value", role))
    if role_name not in CRITIC_VALIDATOR_ROLES:
        raise ValueError(
            f"critic policies exist for {list(CRITIC_VALIDATOR_ROLES)}, not {role_name!r}"
        )
    declared = critic_limits(
        role_name, limits.block if limits is not None else None, agents_config=agents_config
    )
    if limits is not None and limits.session_defaults:
        declared = declared.with_session_defaults(dict(limits.session_defaults))
    return SessionPolicy(
        role=role_name,
        tools=declared_validators(role_name, declared.block),
        submit_tool=providers.tool_name_for(role_name),
        abort_tool="",
        limits=declared,
        mode=declared.mode or "harness_validated",
        wire_tools=tuple(providers.wire_tools_for(role_name)),
        session_salt=int(session_salt),
    )


def critic_validator_worker() -> InProcessValidatorWorker:
    """The D3 worker for a critic session: the last submitted payload as
    the auto-submittable draft of a limit stop."""
    return InProcessValidatorWorker(
        current_draft=lambda ctx: _critic_session_of(ctx, "worker").current_draft(),
    )

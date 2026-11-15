"""Classify critic handoffs without treating protocol failures as task judgments."""

from __future__ import annotations

from elt_taskgen.engine import (
    BLOCKED_ON_HUMAN,
    BLOCKED_ON_KEY,
    RETRY_GUARD_EXPLICIT,
    RETRY_GUARD_KEY,
    StageOutcome,
    StagePayload,
    VERDICT_BLOCKED,
)
from elt_taskgen.models import (
    AttackKind,
    CouncilRole,
    Finding,
    FindingProvenance,
    FindingScreenStatus,
    ProposedAttackCase,
    RepairRoute,
    Severity,
    canonical_json,
    sha256_hex,
)


CRITIC_PROTOCOL_FAILURE_CLASS = "protocol_failure"
CRITIC_PROTOCOL_FAILURE_CODE = "critic_attack_handoff_invalid"
CRITIC_PROTOCOL_RECOVERY = "correct_or_replace_critic_handoff"
CRITIC_MANIFEST_FAILURE_CODE = "critic_transcript_manifest_invalid"
CRITIC_MANIFEST_RECOVERY = "rebuild_or_correct_review_transcript_manifest"
CRITIC_DILIGENCE_FAILURE_CODE = "critic_shortcut_diligence_incomplete"
CRITIC_DILIGENCE_RECOVERY = "replace_shortcut_critic_handoff"
CRITIC_AMBIGUITY_FAILURE_CODE = "critic_ambiguity_requires_adjudication"
CRITIC_GOLD_DISAGREEMENT_CODE = "critic_gold_disagreement"
#: Signal for a proposal still invalid after bounded compile correction.
UNCOMPILABLE_AFTER_CORRECTIONS_SIGNAL = "uncompilable_after_corrections"

#: The same void, for a seat that never had a correction channel at all: its
#: declared `session:` block runs no compile validator on what it submits, so
#: nothing upstream could tell it its proposal was malformed.
UNCOMPILABLE_NO_CHANNEL_SIGNAL = "uncompilable_no_correction_channel"

#: A MAJOR finding its own provider withdrew (structured `disposition`, R02)
#: stays in the screen record but does not block. Explanation text never
#: withdraws a finding.
WITHDRAWN_BY_DISPOSITION_SIGNAL = "withdrawn_by_disposition"


#: A proposal that removes behavior absent from the contract is not a fork;
#: preserve the finding in the screen record without blocking.
MUTATION_TARGETS_NO_DECLARED_RULE_SIGNAL = "mutation_targets_no_declared_rule"


MUTATION_NAMES_NO_SECOND_HOP_SIGNAL = "mutation_names_no_second_hop"


def _proposal_names_a_hop_the_task_never_joins(task, proposal) -> bool:
    """Return whether a second-hop mutation targets a task with no second hop."""
    from elt_taskgen.models import AttackKind, MartOpKind

    kind = getattr(proposal, "kind", None)
    if kind is not AttackKind.INNER_JOIN:
        return False
    params = getattr(proposal, "params", None) or {}
    if str(params.get("variant", "") or "") != "second_hop":
        return False
    sources = {table.name for table in task.tables}
    for mart in task.marts:
        ops = getattr(getattr(mart, "plan", None), "ops", None) or ()
        hops = [
            op
            for op in ops
            if getattr(op, "kind", None) is MartOpKind.JOIN
            and any(name in sources for name in tuple(op.tables)[1:])
        ]
        if len(hops) >= 2:
            return False
    return True


def _proposal_targets_a_rule_the_task_never_states(task, proposal) -> bool:
    from elt_taskgen.models import AttackKind, MartOpKind

    kind = getattr(proposal, "kind", None)
    if kind is not AttackKind.NO_DEDUP:
        return False
    declared = any(
        getattr(op, "kind", None) is MartOpKind.DEDUPE
        for mart in task.marts
        for op in (getattr(getattr(mart, "plan", None), "ops", None) or ())
    )
    return not declared


def _seat_has_compile_channel(role, *, agents_config=None) -> bool:
    """Return whether a seat declares an enabled proposal or probe compiler."""
    from elt_taskgen.review import providers as providers_mod
    from elt_taskgen.review.tools import critic_validators as critic_tools

    role_name = getattr(role, "value", str(role or ""))
    if not role_name:
        return False
    try:
        block = providers_mod.role_loop_limits(role_name, agents_config=agents_config)
    except Exception:  # a role with no block, or an unreadable document
        return False
    if not (isinstance(block, dict) and block.get("enabled", False)):
        return False
    declared = {str(name) for name in (block.get("harness_validators") or ())}
    return bool(
        declared
        & {critic_tools.COMPILE_PROPOSAL_TOOL, critic_tools.COMPILE_PROBE_TOOL}
    )


def _claimed_critic_handoff(
    finding: Finding,
) -> tuple[Severity, AttackKind | None, ProposedAttackCase | None]:
    """Return a provider finding's preserved pre-screen executable claims.

    Harness-voided proposals remain final after their bounded correction.
    """

    screen = finding.screen
    if screen is None:
        return finding.severity, finding.suggested_attack, finding.proposed_case
    if (
        screen.status is FindingScreenStatus.VOID
        and UNCOMPILABLE_AFTER_CORRECTIONS_SIGNAL in tuple(screen.signals)
    ):
        return finding.severity, finding.suggested_attack, finding.proposed_case
    severity = screen.claimed_severity or finding.severity
    attack = finding.suggested_attack
    proposal = finding.proposed_case
    if screen.withheld_attack is not None:
        attack = screen.withheld_attack
    if screen.withheld_proposal is not None:
        proposal = screen.withheld_proposal
    return severity, attack, proposal


def _critic_protocol_block(
    detail: str,
    *,
    failure_code: str = CRITIC_PROTOCOL_FAILURE_CODE,
    recovery_prerequisite: str = CRITIC_PROTOCOL_RECOVERY,
) -> StageOutcome:
    """A critic transport/compile contract failed; the task is not judged.

    The digest is stable for an identical rejected handoff.  It gives the
    orchestration layer an evidence key with which to refuse an unchanged
    ordinary resume instead of repeatedly paying for/replaying the same bad
    critic output.  Recovery must replace that handoff (or otherwise change the
    protocol evidence), not retry it until it happens to pass.
    """
    handoff_digest = sha256_hex(
        canonical_json(
            {
                "failure_code": failure_code,
                "detail": detail,
            }
        )
    )
    return StageOutcome(
        VERDICT_BLOCKED,
        StagePayload(
            error=detail,
            data={
                "failure_class": CRITIC_PROTOCOL_FAILURE_CLASS,
                "failure_code": failure_code,
                BLOCKED_ON_KEY: "provider",
                "handoff_digest": handoff_digest,
                "recovery_prerequisite": recovery_prerequisite,
                RETRY_GUARD_KEY: RETRY_GUARD_EXPLICIT,
            },
        ),
    )


def _critic_adjudication_block(findings: list[Finding]) -> StageOutcome | None:
    """Hold fatal ambiguity and reference disputes for human adjudication.

    Major ambiguity claims route to specification repair; alternatives remain
    evidence and are not promoted.
    """
    disputes: list[Finding] = []
    for finding in findings:
        if (
            finding.screen is not None
            and finding.screen.status
            in {FindingScreenStatus.VOID, FindingScreenStatus.DUPLICATE}
        ):
            continue
        claimed_severity, claimed_attack, claimed_proposal = (
            _claimed_critic_handoff(finding)
        )
        if finding.provenance is not FindingProvenance.PROVIDER:
            continue
        if claimed_severity not in {Severity.MAJOR, Severity.FATAL}:
            continue
        gold_dispute = finding.route_hint is RepairRoute.REFERENCE
        ambiguity = finding.role is CouncilRole.AMBIGUITY_CRITIC
        if not (ambiguity or gold_dispute):
            continue
        if (
            ambiguity
            and not gold_dispute
            and claimed_severity is Severity.MAJOR
        ):
            # Ambiguity proposals are evidence, not gate cases: withhold them
            # from promotion and route the unresolved finding to specification
            # repair. A bare `suggested_attack` remains informational only.
            continue
        disputes.append(finding)
    if not disputes:
        return None
    is_gold_disagreement = any(
        finding.route_hint is RepairRoute.REFERENCE for finding in disputes
    )
    failure_code = (
        CRITIC_GOLD_DISAGREEMENT_CODE
        if is_gold_disagreement
        else CRITIC_AMBIGUITY_FAILURE_CODE
    )
    evidence_digest = sha256_hex(
        canonical_json(
            [
                finding.model_dump(mode="json")
                for finding in sorted(disputes, key=lambda item: item.finding_id)
            ]
        )
    )
    return StageOutcome(
        VERDICT_BLOCKED,
        StagePayload(
            error=(
                "critic evidence identifies an unresolved ambiguity or gold "
                "disagreement; any attached executable alternative is held, "
                "and the task, gold, and critic claim remain unjudged pending "
                "explicit adjudication"
            ),
            data={
                "failure_class": "pending_adjudication",
                "failure_code": failure_code,
                BLOCKED_ON_KEY: BLOCKED_ON_HUMAN,
                "critic_evidence_sha256": evidence_digest,
                "recovery_prerequisite": (
                    "bound_adjudication_or_revised_critic_evidence"
                ),
                RETRY_GUARD_KEY: RETRY_GUARD_EXPLICIT,
            },
        ),
    )

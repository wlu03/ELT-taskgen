"""Typed ledger payloads shared by pipeline stages and repair tools."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.models import CouncilRole, DifficultyMeasurement, Finding, Severity


class ContaminationPayload(BaseModel):
    """check_pre/check_post result recorded in the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_point: str
    collisions: tuple[dict, ...] = ()
    fatal_count: int = 0
    seeded_embedded_deny_lists: bool = False
    #: WHAT the index could see: grade plus the per-store, per-kind fingerprint
    #: census. Recorded because "0 collisions" is meaningless without it.
    coverage_level: str = ""
    coverage: dict = Field(default_factory=dict)
    detail: str = ""


class ReviewPayload(BaseModel):
    """Council findings recorded in the ledger (findings only, never a verdict
    vocabulary — the council cannot accept)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[Finding, ...] = ()
    fatal_count: int = 0
    detail: str = ""
    #: WHICH ADMISSION this review ran under. Recorded because revocation is
    #: PROSPECTIVE: without it, an auditor who later revokes an admission cannot
    #: list the reviews that ran on it. Defaulted, so old ledger rows still validate.
    admission: dict[str, str] = Field(default_factory=dict)
    #: Every council exchange consumed by this review, explicitly rebound to
    #: this task identity. Empty only for non-routed test doubles/legacy callers.
    transcript_manifest: tuple[dict, ...] = ()


class AttackFindingDescriptor(BaseModel):
    """One privacy-safe critic finding selected for an attack repair.

    The attack payload is a ledger artifact, not a model view.  It retains only
    the role/severity stamped by the harness and identifiers that the projector
    proved belong to the task's public surface; no finding prose or promoter
    reason is carried into the bounded repair session.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: CouncilRole
    severity: Severity
    identifiers: tuple[str, ...] = Field(default=(), max_length=16)


class AttackPayload(BaseModel):
    """Measured per-population rewards of every executed attack case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rewards: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: Per-variant rewards from the same execution; variant gates never use legacy rewards.
    rewards_by_variant: dict[str, dict[str, dict[str, float]]] = Field(
        default_factory=dict
    )
    cases: tuple[str, ...] = ()
    #: case -> no-surface reason, for informational probes this task's DATA offers
    #: no surface for. They carry NO rewards entry (an empty one would read as
    #: deleted evidence) but are named here, never silently skipped.
    inapplicable_cases: dict[str, str] = Field(default_factory=dict)
    #: Executed council attack-case PROPOSALS, promoted and rejected. Recorded so
    #: a rejection is VISIBLE in the ledger, never merely absent.
    promoted_proposals: tuple[str, ...] = ()
    rejected_proposals: tuple[dict, ...] = ()
    #: Full predicted/measured combined and EL/T matrices for every proposal,
    #: promoted or rejected. Names alone are not audit evidence.
    proposal_outcomes: tuple[dict, ...] = ()
    #: Value-free repair subject for one blocked critic-to-mutation handoff.
    blocking_finding: AttackFindingDescriptor | None = None
    detail: str = ""


class CalibratePayload(BaseModel):
    """Stage-9 ledger payload: the measurement plus what calibration did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    measurement: DifficultyMeasurement
    #: '' when only the structural measurement was taken.
    roster_fingerprint: str = ""
    #: variant value -> {model_key: measured pass rate}.
    pass_rates: dict[str, dict[str, float]] = Field(default_factory=dict)
    variants_from_cache: tuple[str, ...] = ()
    impossible_variants: tuple[str, ...] = ()
    trivial_variants: tuple[str, ...] = ()
    #: Non-empty when the empirical campaign was skipped (visible, never a
    #: silent pass and never fabricated numbers).
    skipped_reason: str = ""
    detail: str = ""

"""Validate release isolation before publishing RLVR labels.

Labelled releases require a sealed tier A or B attestation, contamination
enforcement, and a credential-free scanned mount. Unknown manifest shapes are
treated as labelled.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.models import RLVR_TASK_VARIANTS
from elt_taskgen.package_resources import resource_path
from elt_taskgen.runtime.attestation import (
    ATTESTING_TIERS,
    CONTAMINATION_MODES,
    SANDBOX_ATTESTATION_SCHEMA_VERSION,
    SandboxAttestation,
    SandboxAttestationError,
    assert_attestation_matches_agents_config,
    sandbox_attestation_digest,
    verify_sandbox_attestation,
)

#: Default to requiring attestation when the setting is absent.
RELEASE_REQUIRE_ATTESTATION_DEFAULT = True

#: The contamination enforcement mode a label-claiming batch must run under.
REQUIRED_CONTAMINATION_MODE = "enforce"

#: The corpus profile that ships the graded EL/T variants (export/release.py).
_RLVR_CORPUS_PROFILE = "eltbench_end_to_end"
#: Historical, pre-RLVR profile: a manifest recording it claims no labels.
_LEGACY_CORPUS_PROFILE = "legacy_full"
_RLVR_VARIANT_VALUES = frozenset(variant.value for variant in RLVR_TASK_VARIANTS)

#: Closed refusal vocabulary. A caller may branch on these; they never change
#: meaning silently.
REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "attestation_missing",
        "attestation_invalid",
        "attestation_unsealed",
        "attestation_digest_mismatch",
        "attestation_config_mismatch",
        "attestation_config_unbound",
        "attestation_tier_refused",
        "attestation_preflight_missing",
        "attestation_secret_material",
        "contamination_mode_unknown",
        "contamination_mode_refused",
        "contamination_mode_mismatch",
        "credential_files_in_mount",
        "mount_not_scanned",
        "attestation_tier_uncorroborated",
        "attestation_stale",
        "attestation_run_mismatch",
        "batch_manifest_invalid",
    }
)

#: Maximum age of an attestation used for labels: 30 days.
MAX_ATTESTATION_AGE_S = 30 * 24 * 60 * 60
_ACTIVE_AGENTS_CONFIG_NOT_SUPPLIED = object()


class AttestationRefusal(RuntimeError):
    """A batch may not claim RLVR labels under the isolation it recorded."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        if code not in REFUSAL_CODES:
            raise ValueError(f"unknown attestation refusal code {code!r}")
        self.code = code


class AttestationGateResult(BaseModel):
    """Report-only record of one gate decision (raised refusals never return)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claims_rlvr_labels: bool
    required: bool
    tier: Literal["0", "A", "B", "C", "D", ""] = ""
    attestation_digest: str = ""
    contamination_mode: str = ""
    reason: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# The configured setting
# ---------------------------------------------------------------------------


def _release_settings(config: Path | str | None) -> dict[str, Any]:
    """The ``release:`` block of the settings document, or ``{}``.

    An explicit path must exist (fail closed), mirroring
    ``review.metrology._metrology_block``; with no path the repository default
    is used when present.
    """

    if config is not None:
        path = Path(config)
        if not path.is_file():
            raise FileNotFoundError(f"config not found: {path} (fail closed)")
    else:
        path = resource_path("config/agents.yaml")
        if not path.is_file():
            return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"config document {path} is not a mapping")
    block = doc.get("release") or {}
    if not isinstance(block, dict):
        raise ValueError("config 'release' block is not a mapping")
    return block


def require_attestation_setting(*, config: Path | str | None = None) -> bool:
    """``release.require_attestation``; default :data:`RELEASE_REQUIRE_ATTESTATION_DEFAULT`.

    Scoped by :func:`require_attestation_for_labels`: setting it false restores
    today's behaviour only for batches that claim no RLVR labels.
    """

    value = _release_settings(config).get(
        "require_attestation", RELEASE_REQUIRE_ATTESTATION_DEFAULT
    )
    if not isinstance(value, bool):
        raise ValueError(
            "release.require_attestation must be a boolean, not "
            f"{type(value).__name__} (fail closed)"
        )
    return value


# ---------------------------------------------------------------------------
# What counts as a batch that claims RLVR labels
# ---------------------------------------------------------------------------


def _field(manifest: Any, name: str) -> Any:
    """Read one field from a manifest-like object (attribute or mapping).

    The same duck-typed reader ``export/release.py`` uses for report rows, so
    a ``ReleaseManifest``, a parsed ``manifest.json`` mapping and a test double
    are all read identically.
    """

    if isinstance(manifest, Mapping):
        return manifest.get(name)
    return getattr(manifest, name, None)


def batch_claims_rlvr_labels(batch_manifest: Any) -> bool:
    """Return whether a batch ships graded EL/T artifacts.

    Check the explicit flag, variant records, then the corpus profile. Treat
    unknown shapes as labelled.
    """

    if batch_manifest is None:
        raise AttestationRefusal(
            "no batch manifest was supplied", code="batch_manifest_invalid"
        )
    explicit = _field(batch_manifest, "claims_rlvr_labels")
    if explicit is not None and not isinstance(explicit, bool):
        raise AttestationRefusal(
            "batch manifest claims_rlvr_labels is not a boolean",
            code="batch_manifest_invalid",
        )

    acceptance = _field(batch_manifest, "variant_acceptance")
    has_acceptance = isinstance(acceptance, Mapping) and any(
        bool(records) for records in acceptance.values()
    )

    variants = _field(batch_manifest, "variants")
    ships_rlvr_variant = False
    if isinstance(variants, Mapping):
        for shipped in variants.values():
            if isinstance(shipped, (list, tuple)) and (
                _RLVR_VARIANT_VALUES & {str(value) for value in shipped}
            ):
                ships_rlvr_variant = True
                break

    profile = _field(batch_manifest, "corpus_profile")
    profile_claims: bool | None = None
    if isinstance(profile, str) and profile:
        if profile == _RLVR_CORPUS_PROFILE:
            profile_claims = True
        elif profile == _LEGACY_CORPUS_PROFILE:
            profile_claims = False

    evidence = has_acceptance or ships_rlvr_variant or profile_claims is True

    # Signals move only in the fail-closed direction: explicit false cannot
    # override contradictory RLVR evidence, which is refused rather than downgraded.
    if explicit is False and evidence:
        raise AttestationRefusal(
            "batch manifest declares claims_rlvr_labels: false while shipping "
            "RLVR evidence ("
            + ", ".join(
                name
                for name, present in (
                    ("variant_acceptance", has_acceptance),
                    ("RLVR variants", ships_rlvr_variant),
                    (f"corpus_profile {profile!r}", profile_claims is True),
                )
                if present
            )
            + "); the declaration cannot override the batch's own contents",
            code="batch_manifest_invalid",
        )
    if explicit is not None:
        return explicit or evidence
    if evidence:
        return True
    if profile_claims is False:
        return False
    # Unknown shape or unknown profile: fail closed.
    return True


# ---------------------------------------------------------------------------
# Verification helper
# ---------------------------------------------------------------------------


def recompute_attestation_digest(attestation: SandboxAttestation) -> str:
    """Re-derive the seal of one sandbox attestation (for ``verify_release``).

    Delegates to ``runtime.attestation.sandbox_attestation_digest`` so writer
    and verifier can never drift apart, and raises a typed refusal rather than
    an opaque error when the record itself is not a valid attestation.
    """

    if not isinstance(attestation, SandboxAttestation):
        raise AttestationRefusal(
            "sandbox attestation object is invalid", code="attestation_invalid"
        )
    try:
        return sandbox_attestation_digest(attestation)
    except SandboxAttestationError as exc:
        raise AttestationRefusal(str(exc), code="attestation_invalid") from exc


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _verified(attestation: SandboxAttestation) -> SandboxAttestation:
    """Re-parse, re-seal-check and secret-scan, mapping faults to refusals."""

    try:
        return verify_sandbox_attestation(attestation)
    except SandboxAttestationError as exc:
        code = getattr(exc, "code", "attestation_invalid")
        if code not in REFUSAL_CODES:
            code = "attestation_invalid"
        raise AttestationRefusal(str(exc), code=code) from exc


def _attestation_age_s(attestation: SandboxAttestation, now: str) -> float | None:
    """Seconds between the attestation's `attested_at` and `now`, or None when
    either is unreadable. Both are ISO-8601 UTC, second resolution."""
    from datetime import datetime

    def parse(text: str):
        try:
            return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            return None

    stamped = parse(str(attestation.attested_at or ""))
    current = parse(str(now or ""))
    if stamped is None or current is None:
        return None
    return (current - stamped).total_seconds()


def require_attestation_for_labels(
    batch_manifest: Any,
    attestation: SandboxAttestation | None,
    *,
    contamination_mode: Any,
    config: Path | str | None = None,
    agents_config: Path | str | None | object = _ACTIVE_AGENTS_CONFIG_NOT_SUPPLIED,
    run_id: str = "",
    now: str | None = None,
    max_age_s: float | None = MAX_ATTESTATION_AGE_S,
) -> AttestationGateResult:
    """Refuse to let a label-claiming batch ship without attested isolation.

    Raises :class:`AttestationRefusal` (a typed, coded refusal) and otherwise
    returns the report-only decision. ``contamination_mode`` is the mode the
    caller is running under — a ``verification.contamination.Enforcement`` or
    its string value.
    """

    mode = getattr(contamination_mode, "value", contamination_mode)
    mode = "" if mode is None else str(mode)
    claims = batch_claims_rlvr_labels(batch_manifest)

    if not claims:
        required = require_attestation_setting(config=config)
        if not required:
            return AttestationGateResult(
                claims_rlvr_labels=False,
                required=False,
                contamination_mode=mode,
                reason=(
                    "batch claims no RLVR labels and release.require_attestation "
                    "is false"
                ),
            )
        if attestation is None:
            raise AttestationRefusal(
                "release.require_attestation is true and this batch has no "
                "sandbox attestation",
                code="attestation_missing",
            )
    elif attestation is None:
        # The scoping: a label-claiming batch can never skip the gate, whatever
        # release.require_attestation says.
        raise AttestationRefusal(
            "this batch claims RLVR labels and has no sandbox attestation; "
            "release.require_attestation cannot exempt a label-claiming batch",
            code="attestation_missing",
        )

    verified = _verified(attestation)
    if claims and agents_config is not _ACTIVE_AGENTS_CONFIG_NOT_SUPPLIED:
        try:
            verified = assert_attestation_matches_agents_config(
                verified,
                agents_config=agents_config,  # type: ignore[arg-type]
            )
        except SandboxAttestationError as exc:
            code = getattr(exc, "code", "attestation_invalid")
            if code not in REFUSAL_CODES:
                code = "attestation_invalid"
            raise AttestationRefusal(str(exc), code=code) from exc

    if mode not in CONTAMINATION_MODES:
        raise AttestationRefusal(
            f"contamination mode {mode!r} is not one of "
            f"{sorted(CONTAMINATION_MODES)}",
            code="contamination_mode_unknown",
        )
    if verified.contamination_mode != mode:
        raise AttestationRefusal(
            "the running contamination mode "
            f"{mode!r} differs from the attested mode "
            f"{verified.contamination_mode!r}",
            code="contamination_mode_mismatch",
        )

    if not claims:
        # Tier, enforcement, and mount checks apply only to labelled batches.
        return AttestationGateResult(
            claims_rlvr_labels=False,
            required=True,
            tier=verified.tier,
            attestation_digest=verified.attestation_digest,
            contamination_mode=mode,
            reason=(
                f"batch claims no RLVR labels; isolation recorded at tier "
                f"{verified.tier} (host class {verified.host_class})"
            ),
        )

    # Schema 1.1 remains readable for historical unlabelled/development
    # records, but it did not seal the sandbox pin or agents-config identity.
    # Such a record cannot be evidence for a labelled run.
    if verified.attestation_schema_version != SANDBOX_ATTESTATION_SCHEMA_VERSION:
        raise AttestationRefusal(
            "legacy sandbox attestation does not bind its configured sandbox "
            "pin and agents configuration; re-mint it before claiming labels",
            code="attestation_config_unbound",
        )

    if mode != REQUIRED_CONTAMINATION_MODE:
        raise AttestationRefusal(
            f"contamination enforcement is {mode!r}; a batch claiming RLVR "
            f"labels requires {REQUIRED_CONTAMINATION_MODE!r} (observe and off "
            "record overlaps without refusing any)",
            code="contamination_mode_refused",
        )
    if verified.tier not in ATTESTING_TIERS:
        raise AttestationRefusal(
            f"sandbox tier {verified.tier!r} (host class "
            f"{verified.host_class!r}) cannot back RLVR labels; "
            f"tiers {sorted(ATTESTING_TIERS)} are attesting, and tier C "
            "(plain runc) and tier D (macOS, Docker Desktop, the dev-only "
            "laptop lane) are refused",
            code="attestation_tier_refused",
        )
    if not verified.mount_scanned:
        raise AttestationRefusal(
            "the attestation records no mount scan, so its "
            "credential_files_in_mount == 0 is not evidence of a clean mount "
            "(A22 control 5); re-mint it with the mount the labelled run "
            "executes in",
            code="mount_not_scanned",
        )
    if verified.credential_files_in_mount:
        raise AttestationRefusal(
            f"{verified.credential_files_in_mount} credential-shaped file(s) "
            "were visible in the attested mount; a label-claiming batch "
            "requires credential_files_in_mount == 0",
            code="credential_files_in_mount",
        )
    if verified.tier == "B" and not (verified.runtime.strip() and verified.cgroup.strip()):
        # Tier B declarations require observed runtime and cgroup evidence.
        raise AttestationRefusal(
            f"tier B rests on the operator's host-class declaration "
            f"({verified.host_class!r}) and this record carries no "
            f"corroborating isolation evidence (runtime "
            f"{verified.runtime!r}, cgroup {verified.cgroup!r})",
            code="attestation_tier_uncorroborated",
        )
    # Bind the attestation to this run and enforce its age below.
    if run_id:
        if verified.run_id and verified.run_id != run_id:
            raise AttestationRefusal(
                f"the attestation was minted for run {verified.run_id!r}, not "
                f"{run_id!r}; an isolation record is evidence about the run it "
                "was taken during",
                code="attestation_run_mismatch",
            )
        if not verified.run_id:
            raise AttestationRefusal(
                f"this batch names run {run_id!r} and the attestation names no "
                "run at all, so it cannot be bound to the batch it is "
                "admitting",
                code="attestation_run_mismatch",
            )
    if max_age_s is not None:
        stamped = str(verified.attested_at or "")
        if not stamped:
            raise AttestationRefusal(
                "the attestation carries no attested_at timestamp, so its age "
                "cannot be checked; a stale record must be a typed refusal, "
                "not a silent pass",
                code="attestation_stale",
            )
        age = _attestation_age_s(verified, _now_utc() if now is None else str(now))
        if age is None:
            raise AttestationRefusal(
                f"the attestation timestamp {stamped!r} is unreadable",
                code="attestation_stale",
            )
        if age < 0:
            raise AttestationRefusal(
                f"the attestation timestamp {stamped!r} is in the future; "
                "future-dated isolation evidence is not fresh evidence about "
                "the current run",
                code="attestation_stale",
            )
        if age > float(max_age_s):
            raise AttestationRefusal(
                f"the attestation was taken at {stamped} "
                f"({age / 86400.0:.1f} days ago); a batch claiming RLVR labels "
                f"requires isolation observed within "
                f"{float(max_age_s) / 86400.0:.0f} days",
                code="attestation_stale",
            )

    return AttestationGateResult(
        claims_rlvr_labels=claims,
        required=True,
        tier=verified.tier,
        attestation_digest=verified.attestation_digest,
        contamination_mode=mode,
        reason=(
            f"attested tier {verified.tier} host class {verified.host_class} "
            f"under contamination mode {mode}"
        ),
    )


def _now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "AttestationGateResult",
    "AttestationRefusal",
    "MAX_ATTESTATION_AGE_S",
    "REFUSAL_CODES",
    "RELEASE_REQUIRE_ATTESTATION_DEFAULT",
    "REQUIRED_CONTAMINATION_MODE",
    "batch_claims_rlvr_labels",
    "recompute_attestation_digest",
    "require_attestation_for_labels",
    "require_attestation_setting",
]

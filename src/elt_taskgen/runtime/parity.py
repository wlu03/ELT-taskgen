"""Measure agreement between local DuckDB scoring and real runtimes.

The battery runs differential, mutation, metamorphic, and sampled warehouse
checks through injected runners. Runtime faults are excluded from the rate.
Disagreements quarantine tasks but do not change labels.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elt_taskgen.destinations import Destination
from elt_taskgen.models import (
    MartSpec,
    Origin,
    PopulationName,
    Row,
    canonical_json,
    readable_json,
    sha256_hex,
)
from elt_taskgen.review.metrology import wilson_interval
from elt_taskgen.runtime.attestation import (
    SandboxAttestation,
    verify_sandbox_attestation,
)
from elt_taskgen.verification import upstream_eval


# Versioned parity schema, pins, and thresholds.

#: Schema of ``reports/parity_<arm>.json``.
PARITY_REPORT_SCHEMA_VERSION = "1.0"

#: The battery's own recipe version: bumping it invalidates old comparisons.
PARITY_BATTERY_VERSION = "1"

#: One-sided confidence levels the report always carries.
DEFAULT_CONFIDENCE = 0.85
SECONDARY_CONFIDENCE = 0.95

#: Adoption threshold (Output 12 §8): agreement lower bound at least 0.90.
AGREEMENT_LOWER_BOUND_THRESHOLD = 0.90

#: Stratification floors for one arm's sample.
MIN_LOCALLY_ACCEPTED_PER_ARM = 20
MIN_LOCALLY_REJECTED_PER_ARM = 10
MIN_ARTIFACTS_PER_SOURCE_POOL = 4

#: Supported ``terraform validate -json`` format for Terraform 1.15.8.
TERRAFORM_VALIDATE_FORMAT_VERSION = "1.0"

#: The Terraform the battery is pinned against.  ``validate`` runs offline via a
#: ``filesystem_mirror`` provider source with ``-backend=false``.
TERRAFORM_VERSION_PIN = "1.15.8"

#: Parser version whose output shape the battery validates.
PYTHON_HCL2_PIN = "7.3.1"

#: Exactly the top-level keys ``terraform validate -json`` format 1.0 defines.
_TERRAFORM_VALIDATE_KEYS = frozenset(
    {"format_version", "valid", "error_count", "warning_count", "diagnostics"}
)
_TERRAFORM_DIAGNOSTIC_SEVERITIES = ("error", "warning")
_MAX_TERRAFORM_VALIDATE_BYTES = 4 * 1024 * 1024
_MAX_VERIFY_PAYLOAD_BYTES = 1 * 1024 * 1024

_ARM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_DESTINATIONS = frozenset(item.value for item in Destination)
_SOURCE_POOLS = frozenset(item.value for item in Origin)
_POPULATIONS = frozenset(item.value for item in PopulationName)


class ParityBatteryError(RuntimeError):
    """The battery refuses to produce a claim it cannot stand behind."""


class ParityRuntimeFault(RuntimeError):
    """An injected runner could not measure this artifact (C7).

    Raised by (or on behalf of) a destination/Terraform runner.  It becomes a
    verdict with ``reward=None`` that leaves the denominator; it is never a
    disagreement and never a task rejection.
    """

    def __init__(self, code: str) -> None:
        if code not in PARITY_FAULT_CODES:
            raise ParityBatteryError(f"unknown parity fault code: {code!r}")
        super().__init__(code)
        self.code = code


#: The closed fault vocabulary.  Stable codes only — a fault record carries no
#: warehouse error text, no counts and no paths.
PARITY_FAULT_CODES: tuple[str, ...] = (
    "connection_error",
    "credential_unavailable",
    "harness_internal",
    "runner_crash",
    "runner_exit_2",
    "runner_timeout",
    "unparseable_receipt",
)


class ReplayStage(str, Enum):
    """Which ``runtime verify-*`` verb judges this artifact."""

    STAGE1 = "stage1"
    STAGE2 = "stage2"
    END_TO_END = "end_to_end"


#: The CLI verbs this battery drives.  ``cli.py`` owns them; the battery only
#: composes argv and reads the printed receipt.
RUNTIME_VERIFY_VERBS: dict[ReplayStage, str] = {
    ReplayStage.STAGE1: "verify-stage1",
    ReplayStage.STAGE2: "verify-stage2",
    ReplayStage.END_TO_END: "verify-end-to-end",
}

#: The ``stage`` label each verb prints in its JSON receipt.
VERIFY_RECEIPT_STAGE: dict[ReplayStage, str] = {
    ReplayStage.STAGE1: "el",
    ReplayStage.STAGE2: "t",
    ReplayStage.END_TO_END: "end_to_end",
}


# --- Verdicts ---------------------------------------------------------------

class ArtifactVerdict(BaseModel):
    """One artifact judged by both arms (or judged by one and faulted on the other).

    ``local_accepted`` is the DuckDB proxy's verdict; ``real_accepted`` is the
    real stack's.  ``real_accepted is None`` means the real arm could not
    measure: ``reward`` is then None and ``fault_code`` names the closed reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=200)
    arm: str = Field(min_length=1, max_length=64)
    destination: str = Field(min_length=1, max_length=32)
    source_pool: str = Field(min_length=1, max_length=32)
    population: str = Field(min_length=1, max_length=64)
    stage: ReplayStage
    local_accepted: bool
    real_accepted: bool | None = None
    reward: float | None = None
    fault_code: str = ""

    @field_validator("destination")
    @classmethod
    def _known_destination(cls, value: str) -> str:
        if value not in _DESTINATIONS:
            raise ValueError(f"unknown destination: {value!r}")
        return value

    @field_validator("source_pool")
    @classmethod
    def _known_source_pool(cls, value: str) -> str:
        if value not in _SOURCE_POOLS:
            raise ValueError(f"unknown source pool: {value!r}")
        return value

    @field_validator("population")
    @classmethod
    def _known_population(cls, value: str) -> str:
        if value not in _POPULATIONS:
            raise ValueError(f"unknown population: {value!r}")
        return value

    @field_validator("arm")
    @classmethod
    def _filesystem_safe_arm(cls, value: str) -> str:
        if not _ARM_NAME.match(value):
            raise ValueError(f"arm names the report file, so it must be simple: {value!r}")
        return value

    @model_validator(mode="after")
    def _fault_is_reward_none(self) -> "ArtifactVerdict":
        # Faults have a code and no reward; measured verdicts have the reverse.
        if self.real_accepted is None:
            if self.reward is not None:
                raise ValueError("a real-runtime fault carries reward None")
            if self.fault_code not in PARITY_FAULT_CODES:
                raise ValueError("a real-runtime fault must name a known fault code")
            return self
        if self.fault_code:
            raise ValueError("a measured verdict carries no fault code")
        if self.reward is None:
            raise ValueError("a measured verdict carries a reward")
        if not 0.0 <= float(self.reward) <= 1.0:
            raise ValueError("reward is a fraction in [0, 1]")
        if math.isnan(float(self.reward)):
            raise ValueError("reward is not a number")
        return self

    @property
    def measured(self) -> bool:
        """True when the real arm returned a verdict at all."""
        return self.real_accepted is not None

    @property
    def disagreement(self) -> bool:
        """Local accept, real reject — the quantity ``pi_c`` counts."""
        return self.local_accepted and self.real_accepted is False


class PopulationSet(BaseModel):
    """THE named set a parity claim ranges over.

    A rate without a stated population is not a measurement.  This record names
    the frame explicitly and digests its membership, so two reports can be told
    apart even when they print the same number.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=900)
    arm: str = Field(min_length=1, max_length=64)
    destinations: tuple[str, ...]
    source_pools: tuple[str, ...]
    populations: tuple[str, ...]
    stages: tuple[str, ...]
    #: Artifacts actually judged by the real arm; this is the agreement bound's
    #: denominator and excludes runtime faults.
    artifact_ids: tuple[str, ...]
    size: int = Field(ge=0)
    #: Locally accepted artifacts excluded by real-runtime faults.
    unmeasured_artifact_ids: tuple[str, ...] = ()
    frame_digest: str

    @model_validator(mode="after")
    def _canonical(self) -> "PopulationSet":
        if not self.populations:
            raise ValueError("a named population set names at least one population")
        for label, values in (
            ("destinations", self.destinations),
            ("source_pools", self.source_pools),
            ("populations", self.populations),
            ("stages", self.stages),
            ("artifact_ids", self.artifact_ids),
            ("unmeasured_artifact_ids", self.unmeasured_artifact_ids),
        ):
            if list(values) != sorted(set(values)):
                raise ValueError(f"{label} must be sorted and unique")
        if self.size != len(self.artifact_ids):
            raise ValueError("size must count the enumerated members")
        if set(self.artifact_ids) & set(self.unmeasured_artifact_ids):
            raise ValueError("an artifact is measured or unmeasured, never both")
        if not _SHA256.match(self.frame_digest):
            raise ValueError("frame_digest must be a sha256 hex digest")
        return self


class ParityReport(BaseModel):
    """``reports/parity_<arm>.json`` — one arm's parity claim and its bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parity_report_schema_version: str = PARITY_REPORT_SCHEMA_VERSION
    parity_battery_version: str = PARITY_BATTERY_VERSION
    arm: str = Field(min_length=1, max_length=64)
    population_set: PopulationSet
    #: The sentence a reader is allowed to quote.  It names the set.
    claim: str = Field(min_length=1)

    confidence: float
    #: Locally accepted AND judged by the real arm: the pi_c denominator.
    denominator: int = Field(ge=0)
    #: Locally accepted, really rejected.
    disagreements: int = Field(ge=0)
    #: Locally accepted, really accepted.
    agreements: int = Field(ge=0)
    #: Locally accepted but unmeasurable: reward None, OUT of the denominator.
    runtime_faults: int = Field(ge=0)
    fault_codes: dict[str, int]

    pi_c: float | None
    pi_c_upper: float
    pi_c_upper_85: float
    pi_c_upper_95: float
    agreement_lower: float
    agreement_lower_85: float
    agreement_lower_95: float

    #: McNemar statistics over artifacts judged by both arms. Runtime faults
    #: have no paired outcome and are excluded.
    mcnemar_pairs: int = Field(ge=0)
    mcnemar_local_only: int = Field(ge=0)
    mcnemar_real_only: int = Field(ge=0)
    mcnemar_mid_p: float

    locally_accepted: int = Field(ge=0)
    locally_rejected: int = Field(ge=0)
    sizing_shortfalls: tuple[str, ...] = ()
    #: Local-accept / real-reject artifacts.  These quarantine the TASK; they
    #: never change a training label (Output 12 §8 rollback row).
    quarantine_artifact_ids: tuple[str, ...] = ()
    adopted: bool = False

    #: Digest of the real arm's sandbox attestation. Empty is valid only for
    #: offline checks, never for a real-warehouse claim.
    sandbox_attestation_digest: str = ""
    #: Digest of the canonical report with this field blanked.
    report_digest: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> "ParityReport":
        if self.arm != self.population_set.arm:
            raise ValueError("the report and its population set name different arms")
        if self.agreements + self.disagreements != self.denominator:
            raise ValueError("denominator must count exactly the measured verdicts")
        # The population set must match the denominator and excluded faults.
        if self.population_set.size != self.denominator:
            raise ValueError(
                "the named population set must enumerate exactly the "
                "denominator it is quoted beside"
            )
        if len(self.population_set.unmeasured_artifact_ids) != self.runtime_faults:
            raise ValueError(
                "the unmeasured members must account for every runtime fault"
            )
        if self.population_set.name not in self.claim:
            raise ValueError("the claim must name its population set")
        if sum(self.fault_codes.values()) != self.runtime_faults:
            raise ValueError("fault codes must account for every recorded fault")
        return self


# --- The rate ---------------------------------------------------------------

def mcnemar_mid_p(local_only: int, real_only: int) -> float:
    """Two-sided McNemar mid-p over the discordant pairs.

    The exact conditional test is Binomial(b + c, 1/2) on the discordant cells;
    the mid-p variant subtracts half the point mass at the observed value, which
    removes most of the conservatism of the exact test without the normal
    approximation's small-sample failure.  No discordance is p = 1.0.
    """
    if local_only < 0 or real_only < 0:
        raise ParityBatteryError("discordant counts are non-negative")
    n = local_only + real_only
    if n == 0:
        return 1.0
    k = min(local_only, real_only)
    total = float(1 << n) if n < 1024 else math.inf
    if math.isinf(total):  # pragma: no cover - defensive, samples are small
        raise ParityBatteryError("discordant pair count is out of range")
    below = sum(math.comb(n, i) for i in range(0, k))
    at = math.comb(n, k)
    mid = 2.0 * ((below + 0.5 * at) / total)
    return min(max(mid, 0.0), 1.0)


def stratification_shortfalls(
    verdicts: "Sequence[ArtifactVerdict | ReplaySpecimen]",
) -> tuple[str, ...]:
    """Return sorted sample-size shortfalls that block adoption.

    The check uses only arm, local acceptance, and source pool, so it can run
    before replay.
    """
    shortfalls: list[str] = []
    arms = sorted({verdict.arm for verdict in verdicts})
    for arm in arms:
        rows = [verdict for verdict in verdicts if verdict.arm == arm]
        accepted = sum(1 for verdict in rows if verdict.local_accepted)
        rejected = len(rows) - accepted
        if accepted < MIN_LOCALLY_ACCEPTED_PER_ARM:
            shortfalls.append(
                f"accepted_below_minimum:{arm}:{accepted}/{MIN_LOCALLY_ACCEPTED_PER_ARM}"
            )
        if rejected < MIN_LOCALLY_REJECTED_PER_ARM:
            shortfalls.append(
                f"rejected_below_minimum:{arm}:{rejected}/{MIN_LOCALLY_REJECTED_PER_ARM}"
            )
    pools = sorted({verdict.source_pool for verdict in verdicts})
    for pool in pools:
        count = sum(1 for verdict in verdicts if verdict.source_pool == pool)
        if count < MIN_ARTIFACTS_PER_SOURCE_POOL:
            shortfalls.append(
                f"source_pool_below_minimum:{pool}:{count}/{MIN_ARTIFACTS_PER_SOURCE_POOL}"
            )
    return tuple(sorted(shortfalls))


def _named_population_set(
    verdicts: Sequence[ArtifactVerdict],
    conditioning: Sequence[ArtifactVerdict],
    measured: Sequence[ArtifactVerdict],
    *,
    name: str,
) -> PopulationSet:
    """Describe the measured population used as the parity denominator.

    List locally accepted but unmeasured artifacts separately. A supplied name
    is appended as an annotation and cannot replace the derived scope.
    """
    arm = conditioning[0].arm
    destinations = tuple(sorted({row.destination for row in conditioning}))
    pools = tuple(sorted({row.source_pool for row in conditioning}))
    populations = tuple(sorted({row.population for row in conditioning}))
    stages = tuple(sorted({row.stage.value for row in conditioning}))
    ids = tuple(sorted({row.artifact_id for row in measured}))
    unmeasured = tuple(
        sorted({row.artifact_id for row in conditioning} - set(ids))
    )
    derived = (
        f"arm={arm}"
        f";destinations={'+'.join(destinations)}"
        f";populations={'+'.join(populations)}"
        f";pools={'+'.join(pools)}"
        f";stages={'+'.join(stages)}"
        f";n={len(ids)}"
        f";unmeasured={len(unmeasured)}"
    )
    annotation = " ".join(str(name).split())[:400]
    frame_digest = sha256_hex(
        canonical_json(
            {
                "arm": arm,
                "artifact_ids": list(ids),
                "battery_version": PARITY_BATTERY_VERSION,
                "destinations": list(destinations),
                "populations": list(populations),
                "sample_size": len(verdicts),
                "source_pools": list(pools),
                "stages": list(stages),
                "unmeasured_artifact_ids": list(unmeasured),
            }
        )
    )
    return PopulationSet(
        name=f"{derived} ({annotation})" if annotation else derived,
        arm=arm,
        destinations=destinations,
        source_pools=pools,
        populations=populations,
        stages=stages,
        artifact_ids=ids,
        size=len(ids),
        unmeasured_artifact_ids=unmeasured,
        frame_digest=frame_digest,
    )


def parity_rate(
    accepted: Sequence[ArtifactVerdict],
    *,
    confidence: float = DEFAULT_CONFIDENCE,
    population_set_name: str = "",
    attestation: SandboxAttestation | None = None,
) -> ParityReport:
    """Compute ``P(real disagrees | local accepted)`` and its bounds.

    Exclude locally rejected artifacts and unmeasured runtime faults from the
    denominator. Verify the sandbox attestation before recording its digest.
    """
    rows = tuple(accepted)
    if not rows:
        raise ParityBatteryError(
            "a parity claim over zero artifacts names no population set"
        )
    if not 0.5 < float(confidence) < 1.0:
        raise ParityBatteryError("confidence must lie strictly between 0.5 and 1.0")
    attestation_digest = ""
    if attestation is not None:
        try:
            attestation_digest = verify_sandbox_attestation(
                attestation
            ).attestation_digest
        except Exception as exc:  # SandboxAttestationError and its kin
            raise ParityBatteryError(
                f"the sandbox attestation this claim would name is invalid: {exc}"
            ) from exc
    ids = [row.artifact_id for row in rows]
    if len(set(ids)) != len(ids):
        raise ParityBatteryError("every artifact appears at most once in a sample")
    arms = {row.arm for row in rows}
    if len(arms) != 1:
        raise ParityBatteryError(
            "one report covers one arm; split the sample by arm before reporting"
        )
    arm = arms.pop()

    conditioning = [row for row in rows if row.local_accepted]
    if not conditioning:
        raise ParityBatteryError(
            "pi_c is conditional on local acceptance; this sample accepts nothing"
        )
    faults = [row for row in conditioning if not row.measured]
    judged = [row for row in conditioning if row.measured]
    disagreements = [row for row in judged if row.disagreement]
    denominator = len(judged)
    agreements = denominator - len(disagreements)

    pi_c = (len(disagreements) / denominator) if denominator else None
    pi_c_upper = wilson_interval(len(disagreements), denominator, confidence)[1]
    pi_c_upper_85 = wilson_interval(
        len(disagreements), denominator, DEFAULT_CONFIDENCE
    )[1]
    pi_c_upper_95 = wilson_interval(
        len(disagreements), denominator, SECONDARY_CONFIDENCE
    )[1]
    agreement_lower = wilson_interval(agreements, denominator, confidence)[0]
    agreement_lower_85 = wilson_interval(
        agreements, denominator, DEFAULT_CONFIDENCE
    )[0]
    agreement_lower_95 = wilson_interval(
        agreements, denominator, SECONDARY_CONFIDENCE
    )[0]

    paired = [row for row in rows if row.measured]
    local_only = sum(
        1 for row in paired if row.local_accepted and row.real_accepted is False
    )
    real_only = sum(
        1 for row in paired if not row.local_accepted and row.real_accepted is True
    )

    fault_codes: dict[str, int] = {}
    for row in faults:
        fault_codes[row.fault_code] = fault_codes.get(row.fault_code, 0) + 1

    population_set = _named_population_set(
        rows, conditioning, judged, name=population_set_name
    )
    # Apply the stratification floor to measured evidence after runtime faults,
    # not only to the originally drawn sample.
    shortfalls = list(stratification_shortfalls(rows))
    if denominator < MIN_LOCALLY_ACCEPTED_PER_ARM:
        shortfalls.append(
            f"denominator_below_minimum:{arm}:{denominator}/"
            f"{MIN_LOCALLY_ACCEPTED_PER_ARM}"
        )
    shortfalls = tuple(sorted(shortfalls))
    adopted = (
        agreement_lower >= AGREEMENT_LOWER_BOUND_THRESHOLD
        and not shortfalls
        and denominator > 0
    )
    rate_text = "undefined (no measured verdict)" if pi_c is None else f"{pi_c:.4f}"
    claim = (
        f"pi_c = P(real disagrees | local accepted) = {rate_text} over the named "
        f"population set '{population_set.name}' "
        f"[frame {population_set.frame_digest[:12]}]: {denominator} locally "
        f"accepted artifacts judged by both arms, {len(faults)} real-runtime "
        f"fault(s) recorded as reward None and excluded from the denominator. "
        f"One-sided Wilson upper bound {pi_c_upper_85:.4f} at 85% and "
        f"{pi_c_upper_95:.4f} at 95%; agreement lower bound "
        f"{agreement_lower:.4f} at {confidence:.2f} "
        f"(adoption threshold {AGREEMENT_LOWER_BOUND_THRESHOLD:.2f})."
    )
    return ParityReport(
        arm=arm,
        population_set=population_set,
        claim=claim,
        confidence=float(confidence),
        denominator=denominator,
        disagreements=len(disagreements),
        agreements=agreements,
        runtime_faults=len(faults),
        fault_codes=dict(sorted(fault_codes.items())),
        pi_c=pi_c,
        pi_c_upper=pi_c_upper,
        pi_c_upper_85=pi_c_upper_85,
        pi_c_upper_95=pi_c_upper_95,
        agreement_lower=agreement_lower,
        agreement_lower_85=agreement_lower_85,
        agreement_lower_95=agreement_lower_95,
        mcnemar_pairs=len(paired),
        mcnemar_local_only=local_only,
        mcnemar_real_only=real_only,
        mcnemar_mid_p=mcnemar_mid_p(local_only, real_only),
        locally_accepted=len(conditioning),
        locally_rejected=len(rows) - len(conditioning),
        sizing_shortfalls=shortfalls,
        quarantine_artifact_ids=tuple(
            sorted(row.artifact_id for row in disagreements)
        ),
        adopted=adopted,
        sandbox_attestation_digest=attestation_digest,
    )


#: ``reports/parity_<arm>.json``.
PARITY_REPORT_FILENAME = "parity_{arm}.json"


def parity_report_path(reports_dir: Path | str, arm: str) -> Path:
    if not _ARM_NAME.match(arm):
        raise ParityBatteryError(f"arm names the report file: {arm!r}")
    return Path(reports_dir) / PARITY_REPORT_FILENAME.format(arm=arm)


def write_parity_report(report: ParityReport, reports_dir: Path | str) -> Path:
    """Publish one arm's report, SEALED.  Readable on disk, canonical hashed."""
    sealed = seal_parity_report(report)
    path = parity_report_path(reports_dir, sealed.arm)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        readable_json(sealed.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    return path


def parity_report_digest(report: ParityReport) -> str:
    """sha256 over the canonical report with ``report_digest`` blanked.

    Blanking is what makes the digest of a sealed report equal the digest of
    the unsealed one it came from, so a reader can re-derive the seal without
    knowing which it holds.
    """
    payload = report.model_dump(mode="json")
    payload["report_digest"] = ""
    return sha256_hex(canonical_json(payload))


def seal_parity_report(report: ParityReport) -> ParityReport:
    """Checksum-bind the report: any later edit is detectable."""
    return report.model_copy(update={"report_digest": parity_report_digest(report)})


def verify_parity_report(report: ParityReport) -> ParityReport:
    """Re-derive the seal, fail closed.  ``model_copy`` skips validation, so
    the record is reparsed before it is re-hashed."""
    try:
        reparsed = ParityReport.model_validate(report.model_dump(mode="python"))
    except ValueError as exc:
        raise ParityBatteryError(f"parity report is invalid: {exc}") from exc
    if not reparsed.report_digest:
        raise ParityBatteryError("parity report was never sealed (no digest)")
    if reparsed.report_digest != parity_report_digest(reparsed):
        raise ParityBatteryError(
            "parity report digest mismatch: the report was modified after sealing"
        )
    return reparsed


# --- Component 1: differential replay ---------------------------------------

class TerraformValidateResult(BaseModel):
    """One strictly parsed ``terraform validate -json`` document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: str
    valid: bool
    error_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    diagnostic_severities: tuple[str, ...] = ()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _strict_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParityBatteryError(f"terraform validate {label} must be an integer")
    if value < 0:
        raise ParityBatteryError(f"terraform validate {label} must not be negative")
    return value


def parse_terraform_validate_json(payload: str | bytes) -> TerraformValidateResult:
    """Parse ``terraform validate -json`` format 1.0, or refuse.

    Require the exact version and key roster, reject duplicate keys, and verify
    that counts agree with their diagnostics.
    """
    if isinstance(payload, bytes):
        if len(payload) > _MAX_TERRAFORM_VALIDATE_BYTES:
            raise ParityBatteryError("terraform validate output exceeds its bound")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParityBatteryError("terraform validate output is not UTF-8") from exc
    else:
        text = str(payload)
        if len(text.encode("utf-8")) > _MAX_TERRAFORM_VALIDATE_BYTES:
            raise ParityBatteryError("terraform validate output exceeds its bound")
    try:
        document = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError) as exc:
        raise ParityBatteryError(f"terraform validate output is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ParityBatteryError("terraform validate output is not a JSON object")

    unknown = sorted(set(document) - _TERRAFORM_VALIDATE_KEYS)
    if unknown:
        raise ParityBatteryError(
            "terraform validate output carries keys format "
            f"{TERRAFORM_VALIDATE_FORMAT_VERSION} does not define: {unknown}"
        )
    version = document.get("format_version")
    if version != TERRAFORM_VALIDATE_FORMAT_VERSION:
        raise ParityBatteryError(
            "terraform validate format_version must be "
            f"{TERRAFORM_VALIDATE_FORMAT_VERSION!r}, got {version!r}"
        )
    valid = document.get("valid")
    if not isinstance(valid, bool):
        raise ParityBatteryError("terraform validate `valid` must be a boolean")
    error_count = _strict_int(document.get("error_count"), label="error_count")
    warning_count = _strict_int(document.get("warning_count"), label="warning_count")

    diagnostics = document.get("diagnostics", [])
    if not isinstance(diagnostics, list):
        raise ParityBatteryError("terraform validate `diagnostics` must be a list")
    severities: list[str] = []
    for entry in diagnostics:
        if not isinstance(entry, dict):
            raise ParityBatteryError("each terraform diagnostic must be an object")
        severity = entry.get("severity")
        if severity not in _TERRAFORM_DIAGNOSTIC_SEVERITIES:
            raise ParityBatteryError(
                f"unknown terraform diagnostic severity: {severity!r}"
            )
        severities.append(severity)
    if severities.count("error") != error_count:
        raise ParityBatteryError("terraform validate error_count contradicts diagnostics")
    if severities.count("warning") != warning_count:
        raise ParityBatteryError(
            "terraform validate warning_count contradicts diagnostics"
        )
    if valid != (error_count == 0):
        raise ParityBatteryError("terraform validate `valid` contradicts error_count")
    return TerraformValidateResult(
        format_version=version,
        valid=valid,
        error_count=error_count,
        warning_count=warning_count,
        diagnostic_severities=tuple(severities),
    )


def runtime_verify_argv(
    *,
    release: Path | str,
    task_id: str,
    stage: ReplayStage,
    destination_credential: Path | str,
    population: str = PopulationName.PRIMARY.value,
    destination: str | None = None,
    physical_container: str | None = None,
    database: str | None = None,
    certification_strict: bool = True,
    expected_repetitions: int = 1,
) -> tuple[str, ...]:
    """Build a runtime verification command for one artifact.

    Pass credential paths, not values, and omit private curator details.
    """
    if stage not in RUNTIME_VERIFY_VERBS:
        raise ParityBatteryError(f"unknown replay stage: {stage!r}")
    if population not in _POPULATIONS:
        raise ParityBatteryError(f"unknown population: {population!r}")
    if destination is not None and destination not in _DESTINATIONS:
        raise ParityBatteryError(f"unknown destination: {destination!r}")
    if isinstance(destination_credential, (dict, Mapping)):
        raise ParityBatteryError("the credential enters argv as a path, never as values")
    credential = Path(destination_credential)
    argv: list[str] = [
        "runtime",
        RUNTIME_VERIFY_VERBS[stage],
        "--release",
        str(Path(release)),
        "--task-id",
        task_id,
        "--population",
        population,
        "--destination-credential",
        str(credential),
    ]
    if destination is not None:
        argv += ["--destination", destination]
    if physical_container is not None:
        argv += ["--physical-container", physical_container]
    if database is not None:
        argv += ["--database", database]
    if certification_strict:
        argv.append("--certification-strict")
    if stage is ReplayStage.STAGE1:
        if isinstance(expected_repetitions, bool) or not isinstance(
            expected_repetitions, int
        ):
            raise ParityBatteryError("expected_repetitions must be an integer")
        if expected_repetitions < 1:
            raise ParityBatteryError("expected_repetitions must be at least 1")
        argv += ["--expected-repetitions", str(expected_repetitions)]
    return tuple(argv)


class ReplayOutcome(BaseModel):
    """What one injected runner observed for one artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: ReplayStage
    accepted: bool | None
    reward: float | None
    fault_code: str = ""

    @model_validator(mode="after")
    def _fault_is_reward_none(self) -> "ReplayOutcome":
        if self.accepted is None:
            if self.reward is not None or self.fault_code not in PARITY_FAULT_CODES:
                raise ValueError("a fault carries reward None and a known code")
            return self
        if self.fault_code:
            raise ValueError("a measured outcome carries no fault code")
        if self.reward is None or not 0.0 <= float(self.reward) <= 1.0:
            raise ValueError("a measured outcome carries a reward in [0, 1]")
        return self


def runtime_fault(stage: ReplayStage, code: str) -> ReplayOutcome:
    """Build the C7 outcome: unmeasured, reward None, named code."""
    return ReplayOutcome(stage=stage, accepted=None, reward=None, fault_code=code)


def interpret_verify_payload(
    stage: ReplayStage,
    exit_code: int,
    stdout: str | bytes,
    *,
    certification_strict: bool = True,
) -> ReplayOutcome:
    """Convert a runtime verification result into a replay outcome.

    Exit 2 is unmeasured. Exit 0 or 1 requires a matching receipt; mismatches
    are faults rather than verdicts.
    """
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ParityBatteryError("exit_code must be an integer")
    if exit_code not in (0, 1):
        return runtime_fault(stage, "runner_exit_2")
    if isinstance(stdout, bytes):
        if len(stdout) > _MAX_VERIFY_PAYLOAD_BYTES:
            return runtime_fault(stage, "unparseable_receipt")
        try:
            text = stdout.decode("utf-8")
        except UnicodeDecodeError:
            return runtime_fault(stage, "unparseable_receipt")
    else:
        text = str(stdout)
        if len(text.encode("utf-8")) > _MAX_VERIFY_PAYLOAD_BYTES:
            return runtime_fault(stage, "unparseable_receipt")
    try:
        payload = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        return runtime_fault(stage, "unparseable_receipt")
    if not isinstance(payload, dict):
        return runtime_fault(stage, "unparseable_receipt")
    if payload.get("stage") != VERIFY_RECEIPT_STAGE[stage]:
        return runtime_fault(stage, "unparseable_receipt")
    reward = payload.get("reward")
    if isinstance(reward, bool) or not isinstance(reward, (int, float)):
        return runtime_fault(stage, "unparseable_receipt")
    reward = float(reward)
    if math.isnan(reward) or not 0.0 <= reward <= 1.0:
        return runtime_fault(stage, "unparseable_receipt")
    if certification_strict:
        certified = payload.get("certification_passed")
        if not isinstance(certified, bool):
            return runtime_fault(stage, "unparseable_receipt")
        accepted = certified
    else:
        accepted = reward == 1.0
    # `cli._runtime_verify` returns 0 exactly when the verdict it printed is a
    # pass; a receipt that contradicts its own exit code was not understood.
    if accepted != (exit_code == 0):
        return runtime_fault(stage, "unparseable_receipt")
    return ReplayOutcome(
        stage=stage, accepted=accepted, reward=reward, fault_code=""
    )


@dataclass(frozen=True)
class ReplaySpecimen:
    """One sampled artifact and the local comparator's verdict on it."""

    artifact_id: str
    arm: str
    destination: str
    source_pool: str
    population: str
    stage: ReplayStage
    local_accepted: bool
    #: Where ``terraform validate`` runs, when the arm exercises it.
    terraform_dir: Path | None = None


#: The two seams the owner binds to a real stack.  Everything in this module
#: takes them as arguments; nothing here starts a process or opens a socket.
DestinationRunner = Callable[[ReplaySpecimen], ReplayOutcome]
TerraformRunner = Callable[[ReplaySpecimen], "str | bytes"]


def replay_specimen(
    specimen: ReplaySpecimen,
    *,
    destination_runner: DestinationRunner,
    terraform_runner: TerraformRunner | None = None,
) -> ArtifactVerdict:
    """Replay one artifact through the real arm and record a verdict.

    Any exception out of an injected runner is infrastructure, never evidence:
    it becomes ``reward=None`` with a named fault code.  A ``ParityRuntimeFault``
    chooses its own code; anything else is ``runner_crash``.
    """
    stage = specimen.stage
    if terraform_runner is not None and specimen.terraform_dir is not None:
        try:
            raw = terraform_runner(specimen)
        except ParityRuntimeFault as fault:
            return _verdict(specimen, runtime_fault(stage, fault.code))
        except Exception:  # noqa: BLE001 - an injected runner may fail any way
            return _verdict(specimen, runtime_fault(stage, "runner_crash"))
        try:
            validated = parse_terraform_validate_json(raw)
        except ParityBatteryError:
            return _verdict(specimen, runtime_fault(stage, "unparseable_receipt"))
        if not validated.valid:
            # A configuration the real Terraform refuses is a real rejection,
            # and no destination is touched for it.
            return _verdict(
                specimen,
                ReplayOutcome(stage=stage, accepted=False, reward=0.0, fault_code=""),
            )
    try:
        outcome = destination_runner(specimen)
    except ParityRuntimeFault as fault:
        outcome = runtime_fault(stage, fault.code)
    except Exception:  # noqa: BLE001 - an injected runner may fail any way
        outcome = runtime_fault(stage, "runner_crash")
    if not isinstance(outcome, ReplayOutcome):
        outcome = runtime_fault(stage, "unparseable_receipt")
    elif outcome.stage is not stage:
        outcome = runtime_fault(stage, "unparseable_receipt")
    return _verdict(specimen, outcome)


def _verdict(specimen: ReplaySpecimen, outcome: ReplayOutcome) -> ArtifactVerdict:
    return ArtifactVerdict(
        artifact_id=specimen.artifact_id,
        arm=specimen.arm,
        destination=specimen.destination,
        source_pool=specimen.source_pool,
        population=specimen.population,
        stage=specimen.stage,
        local_accepted=specimen.local_accepted,
        real_accepted=outcome.accepted,
        reward=outcome.reward,
        fault_code=outcome.fault_code,
    )


def differential_replay(
    specimens: Sequence[ReplaySpecimen],
    *,
    destination_runner: DestinationRunner,
    terraform_runner: TerraformRunner | None = None,
) -> tuple[ArtifactVerdict, ...]:
    """Replay a whole sample, in artifact-id order so reports are stable."""
    ordered = sorted(specimens, key=lambda item: item.artifact_id)
    return tuple(
        replay_specimen(
            specimen,
            destination_runner=destination_runner,
            terraform_runner=terraform_runner,
        )
        for specimen in ordered
    )


# --- Component 2: proxy-rule mutation ---------------------------------------

@dataclass(frozen=True)
class ComparatorRules:
    """Switchable mart-comparison rules used to define mutants.

    Default flags match ``upstream_eval.compare_mart``. Each mutant disables
    exactly one rule while reusing upstream predicates.
    """

    #: gold and actual must hold the same number of rows
    row_count: bool = True
    #: the described column roster must equal the rows' own keys
    column_roster: bool = True
    #: every gold column must exist in the actual output
    missing_column: bool = True
    #: every actual row must carry the same key set
    ragged_rows: bool = True
    #: both sides are sorted into the mart's total order before comparison
    total_order_sort: bool = True
    #: a null on one side only is a mismatch
    null_asymmetry: bool = True
    numeric_rel_tol: float = upstream_eval.REL_TOL
    numeric_abs_tol: float = upstream_eval.ABS_TOL


REFERENCE_RULES = ComparatorRules()


@dataclass(frozen=True)
class ComparatorCase:
    """One gold/actual pair the battery judges."""

    case_id: str
    mart: MartSpec
    gold_csv: str
    rows: tuple[Row, ...]
    columns: tuple[str, ...]


#: A comparator under test: gold plus an actual result, in or out.
MartComparator = Callable[[ComparatorCase], bool]


def reference_comparator(case: ComparatorCase) -> bool:
    """Run the unmodified reward comparator."""
    return upstream_eval.compare_mart(
        case.gold_csv,
        [dict(row) for row in case.rows],
        case.mart,
        actual_columns=case.columns,
    )


def _vectors_match(
    gold: list[Any], actual: list[Any], rules: ComparatorRules
) -> bool:
    """``upstream_eval._vectors_match`` with the null and tolerance rules opened."""
    if len(gold) != len(actual):
        return False
    numeric = upstream_eval._column_is_numeric(
        gold
    ) and upstream_eval._column_is_numeric(actual)
    for left, right in zip(gold, actual):
        left_null = upstream_eval._is_null(left)
        right_null = upstream_eval._is_null(right)
        if left_null and right_null:
            continue
        if left_null or right_null:
            if rules.null_asymmetry:
                return False
            continue
        if numeric:
            x = upstream_eval._numeric_value(left)
            y = upstream_eval._numeric_value(right)
            if x is None or y is None:
                return False
            if x == y:
                continue
            if not abs(x - y) <= rules.numeric_abs_tol + rules.numeric_rel_tol * abs(y):
                return False
            continue
        if isinstance(left, str) and isinstance(right, str):
            if left.strip().lower() != right.strip().lower():
                return False
        elif left != right:
            return False
    return True


def compare_mart_with_rules(case: ComparatorCase, rules: ComparatorRules) -> bool:
    """``compare_mart`` with each rule made switchable.  Defaults == the original."""
    try:
        gold_cols, gold_rows = upstream_eval.parse_canonical_csv(case.gold_csv)
    except ValueError:
        return False
    if not gold_cols:
        return False
    actual_rows = [dict(row) for row in case.rows]
    if rules.row_count and len(actual_rows) != len(gold_rows):
        return False

    if actual_rows:
        actual_cols = tuple(actual_rows[0].keys())
        if rules.column_roster and tuple(
            name.casefold() for name in case.columns
        ) != tuple(name.casefold() for name in actual_cols):
            return False
        col_set = set(actual_cols)
        if rules.ragged_rows and any(set(row) != col_set for row in actual_rows):
            return False
        actual_txt: list[dict[str, Any]] = [
            {name: upstream_eval.cell_text(row.get(name)) for name in actual_cols}
            for row in actual_rows
        ]
    else:
        # `reference_comparator` always supplies `actual_columns`, so the
        # original's gold-schema fallback (for callers that pass None) is not
        # reachable from a `ComparatorCase` and is deliberately not restated.
        actual_cols = tuple(case.columns)
        actual_txt = []

    if rules.total_order_sort:
        gold_sorted = upstream_eval.sort_rows(
            gold_rows, case.mart.key_columns, gold_cols
        )
        actual_sorted = upstream_eval.sort_rows(
            actual_txt, case.mart.key_columns, actual_cols
        )
    else:
        gold_sorted = list(gold_rows)
        actual_sorted = list(actual_txt)
    if not rules.row_count:
        # Dropping the count rule means comparing what both sides have in
        # common, which is exactly the leniency the mutant is meant to expose.
        shortest = min(len(gold_sorted), len(actual_sorted))
        gold_sorted = gold_sorted[:shortest]
        actual_sorted = actual_sorted[:shortest]

    lower = {name.lower(): name for name in actual_cols}
    for gold_col in gold_cols:
        actual_col = lower.get(gold_col.lower())
        if actual_col is None:
            if rules.missing_column:
                return False
            continue
        left = [row.get(gold_col) for row in gold_sorted]
        right = [row.get(actual_col) for row in actual_sorted]
        if not _vectors_match(left, right, rules):
            return False
    return True


def _mutant(name: str, **overrides: Any) -> tuple[str, ComparatorRules]:
    return name, replace(REFERENCE_RULES, **overrides)


#: One dropped rule each.  A battery that cannot separate these from the real
#: comparator is not measuring the comparator.
COMPARATOR_MUTANTS: dict[str, ComparatorRules] = dict(
    (
        _mutant("no_row_count_rule", row_count=False),
        _mutant("no_column_roster_rule", column_roster=False),
        _mutant("no_missing_column_rule", missing_column=False),
        _mutant("no_ragged_row_rule", ragged_rows=False),
        _mutant("no_total_order_sort_rule", total_order_sort=False),
        _mutant("no_null_asymmetry_rule", null_asymmetry=False),
        _mutant("wide_numeric_tolerance", numeric_rel_tol=1.0),
    )
)


def mutant_comparator(name: str) -> MartComparator:
    """The named mutant as a drop-in comparator (for the lane's own input)."""
    if name not in COMPARATOR_MUTANTS:
        raise ParityBatteryError(f"unknown comparator mutant: {name!r}")
    rules = COMPARATOR_MUTANTS[name]
    return lambda case: compare_mart_with_rules(case, rules)


class MutationReport(BaseModel):
    """Which weakened comparators this case set can tell apart."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_count: int = Field(ge=0)
    mutants: tuple[str, ...]
    killed: tuple[str, ...]
    survived: tuple[str, ...]
    killing_case: dict[str, str]
    mutation_score: float


def run_mutation_battery(
    cases: Sequence[ComparatorCase],
    *,
    comparator: MartComparator | None = None,
) -> MutationReport:
    """Kill every weakened comparator, or say which one survived.

    ``comparator`` is the comparator the acceptance lane is actually running.
    A mutant is killed when some case separates it from that comparator — so a
    lane running a mutated comparator cannot kill its own mutation, and the lane
    fails.
    """
    judge = comparator or reference_comparator
    ordered = tuple(cases)
    baseline = {case.case_id: bool(judge(case)) for case in ordered}
    killed: list[str] = []
    survived: list[str] = []
    killing: dict[str, str] = {}
    for name in sorted(COMPARATOR_MUTANTS):
        rules = COMPARATOR_MUTANTS[name]
        witness = ""
        for case in ordered:
            if compare_mart_with_rules(case, rules) != baseline[case.case_id]:
                witness = case.case_id
                break
        if witness:
            killed.append(name)
            killing[name] = witness
        else:
            survived.append(name)
    mutants = tuple(sorted(COMPARATOR_MUTANTS))
    return MutationReport(
        case_count=len(ordered),
        mutants=mutants,
        killed=tuple(killed),
        survived=tuple(survived),
        killing_case=dict(sorted(killing.items())),
        mutation_score=(len(killed) / len(mutants)) if mutants else 0.0,
    )


def default_comparator_cases(
    mart: MartSpec,
    gold_csv: str,
    *,
    numeric_column: str,
) -> tuple[ComparatorCase, ...]:
    """Build gold-derived cases that distinguish every comparator mutant.

    Each case edits gold rows to isolate one rule. ``numeric_column`` must be a
    non-key numeric gold column used for tolerance cases.
    """
    columns, rows = upstream_eval.parse_canonical_csv(gold_csv)
    if len(rows) < 3:
        raise ParityBatteryError("the mutation case set needs at least three gold rows")
    if numeric_column not in columns:
        raise ParityBatteryError(f"{numeric_column!r} is not a gold column")
    if numeric_column in mart.key_columns:
        raise ParityBatteryError("the drift column must not be a key column")
    base = upstream_eval._numeric_value(rows[0].get(numeric_column))
    if base is None or base == 0.0:
        raise ParityBatteryError(
            "the drift column's first value must be a non-zero number"
        )

    def edit(index: int, **changes: Any) -> list[Row]:
        copy = [dict(row) for row in rows]
        copy[index].update(changes)
        return copy

    renamed = tuple(
        "renamed_column" if name == numeric_column else name for name in columns
    )
    renamed_rows = [
        {("renamed_column" if k == numeric_column else k): v for k, v in row.items()}
        for row in rows
    ]
    dropped_rows = [
        {k: v for k, v in row.items() if k != numeric_column} for row in rows
    ]
    extra_rows = [dict(row, spurious_column="x") for row in rows]
    ragged = [dict(row) for row in rows]
    ragged[1]["spurious_column"] = "x"
    reversed_rows = list(reversed([dict(row) for row in rows]))

    cases = (
        ComparatorCase("identity", mart, gold_csv, tuple(dict(r) for r in rows), columns),
        ComparatorCase(
            "reordered_rows", mart, gold_csv, tuple(reversed_rows), columns
        ),
        ComparatorCase(
            "dropped_row", mart, gold_csv, tuple(dict(r) for r in rows[:-1]), columns
        ),
        ComparatorCase(
            "undeclared_extra_column",
            mart,
            gold_csv,
            tuple(extra_rows),
            columns,
        ),
        ComparatorCase(
            "missing_gold_column",
            mart,
            gold_csv,
            tuple(dropped_rows),
            tuple(name for name in columns if name != numeric_column),
        ),
        ComparatorCase(
            "renamed_gold_column", mart, gold_csv, tuple(renamed_rows), renamed
        ),
        ComparatorCase("ragged_row", mart, gold_csv, tuple(ragged), columns),
        ComparatorCase(
            "null_for_value",
            mart,
            gold_csv,
            tuple(edit(0, **{numeric_column: None})),
            columns,
        ),
        ComparatorCase(
            "drift_within_tolerance",
            mart,
            gold_csv,
            tuple(edit(0, **{numeric_column: repr(base * 1.0005)})),
            columns,
        ),
        ComparatorCase(
            "drift_beyond_tolerance",
            mart,
            gold_csv,
            tuple(edit(0, **{numeric_column: repr(base * 1.5)})),
            columns,
        ),
    )
    return cases


# --- Component 3: metamorphic TLP and NoREC ---------------------------------

@dataclass(frozen=True)
class MetamorphicCase:
    """One relation the DuckDB comparator must hold under a rewrite.

    ``select_clause`` and ``from_clause`` are harness-authored SQL fragments
    over the committed fixture, never solver input; ``predicate`` is the
    ternary-logic predicate the rewrite partitions on.
    """

    case_id: str
    relation: str
    mart: MartSpec
    select_clause: str
    from_clause: str
    predicate: str

    def __post_init__(self) -> None:
        if self.relation not in ("tlp", "norec"):
            raise ParityBatteryError(f"unknown metamorphic relation: {self.relation!r}")


class MetamorphicFinding(BaseModel):
    """``holds is None`` means "could not measure", never "violated" (C7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1)
    relation: str
    holds: bool | None
    code: str = ""


def base_sql(select_clause: str, from_clause: str) -> str:
    return f"SELECT {select_clause} FROM {from_clause}"


def tlp_partition_sql(select_clause: str, from_clause: str, predicate: str) -> str:
    """The three ternary-logic partitions, unioned.

    SQL's three-valued logic is exactly why this is a real oracle: a rewrite
    that forgets the ``IS NULL`` arm silently drops rows, which is the bug class
    TLP was designed to surface.
    """
    parts = (
        f"SELECT {select_clause} FROM {from_clause} WHERE ({predicate})",
        f"SELECT {select_clause} FROM {from_clause} WHERE NOT ({predicate})",
        f"SELECT {select_clause} FROM {from_clause} WHERE ({predicate}) IS NULL",
    )
    return " UNION ALL ".join(parts)


def norec_optimized_sql(select_clause: str, from_clause: str, predicate: str) -> str:
    """The optimized form: let the engine push the filter down, then count."""
    return (
        "SELECT COUNT(*) FROM "
        f"(SELECT {select_clause} FROM {from_clause} WHERE ({predicate}))"
        " AS norec_opt"
    )


def norec_unoptimized_sql(from_clause: str, predicate: str) -> str:
    """The unoptimized form: no filter, count the rows whose predicate IS TRUE."""
    return (
        "SELECT COALESCE(SUM(CASE WHEN norec_flag THEN 1 ELSE 0 END), 0) FROM "
        f"(SELECT (({predicate}) IS TRUE) AS norec_flag FROM {from_clause}) AS norec_all"
    )


def _fetch(connection: Any, sql: str) -> tuple[tuple[str, ...], list[Row]]:
    cursor = connection.execute(sql)
    description = cursor.description or ()
    columns = tuple(str(item[0]) for item in description)
    rows = [dict(zip(columns, values)) for values in cursor.fetchall()]
    return columns, rows


def run_metamorphic_checks(
    connection: Any,
    cases: Sequence[MetamorphicCase],
    *,
    comparator: MartComparator | None = None,
) -> tuple[MetamorphicFinding, ...]:
    """Run TLP and NoREC over one DuckDB connection, judged by the comparator.

    ``connection`` is any DB-API handle (the caller opens and owns it), so this
    module needs no database dependency of its own.  A query that fails is
    recorded as "could not measure" rather than as a violation.
    """
    judge = comparator or reference_comparator
    findings: list[MetamorphicFinding] = []
    for case in cases:
        if case.relation == "tlp":
            findings.append(_tlp_finding(connection, case, judge))
        else:
            findings.append(_norec_finding(connection, case))
    return tuple(findings)


def _tlp_finding(
    connection: Any, case: MetamorphicCase, judge: MartComparator
) -> MetamorphicFinding:
    try:
        columns, base_rows = _fetch(
            connection, base_sql(case.select_clause, case.from_clause)
        )
        _, partitioned = _fetch(
            connection,
            tlp_partition_sql(case.select_clause, case.from_clause, case.predicate),
        )
    except Exception:  # noqa: BLE001 - a probe failure is not evidence
        return MetamorphicFinding(
            case_id=case.case_id, relation=case.relation, holds=None, code="query_failed"
        )
    gold_csv = upstream_eval.rows_to_canonical_csv(base_rows, columns)
    holds = judge(
        ComparatorCase(
            case_id=case.case_id,
            mart=case.mart,
            gold_csv=gold_csv,
            rows=tuple(partitioned),
            columns=columns,
        )
    )
    return MetamorphicFinding(
        case_id=case.case_id,
        relation=case.relation,
        holds=bool(holds),
        code="" if holds else "tlp_partition_disagrees",
    )


def _norec_finding(connection: Any, case: MetamorphicCase) -> MetamorphicFinding:
    try:
        _, optimized = _fetch(
            connection,
            norec_optimized_sql(case.select_clause, case.from_clause, case.predicate),
        )
        _, unoptimized = _fetch(
            connection, norec_unoptimized_sql(case.from_clause, case.predicate)
        )
        left = int(next(iter(optimized[0].values())))
        right = int(next(iter(unoptimized[0].values())))
    except Exception:  # noqa: BLE001 - a probe failure is not evidence
        return MetamorphicFinding(
            case_id=case.case_id, relation=case.relation, holds=None, code="query_failed"
        )
    holds = left == right
    return MetamorphicFinding(
        case_id=case.case_id,
        relation=case.relation,
        holds=holds,
        code="" if holds else "norec_count_disagrees",
    )


# --- The acceptance lane ----------------------------------------------------

@dataclass(frozen=True)
class AcceptanceLaneResult:
    """Does this evidence let the arm be adopted?  If not, exactly why."""

    passed: bool
    reasons: tuple[str, ...]
    mutation: MutationReport | None = None
    metamorphic: tuple[MetamorphicFinding, ...] = ()
    parity: ParityReport | None = None


def run_acceptance_lane(
    *,
    comparator_cases: Sequence[ComparatorCase] = (),
    comparator: MartComparator | None = None,
    metamorphic_findings: Sequence[MetamorphicFinding] = (),
    verdicts: Sequence[ArtifactVerdict] = (),
    confidence: float = DEFAULT_CONFIDENCE,
    population_set_name: str = "",
) -> AcceptanceLaneResult:
    """Return the battery verdict as stable reason codes.

    ``comparator`` must match the reward comparator on every case and kill all
    mutants.
    """
    reasons: list[str] = []
    judge = comparator or reference_comparator
    cases = tuple(comparator_cases)
    mutation: MutationReport | None = None
    if not cases:
        reasons.append("no_comparator_cases")
    else:
        for case in cases:
            if bool(judge(case)) != reference_comparator(case):
                reasons.append(
                    f"comparator_disagrees_with_reward_comparator:{case.case_id}"
                )
        mutation = run_mutation_battery(cases, comparator=judge)
        reasons.extend(f"mutant_survived:{name}" for name in mutation.survived)

    findings = tuple(metamorphic_findings)
    if not findings:
        # Four components, not three: a lane that skipped the metamorphic
        # oracles has not measured the DuckDB comparator at all, and must not
        # read as a pass because it found nothing to complain about.
        reasons.append("no_metamorphic_findings")
    for finding in findings:
        if finding.holds is None:
            reasons.append(f"metamorphic_unmeasured:{finding.case_id}")
        elif not finding.holds:
            reasons.append(f"metamorphic_violation:{finding.case_id}")

    report: ParityReport | None = None
    rows = tuple(verdicts)
    if not rows:
        reasons.append("no_verdicts")
    else:
        report = parity_rate(
            rows, confidence=confidence, population_set_name=population_set_name
        )
        reasons.extend(f"sample_shortfall:{code}" for code in report.sizing_shortfalls)
        if report.agreement_lower < AGREEMENT_LOWER_BOUND_THRESHOLD:
            reasons.append("agreement_lower_bound_below_threshold")
    ordered = tuple(sorted(set(reasons)))
    return AcceptanceLaneResult(
        passed=not ordered,
        reasons=ordered,
        mutation=mutation,
        metamorphic=findings,
        parity=report,
    )


__all__ = [
    "AGREEMENT_LOWER_BOUND_THRESHOLD",
    "COMPARATOR_MUTANTS",
    "DEFAULT_CONFIDENCE",
    "MIN_ARTIFACTS_PER_SOURCE_POOL",
    "MIN_LOCALLY_ACCEPTED_PER_ARM",
    "MIN_LOCALLY_REJECTED_PER_ARM",
    "PARITY_BATTERY_VERSION",
    "PARITY_FAULT_CODES",
    "PARITY_REPORT_FILENAME",
    "PARITY_REPORT_SCHEMA_VERSION",
    "PYTHON_HCL2_PIN",
    "REFERENCE_RULES",
    "RUNTIME_VERIFY_VERBS",
    "SECONDARY_CONFIDENCE",
    "TERRAFORM_VALIDATE_FORMAT_VERSION",
    "TERRAFORM_VERSION_PIN",
    "VERIFY_RECEIPT_STAGE",
    "AcceptanceLaneResult",
    "ArtifactVerdict",
    "ComparatorCase",
    "ComparatorRules",
    "DestinationRunner",
    "MartComparator",
    "MetamorphicCase",
    "MetamorphicFinding",
    "MutationReport",
    "ParityBatteryError",
    "ParityReport",
    "ParityRuntimeFault",
    "PopulationSet",
    "ReplayOutcome",
    "ReplaySpecimen",
    "ReplayStage",
    "TerraformRunner",
    "TerraformValidateResult",
    "base_sql",
    "compare_mart_with_rules",
    "default_comparator_cases",
    "differential_replay",
    "interpret_verify_payload",
    "mcnemar_mid_p",
    "mutant_comparator",
    "norec_optimized_sql",
    "norec_unoptimized_sql",
    "parity_rate",
    "parity_report_digest",
    "parity_report_path",
    "parse_terraform_validate_json",
    "reference_comparator",
    "replay_specimen",
    "seal_parity_report",
    "verify_parity_report",
    "run_acceptance_lane",
    "run_metamorphic_checks",
    "run_mutation_battery",
    "runtime_fault",
    "runtime_verify_argv",
    "stratification_shortfalls",
    "tlp_partition_sql",
    "write_parity_report",
]

#: Disambiguation for callers that also import ``verification/parity.py``'s
#: semantic/runtime case registry.  The roadmap's interface block names this
#: class ``ParityReport``; the alias keeps a cross-module import readable.
RuntimeParityReport = ParityReport

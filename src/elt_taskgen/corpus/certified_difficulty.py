"""Bind empirical difficulty to verified runtime certification.

Create ``RuntimeCertifiedDifficulty`` only for a verified release, a current
complete solver campaign, and a sealed certified runtime attestation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from elt_taskgen.corpus.selection import empirical_band_of
from elt_taskgen.export import certification as certification_mod
from elt_taskgen.export import release as release_mod
from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    DifficultyMeasurement,
    EmpiricalDifficulty,
    TaskVariant,
    canonical_json,
    derive_empirical_aggregate_claims,
    sha256_hex,
)


CERTIFIED_DIFFICULTY_SCHEMA_VERSION = "1.2"
DIFFICULTY_EVIDENCE_REL = Path("reports") / "difficulty.json"


class CertifiedDifficultyError(RuntimeError):
    """Empirical/runtime evidence is missing, stale, or contradictory."""


class RuntimeCertifiedDifficulty(BaseModel):
    """A sealed report joining solver outcomes to one live runtime identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Parse historical reports for diagnosis; new reports use schema 1.2.
    schema_version: Literal["1.0", "1.1", "1.2"] = (
        CERTIFIED_DIFFICULTY_SCHEMA_VERSION
    )
    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_id: str = Field(min_length=1)
    semantic_release_id: str = Field(min_length=1)
    runtime_bundle_id: str = Field(min_length=1)
    certification_id: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    populations: tuple[str, ...] = ()
    difficulty: DifficultyMeasurement
    empirical_band: str = Field(pattern=r"^(easy|medium|hard)$")
    variant_pass_rates: dict[str, dict[str, float]]
    empirical_roster_fingerprint: str = Field(min_length=1)
    empirical_campaign_fingerprint: str = ""
    runtime_attestation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_lifecycle_digest: str = Field(
        default="", pattern=r"^(?:|[0-9a-f]{64})$"
    )
    runtime_certified: Literal[True] = True
    evidence_digest: str = ""

    @model_validator(mode="after")
    def _identity_is_consistent(self) -> "RuntimeCertifiedDifficulty":
        if self.difficulty.task_id != self.task_id:
            raise ValueError("difficulty task_id does not match the report")
        if self.difficulty.task_content_hash != self.task_content_hash:
            raise ValueError("difficulty content hash does not match the report")
        required = {variant.value for variant in RLVR_TASK_VARIANTS}
        if set(self.variant_pass_rates) != required:
            raise ValueError(
                "variant_pass_rates must cover exactly the EL and T variants"
            )
        if (
            self.schema_version == CERTIFIED_DIFFICULTY_SCHEMA_VERSION
            and not self.runtime_lifecycle_digest
        ):
            raise ValueError(
                "current certified difficulty report has no verified runtime "
                "lifecycle digest"
            )
        return self


def certified_difficulty_digest(report: RuntimeCertifiedDifficulty) -> str:
    """Canonical seal over the report with its seal field blanked."""

    return sha256_hex(
        canonical_json(
            report.model_copy(update={"evidence_digest": ""}).model_dump(mode="json")
        )
    )


def _derived_empirical_summary(
    empirical: EmpiricalDifficulty,
) -> tuple[
    dict[str, dict[str, float]],
    str,
    str,
    int,
    float,
    float,
    float,
]:
    """Re-derive every aggregate claim from the measured tier attempts."""
    variant_pass_rates = {
        TaskVariant(variant).value: record.pass_rate_vector()
        for variant, record in sorted(
            empirical.variants.items(), key=lambda item: TaskVariant(item[0]).value
        )
    }
    all_rates = [
        rate for vector in variant_pass_rates.values() for rate in vector.values()
    ]
    if not all_rates:
        raise CertifiedDifficultyError("empirical solver evidence has no attempts")
    try:
        aggregates = derive_empirical_aggregate_claims(empirical.variants)
    except ValueError as exc:
        raise CertifiedDifficultyError(str(exc)) from exc
    return (
        variant_pass_rates,
        empirical_band_of(sum(all_rates) / len(all_rates)),
        empirical.roster_fingerprint,
        int(aggregates["n_attempts"]),
        float(aggregates["success_rate"]),
        float(aggregates["stage1_failure_rate"]),
        float(aggregates["stage2_failure_rate"]),
    )


def verify_certified_difficulty(
    report: RuntimeCertifiedDifficulty,
) -> RuntimeCertifiedDifficulty:
    """Recompute the report seal and its internal empirical invariants."""

    try:
        reparsed = RuntimeCertifiedDifficulty.model_validate(
            report.model_dump(mode="json")
        )
    except ValueError as exc:
        # Convert empirical aggregate contradictions to the domain error.
        if "does not reproduce from tier attempts" in str(exc):
            raise CertifiedDifficultyError(
                f"certified difficulty report is internally invalid: {exc}"
            ) from exc
        raise
    if not reparsed.evidence_digest:
        raise CertifiedDifficultyError("certified difficulty report is unsealed")
    expected = certified_difficulty_digest(reparsed)
    if reparsed.evidence_digest != expected:
        raise CertifiedDifficultyError(
            "certified difficulty report digest does not reproduce"
        )
    empirical = reparsed.difficulty.empirical
    if empirical is None:
        raise CertifiedDifficultyError("report contains no empirical solver evidence")
    if reparsed.schema_version != CERTIFIED_DIFFICULTY_SCHEMA_VERSION:
        raise CertifiedDifficultyError(
            "legacy certified difficulty report has no current campaign and "
            "cleanup-lifecycle binding"
        )
    if not empirical.campaign_fingerprint:
        raise CertifiedDifficultyError(
            "empirical solver evidence has no pinned campaign fingerprint"
        )
    if (
        reparsed.empirical_campaign_fingerprint
        != empirical.campaign_fingerprint
    ):
        raise CertifiedDifficultyError(
            "empirical_campaign_fingerprint does not match empirical solver evidence"
        )
    if empirical.measured_at_content_hash != reparsed.task_content_hash:
        raise CertifiedDifficultyError(
            "empirical solver evidence is not bound to the released content hash"
        )
    required = {variant for variant in RLVR_TASK_VARIANTS}
    if set(empirical.variants) != required:
        raise CertifiedDifficultyError(
            "empirical solver evidence does not cover exactly EL and T"
        )
    for variant, record in empirical.variants.items():
        if record.variant is not TaskVariant(variant) or not record.tiers:
            raise CertifiedDifficultyError(
                f"empirical evidence for {TaskVariant(variant).value!r} has no tiers"
            )
    (
        expected_rates,
        expected_band,
        expected_roster,
        expected_attempts,
        expected_success_rate,
        expected_stage1_failure_rate,
        expected_stage2_failure_rate,
    ) = _derived_empirical_summary(empirical)
    if reparsed.variant_pass_rates != expected_rates:
        raise CertifiedDifficultyError(
            "variant_pass_rates do not reproduce from empirical solver evidence"
        )
    if reparsed.empirical_band != expected_band:
        raise CertifiedDifficultyError(
            "empirical_band does not reproduce from empirical solver evidence"
        )
    if reparsed.empirical_roster_fingerprint != expected_roster:
        raise CertifiedDifficultyError(
            "empirical_roster_fingerprint does not match empirical solver evidence"
        )
    aggregate_claims = {
        "n_attempts": (empirical.n_attempts, expected_attempts),
        "success_rate": (empirical.success_rate, expected_success_rate),
        "stage1_failure_rate": (
            empirical.stage1_failure_rate,
            expected_stage1_failure_rate,
        ),
        "stage2_failure_rate": (
            empirical.stage2_failure_rate,
            expected_stage2_failure_rate,
        ),
    }
    for field, (actual, derived) in aggregate_claims.items():
        if actual != derived:
            raise CertifiedDifficultyError(
                f"empirical {field} does not reproduce from tier attempts"
            )
    return reparsed


def _load_release_difficulty(
    release_dir: Path,
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    *,
    workspace: Path | None,
) -> DifficultyMeasurement:
    """Load only release-bound evidence, with a legacy workspace fallback."""

    release_path = release_dir / "private" / task_id / DIFFICULTY_EVIDENCE_REL
    path = release_path
    if not path.is_file() and workspace is not None:
        path = Path(workspace) / "tasks" / task_id / DIFFICULTY_EVIDENCE_REL
    if not path.is_file():
        raise CertifiedDifficultyError(
            f"no difficulty evidence for {task_id!r}; expected {release_path}"
        )
    try:
        measurement = DifficultyMeasurement.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise CertifiedDifficultyError(
            f"difficulty evidence for {task_id!r} is unreadable: {exc}"
        ) from exc

    recorded = getattr(manifest, "difficulty_measurements", {}).get(task_id)
    if recorded:
        actual = sha256_hex(canonical_json(measurement.model_dump(mode="json")))
        # Release manifests bind canonical content rather than formatting bytes.
        if actual != recorded:
            raise CertifiedDifficultyError(
                "difficulty evidence does not match the release manifest"
            )
    elif release_path.is_file():
        raise CertifiedDifficultyError(
            "release ships difficulty evidence but its manifest does not bind it"
        )
    return measurement


def build_runtime_certified_difficulty(
    *,
    release_dir: Path,
    certification_store: Path,
    task_id: str,
    workspace: Path | None = None,
) -> RuntimeCertifiedDifficulty:
    """Promote current solver evidence only after sealed live certification."""

    release_dir = Path(release_dir).resolve()
    verified = release_mod.verify_release(release_dir)
    if not verified.ok:
        raise CertifiedDifficultyError(
            "release verification failed: " + "; ".join(verified.failures[:3])
        )
    manifest = release_mod.ReleaseManifest.model_validate_json(
        (release_dir / "release_manifest.json").read_text(encoding="utf-8")
    )
    if task_id not in manifest.tasks:
        raise CertifiedDifficultyError(f"release has no task {task_id!r}")
    if manifest.release_mode != release_mod.CERTIFIED_RELEASE_MODE:
        raise CertifiedDifficultyError(
            "runtime-certified difficulty requires a certified release; "
            "development releases do not carry label-bearing isolation evidence"
        )
    if not manifest.sandbox_attestation_digest:
        raise CertifiedDifficultyError(
            "certified release records no sandbox attestation digest"
        )
    measurement = _load_release_difficulty(
        release_dir, manifest, task_id, workspace=workspace
    )
    if measurement.task_id != task_id:
        raise CertifiedDifficultyError("difficulty evidence names another task")
    content_hash = manifest.tasks[task_id]
    if measurement.task_content_hash != content_hash:
        raise CertifiedDifficultyError(
            "difficulty evidence is stale for the released task content hash"
        )
    empirical = measurement.empirical
    if empirical is None:
        raise CertifiedDifficultyError(
            "structural-only difficulty cannot be called empirically certified"
        )
    if empirical.measured_at_content_hash != content_hash:
        raise CertifiedDifficultyError(
            "empirical solver evidence is stale for the released task"
        )
    required = {variant for variant in RLVR_TASK_VARIANTS}
    if set(empirical.variants) != required:
        missing = sorted(v.value for v in required - set(empirical.variants))
        extra = sorted(v.value for v in set(empirical.variants) - required)
        raise CertifiedDifficultyError(
            f"empirical solver evidence must cover exactly EL and T "
            f"(missing={missing}, extra={extra})"
        )
    for variant, record in empirical.variants.items():
        if not record.tiers:
            raise CertifiedDifficultyError(
                f"empirical solver evidence for {TaskVariant(variant).value!r} "
                "contains no measured tiers"
            )

    certification_id = manifest.certification_ids.get(task_id, "")
    if not certification_id:
        raise CertifiedDifficultyError(
            "release manifest carries no runtime certification identity"
        )
    status = certification_mod.certification_status(
        Path(certification_store), certification_id
    )
    if status.state is not certification_mod.CertificationState.CERTIFIED:
        reason = f": {status.reason}" if status.reason else ""
        raise CertifiedDifficultyError(
            f"runtime certification {certification_id} is {status.state.value}{reason}"
        )
    attestation = certification_mod.verify_attestation(
        Path(certification_store)
        / certification_id
        / certification_mod.ATTESTATION_FILENAME
    )
    if (
        attestation.task_id != task_id
        or attestation.release_id != manifest.release_id
        or attestation.runtime_bundle_id != manifest.runtime_bundle_ids.get(task_id)
    ):
        raise CertifiedDifficultyError(
            "runtime attestation does not bind the requested release/task"
        )
    # New promotions require sealed lifecycle cleanup evidence. Historical
    # attestations remain readable but cannot gain the stronger label.
    try:
        from elt_taskgen.runtime.certification_lifecycle import (
            CertificationLifecycleError,
            verify_completed_lifecycle,
        )

        lifecycle = verify_completed_lifecycle(
            release_dir=release_dir,
            task_id=task_id,
            certification_store=Path(certification_store),
        )
    except CertificationLifecycleError as exc:
        raise CertifiedDifficultyError(
            f"runtime certification has no verified cleanup lifecycle: {exc}"
        ) from exc
    if lifecycle.certification_attestation_digest != attestation.attestation_digest:
        raise CertifiedDifficultyError(
            "runtime lifecycle does not bind the verified certification attestation"
        )

    (
        variant_pass_rates,
        empirical_band,
        empirical_roster,
        _,
        _,
        _,
        _,
    ) = _derived_empirical_summary(empirical)
    report = RuntimeCertifiedDifficulty(
        task_id=task_id,
        task_content_hash=content_hash,
        release_id=manifest.release_id,
        semantic_release_id=manifest.semantic_release_id,
        runtime_bundle_id=manifest.runtime_bundle_ids[task_id],
        certification_id=certification_id,
        destination=manifest.destinations[task_id],
        populations=tuple(attestation.populations),
        difficulty=measurement,
        empirical_band=empirical_band,
        variant_pass_rates=variant_pass_rates,
        empirical_roster_fingerprint=empirical_roster,
        empirical_campaign_fingerprint=empirical.campaign_fingerprint,
        runtime_attestation_digest=attestation.attestation_digest,
        runtime_lifecycle_digest=lifecycle.lifecycle_digest,
        evidence_digest="",
    )
    sealed = report.model_copy(
        update={"evidence_digest": certified_difficulty_digest(report)}
    )
    return verify_certified_difficulty(sealed)


__all__ = [
    "CERTIFIED_DIFFICULTY_SCHEMA_VERSION",
    "CertifiedDifficultyError",
    "RuntimeCertifiedDifficulty",
    "build_runtime_certified_difficulty",
    "certified_difficulty_digest",
    "verify_certified_difficulty",
]

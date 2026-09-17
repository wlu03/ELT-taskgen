"""Select corpus tasks with deterministic eligibility, quotas, and splits.

Splits hash only ``family_id`` so related EL and T units stay together.
Empirical evidence filters and assigns bands but does not change structural
scores."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field, model_validator

from elt_taskgen.corpus.difficulty import structural_difficulty
from elt_taskgen.models import (
    Backend,
    DifficultyMeasurement,
    Origin,
    RLVR_TASK_VARIANTS,
    TaskIR,
    TaskStatus,
    TaskVariant,
    VariantCalibration,
    canonical_json,
    empirical_aggregate_problem,
    sha256_hex,
    variant_task_id,
)

# Statuses implying the gate battery passed; anything else (including the
# default DRAFT) is rejected even though the engine should have pre-filtered.
ACCEPTED_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.ACCEPTED,
        TaskStatus.CALIBRATED,
        TaskStatus.SELECTED,
        TaskStatus.RELEASED,
    }
)

#: Band edges over combined_score(); lower-inclusive, upper-exclusive.
BAND_EDGES: tuple[tuple[str, float, float], ...] = (
    ("easy", 0.0, 1.0 / 3.0),
    ("medium", 1.0 / 3.0, 2.0 / 3.0),
    ("hard", 2.0 / 3.0, 1.0000001),
)

#: Band edges over the MEASURED pass rate — INVERTED: a low rate is HARD.
EMPIRICAL_BAND_EDGES: tuple[tuple[str, float, float], ...] = (
    ("hard", 0.0, 1.0 / 3.0),
    ("medium", 1.0 / 3.0, 2.0 / 3.0),
    ("easy", 2.0 / 3.0, 1.0000001),
)

#: Features compared against the anchor profile (score fields or structural keys).
PROFILE_SCORE_FEATURES: tuple[str, ...] = (
    "load_score",
    "transform_score",
    "combined_score",
)


class Quotas(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Number of parent projects; an EL/T release emits exactly 2 * size units.
    size: int = Field(ge=1)
    difficulty_bands: dict[str, float] = Field(default_factory=dict)
    backend_mix: dict[Backend, float] = Field(default_factory=dict)
    origin_mix: dict[Origin, float] = Field(default_factory=dict)
    val_fraction: float = Field(ge=0.0, le=1.0, default=0.1)


class SelectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    train: tuple[str, ...]
    val: tuple[str, ...]
    rejected: dict[str, str]  # task_id -> reason
    #: parent task_id -> exactly ('extract_load', 'transform').
    variants: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    #: variant_task_id -> why that graded unit is not in the selection (R7).
    rejected_variants: dict[str, str] = Field(default_factory=dict)
    #: Whether selection required current empirical EL/T evidence.
    empirical_required: bool = False
    #: Content hashes used by selection and rechecked during release.
    task_content_hashes: dict[str, str] = Field(default_factory=dict)
    #: Canonical difficulty measurement digest by task.
    difficulty_measurements: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exact_two_units(self) -> "SelectionResult":
        selected = set(self.train) | set(self.val)
        if set(self.variants) != selected:
            raise ValueError(
                "variants must have exactly one entry for every selected parent"
            )
        required = tuple(v.value for v in RLVR_TASK_VARIANTS)
        wrong = {
            task_id: tuple(values)
            for task_id, values in self.variants.items()
            if tuple(values) != required
        }
        if wrong:
            raise ValueError(
                f"every selected parent must contain exactly {required}; got {wrong}"
            )
        for field_name, values in (
            ("task_content_hashes", self.task_content_hashes),
            ("difficulty_measurements", self.difficulty_measurements),
        ):
            if values and set(values) != selected:
                raise ValueError(
                    f"{field_name} must name exactly the selected parents"
                )
            malformed = sorted(
                task_id
                for task_id, digest in values.items()
                if len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
            )
            if malformed:
                raise ValueError(
                    f"{field_name} contains non-sha256 digest(s) for {malformed}"
                )
        return self


def band_of(score: float) -> str:
    """Difficulty band name for a combined score in [0, 1]."""
    for name, lo, hi in BAND_EDGES:
        if lo <= score < hi:
            return name
    return BAND_EDGES[-1][0]


def empirical_band_of(pass_rate: float) -> str:
    """Difficulty band name for a measured pass rate in [0, 1]."""
    for name, lo, hi in EMPIRICAL_BAND_EDGES:
        if lo <= pass_rate < hi:
            return name
    return EMPIRICAL_BAND_EDGES[-1][0]


def _variant_pass_rates(
    m: DifficultyMeasurement,
) -> dict[str, dict[str, float]]:
    """variant value -> {model_key: pass rate}, empty when uncalibrated.

    A legacy record with no per-variant calibration yields {}, so every
    empirical consumer below degrades to purely structural behaviour.
    """
    e = m.empirical
    if e is None or not e.variants:
        return {}
    return {
        TaskVariant(variant).value: cal.pass_rate_vector()
        for variant, cal in e.variants.items()
    }


def empirical_pass_rate(m: DifficultyMeasurement) -> float | None:
    """Mean measured pass rate over the roster, or None when uncalibrated.

    The active task is an EL/T pair, so historical FULL records are ignored.
    """
    rates = _variant_pass_rates(m)
    if not rates:
        return None
    vectors = [rates[v.value] for v in RLVR_TASK_VARIANTS if v.value in rates]
    values = [rate for vector in vectors for rate in vector.values()]
    if not values:
        return None
    return sum(values) / len(values)


# Empirically trivial variants score perfectly on every pinned tier.

def variant_is_impossible(calibration: VariantCalibration) -> bool:
    """Return whether every pinned solver tier recorded zero successes."""
    return all(tier.successes == 0 for tier in calibration.tiers)


def variant_is_trivial(calibration: VariantCalibration) -> bool:
    """Return whether every pinned tier passed every attempt.

    Any failed attempt keeps the variant measurable, so exclusion requires
    perfect results across all tiers.
    """
    return all(tier.successes == tier.k for tier in calibration.tiers)


def variant_empirical_exclusions(m: DifficultyMeasurement) -> dict[str, str]:
    """Return measured impossible or trivial reasons by variant.

    Gate acceptance does not imply measurable difficulty; pair-level
    eligibility rejects either exclusion.
    """
    out: dict[str, str] = {}
    e = m.empirical
    if e is None or not e.variants:
        return out
    calibrations = sorted(
        ((TaskVariant(variant).value, cal) for variant, cal in e.variants.items()),
        key=lambda item: item[0],
    )
    for variant, cal in calibrations:
        if not cal.tiers:
            continue
        if variant_is_impossible(cal):
            out[variant] = (
                f"empirically impossible: variant {variant!r} scored 0 "
                "successes on every pinned solver tier (feasibility re-review)"
            )
        elif variant_is_trivial(cal):
            out[variant] = (
                f"empirically trivial: every pinned solver tier aced variant "
                f"{variant!r}"
            )
    return out


def empirical_exclusion(m: DifficultyMeasurement) -> str | None:
    """PARENT-level rejection reason from MEASURED evidence, or None.

    Band filtering, never score blending: EL and T are an inseparable release
    pair, so an exclusion on either unit excludes the parent.
    """
    rates = _variant_pass_rates(m)
    if not rates:
        return None
    excluded = variant_empirical_exclusions(m)
    active = [
        excluded[v.value] for v in RLVR_TASK_VARIANTS if v.value in excluded
    ]
    if active:
        return "; ".join(active)
    return None


def empirical_evidence_problem(
    m: DifficultyMeasurement,
    *,
    expected_campaign_fingerprint: str | None = None,
) -> str | None:
    """Return why empirical EL/T evidence is incomplete, or ``None``."""

    empirical = m.empirical
    if empirical is None:
        return "no empirical solver evidence (structural-only difficulty)"
    if empirical.measured_at_content_hash != m.task_content_hash:
        return "empirical solver evidence is stale for this content hash"
    required = {variant for variant in RLVR_TASK_VARIANTS}
    recorded = set(empirical.variants)
    if recorded != required:
        missing = sorted(variant.value for variant in required - recorded)
        extra = sorted(variant.value for variant in recorded - required)
        return (
            "empirical solver evidence must cover exactly EL and T "
            f"(missing={missing}, extra={extra})"
        )
    if not empirical.roster_fingerprint:
        return "empirical solver evidence has no pinned roster fingerprint"
    if not empirical.campaign_fingerprint:
        return "empirical solver evidence has no pinned campaign fingerprint"
    aggregate_problem = empirical_aggregate_problem(empirical)
    if aggregate_problem is not None:
        return aggregate_problem
    if (
        expected_campaign_fingerprint is not None
        and empirical.campaign_fingerprint != expected_campaign_fingerprint
    ):
        if not expected_campaign_fingerprint:
            return "active empirical campaign fingerprint is unavailable"
        return (
            "empirical solver evidence is stale for the active campaign "
            f"({empirical.campaign_fingerprint[:12]} != "
            f"{expected_campaign_fingerprint[:12]})"
        )
    empty = sorted(
        TaskVariant(variant).value
        for variant, record in empirical.variants.items()
        if not record.tiers
    )
    if empty:
        return f"empirical solver evidence has no measured tiers for {empty}"
    return None


def band_for(m: DifficultyMeasurement) -> str:
    """Band a measurement lands in: measured when calibrated, else structural."""
    rate = empirical_pass_rate(m)
    if rate is None:
        return band_of(m.combined_score())
    return empirical_band_of(rate)


def split_for_family(family_id: str, val_fraction: float) -> str:
    """Hash-stable split over family_id and val_fraction alone, so a family can
    never be train in one selection and val in another."""
    digest = hashlib.sha256(f"split\x1f{family_id}".encode("utf-8")).digest()
    u = (int.from_bytes(digest[:8], "big") % 1_000_000) / 1_000_000.0
    return "val" if u < val_fraction else "train"


def _largest_remainder(targets: dict[str, float], total: int) -> dict[str, int]:
    """Apportion `total` slots to fractional targets, deterministically."""
    if not targets or total <= 0:
        return {k: 0 for k in targets}
    weight = sum(max(v, 0.0) for v in targets.values())
    if weight <= 0:
        return {k: 0 for k in targets}
    raw = {k: total * max(v, 0.0) / weight for k, v in sorted(targets.items())}
    counts = {k: int(v) for k, v in raw.items()}
    leftover = total - sum(counts.values())
    # stable order: biggest remainder first, then key
    by_rem = sorted(raw.items(), key=lambda kv: (-(kv[1] - int(kv[1])), kv[0]))
    for k, _ in by_rem:
        if leftover <= 0:
            break
        counts[k] += 1
        leftover -= 1
    return counts


def _eligible(
    task: TaskIR,
    measurements: dict[str, DifficultyMeasurement],
    *,
    require_empirical: bool = False,
    expected_campaign_fingerprint: str | None = None,
) -> str | None:
    """Return a rejection reason, or None when the task may be selected."""
    if task.origin is Origin.ELTBENCH_ANCHOR:
        return "anchor tasks are measurement-only; never selectable"
    if task.status not in ACCEPTED_STATUSES:
        return f"status {task.status.value!r} is not accepted"
    m = measurements.get(task.task_id)
    if m is None:
        return "no difficulty measurement (fail closed)"
    if m.task_id != task.task_id:
        return "difficulty measurement names another task (fail closed)"
    if m.task_content_hash != task.content_hash():
        return "stale difficulty measurement: content hash mismatch (fail closed)"
    if require_empirical:
        problem = empirical_evidence_problem(
            m,
            expected_campaign_fingerprint=expected_campaign_fingerprint,
        )
        if problem is None and expected_campaign_fingerprint is None:
            problem = "active empirical campaign fingerprint was not supplied"
        if problem is not None:
            return problem + " (fail closed)"
    return empirical_exclusion(m)


def select(
    candidates: list[TaskIR],
    measurements: dict[str, DifficultyMeasurement],
    anchors: list[TaskIR],
    quotas: Quotas,
    accepted_variants: dict[str, frozenset[str] | set[str] | tuple[str, ...]] | None = None,
    *,
    require_empirical: bool = False,
    expected_campaign_fingerprint: str | None = None,
) -> SelectionResult:
    """Select quotas deterministically while preserving family independence.

    Anchors affect reports only. Missing accepted EL or T variants reject the
    parent. ``require_empirical`` rejects structural-only, stale, or partial
    solver evidence and can bind it to a campaign fingerprint."""
    rejected: dict[str, str] = {}
    eligible: list[TaskIR] = []
    accepted_map = accepted_variants or {}
    required = {variant.value for variant in RLVR_TASK_VARIANTS}

    seen_ids: set[str] = set()
    for task in sorted(candidates, key=lambda t: t.task_id):
        if task.task_id in seen_ids:
            rejected[task.task_id] = "duplicate task_id in candidate pool"
            continue
        seen_ids.add(task.task_id)
        reason = _eligible(
            task,
            measurements,
            require_empirical=require_empirical,
            expected_campaign_fingerprint=expected_campaign_fingerprint,
        )
        accepted = {str(v) for v in accepted_map.get(task.task_id, ())}
        missing = sorted(required - accepted)
        if reason is None and missing:
            reason = (
                "both EL and T batteries are required; no current accepted "
                f"battery for {missing}"
            )
        if reason is not None:
            rejected[task.task_id] = reason
        else:
            eligible.append(task)

    # family/cluster independence: keep the lexicographically-first task_id.
    seen_families: set[str] = set()
    seen_clusters: set[str] = set()
    independent: list[TaskIR] = []
    for task in eligible:  # already sorted by task_id
        if task.family_id in seen_families:
            rejected[task.task_id] = f"family {task.family_id!r} already represented"
            continue
        if task.cluster_id in seen_clusters:
            rejected[task.task_id] = f"cluster {task.cluster_id!r} already represented"
            continue
        seen_families.add(task.family_id)
        seen_clusters.add(task.cluster_id)
        independent.append(task)

    chosen = _quota_pick(independent, measurements, quotas, rejected)

    train: list[str] = []
    val: list[str] = []
    for task in sorted(chosen, key=lambda t: t.task_id):
        side = split_for_family(task.family_id, quotas.val_fraction)
        (val if side == "val" else train).append(task.task_id)

    variants, rejected_variants = _select_variants(
        chosen, measurements, accepted_variants
    )
    _assert_family_variant_isolation(chosen, variants)

    return SelectionResult(
        train=tuple(train),
        val=tuple(val),
        rejected=rejected,
        variants=variants,
        rejected_variants=rejected_variants,
        empirical_required=require_empirical,
        task_content_hashes={
            task.task_id: task.content_hash()
            for task in sorted(chosen, key=lambda candidate: candidate.task_id)
        },
        difficulty_measurements={
            task.task_id: sha256_hex(
                canonical_json(measurements[task.task_id].model_dump(mode="json"))
            )
            for task in sorted(chosen, key=lambda candidate: candidate.task_id)
        },
    )


def _select_variants(
    chosen: list[TaskIR],
    measurements: dict[str, DifficultyMeasurement],
    accepted_variants: dict[str, frozenset[str] | set[str] | tuple[str, ...]] | None,
) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """Expand an already-eligible parent into its required EL/T pair.

    The eligibility pass above normally guarantees both units; these checks are
    defence in depth and preserve precise refusal evidence.
    """
    accepted_map = accepted_variants or {}
    variants: dict[str, tuple[str, ...]] = {}
    rejected_variants: dict[str, str] = {}
    for task in sorted(chosen, key=lambda t: t.task_id):
        accepted = {str(v) for v in accepted_map.get(task.task_id, ())}
        measurement = measurements.get(task.task_id)
        flagged = (
            variant_empirical_exclusions(measurement) if measurement is not None else {}
        )
        selected: list[str] = []
        for variant in RLVR_TASK_VARIANTS:
            vid = variant_task_id(task.task_id, variant)
            if variant.value not in accepted:
                rejected_variants[vid] = (
                    "no passing variant battery at the current content hash "
                    "(fail closed: an uncertified subtask is never selected)"
                )
                continue
            if variant.value in flagged:
                rejected_variants[vid] = flagged[variant.value]
                continue
            selected.append(variant.value)
        variants[task.task_id] = tuple(selected)
    return variants, rejected_variants


def _assert_family_variant_isolation(
    chosen: list[TaskIR], variants: dict[str, tuple[str, ...]]
) -> None:
    """Require each family to contribute graded units from one parent task."""
    by_family: dict[str, list[str]] = {}
    for task in chosen:
        if variants.get(task.task_id):
            by_family.setdefault(task.family_id, []).append(task.task_id)
    straddling = {f: sorted(ids) for f, ids in by_family.items() if len(ids) > 1}
    if straddling:
        raise ValueError(
            "family/variant isolation violated — these families contribute "
            f"graded units from more than one parent: {straddling}. Variants "
            "share their parent's family_id, so this puts EL and T views of "
            "the same data on both sides of the train/val split."
        )


def _quota_pick(
    pool: list[TaskIR],
    measurements: dict[str, DifficultyMeasurement],
    quotas: Quotas,
    rejected: dict[str, str],
) -> list[TaskIR]:
    """Greedy deficit solver: exact band targets when the pool allows, then
    deficit-driven fill; origin/backend quotas steer within-band ordering."""
    by_band: dict[str, list[TaskIR]] = {name: [] for name, _, _ in BAND_EDGES}
    for task in pool:
        # Measured band when calibrated, structural otherwise; combined_score()
        # itself is never touched by evidence.
        by_band[band_for(measurements[task.task_id])].append(task)

    band_targets = (
        _largest_remainder(quotas.difficulty_bands, quotas.size)
        if quotas.difficulty_bands
        else {}
    )

    # remaining quota deficits, mutated as picks are made
    origin_need: dict[Origin, float] = {
        o: f * quotas.size for o, f in sorted(quotas.origin_mix.items())
    }
    backend_need: dict[Backend, float] = {
        b: f * quotas.size for b, f in sorted(quotas.backend_mix.items())
    }

    def deficit_gain(task: TaskIR) -> float:
        gain = 0.0
        gain += max(origin_need.get(task.origin, 0.0), 0.0)
        backends = {b.backend for b in task.backends}
        for b in backends:
            gain += max(backend_need.get(b, 0.0), 0.0) / len(backends)
        return gain

    def commit(task: TaskIR) -> None:
        if task.origin in origin_need:
            origin_need[task.origin] -= 1.0
        backends = {b.backend for b in task.backends}
        for b in backends:
            if b in backend_need:
                backend_need[b] -= 1.0 / len(backends)

    def take(cands: list[TaskIR], count: int) -> list[TaskIR]:
        picked: list[TaskIR] = []
        remaining = sorted(cands, key=lambda t: t.task_id)
        while remaining and len(picked) < count:
            best = remaining[0]
            best_gain = deficit_gain(best)
            for cand in remaining[1:]:
                g = deficit_gain(cand)
                if g > best_gain:  # strict: first (lowest task_id) wins ties
                    best, best_gain = cand, g
            remaining.remove(best)
            picked.append(best)
            commit(best)
        return picked

    chosen: list[TaskIR] = []
    leftovers: list[TaskIR] = []
    if band_targets:
        for name, _, _ in BAND_EDGES:
            cands = by_band.get(name, [])
            got = take(cands, band_targets.get(name, 0))
            chosen.extend(got)
            leftovers.extend(t for t in cands if t not in got)
        # bands named in quotas but absent from BAND_EDGES contribute nothing;
        # their slots fall through to the fill pass below.
    else:
        leftovers = list(pool)

    if len(chosen) < quotas.size:
        chosen.extend(take(leftovers, quotas.size - len(chosen)))

    chosen_ids = {t.task_id for t in chosen}
    for task in pool:
        if task.task_id not in chosen_ids:
            rejected[task.task_id] = "eligible but not selected (quota/size)"
    return chosen


# Anchor profile comparison (reporting only — never a gate).

def _quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation quantile over pre-sorted values (deterministic)."""
    if not sorted_vals:
        raise ValueError("quantile of empty sequence")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _measurement_features(m: DifficultyMeasurement) -> dict[str, float]:
    feats = dict(sorted(m.structural.items()))
    feats["load_score"] = m.load_score
    feats["transform_score"] = m.transform_score
    feats["combined_score"] = m.combined_score()
    return feats


def anchor_profile(anchors: list[TaskIR]) -> dict[str, tuple[float, float, float]]:
    """Feature -> (q25, q50, q75) over the imported anchor tasks.

    Anchors are structurally scored on the fly; an empty list fails closed, so
    a missing target distribution surfaces instead of comparing against nothing.
    """
    if not anchors:
        raise ValueError("anchor_profile requires at least one anchor task")
    per_feature: dict[str, list[float]] = {}
    for task in sorted(anchors, key=lambda t: t.task_id):
        feats = _measurement_features(structural_difficulty(task))
        for name, value in feats.items():
            per_feature.setdefault(name, []).append(value)
    profile: dict[str, tuple[float, float, float]] = {}
    for name in sorted(per_feature):
        vals = sorted(per_feature[name])
        profile[name] = (
            _quantile(vals, 0.25),
            _quantile(vals, 0.50),
            _quantile(vals, 0.75),
        )
    return profile


def anchor_deviation_report(
    selected_task_ids: list[str],
    measurements: dict[str, DifficultyMeasurement],
    profile: dict[str, tuple[float, float, float]],
) -> dict[str, float]:
    """Per-feature deviation: selection median minus anchor median (positive =
    heavier than the anchor). Missing measurements fail closed (KeyError): a
    report over a partial selection would be silently wrong."""
    if not selected_task_ids:
        raise ValueError("deviation report over an empty selection")
    per_feature: dict[str, list[float]] = {}
    for task_id in sorted(selected_task_ids):
        feats = _measurement_features(measurements[task_id])
        for name, value in feats.items():
            per_feature.setdefault(name, []).append(value)
    report: dict[str, float] = {}
    for name in sorted(profile):
        if name not in per_feature:
            continue
        vals = sorted(per_feature[name])
        report[name] = _quantile(vals, 0.5) - profile[name][1]
    return report

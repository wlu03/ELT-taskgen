"""Configure deterministic mart selection and synthetic population scale."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil
from typing import Mapping


@dataclass(frozen=True)
class GenerationDifficultyProfile:
    """Bounded generation difficulty controls.

    The primary-row floor applies to the total active scale vector, which is
    multiplied uniformly up to ``max_scale_multiplier``.
    """

    name: str
    mart_budgets: tuple[tuple[str, int], ...] = ()
    spread_mart_grains: bool = False
    max_per_shape: int = 1
    max_per_shape_by_pool: tuple[tuple[str, int], ...] = ()
    synthetic_primary_row_floor: int = 0
    max_scale_multiplier: int = 1
    max_primary_rows_per_table: int | None = None
    max_primary_rows_per_task: int | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("difficulty profile name must be non-empty")
        for label, values in (
            ("mart_budgets", self.mart_budgets),
            ("max_per_shape_by_pool", self.max_per_shape_by_pool),
        ):
            names = [pool for pool, _value in values]
            if len(names) != len(set(names)):
                raise ValueError(f"{label} repeats a pool")
            if any(not pool or value < 1 for pool, value in values):
                raise ValueError(f"{label} requires non-empty pools and positive values")
        if self.max_per_shape < 1:
            raise ValueError("max_per_shape must be at least 1")
        if self.synthetic_primary_row_floor < 0:
            raise ValueError("synthetic_primary_row_floor cannot be negative")
        if self.max_scale_multiplier < 1:
            raise ValueError("max_scale_multiplier must be at least 1")
        for label, value in (
            ("max_primary_rows_per_table", self.max_primary_rows_per_table),
            ("max_primary_rows_per_task", self.max_primary_rows_per_task),
        ):
            if value is not None and value < 1:
                raise ValueError(f"{label} must be positive when provided")

    def mart_parameters(
        self,
        *,
        pool: str,
        default_budget: int,
        default_spread_grains: bool = False,
        default_max_per_shape: int = 1,
    ) -> tuple[int, bool, int]:
        """Return mart parameters, preserving native budgets but honoring shape caps."""

        budget = max(default_budget, dict(self.mart_budgets).get(pool, default_budget))
        pool_shape_caps = dict(self.max_per_shape_by_pool)
        per_shape = (
            pool_shape_caps[pool]
            if pool in pool_shape_caps
            else max(default_max_per_shape, self.max_per_shape)
        )
        return (
            budget,
            default_spread_grains or self.spread_mart_grains,
            per_shape,
        )

    def scale_hint(self, hint: Mapping[str, int]) -> dict[str, int]:
        """Scale an active synthetic source vector toward this profile's floor."""

        normalized = {name: int(count) for name, count in sorted(hint.items())}
        if any(count < 1 for count in normalized.values()):
            raise ValueError("synthetic scale hints must contain positive row counts")
        total = sum(normalized.values())
        if not normalized or not self.synthetic_primary_row_floor:
            return normalized
        multiplier = min(
            self.max_scale_multiplier,
            max(1, ceil(self.synthetic_primary_row_floor / total)),
        )
        scaled = {name: count * multiplier for name, count in normalized.items()}
        if sum(scaled.values()) < self.synthetic_primary_row_floor:
            raise ValueError(
                f"difficulty profile {self.name!r} requires at least "
                f"{self.synthetic_primary_row_floor} synthetic primary rows, but "
                f"the {self.max_scale_multiplier}x safety cap reaches only "
                f"{sum(scaled.values())}"
            )
        largest = max(scaled.values(), default=0)
        if (
            self.max_primary_rows_per_table is not None
            and largest > self.max_primary_rows_per_table
        ):
            raise ValueError(
                f"difficulty profile {self.name!r} would generate {largest} rows "
                f"in one PRIMARY table, over its "
                f"{self.max_primary_rows_per_table}-row safety limit"
            )
        if (
            self.max_primary_rows_per_task is not None
            and sum(scaled.values()) > self.max_primary_rows_per_task
        ):
            raise ValueError(
                f"difficulty profile {self.name!r} would generate "
                f"{sum(scaled.values())} PRIMARY rows, over its "
                f"{self.max_primary_rows_per_task}-row task safety limit"
            )
        return scaled

    def catalog_contract(self) -> dict[str, object]:
        """Stable JSON-ready description recorded beside generated examples."""

        return asdict(self)


STANDARD_DIFFICULTY_PROFILE = GenerationDifficultyProfile(name="standard")

# Rounded ELT-Bench median; local bundles do not reproduce its largest tasks.
CHALLENGING_DIFFICULTY_PROFILE = GenerationDifficultyProfile(
    name="challenging",
    # Per-pool caps limit repeated plan shapes; DLT retains its deterministic cap.
    mart_budgets=(("dlt", 2), ("schemapile", 5), ("synsql", 4), ("wikidbs", 2)),
    spread_mart_grains=True,
    max_per_shape_by_pool=(
        ("dlt", 1),
        # SchemaPile may repeat a shape twice; the cohort-level cap also applies.
        ("schemapile", 2),
        ("synsql", 2),
        ("wikidbs", 2),
    ),
    synthetic_primary_row_floor=32_768,
    max_scale_multiplier=128,
    max_primary_rows_per_table=40_000,
    max_primary_rows_per_task=100_000,
)

DIFFICULTY_PROFILES: dict[str, GenerationDifficultyProfile] = {
    profile.name: profile
    for profile in (STANDARD_DIFFICULTY_PROFILE, CHALLENGING_DIFFICULTY_PROFILE)
}


def difficulty_profile(name: str) -> GenerationDifficultyProfile:
    """Resolve a public profile name, failing with the supported choices."""

    try:
        return DIFFICULTY_PROFILES[name]
    except KeyError as exc:
        choices = ", ".join(sorted(DIFFICULTY_PROFILES))
        raise ValueError(f"unknown difficulty profile {name!r}; choose {choices}") from exc

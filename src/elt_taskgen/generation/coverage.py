"""Measure deterministic semantic coverage from serialized ``TaskIR`` data.

Policies may constrain tag vocabularies and minimum mart or task ownership.
Legacy plans without tags appear in the ``<missing>`` bucket.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from elt_taskgen.generation.mart_plan import semantic_pattern_problems
from elt_taskgen.models import TaskIR, canonical_json, sha256_hex


SEMANTIC_COVERAGE_POLICY_VERSION = "1.0.0"
SEMANTIC_COVERAGE_REPORT_VERSION = "1.0.0"
MISSING_TEMPLATE_ID = "<missing>"


def _tag_value(value: object) -> str:
    """Return the canonical string for a string or string-valued Enum."""

    if isinstance(value, Enum):
        value = value.value
    return value.strip() if isinstance(value, str) else str(value).strip()


def _normalize_names(values: Iterable[object], *, label: str) -> tuple[str, ...]:
    normalized = tuple(_tag_value(value) for value in values)
    if any(not value for value in normalized):
        raise ValueError(f"{label} cannot contain an empty name")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} contains duplicate names")
    return tuple(sorted(normalized))


def _normalize_minimums(
    values: Iterable[tuple[object, int]], *, label: str
) -> tuple[tuple[str, int], ...]:
    normalized = tuple((_tag_value(name), int(count)) for name, count in values)
    names = [name for name, _count in normalized]
    if any(not name for name in names):
        raise ValueError(f"{label} cannot contain an empty name")
    if len(names) != len(set(names)):
        raise ValueError(f"{label} repeats a name")
    if any(count < 1 for _name, count in normalized):
        raise ValueError(f"{label} requires positive minimums")
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class SemanticCoveragePolicy:
    """Versioned coverage policy with separate mart and task ownership minima.

    Pattern counts may overlap because a mart can have multiple patterns.
    """

    version: str = SEMANTIC_COVERAGE_POLICY_VERSION
    known_template_ids: tuple[str, ...] = ()
    known_patterns: tuple[str, ...] = ()
    require_explicit_template_id: bool = False
    require_explicit_patterns: bool = False
    require_certified_patterns: bool = False
    minimum_template_marts: tuple[tuple[str, int], ...] = ()
    minimum_template_tasks: tuple[tuple[str, int], ...] = ()
    minimum_pattern_marts: tuple[tuple[str, int], ...] = ()
    minimum_pattern_tasks: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("semantic coverage policy version must be non-empty")

        for field_name in ("known_template_ids", "known_patterns"):
            normalized = _normalize_names(
                getattr(self, field_name), label=field_name
            )
            object.__setattr__(self, field_name, normalized)

        for field_name in (
            "minimum_template_marts",
            "minimum_template_tasks",
            "minimum_pattern_marts",
            "minimum_pattern_tasks",
        ):
            normalized = _normalize_minimums(
                getattr(self, field_name), label=field_name
            )
            object.__setattr__(self, field_name, normalized)

        self._check_minimum_vocabulary(
            "template",
            self.known_template_ids,
            self.minimum_template_marts + self.minimum_template_tasks,
        )
        self._check_minimum_vocabulary(
            "pattern",
            self.known_patterns,
            self.minimum_pattern_marts + self.minimum_pattern_tasks,
        )

    @staticmethod
    def _check_minimum_vocabulary(
        label: str,
        known: tuple[str, ...],
        minimums: tuple[tuple[str, int], ...],
    ) -> None:
        if not known:
            return
        unknown = sorted({name for name, _count in minimums} - set(known))
        if unknown:
            raise ValueError(
                f"minimum {label} requirements name values outside the configured "
                f"vocabulary: {unknown}"
            )

    def canonical_contract(self) -> dict[str, object]:
        return {
            "version": self.version,
            "known_template_ids": list(self.known_template_ids),
            "known_patterns": list(self.known_patterns),
            "require_explicit_template_id": self.require_explicit_template_id,
            "require_explicit_patterns": self.require_explicit_patterns,
            "require_certified_patterns": self.require_certified_patterns,
            "minimum_template_marts": [
                [name, count] for name, count in self.minimum_template_marts
            ],
            "minimum_template_tasks": [
                [name, count] for name, count in self.minimum_template_tasks
            ],
            "minimum_pattern_marts": [
                [name, count] for name, count in self.minimum_pattern_marts
            ],
            "minimum_pattern_tasks": [
                [name, count] for name, count in self.minimum_pattern_tasks
            ],
        }

    @property
    def digest(self) -> str:
        return sha256_hex(canonical_json(self.canonical_contract()))


PERMISSIVE_SEMANTIC_COVERAGE_POLICY = SemanticCoveragePolicy()


@dataclass(frozen=True)
class CoverageOwners:
    """Owners of one template or semantic pattern, in canonical order."""

    name: str
    mart_owners: tuple[str, ...]
    task_owners: tuple[str, ...]

    @property
    def mart_count(self) -> int:
        return len(self.mart_owners)

    @property
    def task_count(self) -> int:
        return len(self.task_owners)

    def canonical_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "mart_count": self.mart_count,
            "task_count": self.task_count,
            "mart_owners": list(self.mart_owners),
            "task_owners": list(self.task_owners),
        }


@dataclass(frozen=True)
class CoverageDeficit:
    """One unmet configured minimum."""

    subject: str
    name: str
    owner_kind: str
    required: int
    observed: int

    @property
    def missing(self) -> int:
        return self.required - self.observed

    def canonical_contract(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "name": self.name,
            "owner_kind": self.owner_kind,
            "required": self.required,
            "observed": self.observed,
            "missing": self.missing,
        }

    def problem(self) -> str:
        return (
            f"cohort: {self.subject} {self.name!r} has {self.observed} "
            f"{self.owner_kind} owners; requires at least {self.required} "
            f"(deficit {self.missing})"
        )


@dataclass(frozen=True)
class CoverageTaskIdentity:
    """Hash-bound task identity included in a coverage report."""

    alias: str
    task_id: str
    content_hash: str

    def canonical_contract(self) -> dict[str, str]:
        return {
            "alias": self.alias,
            "task_id": self.task_id,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class SemanticCoverageReport:
    """Recomputable semantic coverage and every fail-closed finding."""

    policy: SemanticCoveragePolicy
    tasks: tuple[CoverageTaskIdentity, ...]
    mart_count: int
    templates: tuple[CoverageOwners, ...]
    patterns: tuple[CoverageOwners, ...]
    deficits: tuple[CoverageDeficit, ...]
    problems: tuple[str, ...]

    @property
    def task_count(self) -> int:
        return len(self.tasks)

    def canonical_contract(self) -> dict[str, object]:
        return {
            "schema_version": SEMANTIC_COVERAGE_REPORT_VERSION,
            "policy": self.policy.canonical_contract(),
            "policy_sha256": self.policy.digest,
            "tasks": [task.canonical_contract() for task in self.tasks],
            "task_count": self.task_count,
            "mart_count": self.mart_count,
            "templates": [item.canonical_contract() for item in self.templates],
            "patterns": [item.canonical_contract() for item in self.patterns],
            "deficits": [item.canonical_contract() for item in self.deficits],
            "problems": list(self.problems),
        }

    def to_canonical_json(self) -> str:
        return canonical_json(self.canonical_contract())

    @property
    def digest(self) -> str:
        return sha256_hex(self.to_canonical_json())


def _coverage_owners(
    owners: dict[str, set[str]],
    task_owners: dict[str, set[str]],
) -> tuple[CoverageOwners, ...]:
    return tuple(
        CoverageOwners(
            name=name,
            mart_owners=tuple(sorted(owners[name])),
            task_owners=tuple(sorted(task_owners[name])),
        )
        for name in sorted(owners)
    )


def _minimum_deficits(
    coverage: tuple[CoverageOwners, ...],
    minimums: tuple[tuple[str, int], ...],
    *,
    subject: str,
    owner_kind: str,
) -> list[CoverageDeficit]:
    observed_by_name = {
        item.name: item.mart_count if owner_kind == "mart" else item.task_count
        for item in coverage
    }
    return [
        CoverageDeficit(
            subject=subject,
            name=name,
            owner_kind=owner_kind,
            required=required,
            observed=observed_by_name.get(name, 0),
        )
        for name, required in minimums
        if observed_by_name.get(name, 0) < required
    ]


def measure_semantic_coverage(
    tasks: Iterable[tuple[str, TaskIR]],
    *,
    policy: SemanticCoveragePolicy = PERMISSIVE_SEMANTIC_COVERAGE_POLICY,
) -> SemanticCoverageReport:
    """Measure a cohort and return findings in ``report.problems``."""

    entries = tuple(sorted(tasks, key=lambda item: (item[0], item[1].task_id)))
    problems: list[str] = []

    aliases = [alias for alias, _task in entries]
    duplicate_aliases = sorted(
        alias for alias in set(aliases) if aliases.count(alias) > 1
    )
    if duplicate_aliases:
        problems.append(f"cohort: duplicate task aliases {duplicate_aliases}")

    identities = tuple(
        CoverageTaskIdentity(
            alias=alias,
            task_id=task.task_id,
            content_hash=task.content_hash(),
        )
        for alias, task in entries
    )

    template_marts: dict[str, set[str]] = defaultdict(set)
    template_tasks: dict[str, set[str]] = defaultdict(set)
    pattern_marts: dict[str, set[str]] = defaultdict(set)
    pattern_tasks: dict[str, set[str]] = defaultdict(set)
    mart_count = 0
    known_templates = set(policy.known_template_ids)
    known_patterns = set(policy.known_patterns)

    for alias, task in entries:
        for mart in sorted(task.marts, key=lambda item: item.name):
            mart_count += 1
            owner = f"{alias}/{mart.name}"
            template_id = _tag_value(getattr(mart.plan, "template_id", ""))
            if not template_id:
                template_id = MISSING_TEMPLATE_ID
                if policy.require_explicit_template_id:
                    problems.append(f"{owner}: missing template_id")
            elif known_templates and template_id not in known_templates:
                problems.append(
                    f"{owner}: unknown template_id {template_id!r}; configured "
                    f"values are {sorted(known_templates)}"
                )
            template_marts[template_id].add(owner)
            template_tasks[template_id].add(alias)

            raw_patterns = tuple(getattr(mart.plan, "semantic_patterns", ()))
            patterns = tuple(_tag_value(pattern) for pattern in raw_patterns)
            if any(not pattern for pattern in patterns):
                problems.append(f"{owner}: semantic_patterns contains an empty tag")
            if len(patterns) != len(set(patterns)):
                duplicates = sorted(
                    pattern
                    for pattern in set(patterns)
                    if patterns.count(pattern) > 1
                )
                problems.append(
                    f"{owner}: semantic_patterns contains duplicates {duplicates}"
                )
            if not patterns and policy.require_explicit_patterns:
                problems.append(f"{owner}: missing semantic_patterns")
            unknown_patterns = sorted(set(patterns) - known_patterns)
            if known_patterns and unknown_patterns:
                problems.append(
                    f"{owner}: unknown semantic_patterns {unknown_patterns}; "
                    f"configured values are {sorted(known_patterns)}"
                )
            for pattern in sorted(set(patterns) - {""}):
                pattern_marts[pattern].add(owner)
                pattern_tasks[pattern].add(alias)
            if policy.require_certified_patterns:
                problems.extend(
                    f"{owner}: {problem}"
                    for problem in semantic_pattern_problems(mart.plan)
                )

    templates = _coverage_owners(template_marts, template_tasks)
    patterns = _coverage_owners(pattern_marts, pattern_tasks)
    deficits = tuple(
        sorted(
            _minimum_deficits(
                templates,
                policy.minimum_template_marts,
                subject="template",
                owner_kind="mart",
            )
            + _minimum_deficits(
                templates,
                policy.minimum_template_tasks,
                subject="template",
                owner_kind="task",
            )
            + _minimum_deficits(
                patterns,
                policy.minimum_pattern_marts,
                subject="pattern",
                owner_kind="mart",
            )
            + _minimum_deficits(
                patterns,
                policy.minimum_pattern_tasks,
                subject="pattern",
                owner_kind="task",
            ),
            key=lambda item: (item.subject, item.name, item.owner_kind),
        )
    )
    problems.extend(deficit.problem() for deficit in deficits)

    return SemanticCoverageReport(
        policy=policy,
        tasks=identities,
        mart_count=mart_count,
        templates=templates,
        patterns=patterns,
        deficits=deficits,
        problems=tuple(sorted(set(problems))),
    )


def write_semantic_coverage(
    path: Path, report: SemanticCoverageReport
) -> str:
    """Write canonical report bytes and return their SHA-256 digest."""

    text = report.to_canonical_json() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return sha256_hex(text)


__all__ = [
    "MISSING_TEMPLATE_ID",
    "PERMISSIVE_SEMANTIC_COVERAGE_POLICY",
    "SEMANTIC_COVERAGE_POLICY_VERSION",
    "SEMANTIC_COVERAGE_REPORT_VERSION",
    "CoverageDeficit",
    "CoverageOwners",
    "CoverageTaskIdentity",
    "SemanticCoveragePolicy",
    "SemanticCoverageReport",
    "measure_semantic_coverage",
    "write_semantic_coverage",
]

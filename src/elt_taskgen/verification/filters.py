"""Run deterministic, side-effect-free filters before provider spend.

Filters cover execution effects, near-duplicate intake, and format blacklists using
recorded evidence. Missing evidence fails closed, and results can route or reject but
never accept.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from elt_taskgen.models import (
    MartOpKind,
    MartSpec,
    PopulationName,
    RepairRoute,
    Severity,
    TaskIR,
    canonical_json,
    sha256_hex,
)
from elt_taskgen.package_resources import resource_path

__all__ = [
    "EXECUTION_EFFECT_FILTER",
    "FILTERS_CONFIG_FILENAME",
    "FILTERS_DIRNAME",
    "FORMAT_BLACKLIST_FILTER",
    "NEAR_DUPLICATE_FILTER",
    "DEFAULT_FILTER_CONFIG_DOC",
    "ExecutionEffectConfig",
    "FilterAction",
    "FilterConfig",
    "FilterFinding",
    "FilterReport",
    "FormatBlacklistConfig",
    "FormatBlacklistRule",
    "NearDuplicateConfig",
    "canonical_report_text",
    "default_filters_config_path",
    "execution_effect_filter",
    "format_blacklist_filter",
    "jaccard",
    "load_filter_config",
    "mart_plan_tokens",
    "near_duplicate_filter",
    "normalize_tokens",
    "observable_outputs",
    "report_path",
    "run_intake_filters",
    "schema_shape_tokens",
    "write_filter_report",
]


#: Filter names — also the artifact basenames under reports/filters/.
EXECUTION_EFFECT_FILTER = "execution-effect"
NEAR_DUPLICATE_FILTER = "near-duplicate-intake"
FORMAT_BLACKLIST_FILTER = "intake-format-blacklist"

#: Directory (under tasks/<task_id>/reports/) holding the filter artifacts.
FILTERS_DIRNAME = "filters"

FILTERS_CONFIG_FILENAME = "filters.yaml"

#: How the two near-duplicate similarity axes may be combined.
COMBINE_MODES: tuple[str, ...] = ("min", "max", "mean")

#: Known rule kinds. An unknown kind fails closed at load (else silent no-op).
BLACKLIST_RULE_KINDS: tuple[str, ...] = (
    "single_table_no_join",
    "min_source_tables",
    "min_plan_ops",
    "min_mart_columns",
    "distinct_mart_plans",
    "reserved_identifiers",
)


class FilterAction(str, Enum):
    """What a tripped filter does. There is deliberately no 'warn' value: a
    filter finding either routes to a repair or rejects the candidate."""

    ROUTE = "route"      # fail the stage with a RepairRoute (repairable)
    REJECT = "reject"    # fatal: the candidate is rejected outright


# Config

class ExecutionEffectConfig(BaseModel):
    """Which populations must demonstrably change an observable output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    #: Populations that must differ from the baseline (canonically sorted).
    populations: tuple[PopulationName, ...] = (
        PopulationName.COUNTERFACTUAL,
        PopulationName.STRESS,
    )
    baseline: PopulationName = PopulationName.PRIMARY
    action: FilterAction = FilterAction.ROUTE
    route: RepairRoute = RepairRoute.POPULATION

    @model_validator(mode="after")
    def _check(self) -> "ExecutionEffectConfig":
        if self.baseline in self.populations:
            raise ValueError(
                "execution_effect: the baseline population cannot also be a "
                "population that must differ from the baseline"
            )
        if self.action is FilterAction.ROUTE and self.route is RepairRoute.FATAL:
            raise ValueError("execution_effect: route 'fatal' must use action 'reject'")
        return self


class NearDuplicateConfig(BaseModel):
    """Token-Jaccard near-duplicate detection against admitted tasks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    #: Combined similarity at or above this against an admitted task = clone.
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    #: Combine mode; 'min' is conservative — a shared schema alone is no clone.
    combine: str = "min"
    action: FilterAction = FilterAction.REJECT
    route: RepairRoute | None = None

    @model_validator(mode="after")
    def _check(self) -> "NearDuplicateConfig":
        if self.combine not in COMBINE_MODES:
            raise ValueError(
                f"near_duplicate: unknown combine {self.combine!r} "
                f"(known: {sorted(COMBINE_MODES)})"
            )
        if self.action is FilterAction.ROUTE and self.route is None:
            raise ValueError("near_duplicate: action 'route' requires a route")
        return self


class FormatBlacklistRule(BaseModel):
    """One blacklisted candidate SHAPE, by rule kind + integer parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    params: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_kind(self) -> "FormatBlacklistRule":
        if self.kind not in BLACKLIST_RULE_KINDS:
            raise ValueError(
                f"format_blacklist rule {self.name!r}: unknown kind "
                f"{self.kind!r} (known: {sorted(BLACKLIST_RULE_KINDS)})"
            )
        return self


class FormatBlacklistConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    action: FilterAction = FilterAction.REJECT
    route: RepairRoute | None = None
    rules: tuple[FormatBlacklistRule, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "FormatBlacklistConfig":
        if self.action is FilterAction.ROUTE and self.route is None:
            raise ValueError("format_blacklist: action 'route' requires a route")
        names = [r.name for r in self.rules]
        if len(names) != len(set(names)):
            raise ValueError("format_blacklist: duplicate rule names")
        return self


class FilterConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_effect: ExecutionEffectConfig = ExecutionEffectConfig()
    near_duplicate: NearDuplicateConfig = NearDuplicateConfig()
    format_blacklist: FormatBlacklistConfig = FormatBlacklistConfig()
    source: str = "(embedded defaults)"


#: Embedded copy of config/filters.yaml (the repo file wins when present).
DEFAULT_FILTER_CONFIG_DOC: dict = {
    "execution_effect": {
        "enabled": True,
        "populations": ["counterfactual", "stress"],
        "baseline": "primary",
        "action": "route",
        "route": "population",
    },
    "near_duplicate": {
        "enabled": True,
        "threshold": 0.85,
        "combine": "min",
        "action": "reject",
    },
    "format_blacklist": {
        "enabled": True,
        "action": "reject",
        "rules": [
            {
                "name": "single-table-no-join",
                "kind": "single_table_no_join",
                "params": {"max_tables": 1},
            },
            {
                "name": "too-few-source-tables",
                "kind": "min_source_tables",
                "params": {"min_tables": 2},
            },
            {
                "name": "trivial-mart-plan",
                "kind": "min_plan_ops",
                "params": {"min_ops": 2},
            },
            {
                "name": "single-column-mart",
                "kind": "min_mart_columns",
                "params": {"min_columns": 2},
            },
            {
                "name": "duplicate-mart-contract",
                "kind": "distinct_mart_plans",
                "params": {},
            },
        ],
    },
}


def _repo_root() -> Path:
    """Compatibility helper returning the shipped resource root."""

    return resource_path("config").parent


def default_filters_config_path() -> Path:
    return _repo_root() / "config" / FILTERS_CONFIG_FILENAME


def load_filter_config(path: Path | None = None) -> FilterConfig:
    """Load config/filters.yaml (thresholds + blacklist rules).

    An explicit `path` must exist (fail closed); with none, the repo config
    wins when present, else the embedded defaults. Unknown keys, unknown rule
    kinds and out-of-range thresholds raise.
    """
    source = "(embedded defaults)"
    doc: Mapping[str, Any] = DEFAULT_FILTER_CONFIG_DOC
    if path is not None:
        if not Path(path).is_file():
            raise FileNotFoundError(f"filters config not found: {path} (fail closed)")
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        source = str(path)
    else:
        default = default_filters_config_path()
        if default.is_file():
            doc = yaml.safe_load(default.read_text(encoding="utf-8")) or {}
            source = str(default)
    payload = {k: v for k, v in dict(doc).items() if k != "source"}
    return FilterConfig(**payload, source=source)


# Findings + reports (the ledger-visible artifacts)

class FilterFinding(BaseModel):
    """One deterministic filter observation. Routes or rejects; never accepts.

    Deliberately NOT `models.Finding`: attributing code-certified evidence to a
    council seat (that type's `role`) would misreport who found what.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filter: str = Field(min_length=1)
    #: What the finding is about: a population name, a rule name, a task id.
    subject: str = Field(min_length=1)
    severity: Severity = Severity.MAJOR
    summary: str = Field(min_length=1)
    detail: str = ""
    route: RepairRoute | None = None


class FilterReport(BaseModel):
    """The artifact one filter emits — recorded pass or fail (never dropped)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    filter: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    #: TaskIR content hash this verdict binds to (stale evidence is not evidence).
    task_content_hash: str = Field(min_length=64, max_length=64)
    passed: bool
    #: The configured consequence; None iff the filter passed.
    action: FilterAction | None = None
    route: RepairRoute | None = None
    findings: tuple[FilterFinding, ...] = ()
    #: Stringified measurements (scores, compared populations, rule params).
    evidence: dict[str, str] = Field(default_factory=dict)
    detail: str = ""
    config_source: str = ""

    @model_validator(mode="after")
    def _fail_closed(self) -> "FilterReport":
        if self.passed and (self.findings or self.action is not None):
            raise ValueError(
                "a passing FilterReport cannot carry findings or an action "
                "(a tripped filter is never recorded as a pass)"
            )
        if not self.passed:
            if not self.findings:
                raise ValueError("a failing FilterReport must carry at least one finding")
            if self.action is None:
                raise ValueError("a failing FilterReport must carry an action")
            if self.action is FilterAction.ROUTE and self.route is None:
                raise ValueError("a routed FilterReport must name its repair route")
        return self


def canonical_report_text(report: FilterReport) -> str:
    """Canonical JSON of a filter report (deterministic, wall-clock free)."""
    return canonical_json(report.model_dump(mode="json"))


def report_path(reports_dir: Path, filter_name: str) -> Path:
    return Path(reports_dir) / FILTERS_DIRNAME / f"{filter_name}.json"


def write_filter_report(report: FilterReport, reports_dir: Path) -> tuple[Path, str]:
    """Write the artifact under <reports_dir>/filters/<filter>.json.

    Returns (path, sha256) for the artifacts ledger — every filter run is
    visible, pass or fail.
    """
    path = report_path(reports_dir, report.filter)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = canonical_report_text(report)
    path.write_text(text, encoding="utf-8")
    return path, sha256_hex(text)


# 1. Execution-effect filter

def observable_outputs(gold: Any, population: str) -> dict[str, str] | None:
    """The recorded reference-runner outputs of one population, as text.

    `gold` is duck-typed on `stage1`/`stage2_csv` so this module executes
    nothing. None when the population has no recorded outputs (the caller fails
    closed), else {'stage1': counts JSON, 'mart:<name>': canonical CSV}.
    """
    stage1 = dict(getattr(gold, "stage1", {}) or {})
    stage2 = dict(getattr(gold, "stage2_csv", {}) or {})
    if population not in stage1 and population not in stage2:
        return None
    outputs: dict[str, str] = {
        "stage1": canonical_json(dict(stage1.get(population) or {}))
    }
    for mart, csv_text in sorted(dict(stage2.get(population) or {}).items()):
        outputs[f"mart:{mart}"] = csv_text
    return outputs


def execution_effect_filter(
    task: TaskIR, gold: Any, config: FilterConfig | None = None
) -> FilterReport:
    """Each configured population must change >= 1 observable output vs primary.

    Reuses the RECORDED reference outputs; runs nothing. A population identical
    to the baseline cannot distinguish correct logic from wrong, so it is
    routed; a missing baseline or missing outputs is a failure, never a pass.
    """
    config = config or load_filter_config()
    cfg = config.execution_effect
    findings: list[FilterFinding] = []
    evidence: dict[str, str] = {
        "baseline": cfg.baseline.value,
        "populations": ",".join(p.value for p in cfg.populations),
    }
    declared = {p.name for p in task.populations}

    if not cfg.enabled:
        return FilterReport(
            filter=EXECUTION_EFFECT_FILTER,
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            passed=True,
            evidence={**evidence, "enabled": "false"},
            detail="execution-effect filter disabled by config",
            config_source=config.source,
        )

    bound_hash = getattr(gold, "task_content_hash", None)
    if bound_hash is not None and bound_hash != task.content_hash():
        # Stale evidence is not evidence: another identity's bundle says nothing.
        findings.append(
            FilterFinding(
                filter=EXECUTION_EFFECT_FILTER,
                subject="gold",
                severity=Severity.MAJOR,
                summary=(
                    "the recorded reference outputs are bound to content hash "
                    f"{str(bound_hash)[:12]}, task is {task.content_hash()[:12]}"
                ),
                detail=(
                    "re-run the reference stage so the execution-effect filter "
                    "compares populations at the current identity (fail closed)"
                ),
                route=cfg.route,
            )
        )
        evidence["gold_content_hash"] = str(bound_hash)

    baseline = observable_outputs(gold, cfg.baseline.value)
    if baseline is None:
        findings.append(
            FilterFinding(
                filter=EXECUTION_EFFECT_FILTER,
                subject=cfg.baseline.value,
                severity=Severity.MAJOR,
                summary=(
                    f"no recorded reference outputs for the baseline population "
                    f"{cfg.baseline.value!r}"
                ),
                detail=(
                    "the execution-effect filter compares every counterfactual/"
                    "stress population against the baseline's recorded runner "
                    "outputs; without the baseline there is nothing to compare "
                    "against (fail closed)"
                ),
                route=cfg.route,
            )
        )
    else:
        for pop in cfg.populations:
            if pop not in declared:
                evidence[f"{pop.value}:declared"] = "false"
                continue
            outputs = observable_outputs(gold, pop.value)
            if outputs is None:
                findings.append(
                    FilterFinding(
                        filter=EXECUTION_EFFECT_FILTER,
                        subject=pop.value,
                        severity=Severity.MAJOR,
                        summary=(
                            f"population {pop.value!r} is declared but has no "
                            "recorded reference outputs"
                        ),
                        detail=(
                            "a declared population with no recorded runner "
                            "outputs cannot be shown to distinguish anything "
                            "(fail closed)"
                        ),
                        route=cfg.route,
                    )
                )
                continue
            changed = sorted(
                key
                for key in sorted(set(outputs) | set(baseline))
                if outputs.get(key) != baseline.get(key)
            )
            evidence[f"{pop.value}:changed_outputs"] = ",".join(changed)
            if changed:
                continue
            findings.append(
                FilterFinding(
                    filter=EXECUTION_EFFECT_FILTER,
                    subject=pop.value,
                    severity=Severity.MAJOR,
                    summary=(
                        f"population {pop.value!r} changes no observable output "
                        f"versus {cfg.baseline.value!r}"
                    ),
                    detail=(
                        f"stage-1 row counts and every stage-2 mart CSV are "
                        f"byte-identical to the {cfg.baseline.value!r} population "
                        f"({', '.join(sorted(outputs))}). A population that "
                        "produces the same outputs as the baseline cannot "
                        "distinguish correct logic from wrong logic — its "
                        "conditions must be changed so at least one observable "
                        "output moves."
                    ),
                    route=cfg.route,
                )
            )

    if findings:
        return FilterReport(
            filter=EXECUTION_EFFECT_FILTER,
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            passed=False,
            action=cfg.action,
            route=cfg.route if cfg.action is FilterAction.ROUTE else None,
            findings=tuple(findings),
            evidence=evidence,
            detail=(
                f"{len(findings)} population(s) fail the execution-effect "
                f"filter (no observable change vs {cfg.baseline.value})"
            ),
            config_source=config.source,
        )
    return FilterReport(
        filter=EXECUTION_EFFECT_FILTER,
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        passed=True,
        evidence=evidence,
        detail=(
            "every configured population changes at least one observable "
            f"output versus {cfg.baseline.value}"
        ),
        config_source=config.source,
    )


# 2. Near-duplicate intake dedup

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def normalize_tokens(*texts: str) -> frozenset[str]:
    """Lowercase, split on non-alphanumerics, drop pure digits, de-pluralize.

    Crude and deterministic on purpose: renamed or reformatted clones must
    normalize onto the same token set. This is not linguistics.
    """
    out: set[str] = set()
    for text in texts:
        for raw in _TOKEN_SPLIT.split(str(text).lower()):
            if not raw or raw.isdigit():
                continue
            if len(raw) > 3 and raw.endswith("s") and not raw.endswith("ss"):
                raw = raw[:-1]
            out.add(raw)
    return frozenset(out)


def schema_shape_tokens(task: TaskIR) -> frozenset[str]:
    """Normalized SHAPE of the source schema: identifiers plus structure.

    Identity fields (task_id, family_id, title, license) are excluded on
    purpose — a clone filed under a new id is what this must catch; the
    'shape:...' tokens make a RENAMED clone match too.
    """
    tokens: set[str] = set()
    tokens.add(f"shape:tables:{len(task.tables)}")
    for table in sorted(task.tables, key=lambda t: t.name):
        tokens |= normalize_tokens(table.name, table.description)
        tokens.add(f"shape:table:cols={len(table.columns)}:pk={len(table.primary_key)}")
        for column in sorted(table.columns, key=lambda c: c.name):
            tokens |= normalize_tokens(column.name, column.description)
            tokens.add(f"shape:coltype:{column.type.value}")
            if column.enum_values:
                tokens.add(f"shape:enum:{len(column.enum_values)}")
    for rel in sorted(
        task.relationships, key=lambda r: (r.child_table, r.parent_table)
    ):
        tokens.add(
            f"shape:rel:{'req' if rel.required else 'opt'}:{len(rel.child_columns)}"
        )
        tokens |= normalize_tokens(*rel.child_columns, *rel.parent_columns)
    for assignment in sorted(task.backends, key=lambda b: b.table):
        tokens.add(f"shape:backend:{assignment.backend.value}")
    return frozenset(tokens)


def mart_plan_tokens(task: TaskIR) -> frozenset[str]:
    """Normalized SHAPE of what the task computes (marts + declarative plans)."""
    tokens: set[str] = set()
    tokens.add(f"plan:marts:{len(task.marts)}")
    for mart in sorted(task.marts, key=lambda m: m.name):
        tokens |= normalize_tokens(mart.name, mart.description, mart.grain)
        tokens |= normalize_tokens(*mart.key_columns)
        tokens.add(f"plan:mart:cols={len(mart.columns)}:keys={len(mart.key_columns)}")
        for column in sorted(mart.columns, key=lambda c: c.name):
            tokens |= normalize_tokens(column.name, column.description)
            tokens.add(f"plan:coltype:{column.type.value}")
        tokens.add(f"plan:ops:{len(mart.plan.ops)}")
        for op in mart.plan.ops:
            tokens.add(f"plan:op:{op.kind.value}")
            if op.join_type is not None:
                tokens.add(f"plan:join:{op.join_type.value}")
            tokens |= normalize_tokens(op.description, op.predicate)
            tokens |= normalize_tokens(*op.tables, *op.columns)
            for key in sorted(op.details):
                tokens |= normalize_tokens(key, op.details[key])
        tokens |= normalize_tokens(mart.plan.notes)
    return frozenset(tokens)


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    """|A ∩ B| / |A ∪ B|; two empty sets are identical (1.0), by convention."""
    a, b = set(left), set(right)
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union)


_COMBINERS: dict[str, Any] = {
    "min": min,
    "max": max,
    "mean": lambda a, b: (a + b) / 2.0,
}


def _combined(schema: float, plan: float, how: str) -> float:
    return float(_COMBINERS[how](schema, plan))


def near_duplicate_filter(
    task: TaskIR,
    admitted: Sequence[TaskIR],
    config: FilterConfig | None = None,
) -> FilterReport:
    """Reject a candidate that near-duplicates an ALREADY-ADMITTED task.

    Token Jaccard on schema shape and mart plan, combined per config (default
    `min`: sharing a schema while computing something else is not duplication).
    The candidate is excluded from `admitted` by task_id.
    """
    config = config or load_filter_config()
    cfg = config.near_duplicate
    evidence: dict[str, str] = {
        "threshold": f"{cfg.threshold:.4f}",
        "combine": cfg.combine,
        "compared_against": str(len([t for t in admitted if t.task_id != task.task_id])),
    }
    if not cfg.enabled:
        return FilterReport(
            filter=NEAR_DUPLICATE_FILTER,
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            passed=True,
            evidence={**evidence, "enabled": "false"},
            detail="near-duplicate filter disabled by config",
            config_source=config.source,
        )

    schema_self = schema_shape_tokens(task)
    plan_self = mart_plan_tokens(task)
    findings: list[FilterFinding] = []
    best_id, best_score = "", 0.0
    for other in sorted(admitted, key=lambda t: t.task_id):
        if other.task_id == task.task_id:
            continue
        schema_score = jaccard(schema_self, schema_shape_tokens(other))
        plan_score = jaccard(plan_self, mart_plan_tokens(other))
        score = _combined(schema_score, plan_score, cfg.combine)
        evidence[f"score:{other.task_id}"] = (
            f"schema={schema_score:.4f} plan={plan_score:.4f} "
            f"{cfg.combine}={score:.4f}"
        )
        if score > best_score:
            best_id, best_score = other.task_id, score
        if score >= cfg.threshold:
            findings.append(
                FilterFinding(
                    filter=NEAR_DUPLICATE_FILTER,
                    subject=other.task_id,
                    severity=Severity.FATAL
                    if cfg.action is FilterAction.REJECT
                    else Severity.MAJOR,
                    summary=(
                        f"candidate near-duplicates admitted task "
                        f"{other.task_id!r} ({cfg.combine} Jaccard "
                        f"{score:.4f} >= {cfg.threshold:.4f})"
                    ),
                    detail=(
                        f"normalized schema-shape Jaccard {schema_score:.4f}, "
                        f"mart-plan Jaccard {plan_score:.4f} against admitted "
                        f"task {other.task_id!r} (family {other.family_id!r}). "
                        "A near-clone adds no corpus coverage and endangers "
                        "family/split isolation."
                    ),
                    route=cfg.route,
                )
            )
    evidence["max_similarity"] = f"{best_score:.4f}"
    evidence["max_similarity_task"] = best_id or "(none)"

    if findings:
        return FilterReport(
            filter=NEAR_DUPLICATE_FILTER,
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            passed=False,
            action=cfg.action,
            route=cfg.route if cfg.action is FilterAction.ROUTE else None,
            findings=tuple(findings),
            evidence=evidence,
            detail=f"{len(findings)} admitted task(s) within the duplicate threshold",
            config_source=config.source,
        )
    return FilterReport(
        filter=NEAR_DUPLICATE_FILTER,
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        passed=True,
        evidence=evidence,
        detail=(
            f"max similarity {best_score:.4f} < threshold {cfg.threshold:.4f} "
            f"over {evidence['compared_against']} admitted task(s)"
        ),
        config_source=config.source,
    )


# 3. Intake format blacklist

def _rule_single_table_no_join(task: TaskIR, params: Mapping[str, int]) -> str | None:
    max_tables = int(params.get("max_tables", 1))
    joins = [
        op
        for mart in task.marts
        for op in mart.plan.ops
        if op.kind is MartOpKind.JOIN
    ]
    if len(task.tables) <= max_tables and not joins:
        return (
            f"{len(task.tables)} source table(s) (<= {max_tables}) and no JOIN op "
            "in any mart plan: the answer is enumerable from one table rather "
            "than computed across a data model"
        )
    return None


def _rule_min_source_tables(task: TaskIR, params: Mapping[str, int]) -> str | None:
    minimum = int(params.get("min_tables", 2))
    if len(task.tables) < minimum:
        return f"{len(task.tables)} source table(s) < required minimum {minimum}"
    return None


def _rule_min_plan_ops(task: TaskIR, params: Mapping[str, int]) -> str | None:
    minimum = int(params.get("min_ops", 2))
    thin = sorted(
        mart.name for mart in task.marts if len(mart.plan.ops) < minimum
    )
    if thin:
        return (
            f"mart(s) {', '.join(thin)} declare fewer than {minimum} plan ops: "
            "a one-step transform has no wrong-logic surface to grade"
        )
    return None


def _rule_min_mart_columns(task: TaskIR, params: Mapping[str, int]) -> str | None:
    minimum = int(params.get("min_columns", 2))
    thin = sorted(
        mart.name for mart in task.marts if len(mart.columns) < minimum
    )
    if thin:
        return (
            f"mart(s) {', '.join(thin)} emit fewer than {minimum} columns: "
            "the output space is small enough to enumerate"
        )
    return None


def mart_contract_signature(mart: MartSpec) -> str:
    """Return a mart's computation signature independent of its own name.

    The signature binds grain keys, output columns, and plan operations with
    identifier-boundary awareness. Prose is excluded. Equal signatures indicate the same
    computation under different names; residual exact-name collisions reject.
    """
    ops = canonical_json([op.model_dump(mode="json") for op in mart.plan.ops])
    factored = re.sub(
        rf"(?<![A-Za-z0-9_]){re.escape(mart.name)}(?![A-Za-z0-9_])", "<mart>", ops
    )
    return canonical_json(
        {
            "key_columns": list(mart.key_columns),
            "columns": [[c.name, c.type.value] for c in mart.columns],
            "ops": factored,
        }
    )


def _rule_distinct_mart_plans(task: TaskIR, params: Mapping[str, int]) -> str | None:
    """No two marts of one task may share a contract: agents propose, code
    certifies (a council 'major' on a clone is not a gate)."""
    by_signature: dict[str, list[str]] = {}
    for mart in task.marts:
        by_signature.setdefault(mart_contract_signature(mart), []).append(mart.name)
    clones = sorted(
        names for names in by_signature.values() if len(names) > 1
    )
    if clones:
        groups = "; ".join(" == ".join(sorted(names)) for names in clones)
        return (
            f"mart(s) {groups} share an identical contract (grain key, output "
            "columns and plan ops differ only by mart name): the second is a "
            "byte-clone of the first, so a solver copies one mart into the "
            "other and the duplicated share of the transform reward grades "
            "nothing"
        )
    return None


_reserved_words_cache: frozenset[str] | None = None


#: Keyword categories for opt-in policies that reject reserved identifiers.
RESERVED_KEYWORD_CATEGORIES: tuple[str, ...] = ("reserved", "type_function")


def duckdb_reserved_words() -> frozenset[str]:
    """Return the executor keyword vocabulary for the opt-in diagnostic.

    Read from the executor's own `duckdb_keywords()`, not a hard-coded list, so
    a custom policy moves with the engine instead of drifting behind it.
    """
    global _reserved_words_cache
    if _reserved_words_cache is None:
        from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection

        con = sandboxed_memory_connection()
        try:
            placeholders = ", ".join("?" for _ in RESERVED_KEYWORD_CATEGORIES)
            rows = con.execute(
                "SELECT keyword_name FROM duckdb_keywords() "
                f"WHERE keyword_category IN ({placeholders})",
                list(RESERVED_KEYWORD_CATEGORIES),
            ).fetchall()
        finally:
            con.close()
        _reserved_words_cache = frozenset(str(r[0]).lower() for r in rows)
    return _reserved_words_cache


def _rule_reserved_identifiers(task: TaskIR, params: Mapping[str, int]) -> str | None:
    """Apply an opt-in policy forbidding DuckDB keyword relation names.

    The default intake policy does not use this rule: compiler, loader,
    evaluator, and export paths quote unsafe identifiers. Keeping the predicate
    lets an explicit custom policy impose a narrower catalog-naming contract.
    """
    reserved = duckdb_reserved_words()
    hits = sorted(
        {f"table {t.name!r}" for t in task.tables if t.name.lower() in reserved}
        | {f"mart {m.name!r}" for m in task.marts if m.name.lower() in reserved}
    )
    if hits:
        return (
            f"{', '.join(hits)} named after a DuckDB reserved keyword "
            f"(categories {', '.join(RESERVED_KEYWORD_CATEGORIES)}): disallowed "
            "by the configured reserved-identifier naming policy"
        )
    return None


#: Rule kind -> predicate returning the violation detail, or None when clean.
_BLACKLIST_RULES: dict[str, Any] = {
    "single_table_no_join": _rule_single_table_no_join,
    "min_source_tables": _rule_min_source_tables,
    "min_plan_ops": _rule_min_plan_ops,
    "min_mart_columns": _rule_min_mart_columns,
    "distinct_mart_plans": _rule_distinct_mart_plans,
    "reserved_identifiers": _rule_reserved_identifiers,
}

# A config-accepted kind with no implementation would be a SILENT no-op filter.
# Raise rather than assert so the check survives `python -O`.
if set(_BLACKLIST_RULES) != set(BLACKLIST_RULE_KINDS):
    raise RuntimeError(
        "blacklist rule vocabulary drift: "
        f"declared-not-implemented={sorted(set(BLACKLIST_RULE_KINDS) - set(_BLACKLIST_RULES))}, "
        f"implemented-not-declared={sorted(set(_BLACKLIST_RULES) - set(BLACKLIST_RULE_KINDS))}"
    )


def format_blacklist_filter(
    task: TaskIR, config: FilterConfig | None = None
) -> FilterReport:
    """Reject candidate SHAPES whose answers are enumerable, not computable."""
    config = config or load_filter_config()
    cfg = config.format_blacklist
    evidence: dict[str, str] = {"rules": ",".join(r.name for r in cfg.rules)}
    if not cfg.enabled:
        return FilterReport(
            filter=FORMAT_BLACKLIST_FILTER,
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            passed=True,
            evidence={**evidence, "enabled": "false"},
            detail="format blacklist disabled by config",
            config_source=config.source,
        )

    findings: list[FilterFinding] = []
    for rule in cfg.rules:
        predicate = _BLACKLIST_RULES[rule.kind]
        violation = predicate(task, rule.params)
        evidence[f"rule:{rule.name}"] = "tripped" if violation else "clean"
        if violation is None:
            continue
        findings.append(
            FilterFinding(
                filter=FORMAT_BLACKLIST_FILTER,
                subject=rule.name,
                severity=Severity.FATAL
                if cfg.action is FilterAction.REJECT
                else Severity.MAJOR,
                summary=f"blacklisted candidate shape: {rule.name}",
                detail=f"rule {rule.name!r} ({rule.kind}): {violation}",
                route=cfg.route,
            )
        )

    if findings:
        return FilterReport(
            filter=FORMAT_BLACKLIST_FILTER,
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            passed=False,
            action=cfg.action,
            route=cfg.route if cfg.action is FilterAction.ROUTE else None,
            findings=tuple(findings),
            evidence=evidence,
            detail=f"{len(findings)} blacklist rule(s) tripped",
            config_source=config.source,
        )
    return FilterReport(
        filter=FORMAT_BLACKLIST_FILTER,
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        passed=True,
        evidence=evidence,
        detail=f"{len(cfg.rules)} blacklist rule(s) clean",
        config_source=config.source,
    )


# Intake bundle

def run_intake_filters(
    task: TaskIR,
    admitted: Sequence[TaskIR],
    config: FilterConfig | None = None,
) -> tuple[FilterReport, ...]:
    """The two INTAKE filters, in fixed order (blacklist, then dedup).

    Both reports are always returned — the caller records both artifacts even
    when the first already blocks, so the ledger shows what was checked.
    """
    config = config or load_filter_config()
    return (
        format_blacklist_filter(task, config),
        near_duplicate_filter(task, admitted, config),
    )

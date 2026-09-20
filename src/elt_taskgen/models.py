"""Define the canonical typed TaskIR and supporting pipeline models.

Models are frozen Pydantic v2 objects with forbidden extras. Canonical content
uses sorted compact JSON; only ``status`` and ``revisions`` are outside the hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

SCHEMA_VERSION = "1.0.0"

# JSON-scalar value as it appears in generated rows and literal fixtures.
Scalar = str | int | float | bool | None
# One literal source row: column name -> scalar value.
Row = dict[str, Scalar]
# Proposal parameters support ordered identifier tuples, not arbitrary nested JSON.
ProposalParam = Scalar | tuple[str, ...]


# --- Canonical serialization + hashing helpers ---

def canonical_json(data: Any) -> str:
    """Return the compact, sorted, finite-JSON encoding used for hashes."""
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def readable_json(data: Any) -> str:
    """Return deterministic indented JSON for on-disk records."""
    return json.dumps(
        data, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_seed(*parts: str | int) -> int:
    """Derive a deterministic non-negative 63-bit seed from identity parts."""
    material = "\x1f".join(str(p) for p in parts)
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


class CanonicalModel(BaseModel):
    """Base for all IR models: frozen, strict, canonically hashable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Field names excluded from canonical_dump()/content_hash(). Volatile
    #: fields carry pipeline progress, not task semantics.
    VOLATILE_FIELDS: ClassVar[frozenset[str]] = frozenset()

    def canonical_dump(self, *, include_volatile: bool = False) -> dict[str, Any]:
        exclude = None if include_volatile else set(self.VOLATILE_FIELDS) or None
        return self.model_dump(mode="json", exclude=exclude)

    def to_canonical_json(self, *, include_volatile: bool = False) -> str:
        return canonical_json(self.canonical_dump(include_volatile=include_volatile))

    def content_hash(self) -> str:
        """SHA-256 of the canonical JSON, excluding volatile fields."""
        return sha256_hex(self.to_canonical_json(include_volatile=False))


# --- Enums ---

class Origin(str, Enum):
    """Which source pool a task came from (namespaces family ids)."""

    DBT = "dbt"
    SYNSQL = "synsql"
    DLT = "dlt"
    SCHEMAPILE = "schemapile"  # Round 4: schemapile-perm.json, per-record license
    WIKIDBS = "wikidbs"        # Round 4: WikiDBs part-N dirs, CC BY 4.0
    ELTBENCH_ANCHOR = "eltbench_anchor"  # measurement only — never released
    SYNTHETIC = "synthetic"
    DEMO = "demo"


class Backend(str, Enum):
    """Source-side backends a table can be materialized into."""

    POSTGRES = "postgres"
    MONGODB = "mongodb"
    REST = "rest"
    S3 = "s3"
    FILES = "files"


class ColumnType(str, Enum):
    """Canonical logical column types (renderers map these per backend)."""

    INTEGER = "integer"
    BIGINT = "bigint"
    FLOAT = "float"
    DECIMAL = "decimal"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    JSON = "json"


class JoinType(str, Enum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"
    CROSS = "cross"


class MartOpKind(str, Enum):
    """Relational operation kinds used for routing and difficulty measurement."""

    SOURCE = "source"        # bring a source table into scope
    FILTER = "filter"
    JOIN = "join"
    AGGREGATE = "aggregate"
    #: GROUP BY where a measure filters INSIDE the aggregate. Never a WHERE: that
    #: drops the childless parent and turns a 0 into a missing row.
    FILTERED_AGGREGATE = "filtered_aggregate"
    #: GROUP BY with COUNT(DISTINCT ...) — the fan-out-sensitive count.
    DISTINCT = "distinct"
    #: ARGMAX, not MAX: a NON-aggregate attribute of the row at which a measure
    #: is extremal, under an explicit total order (measure + tie-break).
    EXTREMA = "extrema"
    WINDOW = "window"
    DEDUPE = "dedupe"
    DERIVE = "derive"        # computed column, incl. COALESCE/null-defaults
    #: CASE ladder (categorical or ordered thresholds) with a MANDATORY ELSE.
    CONDITIONAL = "conditional"
    #: Guarded division: CAST to double, NULLIF denominator, pinned rounding/units.
    RATIO = "ratio"
    UNION = "union"
    TIE_BREAK = "tie_break"  # explicit deterministic ordering/tie resolution


class SemanticPattern(str, Enum):
    """Closed vocabulary for declared compositions of mart operations."""

    AGGREGATE_THEN_FILTER = "aggregate_then_filter"
    NESTED_AGGREGATE = "nested_aggregate"
    GROUP_GLOBAL_BASELINE = "group_global_baseline"
    AGGREGATE_THEN_EXTREMA = "aggregate_then_extrema"
    WEIGHTED_AVERAGE = "weighted_average"
    FIXED_PERIOD_COMPARISON = "fixed_period_comparison"
    MULTI_GRAIN_TIME = "multi_grain_time"
    CONSECUTIVE_STREAK = "consecutive_streak"
    SCD_ASOF = "scd_asof"
    MULTI_DIMENSION_LATEST = "multi_dimension_latest"
    ORDERED_COLLECTION = "ordered_collection"
    PIVOT = "pivot"
    JSON_EXPANSION = "json_expansion"
    SURROGATE_KEY = "surrogate_key"
    NORMALIZATION = "normalization"
    SESSIONIZATION = "sessionization"
    TEMPORAL_CORRECTION = "temporal_correction"


class PopulationName(str, Enum):
    DEVELOPMENT = "development"      # tiny, solver-visible, for debugging
    PRIMARY = "primary"              # hidden reward population
    RESAMPLED = "resampled"          # new seed/ids — memorization check
    COUNTERFACTUAL = "counterfactual"  # constructed to break wrong logic
    STRESS = "stress"                # scale, skew, duplicates, ties


class AttackKind(str, Enum):
    """Standing catalogue of wrong-logic mutations gates must exercise."""

    INNER_JOIN = "inner_join"              # LEFT->INNER: drops unmatched rows
    NO_DEDUP = "no_dedup"                  # drop DISTINCT / dedupe step
    WRONG_GRAIN = "wrong_grain"            # aggregate at the wrong grain
    WRONG_AGG_STAGE = "wrong_agg_stage"    # filter/rank at the wrong agg stage
    WRONG_DENOMINATOR = "wrong_denominator"
    CONSTANTS = "constants"                # hard-coded outputs
    KEYS_ONLY = "keys_only"                # emit keys, junk measures
    SKIP_EXTRACTION = "skip_extraction"    # omit a source backend entirely
    DROPPED_FILTER = "dropped_filter"
    WRONG_WINDOW = "wrong_window"
    NO_NULL_DEFAULT = "no_null_default"    # drop COALESCE: NULL vs 0
    NO_OP = "no_op"                        # submit nothing / empty outputs
    CUSTOM = "custom"                      # compiled from a specific finding
# Extract-load mutations need distinct kinds for the EL required-mutants gate.
    PARTIAL_BACKEND = "partial_backend"    # one table never loaded at all
    DUPLICATE_ON_LOAD = "duplicate_on_load"  # load run twice, no truncate
    TRUNCATE_TABLE = "truncate_table"      # only the first unit/page/part read
    WRONG_SOURCE_FILE = "wrong_source_file"  # two same-backend tables swapped
    STALE_SNAPSHOT = "stale_snapshot"      # cached extract of another population
    NULL_ROW_DROP = "null_row_drop"        # rows containing any NULL dropped
    HEADER_AS_ROW = "header_as_row"        # CSV header ingested as data
    FABRICATE_COUNTS = "fabricate_counts"  # counts read off nothing (the hint)


class CouncilRole(str, Enum):
    SEMANTIC_AUTHOR = "semantic_author"
    AMBIGUITY_CRITIC = "ambiguity_critic"
    POPULATION_ADVERSARY = "population_adversary"
    SHORTCUT_ATTACKER = "shortcut_attacker"
    FEASIBILITY_REVIEWER = "feasibility_reviewer"


class Severity(str, Enum):
    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    FATAL = "fatal"


class FindingProvenance(str, Enum):
    """Whether deterministic code or a provider authored a finding."""

    #: Built by this repository's deterministic detectors: code, not a proposal.
    CODE = "code"
    #: Parsed out of a council provider's JSON — every word is a model's assertion.
    PROVIDER = "provider"


class RepairRoute(str, Enum):
    """Where a failure routes. Derived from CHANGED ARTIFACTS, never opinion."""

    SPECIFICATION = "specification"  # rewrite prose, rerun review
    POPULATION = "population"        # regenerate data+gold+attacks+downstream
    RUNTIME = "runtime"              # rebuild environment, rerun execution
    REFERENCE = "reference"          # invalidate gold, rerun reference
    FATAL = "fatal"                  # contamination/licensing: reject


class TaskVariant(str, Enum):
    """A parent TaskIR view: extract-load, transform, or legacy full diagnostic."""

    FULL = "full"
    EXTRACT_LOAD = "extract_load"
    TRANSFORM = "transform"


#: The complete active RLVR contract; explicit rather than iterating TaskVariant,
#: because FULL is a compatibility value, not a third task.
RLVR_TASK_VARIANTS: tuple[TaskVariant, ...] = (
    TaskVariant.EXTRACT_LOAD,
    TaskVariant.TRANSFORM,
)


#: Deterministic task-id suffix per variant (FULL is the parent id itself).
_VARIANT_SUFFIX: dict["TaskVariant", str] = {
    TaskVariant.FULL: "",
    TaskVariant.EXTRACT_LOAD: "__el",
    TaskVariant.TRANSFORM: "__t",
}


def variant_task_id(task_id: str, variant: TaskVariant) -> str:
    """Variant id of a parent task id: '<id>', '<id>__el', or '<id>__t'."""
    if not task_id:
        raise ValueError("task_id must be non-empty")
    return task_id + _VARIANT_SUFFIX[TaskVariant(variant)]


class TaskStatus(str, Enum):
    """Pipeline progress. Volatile: never part of the content hash."""

    DRAFT = "draft"                  # normalized IR exists
    GENERATED = "generated"          # populations materialized
    GOLD_FROZEN = "gold_frozen"      # reference executed, gold frozen
    AUTHORED = "authored"            # solver-visible prose written
    REVIEWED = "reviewed"            # council findings recorded
    IN_REPAIR = "in_repair"
    ATTACKED = "attacked"            # mutants compiled + executed
    ACCEPTED = "accepted"            # both EL and T batteries accepted
    CALIBRATED = "calibrated"
    SELECTED = "selected"
    RELEASED = "released"
    REJECTED = "rejected"


# --- Physical schema ---

class ColumnSpec(CanonicalModel):
    name: str = Field(min_length=1)
    type: ColumnType
    nullable: bool = False
    description: str = ""
    #: Closed value domain (enum columns); None means unconstrained.
    enum_values: tuple[str, ...] | None = None


class TableSpec(CanonicalModel):
    name: str = Field(min_length=1)
    description: str = ""
    columns: tuple[ColumnSpec, ...] = Field(min_length=1)
    #: Primary key columns; empty means the table has no PK (allowed).
    primary_key: tuple[str, ...] = ()
    #: Business key whose uniqueness generation must preserve (may differ from PK).
    business_key: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_columns(self) -> "TableSpec":
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError(f"table {self.name!r}: duplicate column names")
        known = set(names)
        for group, label in ((self.primary_key, "primary_key"), (self.business_key, "business_key")):
            missing = [c for c in group if c not in known]
            if missing:
                raise ValueError(f"table {self.name!r}: {label} columns {missing} not in columns")
        for c in self.columns:
            if c.name in self.primary_key and c.nullable:
                raise ValueError(f"table {self.name!r}: primary key column {c.name!r} is nullable")
        return self

    def column(self, name: str) -> ColumnSpec:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"table {self.name!r} has no column {name!r}")


class Relationship(CanonicalModel):
    """A foreign-key link: child columns reference parent columns."""

    child_table: str
    child_columns: tuple[str, ...] = Field(min_length=1)
    parent_table: str
    parent_columns: tuple[str, ...] = Field(min_length=1)
    #: False permits nullable or dangling rows used to test inner joins.
    required: bool = True

    @model_validator(mode="after")
    def _check_arity(self) -> "Relationship":
        if len(self.child_columns) != len(self.parent_columns):
            raise ValueError("relationship child/parent column arity mismatch")
        return self


class BackendAssignment(CanonicalModel):
    """Which source backend one table is materialized into (exactly one each)."""

    table: str
    backend: Backend
    #: Renderer options; string-valued to stay canonically serializable.
    options: dict[str, str] = Field(default_factory=dict)


# --- Marts + declarative plans ---

class MartOp(CanonicalModel):
    """One declared relational operation. Declarative record, not executable code."""

    kind: MartOpKind
    description: str = Field(min_length=1)
    #: Source/intermediate tables this op touches.
    tables: tuple[str, ...] = ()
    #: Columns this op consumes or produces.
    columns: tuple[str, ...] = ()
    join_type: JoinType | None = None
    #: Predicate/expression in prose or SQL fragment form (e.g. "status = 'completed'").
    predicate: str = ""
    #: Free-form structured details (aggregate function, window frame, ...).
    details: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_join(self) -> "MartOp":
        if self.kind == MartOpKind.JOIN and self.join_type is None:
            raise ValueError("join op must declare join_type")
        if self.kind != MartOpKind.JOIN and self.join_type is not None:
            raise ValueError("join_type only valid on join ops")
        return self


class MartPlan(CanonicalModel):
    """Ordered relational operations for one mart.

    Empty legacy identity fields are omitted to preserve old hashes; declaring
    either field changes semantic identity.
    """

    mart: str = Field(min_length=1)
    ops: tuple[MartOp, ...] = Field(min_length=1)
    notes: str = ""
    #: Stable registry identity of the builder that produced this plan. Empty
    #: means undeclared legacy provenance, never "unknown" inferred from prose.
    template_id: str = ""
    #: Compound semantic claims. Atomic operations remain recorded in ``ops``.
    semantic_patterns: tuple[SemanticPattern, ...] = ()

    @field_validator("template_id")
    @classmethod
    def _check_template_id(cls, value: str) -> str:
        if value and not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value):
            raise ValueError(
                "template_id must be empty or lower_snake_case beginning with a letter"
            )
        return value

    @field_validator("semantic_patterns")
    @classmethod
    def _check_semantic_patterns(
        cls, values: tuple[SemanticPattern, ...]
    ) -> tuple[SemanticPattern, ...]:
        raw = [value.value for value in values]
        if raw != sorted(raw) or len(raw) != len(set(raw)):
            raise ValueError(
                "semantic_patterns must be strictly sorted by unique enum value"
            )
        return values

    @model_serializer(mode="wrap")
    def _drop_legacy_semantic_identity(self, handler: Any) -> dict[str, Any]:
        """Keep empty additive fields absent even when this plan is nested."""
        data = handler(self)
        if not data.get("template_id"):
            data.pop("template_id", None)
        if not data.get("semantic_patterns"):
            data.pop("semantic_patterns", None)
        return data

    def declares_pattern(self, pattern: SemanticPattern) -> bool:
        """Return whether this plan explicitly declares ``pattern``."""
        return pattern in self.semantic_patterns


class MartColumnKind(str, Enum):
    """The transformation work that produces a mart column."""

    PASSTHROUGH = "passthrough"    # projected/renamed/cast source column
    DERIVED = "derived"            # arithmetic, ratio, guarded division
    AGGREGATED = "aggregated"      # GROUP BY measure (incl. filtered/distinct)
    CATEGORICAL = "categorical"    # CASE ladder / bucketing
    RANKED = "ranked"              # extremum, rank, window


class MartColumn(CanonicalModel):
    """A target column and its optional transformation kind."""

    name: str = Field(min_length=1)
    type: ColumnType
    #: Required non-empty: exported schemas/<table>.csv needs real descriptions.
    description: str = Field(min_length=1)
    #: None = not declared (legacy); certifiers demand a declaration.
    kind: MartColumnKind | None = None

    @model_serializer(mode="wrap")
    def _drop_undeclared_kind(self, handler: Any) -> dict[str, Any]:
        """Omit undeclared kinds from nested serialization to preserve hashes."""
        data = handler(self)
        if data.get("kind") is None:
            data.pop("kind", None)
        return data

    @property
    def classified(self) -> bool:
        """Has a kind been declared at all? (Certifiers fail closed on False.)"""
        return self.kind is not None

    @property
    def computed(self) -> bool:
        """Return whether declared transform work produces this column."""
        return self.kind is not None and self.kind is not MartColumnKind.PASSTHROUGH


class MartSpec(CanonicalModel):
    name: str = Field(min_length=1)
    description: str = ""
    #: Prose statement of the grain ("one row per customer").
    grain: str = Field(min_length=1)
    #: Unique key columns of the mart (sort_key.json; first-order sort keys).
    key_columns: tuple[str, ...] = Field(min_length=1)
    columns: tuple[MartColumn, ...] = Field(min_length=1)
    #: The declarative plan for this mart.
    plan: MartPlan

    @model_validator(mode="after")
    def _check_keys(self) -> "MartSpec":
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError(f"mart {self.name!r}: duplicate column names")
        missing = [k for k in self.key_columns if k not in names]
        if missing:
            raise ValueError(f"mart {self.name!r}: key columns {missing} not in columns")
        if self.plan.mart != self.name:
            raise ValueError(f"mart {self.name!r}: plan is for {self.plan.mart!r}")
        return self


# --- Populations ---

class PopulationSpec(CanonicalModel):
    """One data condition over the SAME schema and logic."""

    name: PopulationName
    #: Root seed; per-column streams derive from derive_seed(task_id, name, table, column).
    seed: int = Field(ge=0)
    #: Intended, not realized, row counts; empty for literal populations.
    scale: dict[str, int] = Field(default_factory=dict)
    #: Prose data conditions generation must satisfy and audits can check.
    conditions: tuple[str, ...] = ()
    #: Literal constructed rows per table; when non-empty for a table, generation
    #: emits EXACTLY these rows for it.
    literal_rows: dict[str, tuple[Row, ...]] = Field(default_factory=dict)


# --- Reference solution metadata ---

class ReferenceSolution(CanonicalModel):
    """Metadata + transform SQL of the trusted implementation (never solver-visible)."""

    implementation_id: str = Field(min_length=1)
    dialect: str = "duckdb"
    #: Trusted transform SQL per mart name (offline path: DuckDB SQL).
    sql_by_mart: dict[str, str] = Field(default_factory=dict)
    #: How extract+load is performed (prose; runner interprets per backend).
    load_notes: str = ""
    #: Where this implementation came from — provenance is audit material.
    provenance: str = ""
    version: str = "1"


# --- Attacks, findings, repair ---

class AttackCase(CanonicalModel):
    """An executable wrong-logic mutant and its expected per-population effect."""

    name: str = Field(min_length=1)
    kind: AttackKind
    description: str = Field(min_length=1)
    #: Machine-applicable mutation: mutated SQL, or a directive the attack
    #: compiler (verification/attacks.py) turns into one via sqlglot.
    mutation: str = ""
    #: Partial population pass map; required attacks must contain at least one loss.
    expected_pass: dict[PopulationName, bool] = Field(default_factory=dict)
    #: Required attacks gate acceptance; optional ones are informational.
    required: bool = True
    #: Finding id this case was compiled from (None for the standing catalogue).
    source_finding: str | None = None

    @model_validator(mode="after")
    def _check_required_loses(self) -> "AttackCase":
        if self.required and not any(v is False for v in self.expected_pass.values()):
            raise ValueError(
                f"required attack {self.name!r} must expect to lose reward on "
                "at least one population (some expected_pass value must be False)"
            )
        return self


class ProposedAttackCase(CanonicalModel):
    """A five-population attack hypothesis for deterministic promotion.

    Proposals do not affect TaskIR hashes. Measured promotion decides whether
    they become attacks, blocking shortcuts, or proven relational identities.
    """

    kind: AttackKind
    #: Promoter parameters are scalars or identifier tuples in a closed grammar.
    params: dict[str, ProposalParam] = Field(default_factory=dict)
    #: FULL map — all five populations REQUIRED (unlike AttackCase's partial map):
    #: a proposer must commit to a complete falsifiable prediction.
    expected_pass: dict[PopulationName, bool]
    #: Optional legacy extension; new providers predict EL and T for all populations.
    expected_pass_by_stage: dict[TaskVariant, dict[PopulationName, bool]] = Field(
        default_factory=dict
    )
    rationale: str = Field(min_length=1)

    @field_validator("params")
    @classmethod
    def _finite_params(cls, value: dict[str, ProposalParam]) -> dict[str, ProposalParam]:
        """Require finite scalar parameters or identifier tuples."""
        for key, param in value.items():
            if isinstance(param, bool):
                continue
            if isinstance(param, float) and (param != param or param in (float("inf"), float("-inf"))):
                raise ValueError(
                    f"params[{key!r}] is a non-finite number; parameters must be "
                    "finite JSON scalars or identifier lists"
                )
        return value

    @model_validator(mode="after")
    def _check_expectation(self) -> "ProposedAttackCase":
        missing = [p.value for p in PopulationName if p not in self.expected_pass]
        if missing:
            raise ValueError(
                f"proposed attack case must state expected_pass for ALL five "
                f"populations; missing: {missing}"
            )
        if self.expected_pass_by_stage:
            required_stages = set(RLVR_TASK_VARIANTS)
            supplied_stages = set(self.expected_pass_by_stage)
            if supplied_stages != required_stages:
                missing_stages = sorted(s.value for s in required_stages - supplied_stages)
                extra_stages = sorted(s.value for s in supplied_stages - required_stages)
                raise ValueError(
                    "expected_pass_by_stage must contain exactly extract_load "
                    f"and transform (missing={missing_stages}, extra={extra_stages})"
                )
            for stage in RLVR_TASK_VARIANTS:
                stage_map = self.expected_pass_by_stage[stage]
                stage_missing = [
                    p.value for p in PopulationName if p not in stage_map
                ]
                if stage_missing:
                    raise ValueError(
                        f"expected_pass_by_stage[{stage.value!r}] must state all "
                        f"five populations; missing: {stage_missing}"
                    )
            for population in PopulationName:
                combined = all(
                    self.expected_pass_by_stage[stage][population]
                    for stage in RLVR_TASK_VARIANTS
                )
                if self.expected_pass[population] is not combined:
                    raise ValueError(
                        "combined expected_pass must equal extract_load AND "
                        f"transform for {population.value!r}"
                    )
        return self


class FindingDisposition(str, Enum):
    """The providing critic's own structured statement about its finding.

    This is the ONLY authority for withdrawal. Explanation text never is: a
    finding whose detail says "I withdraw nothing." or "I withdraw this
    finding." stays whatever its disposition says.
    """

    ACTIVE = "active"
    WITHDRAWN = "withdrawn"


class FindingScreenStatus(str, Enum):
    """What the deterministic post-parse screen decided about one finding."""

    #: Self-nullifying (withdraws its own claim, or carries no evidence at all).
    #: Demoted to INFO; its executable content is withheld.
    VOID = "void"
    #: A signal fired, but the finding remains executable and will be measured.
    NOTED = "noted"
    #: Executably identical to another finding in the same council run: it compiles
    #: to the SAME mutant, so it is not executed a second time.
    DUPLICATE = "duplicate"
    #: The one finding of a duplicate group that IS executed; names the others.
    REPRESENTATIVE = "representative"


class FindingScreen(CanonicalModel):
    """A deterministic screen result that preserves the original finding.

    Removed claims are retained as screen metadata, and every screen requires a
    reason.
    """

    status: FindingScreenStatus
    #: Deterministic rule names that fired, sorted (review/council.py SCREEN_SIGNALS).
    signals: tuple[str, ...] = ()
    #: Verbatim matched span(s) from the finding's own text — the evidence for the
    #: verdict, so a reader can re-check it without re-running the screen.
    evidence: str = ""
    #: Severity the PROPOSER claimed, preserved when the screen demotes.
    claimed_severity: Severity | None = None
    #: DUPLICATE: the finding_id of the representative that IS executed.
    duplicate_of: str = ""
    #: REPRESENTATIVE: the finding_ids collapsed into this one execution.
    coalesced_from: tuple[str, ...] = ()
    #: The compiled-mutant identity that made two findings duplicates —
    #: recomputable, so the grouping is auditable.
    mutant_key: str = ""
    #: Executable content the screen withheld from compilation (VOID only).
    withheld_attack: AttackKind | None = None
    withheld_proposal: ProposedAttackCase | None = None

    @model_validator(mode="after")
    def _check_reason(self) -> "FindingScreen":
        if self.status is FindingScreenStatus.VOID and not self.signals:
            raise ValueError("a VOID screen must name the signal(s) that fired")
        if self.status is FindingScreenStatus.NOTED and not self.signals:
            raise ValueError("a NOTED screen must name the signal(s) that fired")
        if self.status is FindingScreenStatus.DUPLICATE and not self.duplicate_of:
            raise ValueError(
                "a DUPLICATE screen must name the representative it collapsed into"
            )
        if (
            self.status is FindingScreenStatus.REPRESENTATIVE
            and not self.coalesced_from
        ):
            raise ValueError(
                "a REPRESENTATIVE screen must name the finding(s) it stands for"
            )
        return self


class Finding(CanonicalModel):
    """One council observation. Findings route to repairs; they never accept."""

    finding_id: str = Field(min_length=1)
    role: CouncilRole
    severity: Severity
    #: Authorship is explicit because one role can represent code or a provider.
    provenance: FindingProvenance = FindingProvenance.PROVIDER
    summary: str = Field(min_length=1)
    detail: str = ""
    #: Hint only — repair.py routes from changed artifacts, not from this.
    route_hint: RepairRoute | None = None
    #: Attack kind this finding suggests compiling (if executable).
    suggested_attack: AttackKind | None = None
    #: Optional structured attack-case proposal for the deterministic promoter;
    #: None for plain observations and for every pre-Round-3 finding.
    proposed_case: ProposedAttackCase | None = None
    #: ``None`` means unchanged; otherwise the screen retains withheld claims.
    screen: FindingScreen | None = None
    #: The provider's structured statement that this finding stands or is
    #: withdrawn. The live critic protocol requires it. ``None`` exists only for
    #: records written before the field did: such a finding is ACTIVE and its
    #: withdrawal is never re-read from its explanation text.
    disposition: FindingDisposition | None = None

    @model_validator(mode="after")
    def _only_a_provider_can_withdraw(self) -> "Finding":
        if (
            self.disposition is FindingDisposition.WITHDRAWN
            and self.provenance is not FindingProvenance.PROVIDER
        ):
            raise ValueError(
                "only a provider finding can be withdrawn; a code finding "
                "(a leak screen, a verified check) carries no disposition"
            )
        return self

    @model_serializer(mode="wrap")
    def _omit_undeclared_disposition(self, handler: Any) -> Any:
        # A record written before `disposition` existed keeps its exact
        # serialized bytes, so digests over historical findings still verify.
        data = handler(self)
        if isinstance(data, dict) and data.get("disposition") is None:
            data.pop("disposition", None)
        return data

    @property
    def withdrawn(self) -> bool:
        """True only for a provider finding explicitly withdrawn by its provider."""
        return (
            self.disposition is FindingDisposition.WITHDRAWN
            and self.provenance is FindingProvenance.PROVIDER
        )


class RepairEditOp(str, Enum):
    """Repair operations, including typed canonical-JSON replacement."""

    REPLACE = "replace"
    INSERT = "insert"
    DELETE = "delete"
    REPLACE_JSON = "replace_json"


#: Bound typed JSON anchors before parsing to limit allocation.
MAX_REPLACE_JSON_TEXT_BYTES = 256 * 1024
#: Enough for the known multi-table adjudications while preventing a patch
#: from becoming an unbounded sequence of whole-container rewrites.
MAX_REPLACE_JSON_EDITS = 8


def _parse_canonical_json_text(text: str, *, field: str) -> Any:
    """Parse one bounded canonical-JSON anchor without normalizing its spelling."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value!r} is not allowed")

    try:
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_REPLACE_JSON_TEXT_BYTES:
            raise ValueError(
                f"canonical JSON exceeds {MAX_REPLACE_JSON_TEXT_BYTES} bytes"
            )
        value = json.loads(text, parse_constant=reject_constant)
        rendered = canonical_json(value)
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError(
            f"replace_json edit requires {field!r} to be strict canonical JSON text"
        ) from exc
    if rendered != text:
        raise ValueError(
            f"replace_json edit requires {field!r} to be canonical JSON text"
        )
    return value


def _json_values_equal_exact(left: Any, right: Any) -> bool:
    """Typed JSON equality (unlike Python, ``1``, ``1.0`` and ``true`` differ)."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_values_equal_exact(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_values_equal_exact(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


class RepairEdit(CanonicalModel):
    """One nonempty, behavior-changing edit in a repair patch."""

    op: RepairEditOp
    #: Where in the artifact the edit applies (line anchor, JSON/YAML pointer, ...).
    locator: str = Field(min_length=1)
    old: str = ""
    new: str = ""

    @model_validator(mode="after")
    def _check_op(self) -> "RepairEdit":
        if self.op == RepairEditOp.REPLACE:
            if not self.old:
                raise ValueError("replace edit requires non-empty 'old'")
            if self.new == self.old:
                raise ValueError("replace edit must change the text (new == old)")
        elif self.op == RepairEditOp.INSERT:
            if self.old:
                raise ValueError("insert edit must have empty 'old'")
            if not self.new:
                raise ValueError("insert edit requires non-empty 'new'")
        elif self.op == RepairEditOp.DELETE:
            if not self.old:
                raise ValueError("delete edit requires non-empty 'old'")
            if self.new:
                raise ValueError("delete edit must have empty 'new'")
        elif self.op == RepairEditOp.REPLACE_JSON:
            old_value = _parse_canonical_json_text(self.old, field="old")
            new_value = _parse_canonical_json_text(self.new, field="new")
            if _json_values_equal_exact(old_value, new_value):
                raise ValueError(
                    "replace_json edit must change the typed JSON value (new == old)"
                )
        return self


class RepairPatch(CanonicalModel):
    """A frozen repair proposal that requires external certification."""

    route: RepairRoute
    #: Workspace-relative path of the ONE artifact this patch edits.
    artifact: str = Field(min_length=1)
    edits: tuple[RepairEdit, ...] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    #: Role that proposed the patch; a free string, since proposers are not limited
    #: to the council enum.
    proposer_role: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_route(self) -> "RepairPatch":
        if self.route == RepairRoute.FATAL:
            raise ValueError(
                "a RepairPatch cannot use route 'fatal': fatal routes reject "
                "the task; there is nothing to patch"
            )
        typed_edits = sum(
            edit.op is RepairEditOp.REPLACE_JSON for edit in self.edits
        )
        if typed_edits > MAX_REPLACE_JSON_EDITS:
            raise ValueError(
                "a RepairPatch may contain at most "
                f"{MAX_REPLACE_JSON_EDITS} replace_json edits"
            )
        return self


# --- Gates + acceptance ---

class GateResult(CanonicalModel):
    gate: str = Field(min_length=1)
    passed: bool
    details: str = ""
    #: Evidence pointers: artifact paths, hashes, measured values (stringified).
    evidence: dict[str, str] = Field(default_factory=dict)


class AcceptanceReport(CanonicalModel):
    """Gate verdict; acceptance requires a nonempty, all-passing battery."""

    task_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    #: Content hash of the TaskIR this verdict binds to.
    task_content_hash: str = Field(min_length=64, max_length=64)
    gates: tuple[GateResult, ...] = ()
    accepted: bool
    scorer_version: str = Field(min_length=1)
    #: Gate-roster identity; empty or mismatched values make evidence stale.
    roster_digest: str = ""
    #: The gate names that roster held when the battery ran (optional).
    roster: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _fail_closed(self) -> "AcceptanceReport":
        legitimate = bool(self.gates) and all(g.passed for g in self.gates)
        if self.accepted and not legitimate:
            raise ValueError(
                "accepted=True requires a non-empty gate list with every gate passed"
            )
        return self

    @classmethod
    def from_gates(
        cls,
        *,
        task_id: str,
        revision: int,
        task_content_hash: str,
        gates: tuple[GateResult, ...] | list[GateResult],
        scorer_version: str,
        roster_digest: str = "",
        roster: tuple[str, ...] | list[str] = (),
    ) -> "AcceptanceReport":
        gates_t = tuple(gates)
        return cls(
            task_id=task_id,
            revision=revision,
            task_content_hash=task_content_hash,
            gates=gates_t,
            accepted=bool(gates_t) and all(g.passed for g in gates_t),
            scorer_version=scorer_version,
            roster_digest=roster_digest,
            roster=tuple(roster),
        )


# --- Difficulty ---

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def solver_roster_fingerprint(model_keys: Iterable[str]) -> str:
    """Hash the unique sorted solver-model keys in a nonempty roster."""
    keys = sorted(set(model_keys))
    if not keys:
        raise ValueError("solver roster must contain at least one model key")
    if any(not k for k in keys):
        raise ValueError("solver roster model keys must be non-empty strings")
    return sha256_hex(canonical_json(keys))


class SolverTierResult(CanonicalModel):
    """A fixed solver tier's bounded attempt results for one task variant."""

    #: Pinned solver identity, e.g. "anthropic:claude-haiku-4-5".
    model_key: str = Field(min_length=1)
    #: Attempts run (k of pass@k / pass-rate estimation).
    k: int = Field(ge=1)
    #: Attempts that earned FULL reward (per verification/upstream_eval.py).
    successes: int = Field(ge=0)
    #: Failed attempts attributed to stage 1 (extract/load).
    stage1_failures: int = Field(ge=0, default=0)
    #: Failed attempts attributed to stage 2 (transform).
    stage2_failures: int = Field(ge=0, default=0)

    @model_validator(mode="after")
    def _check_accounting(self) -> "SolverTierResult":
        if self.successes > self.k:
            raise ValueError(
                f"tier {self.model_key!r}: successes ({self.successes}) exceed "
                f"attempts (k={self.k})"
            )
        failed = self.k - self.successes
        attributed = self.stage1_failures + self.stage2_failures
        if attributed > failed:
            raise ValueError(
                f"tier {self.model_key!r}: attributed failures ({attributed}) "
                f"exceed failed attempts ({failed})"
            )
        return self

    @property
    def pass_rate(self) -> float:
        return self.successes / self.k


class VariantCalibration(CanonicalModel):
    """Per-variant pass rates for a sorted, pinned solver roster."""

    variant: TaskVariant
    tiers: tuple[SolverTierResult, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_tiers(self) -> "VariantCalibration":
        keys = [t.model_key for t in self.tiers]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError(
                f"variant {self.variant.value!r}: tiers must be strictly "
                f"sorted by unique model_key (got {keys})"
            )
        if self.variant == TaskVariant.EXTRACT_LOAD:
            bad = [t.model_key for t in self.tiers if t.stage2_failures]
            if bad:
                raise ValueError(
                    f"extract_load variant has no stage 2; tiers {bad} "
                    "attribute failures to it"
                )
        if self.variant == TaskVariant.TRANSFORM:
            bad = [t.model_key for t in self.tiers if t.stage1_failures]
            if bad:
                raise ValueError(
                    f"transform variant is handed stage 1; tiers {bad} "
                    "attribute failures to it"
                )
        return self

    def pass_rate_vector(self) -> dict[str, float]:
        """model_key -> measured pass rate, in canonical (sorted-key) order."""
        return {t.model_key: t.pass_rate for t in self.tiers}


def derive_empirical_aggregate_claims(
    variants: Mapping[TaskVariant, VariantCalibration],
) -> dict[str, int | float]:
    """Derive campaign totals and conditional stage-failure rates from tiers."""

    tiers = [tier for calibration in variants.values() for tier in calibration.tiers]
    n_attempts = sum(tier.k for tier in tiers)
    if n_attempts <= 0:
        raise ValueError("empirical solver evidence has no tier attempts")
    successes = sum(tier.successes for tier in tiers)
    stage1_failures = sum(tier.stage1_failures for tier in tiers)
    stage2_failures = sum(tier.stage2_failures for tier in tiers)
    failed_attempts = n_attempts - successes
    return {
        "n_attempts": n_attempts,
        "success_rate": successes / n_attempts,
        "stage1_failure_rate": (
            stage1_failures / failed_attempts if failed_attempts else 0.0
        ),
        "stage2_failure_rate": (
            stage2_failures / failed_attempts if failed_attempts else 0.0
        ),
    }


def empirical_aggregate_problem(empirical: "EmpiricalDifficulty") -> str | None:
    """Return the first mismatch between aggregate claims and tier counts."""

    if not empirical.variants:
        return None
    try:
        expected = derive_empirical_aggregate_claims(empirical.variants)
    except ValueError as exc:
        return str(exc)
    for field, derived in expected.items():
        actual = getattr(empirical, field)
        if actual != derived:
            return (
                f"empirical {field} does not reproduce from tier attempts "
                f"(recorded {actual!r}, derived {derived!r})"
            )
    return None


class EmpiricalDifficulty(CanonicalModel):
    """Difficulty measured on the public task with fixed solver configurations."""

    solver_config: str = Field(min_length=1)
    n_attempts: int = Field(ge=1)
    success_rate: float = Field(ge=0.0, le=1.0)
    #: Fraction of failed attempts that failed at stage 1 (extract/load).
    stage1_failure_rate: float = Field(ge=0.0, le=1.0)
    #: Fraction of failed attempts that failed at stage 2 (transform).
    stage2_failure_rate: float = Field(ge=0.0, le=1.0)
    #: Per-variant pass-rate vectors; keys must match each entry's own variant.
    variants: dict[TaskVariant, VariantCalibration] = Field(default_factory=dict)
    #: Required with variants and derived from every recorded tier model key.
    roster_fingerprint: str = ""
    #: Campaign identity also binds routing, limits, instrument, and scorer.
    campaign_fingerprint: str = ""
    #: Content hash the solver runs were measured at; when set,
    #: corpus/difficulty.with_empirical refuses to attach it to any OTHER hash.
    measured_at_content_hash: str = ""

    @model_validator(mode="after")
    def _check_calibration(self) -> "EmpiricalDifficulty":
        if self.campaign_fingerprint and not _HEX64_RE.match(
            self.campaign_fingerprint
        ):
            raise ValueError(
                "campaign_fingerprint must be empty or 64 lowercase hex chars"
            )
        if self.measured_at_content_hash and not _HEX64_RE.match(
            self.measured_at_content_hash
        ):
            raise ValueError(
                "measured_at_content_hash must be empty or 64 lowercase hex chars"
            )
        for key, cal in self.variants.items():
            if TaskVariant(key) != cal.variant:
                raise ValueError(
                    f"variants[{TaskVariant(key).value!r}] holds a calibration "
                    f"for variant {cal.variant.value!r}"
                )
        if self.variants:
            rosters = {
                TaskVariant(variant).value: tuple(tier.model_key for tier in cal.tiers)
                for variant, cal in self.variants.items()
            }
            if len(set(rosters.values())) != 1:
                raise ValueError(
                    "every variant calibration must use exactly the same solver "
                    f"roster; got {rosters}"
                )
            attempt_budgets = {
                TaskVariant(variant).value: tuple(
                    (tier.model_key, tier.k) for tier in cal.tiers
                )
                for variant, cal in self.variants.items()
            }
            if len(set(attempt_budgets.values())) != 1:
                raise ValueError(
                    "every variant calibration must use exactly the same solver "
                    f"tier attempt counts; got {attempt_budgets}"
                )
            all_keys = {
                t.model_key for cal in self.variants.values() for t in cal.tiers
            }
            expected = solver_roster_fingerprint(all_keys)
            if self.roster_fingerprint != expected:
                raise ValueError(
                    "roster_fingerprint does not match the recorded tiers: "
                    f"expected solver_roster_fingerprint({sorted(all_keys)}) = "
                    f"{expected!r}, got {self.roster_fingerprint!r}"
                )
        elif self.roster_fingerprint:
            raise ValueError(
                "roster_fingerprint set but no variant calibrations recorded"
            )
        aggregate_problem = empirical_aggregate_problem(self)
        if aggregate_problem is not None:
            raise ValueError(aggregate_problem)
        return self


class DifficultyMeasurement(CanonicalModel):
    task_id: str = Field(min_length=1)
    #: Content hash of the TaskIR this measurement is valid for.
    task_content_hash: str = Field(min_length=64, max_length=64)
    #: Structural load (extraction) difficulty, [0, 1].
    load_score: float = Field(ge=0.0, le=1.0)
    #: Structural transform difficulty, [0, 1]. Kept separate from load.
    transform_score: float = Field(ge=0.0, le=1.0)
    #: Named structural features (table_count, join_count, window_count, ...).
    structural: dict[str, float] = Field(default_factory=dict)
    empirical: EmpiricalDifficulty | None = None

    def combined_score(self, load_weight: float = 0.3) -> float:
        """Selection-time blend (default 0.3 load / 0.7 transform)."""
        return load_weight * self.load_score + (1.0 - load_weight) * self.transform_score


# --- Revisions ---

class TaskRevision(BaseModel):
    """One volatile, unhashed repair round in a task lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: int = Field(ge=1)
    route: RepairRoute | None = None  # None for the initial revision
    reason: str = Field(min_length=1)
    #: Content hash of the TaskIR before this revision (None for revision 1).
    parent_content_hash: str | None = None
    #: Content hash of the TaskIR at this revision.
    content_hash: str = Field(min_length=64, max_length=64)


# --- The Task IR ---

_FAMILY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*__[a-z0-9][a-z0-9_.-]*$")

#: One segment of a family id (the pool token, and the family token).
_FAMILY_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

#: Portable filesystem-component byte limit.
TASK_ID_MAX_SEGMENT_BYTES = 255

#: Windows device basenames, including aliases reserved with extensions.
_WINDOWS_RESERVED_TASK_BASENAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        "CONIN$",
        "CONOUT$",
        *(f"COM{digit}" for digit in "123456789¹²³"),
        *(f"LPT{digit}" for digit in "123456789¹²³"),
    }
)


def validate_task_id_segment(value: str) -> str:
    """Validate one portable task-ID path segment without traversal forms."""

    if not isinstance(value, str):
        raise TypeError("task_id must be a string")
    if not value:
        raise ValueError("task_id must not be empty")
    if value in {".", ".."}:
        raise ValueError("task_id must be one non-dot path segment")
    if value != value.strip():
        raise ValueError("task_id must not have leading or trailing whitespace")
    if value.endswith("."):
        raise ValueError("task_id must not end with a dot")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("task_id must not contain control characters")
    invalid = sorted(set(value) & set('<>:"/\\|?*'))
    if invalid:
        raise ValueError(
            "task_id must be a single portable path segment; invalid "
            f"character(s): {''.join(invalid)!r}"
        )
    if len(value.encode("utf-8")) > TASK_ID_MAX_SEGMENT_BYTES:
        raise ValueError(
            f"task_id exceeds the {TASK_ID_MAX_SEGMENT_BYTES}-byte path-segment limit"
        )
    windows_basename = value.split(".", 1)[0].upper()
    if windows_basename in _WINDOWS_RESERVED_TASK_BASENAMES:
        raise ValueError(f"task_id {value!r} is a reserved Windows device name")
    return value

#: Longest family segment slugify_family will emit before hash-truncating.
FAMILY_SLUG_MAX_LEN = 64


def slugify_family(text: str) -> str:
    """Normalize a record name to a bounded family segment; not injective."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    digest = sha256_hex(text)[:12]
    if not slug:
        return f"x{digest}"
    if len(slug) > FAMILY_SLUG_MAX_LEN:
        head = slug[: FAMILY_SLUG_MAX_LEN - len(digest) - 1].rstrip("_")
        slug = f"{head}_{digest}"
    return slug


class PoolSelection(CanonicalModel):
    """A licensed, namespaced vendored-pool record selected for ingestion."""

    #: Pool token, also the family-id namespace (e.g. "wikidbs", "schemapile").
    pool: str = Field(min_length=1)
    #: The record key VERBATIM as the pool spells it, kept unnormalized so
    #: provenance can point back at the real on-disk record.
    selector: str = Field(min_length=1)
    origin: Origin
    #: Namespaced 'pool__family' (see slugify_family / for_record).
    family_id: str = Field(min_length=1)
    #: SPDX identifier or license name of THIS record's source material.
    license: str = Field(min_length=1)
    attribution: str = ""

    @field_validator("pool")
    @classmethod
    def _pool_token(cls, v: str) -> str:
        if not _FAMILY_SEGMENT_RE.match(v) or "__" in v:
            # '__' inside the pool token would make 'pool__family' ambiguous.
            raise ValueError(
                f"pool {v!r} must be a lowercase family-id segment "
                "(alphanumeric/underscore/dash/dot, no '__')"
            )
        return v

    @field_validator("license")
    @classmethod
    def _license_present(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("license must be a non-blank SPDX id or license name")
        return v

    @model_validator(mode="after")
    def _family_in_pool(self) -> "PoolSelection":
        if not _FAMILY_ID_RE.match(self.family_id):
            raise ValueError(
                f"family_id {self.family_id!r} must be namespaced 'pool__family'"
            )
        if not self.family_id.startswith(f"{self.pool}__"):
            raise ValueError(
                f"family_id {self.family_id!r} is not in pool {self.pool!r}'s "
                f"namespace (expected a '{self.pool}__' prefix)"
            )
        return self

    @classmethod
    def for_record(
        cls,
        *,
        pool: str,
        selector: str,
        origin: Origin,
        license: str,
        attribution: str = "",
        family: str | None = None,
    ) -> "PoolSelection":
        """Build a selection, using ``family`` instead of the record name if set."""
        segment = slugify_family(family if family is not None else selector)
        return cls(
            pool=pool,
            selector=selector,
            origin=origin,
            family_id=f"{pool}__{segment}",
            license=license,
            attribution=attribution,
        )

    def ir_identity(self) -> dict[str, Any]:
        """Return the TaskIR identity fields fixed by this selection."""
        return {
            "family_id": self.family_id,
            "origin": self.origin,
            "license": self.license,
            "attribution": self.attribution,
        }


class TaskIR(CanonicalModel):
    """A complete, frozen data project.

    Every semantic field is hashed; ``status`` and ``revisions`` are not.
    """

    VOLATILE_FIELDS: ClassVar[frozenset[str]] = frozenset({"status", "revisions"})

    schema_version: str = SCHEMA_VERSION

    # --- identity + lineage ---
    task_id: str = Field(min_length=1)
    #: Namespaced pool__family (e.g. "synsql__retail_orders_0042").
    family_id: str
    #: Similarity cluster within the pool; clusters never straddle splits.
    cluster_id: str = Field(min_length=1)
    origin: Origin
    #: SPDX identifier or license name of the source material.
    license: str = Field(min_length=1)
    attribution: str = ""

    # --- solver-facing description ---
    title: str = ""
    #: Solver-visible prose from semantic authoring (empty until authored). MUST
    #: NEVER contain reference SQL, gold values, or SynSQL question/sql/cot text.
    solver_prompt: str = ""

    # --- physical schema ---
    tables: tuple[TableSpec, ...] = Field(min_length=1)
    relationships: tuple[Relationship, ...] = ()
    backends: tuple[BackendAssignment, ...] = Field(min_length=1)

    # --- semantic targets ---
    marts: tuple[MartSpec, ...] = Field(min_length=1)

    # --- data populations ---
    populations: tuple[PopulationSpec, ...] = ()

    # --- private reference + attacks ---
    reference: ReferenceSolution | None = None
    attack_cases: tuple[AttackCase, ...] = ()

    # --- volatile (excluded from content_hash) ---
    status: TaskStatus = TaskStatus.DRAFT
    revisions: tuple[TaskRevision, ...] = ()

    # -- validators --------------------------------------------------------

    @field_validator("task_id")
    @classmethod
    def _task_id_is_safe_segment(cls, v: str) -> str:
        return validate_task_id_segment(v)

    @field_validator("family_id")
    @classmethod
    def _family_namespaced(cls, v: str) -> str:
        if not _FAMILY_ID_RE.match(v):
            raise ValueError(
                f"family_id {v!r} must be namespaced 'pool__family' "
                "(lowercase alphanumeric/underscore/dash/dot, double-underscore separator)"
            )
        return v

    @model_validator(mode="after")
    def _check_integrity(self) -> "TaskIR":
        table_names = [t.name for t in self.tables]
        if len(table_names) != len(set(table_names)):
            raise ValueError("duplicate table names")
        tables = {t.name: t for t in self.tables}

        for rel in self.relationships:
            for side, tname, cols in (
                ("child", rel.child_table, rel.child_columns),
                ("parent", rel.parent_table, rel.parent_columns),
            ):
                if tname not in tables:
                    raise ValueError(f"relationship {side} table {tname!r} unknown")
                have = {c.name for c in tables[tname].columns}
                missing = [c for c in cols if c not in have]
                if missing:
                    raise ValueError(
                        f"relationship {side} columns {missing} not in table {tname!r}"
                    )

        assigned = [b.table for b in self.backends]
        if len(assigned) != len(set(assigned)):
            raise ValueError("a table has more than one backend assignment")
        unknown = [t for t in assigned if t not in tables]
        if unknown:
            raise ValueError(f"backend assignments for unknown tables: {unknown}")
        unassigned = [t for t in table_names if t not in set(assigned)]
        if unassigned:
            raise ValueError(f"tables without a backend assignment: {unassigned}")

        mart_names = [m.name for m in self.marts]
        if len(mart_names) != len(set(mart_names)):
            raise ValueError("duplicate mart names")
        # Databricks and Redshift land the raw tables into the same schema the
        # marts are built in, so a mart named after a raw table would overwrite
        # that table on a real warehouse. Refuse the task instead of shipping
        # one whose meaning depends on the destination.
        shadowed = sorted(
            {name.casefold() for name in mart_names} & {name.casefold() for name in table_names}
        )
        if shadowed:
            raise ValueError(f"marts shadow raw tables: {shadowed}")

        pop_names = [p.name for p in self.populations]
        if len(pop_names) != len(set(pop_names)):
            raise ValueError("duplicate population names")
        for pop in self.populations:
            bad_scale = [t for t in pop.scale if t not in tables]
            if bad_scale:
                raise ValueError(f"population {pop.name.value}: scale for unknown tables {bad_scale}")
            bad_lit = [t for t in pop.literal_rows if t not in tables]
            if bad_lit:
                raise ValueError(
                    f"population {pop.name.value}: literal rows for unknown tables {bad_lit}"
                )

        if self.reference is not None:
            extra = [m for m in self.reference.sql_by_mart if m not in set(mart_names)]
            if extra:
                raise ValueError(f"reference SQL for unknown marts: {extra}")

        attack_names = [a.name for a in self.attack_cases]
        if len(attack_names) != len(set(attack_names)):
            raise ValueError("duplicate attack case names")

        return self

    # -- accessors ---------------------------------------------------------

    def table(self, name: str) -> TableSpec:
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(f"no table {name!r}")

    def mart(self, name: str) -> MartSpec:
        for m in self.marts:
            if m.name == name:
                return m
        raise KeyError(f"no mart {name!r}")

    def population(self, name: PopulationName) -> PopulationSpec:
        for p in self.populations:
            if p.name == name:
                return p
        raise KeyError(f"no population {name.value!r}")

    def backend_for(self, table: str) -> BackendAssignment:
        for b in self.backends:
            if b.table == table:
                return b
        raise KeyError(f"no backend assignment for table {table!r}")

    def has_all_populations(self) -> bool:
        return {p.name for p in self.populations} == set(PopulationName)

    # -- lineage helpers ---------------------------------------------------

    @property
    def current_revision(self) -> int:
        return self.revisions[-1].revision if self.revisions else 1

    def with_status(self, status: TaskStatus) -> "TaskIR":
        return self.model_copy(update={"status": status})

    def with_revision(self, *, route: RepairRoute | None, reason: str) -> "TaskIR":
        """Append a revision entry recording THIS object's semantic identity."""
        parent = self.revisions[-1].content_hash if self.revisions else None
        entry = TaskRevision(
            revision=(self.revisions[-1].revision + 1) if self.revisions else 1,
            route=route,
            reason=reason,
            parent_content_hash=parent,
            content_hash=self.content_hash(),
        )
        return self.model_copy(update={"revisions": self.revisions + (entry,)})


# --- (De)serialization helpers ---

def task_to_json(task: TaskIR) -> str:
    """Serialize full TaskIR state, including volatile fields, for round trips."""
    return readable_json(task.model_dump(mode="json"))


def task_from_json(text: str) -> TaskIR:
    return TaskIR.model_validate_json(text)

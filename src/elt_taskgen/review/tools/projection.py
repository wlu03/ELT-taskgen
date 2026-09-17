"""Project validator results into model-safe diagnostics.

The projector knows private task values; the gatekeeper independently revalidates
serialized bytes without gold data. Numeric measurements, free text, private SQL, paths,
secrets, and non-public identifiers are forbidden. A leak raises `DiagnosticTripwire`
before delivery.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any, ClassVar

from pydantic import Field, StrictBool, ValidationError, field_validator, model_validator

from elt_taskgen.models import (
    AcceptanceReport,
    AttackCase,
    AttackKind,
    Backend,
    CanonicalModel,
    ColumnType,
    CouncilRole,
    GateResult,
    MartOpKind,
    PopulationName,
    RepairRoute,
    Severity,
    TaskIR,
    TaskVariant,
    canonical_json,
)
from elt_taskgen.review.session import SessionFault, ToolDeadlineExceeded, ToolHarnessFault
from elt_taskgen.training.contract import WorkspaceFailureClass
from elt_taskgen.training.models import _CODE_RE

__all__ = [
    "CERTIFY_ATTACK_CODES",
    "CERTIFY_CODES",
    "CERTIFY_FLAGS",
    "CHEAP_CODES",
    "COMPILE_CODES",
    "DIAGNOSTICS_VERSION",
    "DUAL_BUILD_CODES",
    "DevRows",
    "FIELD_CODES",
    "Diagnostic",
    "DiagnosticSource",
    "DiagnosticText",
    "DiagnosticTripwire",
    "GATE_CODES",
    "LeakTripwire",
    "MAX_DEV_ROWS",
    "POPULATION_CHEAP_CODES",
    "PROMOTION_CODES",
    "PROSE_KINDS",
    "PublicIdentifierSet",
    "ProjectorWorkerResult",
    "RejectionCode",
    "TEXT_ALLOWLISTED_SOURCES",
    "TEXT_PRODUCER_PUBLIC_FIELDS",
    "TRIAL_CODES",
    "WITNESS_PROBLEM_CODES",
    "assert_value_free",
    "codes_for",
    "dev_rows_leak_shape",
    "flagged_codes_for",
    "private_scalars",
    "project",
    "project_bind",
    "project_certify",
    "project_certify_attack",
    "project_compile",
    "project_dev_query_error",
    "project_dual_build",
    "project_gate",
    "project_gate_battery",
    "project_gate_details",
    "project_in_worker",
    "project_list_schemas",
    "project_load_plan",
    "project_mart_dev",
    "project_promotion",
    "project_prose_problems",
    "serialize_for_transport",
    "transport_sha256",
    "BIND_CODES",
    "DEV_QUERY_CODES",
    "LOAD_PLAN_CODES",
    "MART_DEV_CODES",
    "SCHEMA_CODES",
]

#: Evidence records this projection-contract version. It affects a role digest
#: only when wired there; changing it alone does not alter tools or routing.
DIAGNOSTICS_VERSION = "2"

#: `dev_query` returns at most this many rows (the sibling verifier's cap).
MAX_DEV_ROWS = 200

#: Projected identifiers may begin with underscores but must contain a letter.
#: Reject numbers, paths, operators, and whitespace, then require membership in
#: the public identifier set.
_IDENT_RE = re.compile(r"(?=_*[A-Za-z])[A-Za-z_][A-Za-z0-9_.\-]{0,127}")

#: Bound on the one allowlisted text field.
_MAX_TEXT_CHARS = 512


# ---------------------------------------------------------------------------
# Sources and their closed code vocabularies
# ---------------------------------------------------------------------------

class DiagnosticSource(str, Enum):
    """Where a projection came from. Closed: an unknown source is a schema trip."""

    GATE = "gate"              # one gate of a recorded AcceptanceReport
    REJECTION = "rejection"    # a repair_proposer.PatchRejected subclass
    PROMOTION = "promotion"    # verification.attacks.PromotionOutcome (post-session)
    PROSE = "prose"            # prose_fidelity.check_prose_fidelity problems (text)
    COMPILE = "compile"        # compile_probe / compile_proposal over compile_attacks
    DUAL_BUILD = "dual_build"  # the pending independent-build adjudication record
    # Phase 1 (roadmap 1.P-a): the repair proposer's session tools
    # (review/tools/validators.py, review/tools/certify.py).
    CERTIFY = "certify"        # the provider-free in-session certify (certify addendum §3.1)
    TRIAL = "trial"            # read_view, apply_edit_trial, check_scope, submit_patch, abort
    FIELD = "field"            # read_field over the route's IR paths and the public schema
    CHEAP = "cheap"            # check_cheap: the cheap TaskIR gates, codes only
    # Phase 2 (roadmap 2.a): the DEV/T witness session tools
    # (review/tools/validators.py, training/dev_tool.py).
    SCHEMA = "schema"          # list_schemas: the DEVELOPMENT source table names
    BIND = "bind"              # dry_run_sql: EXPLAIN over empty typed tables
    MART_DEV = "mart_dev"      # run_mart_sql_dev: DEVELOPMENT execution, code only
    DEV_QUERY = "dev_query"    # dev_query error path (success is DevRows, not a Diagnostic)
    LOAD_PLAN = "load_plan"    # check_load_plan: STATIC reader/confinement/coverage


GATE_CODES: tuple[str, ...] = ("ok", "failed", "gate_crashed")

DUAL_BUILD_CODES: tuple[str, ...] = (
    "agreed",
    "mismatch",
    "parse_error",
    "execution_error",
    "unclassified",
)

PROMOTION_CODES: tuple[str, ...] = (
    "promoted",
    "mismatch",
    "inert",
    "inapplicable",
    "uncompilable",
    "fidelity_failed",
    "no_kill_predicted",
    "unknown_kind",
)

PROSE_KINDS: tuple[str, ...] = (
    "not_represented",
    "contradicted",
    "operator_vocabulary",
    "missing_object",
    "lineage_disagrees",
    "empty_prose",
)

COMPILE_CODES: tuple[str, ...] = ("compiles", "uncompilable")

#: Base certify codes cover green, provider-free red, and no-cost refusal.
#: Edit-caused resource exhaustion on a touched stage is paid, while attack
#: codes remain flag-gated until attack certification is enabled.
CERTIFY_CODES: tuple[str, ...] = (
    "certify_green",
    "certify_red_generate",
    "certify_red_reference",
    "certify_refused_no_provider_free_stage",
    "certify_refused_unchanged_trial",
    "certify_refused_resource_budget",
)

#: The two booleans a certify projection carries, and nothing else: whether
#: model-bearing stages of the route's rerun set were left to the submit-time
#: certifier, and the discrimination guard's bit (always False in Phase 1 —
#: the guard needs `attack`).
CERTIFY_FLAGS: tuple[str, ...] = ("model_stages_deferred", "discrimination_weakened")

#: Attack-certify codes exist only when `repair.certify.attack_enabled` is on.
#: Keep them outside the base vocabulary and one-shot pin.
CERTIFY_ATTACK_CODES: tuple[str, ...] = (
    "certify_red_attack",
    "certify_refused_review_view_changed",
)

#: The held-trial tools' own codes; their REFUSALS reuse the `rejection`
#: vocabulary (`project_rejection`), the same codes a second session's view
#: carries, so one spelling names one defect everywhere.
TRIAL_CODES: tuple[str, ...] = ("view_served", "applied", "scope_ok", "submitted", "aborted")

#: `read_field`: a public identifier (or a list of them / a boolean) travels
#: as `subject` / `names` / `flags`; free text the view already shows is
#: acknowledged, anything else (text outside the view, any number) is withheld;
#: a public field outside the route's allowlist is a refusal CODE (private
#: material is a `ForbiddenArgument`, never a code).
FIELD_CODES: tuple[str, ...] = (
    "field_value",
    "field_not_found",
    "field_text_in_view",
    "field_withheld",
    "field_outside_allowlist",
)

#: Cheap population checks project all matching flags in precedence order;
#: the first becomes the code and unmatched problem classes stay generic.
POPULATION_CHEAP_CODES: tuple[str, ...] = (
    "missing_population",
    "scale_drift",
    "no_scale_no_rows",
    "counterfactual_untargeted",
)

#: `witness_problem_codes` (roadmap Phase 3): `mart_plan._witness_problems`
#: projected to codes — an unknown witness, a witness that needs a bridge
#: table the shape never joined, a witness whose discriminating column the
#: constructed rows never control, a witness that needs the second hop.
#: Codes only: never the witness prose, never a row value.
WITNESS_PROBLEM_CODES: tuple[str, ...] = (
    "witness_unknown",
    "witness_no_bridge",
    "witness_role_missing",
    "witness_no_second_hop",
)

#: `check_cheap`: codes only (SoT T3); which gate failed first, per-gate
#: booleans in `flags`, the affected marts (public names) in `names`. The
#: POPULATION route adds `check_population_cheap`'s codes.
CHEAP_CODES: tuple[str, ...] = (
    "cheap_green",
    "ir_invalid",
    "prose_problems",
    "structure_problems",
    "population_problems",
    *POPULATION_CHEAP_CODES,
    *WITNESS_PROBLEM_CODES,
)

#: `list_schemas`: the DEVELOPMENT source table names travel in `names`; no
#: count, no gold, no mart (SoT T3; roadmap 2.a). Names only.
SCHEMA_CODES: tuple[str, ...] = ("schemas_listed",)

#: `dry_run_sql`: `ok` is whether the SQL binds over the empty typed tables;
#: the code is the bind outcome (`binds`) or its first `error_class`; `binds`
#: and `columns_match` are flags; the missing mart columns (public names) are
#: in `names`. DuckDB text is DROPPED (roadmap 2.a; SoT T3 `dry_run_sql` row).
BIND_CODES: tuple[str, ...] = (
    "binds",
    "parse_error",
    "unknown_table",
    "unknown_column",
    "type_error",
    "other",
)

#: `run_mart_sql_dev`: DEVELOPMENT-only execution, rows and row counts
#: DISCARDED — one `{mart, code}` per call, first failing mart wins (SoT T3;
#: OQ-43: no `own_row_count`-class field on any T-seat projection).
MART_DEV_CODES: tuple[str, ...] = (
    "ok",
    "invalid_query",
    "output_limit",
    "memory_limit",
    "execution_timeout",
    "query_failed",
    "column_set_mismatch",
)

#: `dev_query` ERROR codes (the success path is `DevRows`, never a
#: `Diagnostic`): a query that does not parse, names an external-access
#: function, fails at execution, exceeds the output cap or times out.
DEV_QUERY_CODES: tuple[str, ...] = (
    "invalid_query",
    "external_access",
    "query_failed",
    "output_limit",
    "execution_timeout",
)

#: `check_load_plan` statically checks readers, path confinement, and table
#: coverage without execution or counts. Unknown subjects travel only if public.
LOAD_PLAN_CODES: tuple[str, ...] = (
    "ok",
    "unknown_reader",
    "path_escape",
    "table_uncovered",
    "table_unknown",
    "table_duplicate",
    "s3_part_file",
)


def _stage_names() -> tuple[str, ...]:
    from elt_taskgen.engine import StageName  # engine never imports review.tools

    return tuple(s.value for s in StageName)


#: `RejectionCode` (trust-boundary row 13): what the next session's view carries
#: in place of the "RETRY n" sentence. `revalidation_red_<stage>` is one member
#: per ledger stage plus `revalidation_red_unknown` for a rejection whose stage
#: was not recorded; `patch_artifact_unreadable` covers an artifact that exists
#: but is not parseable, which the row's list did not name.
RejectionCode = Enum(  # type: ignore[misc]
    "RejectionCode",
    {
        **{
            name.upper(): name
            for name in (
                "scope_route_mismatch",
                "scope_path_outside_allowlist",
                "scope_field_outside_allowlist",
                "scope_path_escape",
                "patch_artifact_missing",
                "patch_artifact_unreadable",
                "patch_anchor_not_found",
                "patch_anchor_ambiguous",
                "patch_noop",
                "discrimination_weakened",
                "revalidation_red_unknown",
            )
        },
        **{
            f"REVALIDATION_RED_{stage.upper()}": f"revalidation_red_{stage}"
            for stage in _stage_names()
        },
    },
    type=str,
)

_CODES_BY_SOURCE: dict[DiagnosticSource, frozenset[str]] = {
    DiagnosticSource.GATE: frozenset(GATE_CODES),
    DiagnosticSource.REJECTION: frozenset(m.value for m in RejectionCode),
    DiagnosticSource.PROMOTION: frozenset(PROMOTION_CODES),
    DiagnosticSource.PROSE: frozenset(PROSE_KINDS),
    DiagnosticSource.COMPILE: frozenset(COMPILE_CODES),
    DiagnosticSource.DUAL_BUILD: frozenset(DUAL_BUILD_CODES),
    DiagnosticSource.CERTIFY: frozenset(CERTIFY_CODES),
    DiagnosticSource.TRIAL: frozenset(TRIAL_CODES),
    DiagnosticSource.FIELD: frozenset(FIELD_CODES),
    DiagnosticSource.CHEAP: frozenset(CHEAP_CODES),
    DiagnosticSource.SCHEMA: frozenset(SCHEMA_CODES),
    DiagnosticSource.BIND: frozenset(BIND_CODES),
    DiagnosticSource.MART_DEV: frozenset(MART_DEV_CODES),
    DiagnosticSource.DEV_QUERY: frozenset(DEV_QUERY_CODES),
    DiagnosticSource.LOAD_PLAN: frozenset(LOAD_PLAN_CODES),
}

#: Flag-gated codes are schema-valid but excluded from the shipped vocabulary
#: and one-shot hash until their feature is enabled.
_FLAGGED_CODES_BY_SOURCE: dict[DiagnosticSource, frozenset[str]] = {
    DiagnosticSource.CERTIFY: frozenset(CERTIFY_ATTACK_CODES),
}

#: Sources whose projection may carry `DiagnosticText`, because every input of
#: the producer is pinned public by `test_text_allowlisted_producers_have_public_inputs`.
TEXT_ALLOWLISTED_SOURCES: frozenset[DiagnosticSource] = frozenset({DiagnosticSource.PROSE})

#: The TaskIR fields a text-allowlisted producer may read. Everything else on
#: the TaskIR (reference SQL, attack mutations, populations, gold bindings) is
#: private to some route and must never feed a text projection.
TEXT_PRODUCER_PUBLIC_FIELDS: dict[DiagnosticSource, frozenset[str]] = {
    DiagnosticSource.PROSE: frozenset({"solver_prompt", "marts", "tables"}),
}


def codes_for(source: DiagnosticSource | str) -> frozenset[str]:
    """The closed code vocabulary of one source (as shipped; the flag-gated
    members are `flagged_codes_for`)."""
    return _CODES_BY_SOURCE[DiagnosticSource(source)]


def flagged_codes_for(source: DiagnosticSource | str) -> frozenset[str]:
    """The flag-gated members of one source's vocabulary (empty for every
    source but `certify`): live only under a config flag that ships OFF."""
    return _FLAGGED_CODES_BY_SOURCE.get(DiagnosticSource(source), frozenset())


# ---------------------------------------------------------------------------
# The tripwire
# ---------------------------------------------------------------------------

class DiagnosticTripwire(SessionFault):
    """Signal that projected data was unsafe for model delivery.

    The projector or gatekeeper raises before bytes leave the harness. Quarantined
    producer text is retained for humans but excluded from the model-facing exception.
    """

    boundary = "sanitizer"
    failure_class: ClassVar[WorkspaceFailureClass] = WorkspaceFailureClass.HARNESS_DEFECT
    terminal: ClassVar[str] = "LEAK_TRIPWIRE"

    def __init__(
        self,
        detector: str,
        code: str,
        *,
        source: str = "",
        payload_sha256: str = "",
        quarantined: bytes = b"",
    ) -> None:
        self.detector = str(detector)
        self.code = str(code)
        self.source = str(source)
        self.payload_sha256 = str(payload_sha256)
        #: The producer bytes, for `reports/leak_incident.json` — never in the message.
        self._quarantined = bytes(quarantined)
        where = f" of a {self.source} projection" if self.source else ""
        super().__init__(
            f"diagnostic tripwire: the {self.detector} detector refused delivery"
            f"{where} ({self.code}); nothing was sent — a producer leaked, which "
            "is a harness fault, not a task defect",
            code=self.code,
        )

    @property
    def quarantined(self) -> bytes:
        return self._quarantined


#: Alias binding (SoT T6): one class, one name in the engine's set.
LeakTripwire = DiagnosticTripwire


# ---------------------------------------------------------------------------
# The public identifier set
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _vocabulary_identifiers() -> frozenset[str]:
    """Identifiers that are public for EVERY task: gate, stage, route, variant,
    attack-kind, role, severity and backend names, plus the one solver-visible
    population."""
    from elt_taskgen.engine import StageName
    from elt_taskgen.verification import gates as gates_mod

    names: set[str] = set(gates_mod.GATE_NAMES)
    for roster in gates_mod.VARIANT_GATE_NAMES.values():
        names.update(roster)
    names.update(s.value for s in StageName)
    names.update(r.value for r in RepairRoute)
    names.update(v.value for v in TaskVariant)
    names.update(k.value for k in AttackKind)
    # The attack-kind VARIANT names are public vocabulary too: a compile
    # correction must be able to NAME the allowed variants for a kind (the
    # batch's report-156 shape, a kind directive with no variant). attacks
    # imports nothing from review at module load, so there is no cycle.
    from elt_taskgen.verification import attacks as attacks_mod

    names.update(attacks_mod.all_kind_variant_names())
    names.update(r.value for r in CouncilRole)
    names.update(s.value for s in Severity)
    names.update(b.value for b in Backend)
    names.update(k.value for k in MartOpKind)
    names.update(t.value for t in ColumnType)
    names.add(PopulationName.DEVELOPMENT.value)
    return frozenset(names)


def _task_public_identifiers(task: TaskIR) -> set[str]:
    """Identifiers the exported PUBLIC bundle publishes for this task: the task
    id, table and column names (schemas/*.csv), mart names, mart columns and key
    columns (data_model.yaml), backends, relationship endpoints and attack case
    NAMES (case names are public; their mutations and kill vectors are not).
    Never a population other than DEVELOPMENT, never a literal value."""
    names: set[str] = {task.task_id}
    for table in task.tables:
        names.add(table.name)
        names.update(c.name for c in table.columns)
    for mart in task.marts:
        names.add(mart.name)
        names.update(c.name for c in mart.columns)
        names.update(mart.key_columns)
    for binding in task.backends:
        names.add(binding.table)
        names.add(binding.backend.value)
    for rel in task.relationships:
        names.add(rel.child_table)
        names.add(rel.parent_table)
        names.update(rel.child_columns)
        names.update(rel.parent_columns)
    names.update(case.name for case in task.attack_cases)
    return {n for n in names if isinstance(n, str) and n}


class PublicIdentifierSet:
    """The identifiers a projection for `task` may name in `subject` / `names`.

    Built from the task's PUBLIC surfaces (what the exported bundle carries)
    plus the harness vocabulary, never from private material: a hidden
    population name, a literal row value or a gold count is not a member, so a
    projection naming one fails the schema detector.
    """

    __slots__ = ("identifiers",)

    def __init__(self, task: TaskIR | None = None, *, extra: Iterable[str] = ()) -> None:
        names: set[str] = set(_vocabulary_identifiers())
        if task is not None:
            names |= _task_public_identifiers(task)
        names.update(str(n) for n in extra)
        self.identifiers: frozenset[str] = frozenset(names)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self.identifiers

    def __iter__(self):
        return iter(sorted(self.identifiers))

    def __len__(self) -> int:
        return len(self.identifiers)

    def unknown(self, names: Iterable[str]) -> tuple[str, ...]:
        """The names NOT in the set, deduplicated and sorted (empty = all public)."""
        return tuple(sorted({n for n in names if n and n not in self.identifiers}))


# ---------------------------------------------------------------------------
# The projection models
# ---------------------------------------------------------------------------

def _check_identifier(value: str, *, label: str, allow_empty: bool) -> str:
    if value == "" and allow_empty:
        return value
    if _IDENT_RE.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be an identifier (a letter, or underscores then a "
            "letter, first; letters, digits, '_', '.', '-'), never a value, a "
            "number or a path"
        )
    return value


class Diagnostic(CanonicalModel):
    """A value-free observation: closed source, code, booleans, public identifiers.

    Frozen, `extra="forbid"`, no int, float or free-text field. `code` must be
    a member of the source's closed vocabulary; `subject` and `names` are
    identifiers whose membership in `PublicIdentifierSet(task)` is enforced by
    `serialize_for_transport` (projector) and `assert_value_free` (gatekeeper).
    """

    source: DiagnosticSource
    ok: StrictBool
    code: str = Field(min_length=1, max_length=128)
    subject: str = ""
    flags: Mapping[str, StrictBool] = Field(default_factory=dict)
    names: tuple[str, ...] = ()

    @field_validator("code")
    @classmethod
    def _stable_code(cls, value: str) -> str:
        if _CODE_RE.fullmatch(value) is None:
            raise ValueError("code must be a lowercase stable code (_CODE_RE)")
        return value

    @field_validator("subject")
    @classmethod
    def _subject_identifier(cls, value: str) -> str:
        return _check_identifier(value, label="subject", allow_empty=True)

    @field_validator("names")
    @classmethod
    def _names_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            _check_identifier(name, label="names entry", allow_empty=False)
        return tuple(value)

    @field_validator("flags")
    @classmethod
    def _flag_keys(cls, value: Mapping[str, bool]) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for key, flag in value.items():
            if not isinstance(key, str) or _CODE_RE.fullmatch(key) is None:
                raise ValueError("flag names must be lowercase stable codes (_CODE_RE)")
            out[key] = bool(flag)
        return out

    @model_validator(mode="after")
    def _code_in_source_vocabulary(self) -> "Diagnostic":
        allowed = _CODES_BY_SOURCE[self.source] | _FLAGGED_CODES_BY_SOURCE.get(
            self.source, frozenset()
        )
        if self.code not in allowed:
            raise ValueError(
                f"code {self.code!r} is not in the closed vocabulary of source "
                f"{self.source.value!r}"
            )
        return self

    def identifiers(self) -> tuple[str, ...]:
        """Every identifier this projection names (subject first)."""
        return tuple(n for n in (self.subject, *self.names) if n)

    @property
    def sha256(self) -> str:
        """sha256 of the canonical transport document (S2's
        `SanitizedToolResult.sha256`): the observation fingerprint an action
        trace records, equal to `DiagnosticTripwire.payload_sha256` for the
        same object."""
        return transport_sha256(self)

    def render(self) -> str:
        """Fixed template -> `tool_result` text (S2's `render()`): the code, the
        boolean, the flags and the public identifiers, nothing else — never
        executor text. A controller sends it only after
        `serialize_for_transport` and `assert_value_free` passed on the same
        object; the template adds no information the wire bytes do not carry."""
        parts = [f"[{self.source.value}] {self.code}"]
        if self.subject:
            parts.append(f"subject={self.subject}")
        parts.append("ok=true" if self.ok else "ok=false")
        for key in sorted(self.flags):
            parts.append(f"{key}={'true' if self.flags[key] else 'false'}")
        if self.names:
            parts.append("names=" + ",".join(self.names))
        return " ".join(parts)


class DiagnosticText(CanonicalModel):
    """A text-allowlisted projection: one problem SENTENCE from a producer whose
    inputs are all public (`TEXT_PRODUCER_PUBLIC_FIELDS`), carried through the
    gatekeeper with identifiers restricted to public names. Only sources in
    `TEXT_ALLOWLISTED_SOURCES` may construct one."""

    source: DiagnosticSource
    code: str = Field(min_length=1, max_length=128)
    subject: str = ""
    names: tuple[str, ...] = ()
    text: str = Field(min_length=1, max_length=_MAX_TEXT_CHARS)

    @field_validator("code")
    @classmethod
    def _stable_code(cls, value: str) -> str:
        if _CODE_RE.fullmatch(value) is None:
            raise ValueError("code must be a lowercase stable code (_CODE_RE)")
        return value

    @field_validator("subject")
    @classmethod
    def _subject_identifier(cls, value: str) -> str:
        return _check_identifier(value, label="subject", allow_empty=True)

    @field_validator("names")
    @classmethod
    def _names_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            _check_identifier(name, label="names entry", allow_empty=False)
        return tuple(value)

    @field_validator("text")
    @classmethod
    def _single_line_text(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("text must be one line")
        return value

    @model_validator(mode="after")
    def _allowlisted_source(self) -> "DiagnosticText":
        if self.source not in TEXT_ALLOWLISTED_SOURCES:
            raise ValueError(
                f"source {self.source.value!r} is not text-allowlisted; use Diagnostic"
            )
        if self.code not in _CODES_BY_SOURCE[self.source]:
            raise ValueError(
                f"code {self.code!r} is not in the closed vocabulary of source "
                f"{self.source.value!r}"
            )
        return self

    def identifiers(self) -> tuple[str, ...]:
        return tuple(n for n in (self.subject, *self.names) if n)

    @property
    def sha256(self) -> str:
        return transport_sha256(self)

    def render(self) -> str:
        """Fixed template: the code line, then the one allowlisted sentence."""
        head = f"[{self.source.value}] {self.code}"
        if self.subject:
            head += f" subject={self.subject}"
        if self.names:
            head += " names=" + ",".join(self.names)
        return f"{head}: {self.text}"


_DevCell = str | int | float | bool | None


class DevRows(CanonicalModel):
    """Rows of the DEVELOPMENT warehouse (solver-visible by design): the only
    projection that carries data values. At most `MAX_DEV_ROWS` rows; column
    names are public identifiers. Exempt from the gold-count canary, subject
    to the private-material, path and secret detectors."""

    columns: tuple[str, ...] = Field(min_length=1, max_length=256)
    rows: tuple[tuple[_DevCell, ...], ...] = ()
    truncated: StrictBool = False

    @field_validator("columns")
    @classmethod
    def _column_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            _check_identifier(name, label="column", allow_empty=False)
        return tuple(value)

    @model_validator(mode="after")
    def _bounded(self) -> "DevRows":
        if len(self.rows) > MAX_DEV_ROWS:
            raise ValueError(f"at most {MAX_DEV_ROWS} rows")
        width = len(self.columns)
        for row in self.rows:
            if len(row) != width:
                raise ValueError("every row must have exactly one cell per column")
            for cell in row:
                if isinstance(cell, str) and len(cell) > 4096:
                    raise ValueError("a cell is bounded at 4096 characters")
        return self

    def identifiers(self) -> tuple[str, ...]:
        return tuple(self.columns)

    @property
    def sha256(self) -> str:
        return transport_sha256(self)

    def render(self) -> str:
        """Fixed template: a header of column names, one `|`-joined row per
        line with every cell JSON-encoded (unambiguous, one line each), and a
        `(truncated)` marker when the cap cut the result."""
        lines = ["[dev_rows] " + ",".join(self.columns)]
        for row in self.rows:
            lines.append("|".join(json.dumps(cell, ensure_ascii=True) for cell in row))
        if self.truncated:
            lines.append("(truncated)")
        return "\n".join(lines)


_KIND_DIAGNOSTIC = "diagnostic"
_KIND_TEXT = "diagnostic_text"
_KIND_DEV_ROWS = "dev_rows"

_MODEL_BY_KIND: dict[str, type[CanonicalModel]] = {
    _KIND_DIAGNOSTIC: Diagnostic,
    _KIND_TEXT: DiagnosticText,
    _KIND_DEV_ROWS: DevRows,
}


def _kind_of(obj: object) -> str:
    if isinstance(obj, Diagnostic):
        return _KIND_DIAGNOSTIC
    if isinstance(obj, DiagnosticText):
        return _KIND_TEXT
    if isinstance(obj, DevRows):
        return _KIND_DEV_ROWS
    raise TypeError(
        f"only Diagnostic, DiagnosticText or DevRows may be serialized for "
        f"transport, not {type(obj).__name__}"
    )


def _transport_payload(obj: Diagnostic | DiagnosticText | DevRows) -> dict[str, Any]:
    """The wire document: `kind`, `diagnostics_version`, then the model body."""
    return {
        "kind": _kind_of(obj),
        "diagnostics_version": DIAGNOSTICS_VERSION,
        **obj.model_dump(mode="json"),
    }


def transport_sha256(obj: Diagnostic | DiagnosticText | DevRows) -> str:
    """sha256 of the canonical transport bytes of one projection: what
    `serialize_for_transport` would emit for it, hashed. The `observation_sha256`
    of an action-trace entry and the `payload_sha256` of a tripwire over the
    same object are this value."""
    return _sha256(canonical_json(_transport_payload(obj)).encode("utf-8"))


# ---------------------------------------------------------------------------
# Projectors (D3): certifier result -> typed projection
# ---------------------------------------------------------------------------

_GATE_CRASH_PREFIX = "gate crashed"


def _gate_fields(raw: GateResult | Mapping[str, Any]) -> tuple[str, bool, str]:
    if isinstance(raw, GateResult):
        return raw.gate, bool(raw.passed), raw.details
    if isinstance(raw, Mapping):
        return str(raw.get("gate", "")), bool(raw.get("passed")), str(raw.get("details", ""))
    raise TypeError(f"a gate projection needs a GateResult or a mapping, not {type(raw).__name__}")


def project_gate(raw: GateResult | Mapping[str, Any]) -> Diagnostic:
    """One gate result -> `{gate, passed, code}`; details and evidence are DROPPED.

    `gate_crashed` is read off the `_guarded` wrapper's fixed prefix; the
    exception text after it (DuckDB errors, paths) never enters the projection."""
    name, passed, details = _gate_fields(raw)
    if passed:
        code = "ok"
    elif details.startswith(_GATE_CRASH_PREFIX):
        code = "gate_crashed"
    else:
        code = "failed"
    try:
        return Diagnostic(source=DiagnosticSource.GATE, ok=passed, code=code, subject=name)
    except ValidationError as exc:
        # A gate NAME that is not an identifier is a producer leak, not a value
        # to render: the name is the only thing this projection would carry.
        raise DiagnosticTripwire(
            "schema", "gate_name_not_identifier", source=DiagnosticSource.GATE.value
        ) from exc


def project_gate_battery(report: AcceptanceReport | Mapping[str, Any] | None) -> list[dict]:
    """`{gate, passed, code}` rows for a recorded battery, in battery order.

    The view builder behind `cli._triage_view`: replaces the raw `details` loop.
    A payload without a gate list (a StagePayload, `None`) yields no rows."""
    if report is None:
        return []
    if isinstance(report, AcceptanceReport):
        gates: Sequence[Any] = report.gates
    elif isinstance(report, Mapping):
        raw_gates = report.get("gates")
        gates = raw_gates if isinstance(raw_gates, (list, tuple)) else ()
    else:
        raise TypeError(
            f"project_gate_battery takes an AcceptanceReport or a mapping, not "
            f"{type(report).__name__}"
        )
    rows: list[dict] = []
    for gate in gates:
        if not isinstance(gate, (GateResult, Mapping)):
            continue
        diag = project_gate(gate)
        rows.append({"gate": diag.subject, "passed": diag.ok, "code": diag.code})
    return rows


# Gate-detail parsers expose only booleans and public identifiers. They never
# project populations, counts, rewards, or key tuples; unknown shapes yield the
# generic gate result.

_RM_LEAK_RE = re.compile(
    r"(?P<case>[A-Za-z][A-Za-z0-9_.\-]*): LEAK \u2014 must lose reward on"
)
_RM_FULL_MISSING_RE = re.compile(
    r"(?P<case>[A-Za-z][A-Za-z0-9_.\-]*): expected FULL reward on"
)
_RM_UNMEASURED_RE = re.compile(
    r"(?P<case>[A-Za-z][A-Za-z0-9_.\-]*): no rewards? recorded"
)
#: `<pop>:<child>-><parent>:` — the referential-integrity gate's label.
_RI_LABEL_RE = re.compile(
    r"(?P<pop>[a-z]+):(?P<child>[A-Za-z][A-Za-z0-9_.\-]*)->"
    r"(?P<parent>[A-Za-z][A-Za-z0-9_.\-]*): "
)
#: `<pop>:<mart>:` — the mart-key-unique gate's label.
_KU_LABEL_RE = re.compile(r"(?P<pop>[a-z]+):(?P<mart>[A-Za-z][A-Za-z0-9_.\-]*): ")

#: Gate names whose failure detail has a sanctioned boolean projection.
_DETAILED_GATES: frozenset[str] = frozenset(
    {"required-mutants", "referential-integrity", "mart-key-unique"}
)


def _segments(text: str, label_re: re.Pattern[str]) -> list[tuple[re.Match[str], str]]:
    """(label match, the text up to the next label) for every label in `text`."""
    matches = list(label_re.finditer(text))
    out: list[tuple[re.Match[str], str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        out.append((match, text[match.end():end]))
    return out


def _required_mutant_rows(details: str, *, code: str) -> list[Diagnostic]:
    """Row 1: `{case: {killed_where_expected, leaked_somewhere, ...}}`.

    Read off the gate's per-case leak sentences; the per-population reward
    vector in `evidence` is never read. Case names are public."""
    leaked = {m.group("case") for m in _RM_LEAK_RE.finditer(details)}
    full_missing = {m.group("case") for m in _RM_FULL_MISSING_RE.finditer(details)}
    unmeasured = {m.group("case") for m in _RM_UNMEASURED_RE.finditer(details)}
    rows: list[Diagnostic] = []
    for case in sorted(leaked | full_missing | unmeasured):
        rows.append(
            Diagnostic(
                source=DiagnosticSource.GATE,
                ok=False,
                code=code,
                subject="required-mutants",
                names=(case,),
                flags={
                    "killed_where_expected": case not in leaked and case not in unmeasured,
                    "leaked_somewhere": case in leaked,
                    "full_reward_missing": case in full_missing,
                    "unmeasured": case in unmeasured,
                },
            )
        )
    return rows


def _referential_integrity_rows(details: str, *, code: str) -> list[Diagnostic]:
    """Row 6: `{relationship: (child, parent), violation_kind}` as booleans,
    OR-ed over populations so no hidden population is named."""
    kinds: dict[tuple[str, str], dict[str, bool]] = {}
    for match, body in _segments(details, _RI_LABEL_RE):
        key = (match.group("child"), match.group("parent"))
        flags = kinds.setdefault(
            key,
            {"dangling": False, "partial_null": False, "null_required": False, "unreadable": False},
        )
        if "has no parent row" in body:
            flags["dangling"] = True
        if "partially-NULL" in body:
            flags["partial_null"] = True
        if "NULL key on a REQUIRED link" in body:
            flags["null_required"] = True
        if "missing or unreadable" in body:
            flags["unreadable"] = True
    return [
        Diagnostic(
            source=DiagnosticSource.GATE,
            ok=False,
            code=code,
            subject="referential-integrity",
            names=key,
            flags=flags,
        )
        for key, flags in sorted(kinds.items())
    ]


def _key_unique_rows(details: str, *, code: str) -> list[Diagnostic]:
    """Row 6 (key-unique gates): `{mart, violation_kind: duplicate}`."""
    kinds: dict[str, dict[str, bool]] = {}
    for match, body in _segments(details, _KU_LABEL_RE):
        flags = kinds.setdefault(
            match.group("mart"), {"duplicate": False, "undeclared_mart": False}
        )
        if "more than once" in body:
            flags["duplicate"] = True
        if "does not declare" in body:
            flags["undeclared_mart"] = True
    return [
        Diagnostic(
            source=DiagnosticSource.GATE,
            ok=False,
            code=code,
            subject="mart-key-unique",
            names=(mart,),
            flags=flags,
        )
        for mart, flags in sorted(kinds.items())
    ]


def project_gate_details(
    raw: GateResult | Mapping[str, Any],
    *,
    variant: TaskVariant | str | None = None,
) -> tuple[Diagnostic, ...]:
    """Project one failing gate into sanctioned population-route booleans.

    Expose only closed relationship/case flags and public identifiers; never include
    reward values, counts, SQL, or prose evidence.
    """
    base = project_gate(raw)
    name, passed, details = _gate_fields(raw)
    flags: dict[str, bool] = {}
    if variant is not None:
        from elt_taskgen.verification import gates as gates_mod

        scope = gates_mod.classify_variant_failure(TaskVariant(variant), name)
        flags["variant_local"] = scope == gates_mod.FAILURE_VARIANT_LOCAL
    head = base.model_copy(update={"flags": flags}) if flags else base
    if passed or base.code == "gate_crashed" or name not in _DETAILED_GATES:
        return (head,)
    if name == "required-mutants":
        rows = _required_mutant_rows(details, code=base.code)
    elif name == "referential-integrity":
        rows = _referential_integrity_rows(details, code=base.code)
    else:
        rows = _key_unique_rows(details, code=base.code)
    return (head, *rows)


def project_dual_build(record: Mapping[str, Any] | None) -> Diagnostic:
    """The pending independent-build adjudication -> `{ok, code}`.

    The record's `detail` is the independent build's own text (row 10:
    "expected N rows, got M", per-population outcomes, DuckDB errors), and its
    `agreement` map names which HIDDEN populations disagreed. Both are for the
    human adjudicator; the model-facing projection is one code and no
    population identity."""
    if record is None:
        return Diagnostic(source=DiagnosticSource.DUAL_BUILD, ok=True, code="agreed")
    if not isinstance(record, Mapping):
        raise TypeError("a dual-build projection needs the adjudication mapping")
    status = str(record.get("status", ""))
    detail = str(record.get("detail", "")).lower()
    if status == "agreed":
        return Diagnostic(source=DiagnosticSource.DUAL_BUILD, ok=True, code="agreed")
    if "parse" in detail or "syntax" in detail:
        code = "parse_error"
    elif (
        ("expected" in detail and "row" in detail)
        or "mismatch" in detail
        or "disagree" in detail
        or "differ" in detail
        or "agreement" in detail
    ):
        code = "mismatch"
    elif any(word in detail for word in ("error", "exception", "raised", "failed", "crash")):
        code = "execution_error"
    else:
        code = "unclassified"
    return Diagnostic(source=DiagnosticSource.DUAL_BUILD, ok=False, code=code)


_PROMOTION_EXCEPTION_CODES: tuple[tuple[str, str], ...] = (
    ("InertAstMutationError", "inert"),
    ("InertLoadMutationError", "inert"),
    ("InapplicableLoadMutationError", "inapplicable"),
    ("MutationFidelityError", "fidelity_failed"),
    ("ValueError", "uncompilable"),
    ("KeyError", "uncompilable"),
    ("TypeError", "uncompilable"),
)


def _promotion_code(promoted: bool, reason: str, mismatches: Sequence[str]) -> str:
    if promoted:
        return "promoted"
    if mismatches or "does not match the proposed expectation" in reason:
        return "mismatch"
    if "keep FULL combined reward" in reason:
        return "no_kill_predicted"
    if reason.startswith("proposal could not be executed:"):
        for exc_name, code in _PROMOTION_EXCEPTION_CODES:
            if f": {exc_name}:" in reason or reason.endswith(exc_name):
                return code
    return "unknown_kind"


def _promotion_fields(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    raise TypeError(
        f"a promotion projection needs a PromotionOutcome or a mapping, not "
        f"{type(raw).__name__}"
    )


def project_promotion(raw: Any) -> dict:
    """Build the post-session proposal-promotion audit projection.

    Emit finding/population booleans and fidelity status only. This record is for
    rejection audit and is not returned to a model session.
    """
    fields = _promotion_fields(raw)
    promoted = bool(fields.get("promoted"))
    reason = str(fields.get("reason", ""))
    mismatches = fields.get("mismatches") or ()
    predicted = fields.get("predicted") or {}
    measured_pass = fields.get("measured_pass") or {}
    fidelity = fields.get("fidelity") or {}
    per_population: dict[str, dict[str, bool]] = {}
    for pop in PopulationName:
        if pop.value in predicted or pop.value in measured_pass:
            per_population[pop.value] = {
                "predicted": bool(predicted.get(pop.value, False)),
                "measured_pass": bool(measured_pass.get(pop.value, False)),
            }
    code = _promotion_code(promoted, reason, tuple(mismatches))
    return {
        "diagnostics_version": DIAGNOSTICS_VERSION,
        "finding_id": str(fields.get("finding_id", "")),
        "case_name": str(fields.get("case_name", "")),
        "kind": str(fields.get("kind", "")),
        "promoted": promoted,
        "code": code,
        "per_population": per_population,
        "fidelity_ok": bool(fidelity.get("passed", False)) if isinstance(fidelity, Mapping) else False,
    }


def project_compile(cases: Sequence[AttackCase]) -> Diagnostic:
    """`compile_probe` / `compile_proposal` -> `{compiles, kind, is_directive}`.

    Takes what `attacks.compile_attacks(bare, [probe])` returned. Never the
    compiled key (it embeds `case.mutation`), never an inert or inapplicable
    sentence: inertness needs gold and is not decided inside any session."""
    if not cases:
        return Diagnostic(source=DiagnosticSource.COMPILE, ok=False, code="uncompilable")
    case = cases[0]
    kind = case.kind.value if isinstance(case.kind, AttackKind) else str(case.kind)
    return Diagnostic(
        source=DiagnosticSource.COMPILE,
        ok=True,
        code="compiles",
        subject=kind,
        flags={"is_directive": str(case.mutation).startswith("directive:")},
    )


def project_certify(
    code: str,
    *,
    model_stages_deferred: bool,
    discrimination_weakened: bool = False,
) -> Diagnostic:
    """Project provider-free certification into a value-free diagnostic.

    Return approved stage code, overall status, and whether model stages were deferred.
    Do not expose stage payloads, counts, paths, or raw errors.
    """
    if code not in CERTIFY_CODES:
        raise ValueError(f"certify code {code!r} is not one of {CERTIFY_CODES}")
    if discrimination_weakened:
        raise ValueError(
            "discrimination_weakened is always False in Phase 1: the guard runs "
            "only inside trial_phase at submit (the in-session attack member is Phase 3)"
        )
    return Diagnostic(
        source=DiagnosticSource.CERTIFY,
        ok=code == "certify_green",
        code=code,
        flags={
            "model_stages_deferred": bool(model_stages_deferred),
            "discrimination_weakened": False,
        },
    )


def project_certify_attack(
    code: str,
    *,
    model_stages_deferred: bool,
    discrimination_weakened: bool = False,
) -> Diagnostic:
    """Project certification that may include the attack stage.

    Extend the base vocabulary with attack codes and the discrimination-weakened flag.
    Overall `ok` requires green stages and a non-weakened matrix. Subjects and names
    remain empty.
    """
    if code not in CERTIFY_CODES and code not in CERTIFY_ATTACK_CODES:
        raise ValueError(
            f"certify code {code!r} is not one of {CERTIFY_CODES + CERTIFY_ATTACK_CODES}"
        )
    weakened = bool(discrimination_weakened)
    return Diagnostic(
        source=DiagnosticSource.CERTIFY,
        ok=code == "certify_green" and not weakened,
        code=code,
        flags={
            "model_stages_deferred": bool(model_stages_deferred),
            "discrimination_weakened": weakened,
        },
    )


# ---------------------------------------------------------------------------
# Phase 2 (roadmap 2.a): the DEV/T witness tool projections
# ---------------------------------------------------------------------------

def project_list_schemas(table_names: Sequence[str]) -> Diagnostic:
    """`list_schemas` -> `{schemas_listed, names}`: the DEVELOPMENT source
    table names, deduplicated and sorted, and nothing else. Names only (no
    count, no gold, no mart): the gatekeeper holds every name to
    `PublicIdentifierSet(task)`."""
    names = tuple(sorted({str(n) for n in table_names if n}))
    return Diagnostic(
        source=DiagnosticSource.SCHEMA, ok=True, code="schemas_listed", names=names
    )


def project_bind(
    *,
    binds: bool,
    error_class: str,
    columns_match: bool,
    missing_columns: Sequence[str],
    mart: str,
) -> Diagnostic:
    """`dry_run_sql` -> `{binds, error_class, columns_match, missing_columns}`.

    `ok` is whether the compiled SQL binds over the empty typed tables; the
    code is `binds` when it does, else the classified `error_class` (never the
    DuckDB text); `binds` and `columns_match` are booleans; the missing mart
    columns (public names) travel in `names`; the mart is the `subject`."""
    if binds:
        code = "binds"
    else:
        code = str(error_class)
        if code not in BIND_CODES:
            code = "other"
    return Diagnostic(
        source=DiagnosticSource.BIND,
        ok=bool(binds),
        code=code,
        subject=str(mart),
        flags={"binds": bool(binds), "columns_match": bool(columns_match)},
        names=tuple(str(c) for c in missing_columns),
    )


def project_mart_dev(*, mart: str, code: str) -> Diagnostic:
    """`run_mart_sql_dev` -> `{mart, code}`; rows and row counts are DISCARDED.

    One code per call (the first failing mart wins); `ok` iff `ok`. `subject`
    is the mart (empty when a timeout stops the batch before a mart is
    attributed)."""
    if code not in MART_DEV_CODES:
        raise ValueError(f"run_mart_sql_dev code {code!r} is not one of {MART_DEV_CODES}")
    return Diagnostic(
        source=DiagnosticSource.MART_DEV, ok=code == "ok", code=code, subject=str(mart)
    )


def project_dev_query_error(code: str) -> Diagnostic:
    """`dev_query` ERROR -> `{ok: false, code}` (the success path is
    `DevRows`). The code is one of `DEV_QUERY_CODES`; no DuckDB text, no
    path, no count."""
    if code not in DEV_QUERY_CODES:
        raise ValueError(f"dev_query error code {code!r} is not one of {DEV_QUERY_CODES}")
    return Diagnostic(source=DiagnosticSource.DEV_QUERY, ok=False, code=code)


def project_load_plan(*, code: str, table: str = "") -> Diagnostic:
    """`check_load_plan` -> `{ok, code, subject}` with the offending table in
    `subject` (R0.3: the code is a lowercase snake, the identifier travels in
    `subject`). STATIC: this projection never carries a count."""
    if code not in LOAD_PLAN_CODES:
        raise ValueError(f"check_load_plan code {code!r} is not one of {LOAD_PLAN_CODES}")
    return Diagnostic(
        source=DiagnosticSource.LOAD_PLAN, ok=code == "ok", code=code, subject=str(table)
    )


_PROSE_MART_RE = re.compile(r"^mart '([^']+)'")
_PROSE_COLUMN_RE = re.compile(r"output column '([^']+)'")
_PROSE_RULE_RE = re.compile(r"\brule (\d+) \[")


def _prose_kind(problem: str) -> str:
    lowered = problem.lower()
    if "solver prose is empty" in lowered:
        return "empty_prose"
    if "prose states the sql mechanics" in lowered or "operator" in lowered.split(":", 1)[0]:
        return "operator_vocabulary"
    if "mutually exclusive" in lowered or "negation" in lowered or "contradict" in lowered:
        return "contradicted"
    if "never mentioned" in lowered:
        return "missing_object"
    if "lineage" in lowered or "attribution" in lowered:
        return "lineage_disagrees"
    return "not_represented"


def project_prose_problems(problems: Sequence[str], *, task: TaskIR) -> tuple[DiagnosticText, ...]:
    """`check_prose_fidelity(task)` problems -> `{gate: prose, kind, mart, column?}`
    with the sentence carried as text.

    Text pass-through is allowed ONLY because the producer's inputs are pinned
    public (`TEXT_PRODUCER_PUBLIC_FIELDS[PROSE]`); the gatekeeper still holds
    every identifier in the sentence to `PublicIdentifierSet(task)` and the
    projector's canary refuses any frozen count inside it."""
    public = PublicIdentifierSet(task)
    out: list[DiagnosticText] = []

    def _admissible(name: str) -> bool:
        # Preserve verbatim public identifiers, including leading-underscore
        # names. Bare numbers, paths, and values still fail the shape check.
        return name in public and _IDENT_RE.fullmatch(name) is not None

    for problem in problems:
        sentence = " ".join(str(problem).split())
        if not sentence:
            continue
        mart_match = _PROSE_MART_RE.search(sentence)
        column_match = _PROSE_COLUMN_RE.search(sentence)
        mart = mart_match.group(1) if mart_match and _admissible(mart_match.group(1)) else ""
        names: tuple[str, ...] = ()
        if column_match and _admissible(column_match.group(1)):
            names = (column_match.group(1),)
        out.append(
            DiagnosticText(
                source=DiagnosticSource.PROSE,
                code=_prose_kind(sentence),
                subject=mart,
                names=names,
                text=sentence[:_MAX_TEXT_CHARS],
            )
        )
    return tuple(out)


def project(
    source: DiagnosticSource | str,
    raw: Any,
    *,
    task: TaskIR,
    package: Any = None,
) -> Diagnostic:
    """Project one certifier result into a `Diagnostic`. Runs in the D3 worker.

    `task` and `package` are what the projector is allowed to hold (the private
    scalar set is derived from them by `serialize_for_transport`); the code-only
    projectors below never read a value out of them. PROSE is text-allowlisted
    and uses `project_prose_problems` instead."""
    source = DiagnosticSource(source)
    if source is DiagnosticSource.GATE:
        return project_gate(raw)
    if source is DiagnosticSource.DUAL_BUILD:
        return project_dual_build(raw)
    if source is DiagnosticSource.COMPILE:
        return project_compile(tuple(raw))
    if source is DiagnosticSource.PROMOTION:
        record = project_promotion(raw)
        return Diagnostic(
            source=DiagnosticSource.PROMOTION,
            ok=bool(record["promoted"]),
            code=str(record["code"]),
            flags={"fidelity_ok": bool(record["fidelity_ok"])},
        )
    if source is DiagnosticSource.REJECTION:
        from elt_taskgen.review import repair_proposer as rp  # lazy: rp imports this module

        return rp.project_rejection(raw)
    if source is DiagnosticSource.CERTIFY:
        fields = dict(raw) if isinstance(raw, Mapping) else {"code": str(raw)}
        return project_certify(
            str(fields.get("code", "")),
            model_stages_deferred=bool(fields.get("model_stages_deferred", False)),
            discrimination_weakened=bool(fields.get("discrimination_weakened", False)),
        )
    raise ValueError(
        f"source {source.value!r} is text-allowlisted; use project_prose_problems"
    )


# ---------------------------------------------------------------------------
# The private scalar set (held by the projector only)
# ---------------------------------------------------------------------------

def private_scalars(
    task: TaskIR, package: Any = None, *, route: RepairRoute | None = None
) -> frozenset[str]:
    """Return every private numeric scalar as a digit string.

    The set includes hidden population scales and realized counts, literal-row numbers,
    frozen stage-one counts, and gold mart counts available in the package. Public
    identifiers are masked separately. Returned values are used only by leak detection
    and never sent to a model.
    """
    route_v = RepairRoute(route) if route is not None else None
    scalars: set[str] = set()
    for spec in task.populations:
        if spec.name is PopulationName.DEVELOPMENT:
            continue
        if route_v is not RepairRoute.POPULATION:
            for value in spec.scale.values():
                scalars.add(str(int(value)))
        for rows in spec.literal_rows.values():
            scalars.add(str(len(rows)))
    gold = getattr(package, "gold", package)
    stage1 = getattr(gold, "stage1", None)
    if isinstance(stage1, Mapping):
        for pop_name, counts in stage1.items():
            # OQ-23 option C: the DEVELOPMENT stage-1 counts are solver-visible
            # by construction, so they are never canaries.
            if str(pop_name) == PopulationName.DEVELOPMENT.value:
                continue
            if isinstance(counts, Mapping):
                for value in counts.values():
                    try:
                        scalars.add(str(int(value)))
                    except (TypeError, ValueError):
                        continue
    stage2 = getattr(gold, "stage2_csv", None)
    if isinstance(stage2, Mapping):
        for marts in stage2.values():
            if not isinstance(marts, Mapping):
                continue
            for csv_text in marts.values():
                if isinstance(csv_text, str):
                    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
                    scalars.add(str(max(len(lines) - 1, 0)))
    return frozenset(scalars)


def hidden_realized_counts(task: TaskIR) -> frozenset[str]:
    """Return realized row counts for hidden population scales as digit strings.

    Counts are derived deterministically without gold data. Development is public and
    excluded; invalid collapsed scale bands contribute nothing.
    """
    from elt_taskgen.generation import source_data

    counts: set[str] = set()
    for spec in task.populations:
        if spec.name is PopulationName.DEVELOPMENT:
            continue
        for table, declared in spec.scale.items():
            try:
                counts.add(str(int(source_data.realized_row_count(task.task_id, str(table), int(declared)))))
            except (TypeError, ValueError):
                continue
    return frozenset(counts)


#: The detector codes `population_condition_private_shape` answers, beyond
#: the shared shape detectors (`measured_value`, `count_vector`, `key_tuple`,
#: `path`, `secret_literal`, `executor_text`, `private_scalar`).
CONDITION_STANDALONE_NUMBER = "standalone_number"


def population_condition_private_shape(
    text: str,
    *,
    task: TaskIR,
    population: PopulationName | str,
    package: Any = None,
) -> str | None:
    """Return the detector code when a population condition contains private-shaped
    material.

    Reject measured values, paths, secrets, executor text, hidden-population counts or
    key tuples, and standalone private scalars. Public identifiers are masked before
    numeric-shape checks. The function returns only a code and never the condition text.
    """
    if _MEASURED_VALUE_RE.search(text):
        return "measured_value"
    shape = _detect_paths_and_secrets(text)
    if shape is not None:
        return shape
    name = PopulationName(population)
    if name is PopulationName.DEVELOPMENT:
        return None
    shape = _detect_value_shapes(text)
    if shape is not None:
        return shape
    declared: set[str] = set()
    for spec in task.populations:
        if spec.name is name:
            declared = {str(int(v)) for v in spec.scale.values()}
    for match in _STANDALONE_NUMBER_RE.finditer(text):
        if match.group(0).lstrip("-") not in declared:
            return CONDITION_STANDALONE_NUMBER
    canary = private_scalars(task, package, route=RepairRoute.POPULATION) | hidden_realized_counts(task)
    runs = _digit_runs_outside_public(text, PublicIdentifierSet(task), exempt_rule_ordinals=False)
    if any(run in canary or run.lstrip("0") in canary for run in runs):
        return "private_scalar"
    return None


# ---------------------------------------------------------------------------
# Detectors shared by the projector and the gatekeeper
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_DIGIT_RUN_RE = re.compile(r"\d+")
#: A number standing on its own (not inside an identifier): `12`, `-3`, `0.25`.
_STANDALONE_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?(?![A-Za-z0-9_])")
#: Plan-rule ordinals in a prose-fidelity sentence ("rule 3 [filter]") are
#: public: they index the mart's plan, which the prose must represent.
_RULE_ORDINAL_RE = re.compile(r"\brule (\d{1,3})\b")
#: A count-vector cell (`orders=4711`) and a key-tuple literal (`('c_9001', 3)`):
#: the two shapes the EL gates render hidden rows in. Refused in any text.
_COUNT_VECTOR_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\s*=\s*-?\d+(?:\.\d+)?(?![A-Za-z0-9_])")
_KEY_TUPLE_RE = re.compile(
    r"\(\s*(?:'[^']*'|\"[^\"]*\"|-?\d+(?:\.\d+)?)"
    r"(?:\s*,\s*(?:'[^']*'|\"[^\"]*\"|-?\d+(?:\.\d+)?))+\s*,?\s*\)"
)


def _detect_value_shapes(text: str) -> str | None:
    if _COUNT_VECTOR_RE.search(text):
        return "count_vector"
    if _KEY_TUPLE_RE.search(text):
        return "key_tuple"
    return None


#: An identifier-looking token in a sentence: snake_case with at least one
#: underscore. Plain words are prose; these are names, and in a text-allowlisted
#: projection every name must be public (row 9: "identifiers restricted to
#: public column names").
_UNDERSCORE_IDENT_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")


@lru_cache(maxsize=1)
def _all_codes() -> frozenset[str]:
    return frozenset(code for codes in _CODES_BY_SOURCE.values() for code in codes) | frozenset(
        code for codes in _FLAGGED_CODES_BY_SOURCE.values() for code in codes
    )


def _non_public_identifiers_in_text(text: str, public: PublicIdentifierSet) -> tuple[str, ...]:
    codes = _all_codes()
    return tuple(sorted({
        tok for tok in _UNDERSCORE_IDENT_RE.findall(text)
        if tok not in public and tok not in codes
    }))

#: Per-population MEASURED values in every shape the battery emits them: the
#: evidence matrix (`primary=0.250000`), the JSON reward map (`"primary": 1.0`),
#: the failing-mutant prose (`on primary, got 1.0`), the dual-build agreement
#: value, and the stage-1 comparator sentence (`expected 4711 rows, got 4710`).
_POPULATIONS_ALT = "|".join(p.value for p in PopulationName)
_MEASURED_VALUE_RE = re.compile(
    rf"\b(?:{_POPULATIONS_ALT})\b\"?\s*(?:=|:|,\s*got)\s*\"?-?\d"
    r"|\bagreement\b\"?\s*(?:[:=]|of|is|was)?\s*\"?-?\d"
    r"|\bmeasured reward\b"
    r"|\bexpected\s+\d+\s+rows?\b"
    r"|\bgot\s+-?\d+(?:\.\d+)?\b",
    re.IGNORECASE,
)

#: Paths and private-tree tokens: absolute paths of every host family, home
#: shorthand, the private release and attack trees, DuckDB oracles and
#: workspace runtime state, `runs/` and credential files.
_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_])/(?:Users|home|tmp|private|var|opt|etc|srv|mnt|root|workspaces)/"
    r"|(?<![A-Za-z0-9_])~/"
    r"|(?<![A-Za-z0-9_])[A-Za-z]:\\"
    r"|answer_key/|(?<![A-Za-z0-9_])private/|attacks/|populations/"
    r"|\.duckdb\b|oracle/|/rendered/|(?<![A-Za-z0-9_])rendered/"
    r"|\.workspace-runtime|(?<![A-Za-z0-9_])runs/|_credential\.json|\.tfstate)",
    re.IGNORECASE,
)

#: Executor text: a DuckDB, Python or Terraform error shape.
_EXECUTOR_TEXT_RE = re.compile(
    r"\b(?:Binder|Catalog|Parser|Conversion|Constraint|Invalid Input|Out of Memory|"
    r"Not implemented|Syntax|IO|Internal) Error\b"
    r"|\bTraceback \(most recent call last\)"
    r"|\bduckdb\.(?:duckdb\.)?[A-Za-z]+Error\b"
    r"|\bLINE \d+:",
)

_SECRET_RE = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|"
    r"sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{8,})"
)


def _string_values(kind: str, body: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(field, string) pairs of the projection body: every place a value could hide."""
    out: list[tuple[str, str]] = []
    out.append(("code", str(body.get("code", ""))))
    subject = body.get("subject")
    if isinstance(subject, str) and subject:
        out.append(("subject", subject))
    for name in body.get("names") or ():
        out.append(("names", str(name)))
    for key in (body.get("flags") or {}):
        out.append(("flags", str(key)))
    if kind == _KIND_TEXT:
        out.append(("text", str(body.get("text", ""))))
    if kind == _KIND_DEV_ROWS:
        for column in body.get("columns") or ():
            out.append(("columns", str(column)))
        for row in body.get("rows") or ():
            for cell in row:
                if isinstance(cell, str):
                    out.append(("rows", cell))
    return out


def _digit_runs_outside_public(text: str, public: PublicIdentifierSet, *, exempt_rule_ordinals: bool) -> list[str]:
    """Digit runs of `text` that are not embedded in a public identifier."""
    if exempt_rule_ordinals:
        text = _RULE_ORDINAL_RE.sub("rule", text)
    runs: list[str] = []
    for match in _WORD_RE.finditer(text):
        word = match.group(0)
        if word in public:
            continue
        runs.extend(_DIGIT_RUN_RE.findall(word))
    return runs


def _mask_public_identifiers(text: str, public: PublicIdentifierSet) -> str:
    """Mask public identifiers with equal-length letters before shape scanning.

    This prevents digits inside approved names from looking like private numbers without
    weakening checks on other text.
    """
    return _WORD_RE.sub(
        lambda m: "x" * len(m.group(0)) if m.group(0) in public else m.group(0), text
    )


def _detect_private_material(text: str, task: TaskIR, route: RepairRoute | None) -> str | None:
    """The council's definition of private, applied to outbound bytes: SQL
    shingles and anonymized-AST fingerprints of the reference and the attack
    mutations (route-aware: REFERENCE may name its own subject), plus the
    measured-value shapes. Returns a code, or None when clean."""
    from elt_taskgen.review import council
    from elt_taskgen.review import repair_proposer as rp  # lazy: rp imports this module

    if _MEASURED_VALUE_RE.search(text):
        return "measured_value"
    normalized = council._normalize(text)
    if route is not None:
        fragments = rp._private_fragments(task, route)
    else:
        fragments = sorted(
            {f for frags in council._private_sql_fragments(task).values() for f in frags}
        )
    for fragment in fragments:
        if fragment and fragment in normalized:
            return "sql_shingle"
    private_fps = council._private_ast_fingerprints(task)
    if route is RepairRoute.REFERENCE:
        reference_fps: set[str] = {
            fp
            for label, fps in private_fps.items()
            if label.startswith("reference:")
            for fp in fps
        }
        private_fps = {
            label: frozenset(fps - reference_fps)
            for label, fps in private_fps.items()
            if not label.startswith("reference:")
        }
    if any(private_fps.values()):
        view_fps: set[str] = set()
        for span in council._prose_sql_spans(text):
            view_fps |= council._sql_ast_fingerprints(span)
        if view_fps and any(fps & view_fps for fps in private_fps.values()):
            return "sql_ast"
    return None


def _detect_paths_and_secrets(text: str) -> str | None:
    if _SECRET_RE.search(text):
        return "secret_literal"
    if _PATH_RE.search(text):
        return "path"
    if _EXECUTOR_TEXT_RE.search(text):
        return "executor_text"
    return None


def dev_rows_leak_shape(rows: DevRows) -> str | None:
    """Return the leak-shape code for development-row transport data.

    Detect paths, secrets, executor text, private scalars, and non-public identifiers
    without returning offending content.
    """
    joined = "\n".join(cell for row in rows.rows for cell in row if isinstance(cell, str))
    return _detect_paths_and_secrets(joined)


# ---------------------------------------------------------------------------
# The projector half: serialize_for_transport (D3, value-aware)
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def serialize_for_transport(
    diag: Diagnostic | DiagnosticText | DevRows,
    *,
    task: TaskIR,
    package: Any = None,
    route: RepairRoute | None = None,
) -> str:
    """Serialize a projected result into canonical model-bound JSON.

    Include kind and diagnostic version, then enforce public identifiers and reject
    private scalars, SQL, paths, executor text, and secrets with `DiagnosticTripwire`.
    """
    kind = _kind_of(diag)
    payload = _transport_payload(diag)
    body = {k: v for k, v in payload.items() if k not in ("kind", "diagnostics_version")}
    text = canonical_json(payload)
    raw = text.encode("utf-8")
    source = str(body.get("source", kind))
    sha = _sha256(raw)

    public = PublicIdentifierSet(task)
    unknown = public.unknown(diag.identifiers())
    if unknown:
        raise DiagnosticTripwire(
            "schema", "identifier_not_public", source=source, payload_sha256=sha, quarantined=raw
        )

    if kind != _KIND_DEV_ROWS:
        scalars = private_scalars(task, package, route=route)
        for field, value in _string_values(kind, body):
            # ``Diagnostic.code`` is already constrained by the model's
            # source-specific closed vocabulary.  Treating digits inside that
            # trusted enum as measured values makes the legitimate
            # ``s3_part_file`` loader diagnostic trip its own leak barrier.
            # Identifier- and flag-bearing fields remain fully scanned.
            if kind == _KIND_DIAGNOSTIC and field == "code":
                continue
            runs = _digit_runs_outside_public(
                value, public, exempt_rule_ordinals=(field == "text")
            )
            if kind == _KIND_DIAGNOSTIC and runs:
                # A Diagnostic has no legitimate number anywhere: a digit run
                # outside a public identifier is a value, whatever it equals.
                raise DiagnosticTripwire(
                    "canary", "numeric_value", source=source, payload_sha256=sha, quarantined=raw
                )
            if any(run in scalars or run.lstrip("0") in scalars for run in runs):
                raise DiagnosticTripwire(
                    "canary", "private_scalar", source=source, payload_sha256=sha, quarantined=raw
                )
            if field == "text":
                shape = _detect_value_shapes(value)
                if shape is not None:
                    raise DiagnosticTripwire(
                        "numbers", shape, source=source, payload_sha256=sha, quarantined=raw
                    )

    joined = "\n".join(value for _, value in _string_values(kind, body))
    material = _detect_private_material(joined, task, None)
    if material is not None:
        raise DiagnosticTripwire(
            "private_material", material, source=source, payload_sha256=sha, quarantined=raw
        )
    shape = _detect_paths_and_secrets(joined)
    if shape is not None:
        raise DiagnosticTripwire(
            "paths_and_secrets", shape, source=source, payload_sha256=sha, quarantined=raw
        )
    if kind == _KIND_TEXT and _non_public_identifiers_in_text(str(body.get("text", "")), public):
        raise DiagnosticTripwire(
            "schema", "identifier_not_public", source=source, payload_sha256=sha, quarantined=raw
        )
    return text


# ---------------------------------------------------------------------------
# The gatekeeper half: assert_value_free (D1, no gold)
# ---------------------------------------------------------------------------

def assert_value_free(
    payload: bytes,
    *,
    task: TaskIR,
    route: RepairRoute | None = None,
) -> None:
    """Validate model-bound bytes in the gold-free gatekeeper.

    Require the closed transport schema, approved codes, public identifiers, and boolean
    flags. Reject private numeric material, SQL, paths, secrets, executor text, or
    unapproved free text with `DiagnosticTripwire`. No offending value is included in
    the exception delivered upstream.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("assert_value_free takes the serialized bytes, not an object")
    raw = bytes(payload)
    sha = _sha256(raw)

    def trip(detector: str, code: str, source: str = "") -> DiagnosticTripwire:
        return DiagnosticTripwire(
            detector, code, source=source, payload_sha256=sha, quarantined=raw
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise trip("schema", "not_utf8") from None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        raise trip("schema", "not_json") from None
    if not isinstance(doc, dict):
        raise trip("schema", "not_an_object")
    kind = doc.get("kind")
    if kind not in _MODEL_BY_KIND:
        raise trip("schema", "unknown_kind")
    if doc.get("diagnostics_version") != DIAGNOSTICS_VERSION:
        raise trip("schema", "diagnostics_version_mismatch", str(kind))
    body = {k: v for k, v in doc.items() if k not in ("kind", "diagnostics_version")}
    source = str(body.get("source", kind))
    try:
        obj = _MODEL_BY_KIND[kind].model_validate(body)
    except ValidationError:
        raise trip("schema", "invalid_shape", source) from None
    if canonical_json(_transport_payload(obj)) != text:
        # Re-serialization must be byte-identical: anything else is a payload
        # that carries more than its schema (whitespace smuggling included).
        raise trip("schema", "not_canonical", source)

    public = PublicIdentifierSet(task)
    if public.unknown(obj.identifiers()):
        raise trip("schema", "identifier_not_public", source)

    values = _string_values(kind, body)
    joined = "\n".join(value for _, value in values)

    material = _detect_private_material(joined, task, route)
    if material is not None:
        raise trip("private_material", material, source)

    if kind == _KIND_DIAGNOSTIC:
        for field, value in values:
            # Codes are schema-validated members of the source's closed
            # vocabulary.  The projector applies the same exemption; all
            # model- or producer-selected identifiers and flag keys are still
            # checked below for numeric material.
            if field == "code":
                continue
            # Mask approved public identifiers before scanning for standalone
            # numbers; digits inside names are allowed, all other numeric runs are not.
            if _STANDALONE_NUMBER_RE.search(
                _mask_public_identifiers(value, public)
            ) or _digit_runs_outside_public(value, public, exempt_rule_ordinals=False):
                raise trip("numbers", "numeric_value", source)
    elif kind == _KIND_TEXT:
        for field, value in values:
            if field != "text":
                continue
            shape = _detect_value_shapes(value)
            if shape is not None:
                raise trip("numbers", shape, source)

    shape = _detect_paths_and_secrets(joined)
    if shape is not None:
        raise trip("paths_and_secrets", shape, source)

    if kind == _KIND_TEXT and _non_public_identifiers_in_text(str(body.get("text", "")), public):
        raise trip("schema", "identifier_not_public", source)


# ---------------------------------------------------------------------------
# Running the projector in its own process (D3 is a process boundary)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectorWorkerResult:
    """What the projector worker sends back: the pid it ran in and the bytes."""

    pid: int
    payload: str


def _projector_worker_main(send, source: str, raw: Any, task: TaskIR, package: Any) -> None:
    try:
        diag = project(source, raw, task=task, package=package)
        text = serialize_for_transport(diag, task=task, package=package)
        send.send(("result", (os.getpid(), text)))
    except DiagnosticTripwire as exc:
        send.send(("tripwire", (exc.detector, exc.code, exc.source, exc.payload_sha256)))
    except Exception as exc:  # noqa: BLE001 - reported as a harness fault, never delivered
        send.send(("harness", f"{type(exc).__name__}"))
    finally:
        send.close()


def project_in_worker(
    source: DiagnosticSource | str,
    raw: Any,
    *,
    task: TaskIR,
    package: Any = None,
    timeout_s: float = 30.0,
) -> ProjectorWorkerResult:
    """Project and serialize a validator result in a killable child process.

    Return only gatechecked model-bound bytes. Worker crashes, deadlines, or unsafe
    output become typed harness faults or tripwires.
    """
    source = DiagnosticSource(source)
    methods = multiprocessing.get_all_start_methods()
    method = "spawn" if "spawn" in methods else methods[0]
    context = multiprocessing.get_context(method)
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_projector_worker_main,
        args=(send, source.value, raw, task, package),
        daemon=True,
    )
    process.start()
    send.close()
    deadline = time.monotonic() + float(timeout_s)
    message: tuple[str, Any] | None = None
    try:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if receive.poll(min(0.05, remaining)):
                try:
                    message = receive.recv()
                except EOFError:
                    message = None
                break
            if not process.is_alive():
                break
        if message is None and process.is_alive():
            process.kill()
            process.join(1.0)
            raise ToolDeadlineExceeded("projector", deadline_s=float(timeout_s))
        process.join(1.0)
    finally:
        receive.close()
        if process.is_alive():
            process.kill()
            process.join(1.0)
        process.close()
    if message is None:
        raise ToolHarnessFault("projector", code="worker_died")
    kind, body = message
    if kind == "tripwire":
        detector, code, src, sha = body
        raise DiagnosticTripwire(detector, code, source=src, payload_sha256=sha)
    if kind != "result":
        # `body` is the exception's class name, the only thing the child sends.
        raise ToolHarnessFault(
            "projector", code="projector_exception", cause_type=str(body)
        )
    pid, text = body
    return ProjectorWorkerResult(pid=int(pid), payload=str(text))

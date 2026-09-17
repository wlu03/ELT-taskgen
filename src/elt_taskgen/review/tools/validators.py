"""Define bounded-session validators for authors, implementers, and repair proposers.

Tools accept structured arguments, operate on confined trial state, and return
value-free diagnostics. Route allowlists, hidden population data, private reference
material, and provider-bearing stages remain outside the model tool surface.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from elt_taskgen import repair as repair_mod
from elt_taskgen.generation.mart_plan import _COMPILER_ONLY_DETAIL_KEYS
from elt_taskgen.models import (
    MartOpKind,
    RepairEdit,
    RepairEditOp,
    RepairPatch,
    RepairRoute,
    TaskIR,
    canonical_json,
    sha256_hex,
    task_from_json,
)
from elt_taskgen.review.session import (
    ABORT_REASON_CODES,
    ABORT_TOOL_SCHEMA,
    ForbiddenArgument,
    InProcessValidatorWorker,
    OracleCapExceeded,
    SessionLimits,
    SessionPolicy,
    ToolHarnessFault,
    ToolProtocolFault,
    abort_tool_wire,
    canonical_list_index,
    locator_argument_problem,
)
from elt_taskgen.review.tools import certify as certify_mod
from elt_taskgen.review.tools import projection as PJ
from elt_taskgen.review.tools.projection import Diagnostic, DiagnosticSource, PublicIdentifierSet
from elt_taskgen.review.tools.registry import ToolContext, ToolCost, ToolRegistry
from elt_taskgen.training.models import _CODE_RE

__all__ = [
    "ABORT_TOOL",
    "AUTHOR_ABORT_TOOL",
    "AUTHOR_CHECK_TOOL",
    "AUTHOR_HARNESS_VALIDATORS",
    "AUTHOR_PRECHECK_TOOL",
    "AUTHOR_REPLACE_TOOL",
    "AUTHOR_ROLE",
    "AUTHOR_SUBMIT_TOOL",
    "AUTHOR_TOOLS",
    "AUTHOR_TOOL_NAMES",
    "AUTHOR_WIRE_TOOL_NAMES",
    "AbortTool",
    "AuthorAbortTool",
    "AuthorSession",
    "AuthorToolContext",
    "COMPILER_ONLY_PLAN_PATHS",
    "editable_field_paths",
    "field_is_editable",
    "CONTAMINATION_GATE",
    "CONTAMINATION_LEVELS",
    "CheckProseTool",
    "ContaminationPrecheckTool",
    "MAX_PROSE_CHARS",
    "PROSE_CHECK_GREEN",
    "PROSE_CHECK_RED",
    "ReplaceProseTool",
    "SubmitProseTool",
    "author_limits",
    "author_policy",
    "author_registry",
    "author_session_enabled",
    "author_tool",
    "author_validator_worker",
    "anchor_is_visible",
    "session_defaults_for",
    "project_contamination_precheck",
    "project_prose_check",
    "ApplyEditTrialTool",
    "CertifyTool",
    "CheckCheapTool",
    "CheckScopeTool",
    "LITERAL_ROWS_PATHS",
    "MODEL_FACING_TOOL_NAMES",
    "PROPOSER_TOOLS",
    "PROPOSER_TOOL_NAMES",
    "PUBLIC_SCHEMA_PATHS",
    "ProposerSession",
    "ProposerToolContext",
    "ROLE",
    "ReadFieldTool",
    "ReadViewTool",
    "SESSION_ARTIFACTS",
    "SUBMIT_TOOL",
    "SubmitPatchTool",
    "field_is_readable",
    "proposer_policy",
    "proposer_registry",
    "proposer_tool",
    "proposer_validator_worker",
    "readable_field_paths",
    # Phase 2 (roadmap 2.a): the DEV/T implementer session tools
    "IMPLEMENTER_ROLE",
    "IMPLEMENTER_TOOLS",
    "IMPLEMENTER_TOOL_NAMES",
    "IMPLEMENTER_MODEL_FACING_TOOL_NAMES",
    "ImplementerSession",
    "ImplementerToolContext",
    "ListSchemasTool",
    "DevQueryTool",
    "DryRunSqlTool",
    "RunMartSqlDevTool",
    "SubmitSqlByMartTool",
    "ImplementerAbortTool",
    "implementer_limits",
    "implementer_policy",
    "implementer_registry",
    "implementer_tool",
    "implementer_validator_worker",
    # Phase 2 (roadmap 2.a): the EL loader session tools
    "LOADER_ROLE",
    "LOADER_TOOLS",
    "LOADER_TOOL_NAMES",
    "LOADER_WIRE_TOOL_NAMES",
    "LOADER_MODEL_FACING_TOOL_NAMES",
    "LoaderSession",
    "LoaderToolContext",
    "ReplaceLoadPlanTool",
    "CheckLoadPlanTool",
    "SubmitLoadPlanTool",
    "LoaderAbortTool",
    "loader_limits",
    "loader_policy",
    "loader_registry",
    "loader_tool",
    "loader_validator_worker",
]

ROLE = "repair_proposer"
SUBMIT_TOOL = "submit_patch"
ABORT_TOOL = "abort"

#: The one artifact a session may edit. `answer_key/reference/` (a prefix of
#: the REFERENCE allowlist for the one-shot proposer) names a denied tree
#: under the argument policy, so a session edits reference SQL at
#: `task_ir.json: reference.sql_by_mart.<mart>` only.
SESSION_ARTIFACTS: tuple[str, ...] = ("task_ir.json",)

#: The public schema paths every route may read (what the exported bundle
#: publishes: table and column names and types, keys, marts, backends,
#: relationship endpoints). No population, no reference, no attack case.
PUBLIC_SCHEMA_PATHS: tuple[str, ...] = (
    "task_id",
    "tables",
    "tables.*",
    "tables.*.name",
    "tables.*.columns",
    "tables.*.columns.*",
    "tables.*.columns.*.name",
    "tables.*.columns.*.type",
    "tables.*.columns.*.nullable",
    "tables.*.primary_key",
    "tables.*.business_key",
    "marts",
    "marts.*",
    "marts.*.name",
    "marts.*.key_columns",
    "marts.*.columns",
    "marts.*.columns.*",
    "marts.*.columns.*.name",
    "marts.*.columns.*.type",
    "backends",
    "backends.*",
    "backends.*.table",
    "backends.*.backend",
    "relationships",
    "relationships.*",
    "relationships.*.child_table",
    "relationships.*.child_columns",
    "relationships.*.parent_table",
    "relationships.*.parent_columns",
    "relationships.*.required",
)

#: Refused on EVERY route (C5; SoT T3 `read_field` row; OQ-21 not reopened).
LITERAL_ROWS_PATHS: tuple[str, ...] = (
    "populations.*.literal_rows",
    "populations.*.literal_rows.**",
)

#: Private on every route: an edit or a read naming one is a violation, not
#: a scope refusal (SoT T4 `ForbiddenArgument`).
_PRIVATE_FIELD_ROOTS: tuple[str, ...] = ("attack_cases",)

#: Free-text fields already shown in a route view are acknowledged, not served
#: again. Derive their paths from the editable allowlist.
_VIEW_TEXT_PATHS: dict[RepairRoute, tuple[str, ...]] = {
    RepairRoute.REFERENCE: ("reference.sql_by_mart.*",),
    RepairRoute.POPULATION: ("populations.*.conditions", "populations.*.conditions.*"),
}

#: Locator segments are identifiers or canonically spelled unsigned indexes.
#: Every schema, tool, and permit layer rejects aliases before spawning a worker.
_PATH_SEGMENT_RE = r"(?:[a-z][a-z0-9_]*|0|[1-9][0-9]*)"
_FIELD_PATH_RE = rf"^[a-z][a-z0-9_]*(\.{_PATH_SEGMENT_RE})*$"
_LOCATOR_RE = _FIELD_PATH_RE
_MAX_EDIT_TEXT = 20000
_MAX_RATIONALE = 2000

_NO_ARGS: Mapping[str, Any] = MappingProxyType(
    {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
)
_RPR: frozenset[str] = frozenset({ROLE})

_MISSING = object()


def _rp():
    """`review.repair_proposer`, imported lazily: it imports the projection
    module, and the registry imports this module lazily."""
    from elt_taskgen.review import repair_proposer as rp

    return rp


# ---------------------------------------------------------------------------
# Field-path matching (the `_ir_path_allowed` rule, over any pattern list)
# ---------------------------------------------------------------------------

def _path_matches(path: str, pattern: str) -> bool:
    """`*` matches one segment; a trailing `**` matches one or more."""
    parts = path.split(".")
    pat = pattern.split(".")
    if pat[-1] == "**":
        head = pat[:-1]
        return len(parts) > len(head) and all(p == "*" or p == q for p, q in zip(head, parts))
    if len(pat) != len(parts):
        return False
    return all(p == "*" or p == q for p, q in zip(pat, parts))


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(_path_matches(path, pattern) for pattern in patterns)


def editable_field_paths(route: RepairRoute | str) -> tuple[str, ...]:
    """Return the TaskIR paths editable for a repair route.

    The result is the same allowlist used by prompts and scope validation. The reference
    route permits only `reference.sql_by_mart.*`; inert metadata remains outside every
    route.
    """
    return tuple(_rp().ROUTE_IR_PATHS.get(RepairRoute(route), ()))


def field_is_editable(path: str, route: RepairRoute | str) -> bool:
    """May a session edit `path` on `route`? Decided from the LOCATOR alone,
    before any anchor is applied: a hit and a miss on a field outside the
    route answer the same code (never a substring oracle over fields the
    route may neither read nor edit)."""
    return _matches_any(path, editable_field_paths(route))


def readable_field_paths(route: RepairRoute | str) -> tuple[str, ...]:
    """`editable_field_paths(route)` minus the literal-row patterns, plus the
    public schema paths — what `read_field` may resolve on this route."""
    route_paths = tuple(
        p for p in editable_field_paths(route) if p not in LITERAL_ROWS_PATHS
    )
    return route_paths + PUBLIC_SCHEMA_PATHS


def field_is_readable(path: str, route: RepairRoute | str) -> bool:
    if _matches_any(path, LITERAL_ROWS_PATHS):
        return False
    return _matches_any(path, readable_field_paths(route))


def _view_text_paths(route: RepairRoute | str) -> tuple[str, ...]:
    route_v = RepairRoute(route)
    if route_v is RepairRoute.SPECIFICATION:
        return editable_field_paths(route_v)
    return _VIEW_TEXT_PATHS.get(route_v, ())


def anchor_is_visible(path: str, route: RepairRoute | str) -> bool:
    """May a `replace` / `delete` anchor address `path` on `route`? Only a
    field whose TEXT the route's view already shows (`_VIEW_TEXT_PATHS`:
    the specification author context and prose, the reference SQL, or the
    population conditions) — an anchor over a field `read_field` withholds
    would be a substring oracle, so its two outcomes answer ONE code
    (`patch_anchor_not_found`) without a byte being read (finding 3-0)."""
    return _matches_any(path, _view_text_paths(route))


#: Plan-op detail keys the compiler alone consumes (`mart_plan.
#: _COMPILER_ONLY_DETAIL_KEYS`): reference SQL "whatever field it travels
#: in" (export/eltbench strips them from every public export). Naming one
#: of these paths is naming the reference solution on every route.
COMPILER_ONLY_PLAN_PATHS: tuple[str, ...] = tuple(
    f"marts.*.plan.ops.*.details.{key}" for key in sorted(_COMPILER_ONLY_DETAIL_KEYS)
)


def _names_private_field(path: str, route: RepairRoute | None = None) -> bool:
    """Does `path` name PRIVATE material on `route` (SoT T4 `ForbiddenArgument`
    territory): literal rows on every route, attack cases on every route,
    the compiler-only plan details (reference SQL by another name) on every
    route, the reference solution off the REFERENCE route, the populations
    off the POPULATION route? A public field that is merely outside the
    route's allowlist is NOT private (a refusal code, never a violation)."""
    if _matches_any(path, LITERAL_ROWS_PATHS):
        return True
    if _matches_any(path, COMPILER_ONLY_PLAN_PATHS):
        return True
    head = path.split(".", 1)[0]
    if head in _PRIVATE_FIELD_ROOTS:
        return True
    if route is not None:
        if head == "reference" and route is not RepairRoute.REFERENCE:
            return True
        if head == "populations" and route is not RepairRoute.POPULATION:
            return True
    return False


def _resolve(doc: Any, path: str) -> Any:
    """Resolve a dotted field path; a list segment is an index only in its
    canonical unsigned spelling (`session.canonical_list_index`, the rule
    `apply_patch_text` and the withheld-condition guard share), so no
    alias of an index resolves anywhere."""
    node: Any = doc
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return _MISSING
            node = node[part]
        elif isinstance(node, list):
            index = canonical_list_index(part)
            if index is None or index >= len(node):
                return _MISSING
            node = node[index]
        else:
            return _MISSING
    return node


# ---------------------------------------------------------------------------
# The per-session state and its context
# ---------------------------------------------------------------------------

class ProposerSession:
    """Hold mutable harness state for one bounded repair session.

    Track the live baseline, held trial, accumulated single-artifact edit, route,
    certification epoch, receipts, and exact tool accounting without exposing private
    state.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        trial: Path,
        task: TaskIR,
        route: RepairRoute | str,
        failed_stage: str,
        view: str = "",
        certify_runners: Mapping[str, Callable[..., Any]] | None = None,
        certify_deadline_s: float = certify_mod.CERTIFY_DEADLINE_S,
        max_certify: int = certify_mod.MAX_CERTIFY_PER_SESSION,
        clock: Callable[[], float] = time.monotonic,
        certify_worker: str | None = None,
        attack_enabled: bool = False,
    ) -> None:
        route_v = RepairRoute(route)
        if route_v in (RepairRoute.RUNTIME, RepairRoute.FATAL):
            raise ValueError(f"no proposer session exists for the {route_v.value} route")
        self.workspace = Path(workspace).resolve()
        self.trial = Path(trial).resolve()
        self.task = task
        self.task_id = str(task.task_id)
        self.route = route_v
        self.failed_stage = str(failed_stage)
        self.view = str(view)
        live_ir = self.workspace / "tasks" / self.task_id / "task_ir.json"
        if not live_ir.is_file():
            raise ValueError(f"task {self.task_id!r} has no task_ir.json in the live workspace")
        if not (self.trial / "tasks" / self.task_id / "task_ir.json").is_file():
            raise ValueError(f"task {self.task_id!r} has no task_ir.json in the held trial")
        #: The LIVE snapshot, taken once at INIT (certify addendum §3.1
        #: "validate_scope baseline rule"). `certify` never changes it.
        self.before: dict[str, str] = repair_mod.snapshot(self.workspace, self.task_id)
        self.before_ir: str = live_ir.read_text(encoding="utf-8")
        self.artifact: str | None = None
        self.edits: list[RepairEdit] = []
        self.rationales: list[str] = []
        self.state_epoch = 0
        self.certified_epoch = 0
        self.certify_calls = 0
        self.certify_refusals = 0
        self.oracle_bits_used = 0
        self.certify_receipts: list[certify_mod.CertifyReceipt] = []
        self.certify_runners = certify_runners
        self.certify_deadline_s = float(certify_deadline_s)
        self.max_certify = int(max_certify)
        #: Phase 3 (`repair.certify.attack_enabled`): may this session's
        #: certify hold the `attack` member? Decides the members, the bits
        #: per executed call and the view-changed refusal (`review/tools/
        #: certify.py` module docstring).
        self.attack_enabled = bool(attack_enabled)
        self.certify_oracle_bits = (
            certify_mod.CERTIFY_ATTACK_ORACLE_BITS if self.attack_enabled else certify_mod.CERTIFY_ORACLE_BITS
        )
        self._review_memo: tuple[str, dict | None] | None = None
        self.clock = clock
        #: The certify worker shape (`certify.resolve_worker_kind`): None
        #: selects the spawned process for the production runner dict and
        #: the in-process thread for an injected one.
        self.certify_worker = certify_mod.resolve_worker_kind(certify_worker, certify_runners)
        self.tool_calls = 0
        self.submitted = False
        self.abort_reason: str | None = None
        self.public = PublicIdentifierSet(task)
        #: Withheld population-condition slots are fixed from the live task.
        #: Reads and anchored edits return one code without inspecting text,
        #: preventing a substring oracle.
        self.withheld_conditions: frozenset[tuple[int, int]] = (
            _rp().withheld_population_conditions(task)
            if route_v is RepairRoute.POPULATION
            else frozenset()
        )

    def condition_is_withheld(self, path: str) -> bool:
        """Does `path` (`populations.<i>.conditions.<j>`, or the whole
        `populations.<i>.conditions` list) RESOLVE to a withheld condition?
        Decided on the resolved `(index, slot)` pair
        (`repair_proposer.resolved_condition_slot`, the resolvers' own
        canonical-index rule), so the guard and `read_field` /
        `apply_patch_text` can never disagree about the slot a path names."""
        return _rp().condition_path_is_withheld(path, self.withheld_conditions)

    @classmethod
    @contextmanager
    def open(
        cls,
        workspace: Path,
        task: TaskIR,
        route: RepairRoute | str,
        failed_stage: str,
        *,
        failure: str = "",
        **options: Any,
    ) -> Iterator["ProposerSession"]:
        """Hold ONE `trial_workspace(workspace)` open as the editing trial for
        the session's lifetime; `failure` (when given) renders the route view
        through `view_for_route` (value-free by construction)."""
        rp = _rp()
        route_v = RepairRoute(route)
        view = rp.view_for_route(task, route_v, failure) if failure else ""
        with rp.trial_workspace(Path(workspace), task_id=task.task_id) as trial:
            yield cls(
                workspace=workspace,
                trial=trial,
                task=task,
                route=route_v,
                failed_stage=failed_stage,
                view=view,
                **options,
            )

    # -- what the harness reads --------------------------------------------

    def trial_text(self, artifact: str = "task_ir.json") -> str:
        return (self.trial / "tasks" / self.task_id / artifact).read_text(encoding="utf-8")

    def trial_task(self) -> TaskIR:
        """The held trial's IR, re-validated (raises `ValidationError`)."""
        return task_from_json(self.trial_text())

    def surface_fingerprint(self) -> str:
        """sha256 of the held trial's task-tree snapshot (R4: a write that
        leaves it unchanged is a no-op)."""
        return sha256_hex(canonical_json(repair_mod.snapshot(self.trial, self.task_id)))

    def accumulated_patch(self, *, rationale: str | None = None) -> RepairPatch:
        """The ONE `RepairPatch` this session built (edits in order, one
        artifact), for `attempt_patch`. Raises when nothing was applied."""
        if not self.edits or self.artifact is None:
            raise ValueError("the session applied no edit; there is no patch to submit")
        text = rationale if rationale else "; ".join(dict.fromkeys(self.rationales))
        return RepairPatch(
            route=self.route,
            artifact=self.artifact,
            edits=tuple(self.edits),
            rationale=text or "session patch",
            proposer_role=ROLE,
        )

    def current_draft(self) -> dict | None:
        """The accumulated patch as JSON data (the auto-submittable draft of
        a limit stop), or None before the first applied edit."""
        if not self.edits:
            return None
        return self.accumulated_patch().model_dump(mode="json")

    # -- the Phase 3 `attack` member (review/tools/certify.py) ----------------

    def literal_rows_moved(self) -> bool:
        """Did the accumulated edit touch `populations.*.literal_rows` (as
        specified) or a materialized counterfactual file — the condition
        under which `trial_phase` adds the proof stages and runs the guard
        (`repair_proposer._moved_counterfactual_rows`, the same test)?"""
        rp = _rp()
        after = repair_mod.snapshot(self.trial, self.task_id)
        diff = repair_mod.diff_snapshots(self.before, after)
        return bool(rp._moved_counterfactual_rows(self.before_ir, self.trial, self.task_id, diff))

    def population_moved(self) -> bool:
        """Did the accumulated edit touch ANY population material
        (`repair_proposer._moved_population_material`: conditions, scale,
        literal rows, the list, a materialized population file)?"""
        rp = _rp()
        after = repair_mod.snapshot(self.trial, self.task_id)
        diff = repair_mod.diff_snapshots(self.before, after)
        return self.literal_rows_moved() or bool(
            rp._moved_population_material(self.before_ir, self.trial, self.task_id, diff)
        )

    def guard_armed(self) -> bool:
        """`trial_phase`'s exact predicate (`repair_proposer.
        discrimination_guard_armed`): the proof stages join the set and the
        `discrimination_weakened` bit is measured when literal rows moved,
        or when other population material moved over a measured live
        baseline (Phase 3 review finding 2-1)."""
        rp = _rp()
        literal = self.literal_rows_moved()
        if literal:
            return True
        if not self.population_moved():
            return False
        baseline = certify_mod.discrimination_baseline(self.workspace, self.task)
        return bool(
            rp.discrimination_guard_armed(
                literal_rows_moved=False, population_moved=True, matrix_before=baseline["matrix"]
            )
        )

    def live_review_payload(self) -> dict | None:
        """The live ledger's `review` PASS payload at the live hash
        (`certify.memo_served_review`), read once per live hash."""
        live_hash = self.task.content_hash()
        if self._review_memo is None or self._review_memo[0] != live_hash:
            self._review_memo = (live_hash, certify_mod.memo_served_review(self.workspace, self.task))
        return self._review_memo[1]

    def attack_member_available(self) -> bool:
        """May `attack` join this session's stage set: the flag, an
        `ATTACK_ROUTES` route and a live review PASS at the live hash the
        member can be memo-served from (never faked)."""
        return (
            self.attack_enabled
            and self.route in certify_mod.ATTACK_ROUTES
            and self.live_review_payload() is not None
        )

    def critic_views_changed(self) -> tuple[str, ...]:
        """The critic seats whose view moved between the live task and the
        held trial's IR (`certify.critic_views_changed`); a trial whose IR
        no longer loads counts every seat as moved (fail closed)."""
        try:
            trial_task = self.trial_task()
        except (ValidationError, ValueError):
            from elt_taskgen.review.council import CRITIC_ROLES

            return tuple(role.value for role in CRITIC_ROLES)
        return certify_mod.critic_views_changed(self.task, trial_task)

    def attack_member_admitted(self) -> bool:
        """Is `attack` a member of this session's NEXT certify: available
        (`attack_member_available`) AND no critic view moved between the
        live task and the held trial, so its review can be memo-served
        (certify addendum §3.1 "Refusals" (3)). A `conditions` edit moves
        the adversary's view (F3b) and drops the MEMBER — the Phase 1
        members still run and the call answers `model_stages_deferred=True`
        (Phase 3 review finding 1-1) — a literal-rows move drops nothing."""
        return self.attack_member_available() and not self.critic_views_changed()

    def certify_stages(self) -> tuple[str, ...]:
        """The stages this session's certify runs now: the Phase 1 members,
        plus `attack` (and the literal-rows proof stages) when the member is
        ADMITTED (`attack_member_admitted`)."""
        attack = self.attack_member_admitted()
        moved = self.guard_armed() if attack else False
        stages = certify_mod.provider_free_stages(
            self.route, self.failed_stage, attack_enabled=attack, literal_rows_moved=moved
        )
        return stages

    def certify_deferred(self) -> bool:
        """Is a member of the route's rerun set left to the submit-time
        certifier — a model-bearing stage always, and `attack` whenever it
        is not admitted (the flag off, no review PASS at the live hash, or a
        critic view moved)?"""
        attack = self.attack_member_admitted()
        moved = self.guard_armed() if attack else False
        return certify_mod.model_stages_deferred(
            self.route, self.failed_stage, attack_enabled=attack, literal_rows_moved=moved
        )

    def attack_member_dropped_for_view(self) -> bool:
        """Would `attack` be a member but for a moved critic view? (What the
        call-level refusal `certify_refused_review_view_changed` names when
        NO other member remains.)"""
        return self.attack_member_available() and bool(self.critic_views_changed())

    def attack_context(self) -> dict | None:
        """The `attack` member's context for the worker
        (`certify.attack_member_context`) when the member is in this
        session's stage set, else None."""
        if certify_mod.CERTIFY_ATTACK_STAGE not in self.certify_stages():
            return None
        return certify_mod.attack_member_context(
            self.workspace,
            self.task,
            literal_rows_moved=self.literal_rows_moved(),
            population_moved=self.population_moved(),
        )

    def touched_stages(self) -> tuple[str, ...]:
        """Return provider-free stages whose measurements read the accumulated edit.

        Use route and validated edited paths so resource exhaustion is charged to the
        model only when its bytes feed the affected stage.
        """
        return certify_mod.edit_touched_stages(self.route)

    def context(self, *, package: Any = None) -> "ProposerToolContext":
        return ProposerToolContext(
            root=self.trial,
            task_id=self.task_id,
            role=ROLE,
            task=self.task,
            route=self.route,
            package=package,
            session=self,
        )


@dataclass(frozen=True)
class ProposerToolContext(ToolContext):
    """A `ToolContext` rooted at the held trial, carrying the session state
    (non-identity: never compared, never serialized)."""

    session: Any = field(default=None, compare=False, repr=False)


def _session_of(ctx: Any, tool: str) -> ProposerSession:
    session = getattr(ctx, "session", None)
    if not isinstance(session, ProposerSession):
        raise ToolHarnessFault(tool, code="no_proposer_session")
    return session


def _dedupe(names: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(n for n in names if n))


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

class ReadViewTool:
    """`read_view`: idempotent; confirms the route view (the first user
    message) is the session's whole view and was not truncated."""

    name = "read_view"
    description = (
        "Confirm the route-scoped view you were given as the first message: it is "
        "the whole view for this session and does not change. Returns the route "
        "and truncated=false; the view text itself is the first message."
    )
    input_schema = _NO_ARGS
    cost = ToolCost(oracle_bits=0, wall_s=10.0, idempotent_read=True)
    permitted_roles = _RPR

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _session_of(ctx, self.name)
        session.tool_calls += 1
        return Diagnostic(
            source=DiagnosticSource.TRIAL,
            ok=True,
            code="view_served",
            subject=session.route.value,
            flags={"truncated": False},
        )


class ReadFieldTool:
    """`read_field(field)`: a public IR / schema field of the HELD trial's
    IR, projected value-free."""

    name = "read_field"
    description = (
        "Read one field of task_ir.json by dotted path (e.g. tables.0.columns, "
        "marts.0.key_columns, tables.1.columns.2.type). Allowed: this route's "
        "editable fields and the public schema (tables, columns, types, keys, "
        "marts, backends, relationships). Identifiers come back as subject/names, "
        "booleans as flags; free text you already have in the view is acknowledged, "
        "other values are withheld. populations.*.literal_rows is never readable."
    )
    input_schema = MappingProxyType(
        {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "pattern": _FIELD_PATH_RE,
                }
            },
            "required": ["field"],
            "additionalProperties": False,
        }
    )
    cost = ToolCost(oracle_bits=0, wall_s=10.0, idempotent_read=True)
    permitted_roles = _RPR

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _session_of(ctx, self.name)
        session.tool_calls += 1
        path = str(args.get("field", ""))
        if locator_argument_problem(path):
            # An aliasing list index (`-1`, `01`, ` 1`, `1_0`): the runner's
            # PERMIT rule answers it before any worker; the tool answers the
            # same violation on a direct dispatch (SoT T4).
            raise ForbiddenArgument(tool=self.name, detail=locator_argument_problem(path))
        if not path or re.fullmatch(_FIELD_PATH_RE, path) is None:
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="field")
        if _matches_any(path, LITERAL_ROWS_PATHS):
            raise ForbiddenArgument(tool=self.name, detail="literal_rows")
        if _names_private_field(path, session.route):
            raise ForbiddenArgument(tool=self.name, detail="private_field")
        if not field_is_readable(path, session.route):
            return Diagnostic(
                source=DiagnosticSource.FIELD, ok=False, code="field_outside_allowlist"
            )
        try:
            doc = json.loads(session.trial_text())
        except (json.JSONDecodeError, ValueError):
            return Diagnostic(source=DiagnosticSource.FIELD, ok=False, code="field_not_found")
        value = _resolve(doc, path)
        if value is _MISSING:
            return Diagnostic(source=DiagnosticSource.FIELD, ok=False, code="field_not_found")
        if session.condition_is_withheld(path):
            # A hidden population's condition the view withheld (private
            # material): never acknowledged as text in the view, never
            # re-served — the same code the view's label already implies.
            return Diagnostic(source=DiagnosticSource.FIELD, ok=False, code="field_withheld")
        in_view = _matches_any(path, _view_text_paths(session.route))
        return _project_field(value, public=session.public, in_view=in_view)


def _label(node: Mapping[str, Any], public: PublicIdentifierSet) -> str:
    for key in ("name", "table", "child_table"):
        value = node.get(key)
        if isinstance(value, str) and value in public:
            return value
    return ""


def _project_field(value: Any, *, public: PublicIdentifierSet, in_view: bool) -> Diagnostic:
    """A resolved field value -> `Diagnostic{source: field}` carrying public
    identifiers, booleans, or an acknowledgement / withholding — never a
    number, never free text."""
    source = DiagnosticSource.FIELD
    if isinstance(value, bool):
        return Diagnostic(source=source, ok=True, code="field_value", flags={"value": value})
    if isinstance(value, str):
        if value in public:
            return Diagnostic(source=source, ok=True, code="field_value", subject=value)
        if in_view:
            return Diagnostic(source=source, ok=True, code="field_text_in_view")
        return Diagnostic(source=source, ok=False, code="field_withheld")
    if isinstance(value, (int, float)) or value is None:
        return Diagnostic(source=source, ok=False, code="field_withheld")
    if isinstance(value, list):
        names = [v for v in value if isinstance(v, str) and v in public]
        names.extend(_label(v, public) for v in value if isinstance(v, Mapping))
        names = [n for n in names if n]
        if not names and value:
            code = "field_text_in_view" if in_view and all(isinstance(v, str) for v in value) else "field_withheld"
            return Diagnostic(source=source, ok=code == "field_text_in_view", code=code)
        return Diagnostic(source=source, ok=True, code="field_value", names=_dedupe(names))
    if isinstance(value, Mapping):
        subject = _label(value, public)
        names: list[str] = []
        flags: dict[str, bool] = {}
        for key, item in value.items():
            if isinstance(item, bool):
                if _CODE_RE.fullmatch(str(key)):
                    flags[str(key)] = item
            elif isinstance(item, str):
                if item in public and item != subject:
                    names.append(item)
            elif isinstance(item, list):
                names.extend(v for v in item if isinstance(v, str) and v in public)
                names.extend(_label(v, public) for v in item if isinstance(v, Mapping))
        return Diagnostic(
            source=source,
            ok=True,
            code="field_value",
            subject=subject,
            names=_dedupe([n for n in names if n and n != subject]),
            flags=flags,
        )
    return Diagnostic(source=source, ok=False, code="field_withheld")



#: Match malformed tool-call tails captured inside a string argument; the
#: preceding text is the intended value.
_LEAKED_PARAMETER_TAIL_RE = re.compile(
    r"</(?:antml)?[:\uff1a\u0903\u02d0]?parameter>\s*<parameter\b.*\Z", re.DOTALL
)

#: Recover a following parameter shifted into the previous argument by a
#: malformed closing tag.
_LEAKED_PARAMETER_CARRY_RE = re.compile(
    r"</(?:antml)?[:\uff1a\u0903\u02d0]?parameter>\s*<parameter\s+name=\"(?P<name>[a-z_]+)\">"
    r"(?P<text>.*)\Z",
    re.DOTALL,
)
#: A leaked closing tag with nothing after it: the end of a carried value.
_LEAKED_CLOSING_TAG_RE = re.compile(
    r"\s*</(?:antml)?[:\uff1a\u0903\u02d0]?parameter>\s*\Z"
)
_RECOVERABLE_EDIT_FIELDS = ("old", "new", "rationale")


def _strip_leaked_parameter_tail(value: str) -> str:
    """The argument value the model meant: everything before a leaked
    `</...parameter><parameter ...>` tail, or the value unchanged."""
    match = _LEAKED_PARAMETER_TAIL_RE.search(value)
    return value[: match.start()] if match else value


def _recover_leaked_parameters(args: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct text arguments when tool-call markup leaked between fields.

    Recover only the model's named parameter boundaries and reject ambiguous or
    unsupported markup rather than writing it into task content.
    """
    recovered = dict(args)
    for _ in range(len(_RECOVERABLE_EDIT_FIELDS) + 1):
        carried: dict[str, str] = {}
        for field in _RECOVERABLE_EDIT_FIELDS:
            value = recovered.get(field)
            if not isinstance(value, str):
                continue
            match = _LEAKED_PARAMETER_CARRY_RE.search(value)
            if match is None:
                stripped = _strip_leaked_parameter_tail(value)
                stripped = _LEAKED_CLOSING_TAG_RE.sub("", stripped)
                if stripped != value:
                    recovered[field] = stripped
                continue
            recovered[field] = value[: match.start()]
            carried[match.group("name")] = match.group("text")
        if not carried:
            break
        for name, text in carried.items():
            current = recovered.get(name)
            shifted = (
                not isinstance(current, str)
                or not current.strip()
                or _LEAKED_TOOL_SYNTAX_RE.search(current) is not None
            )
            if shifted and name in _RECOVERABLE_EDIT_FIELDS:
                recovered[name] = text
    return recovered


_LEAKED_TOOL_SYNTAX_RE = re.compile(
    r"</?antml|<parameter\b|</parameter>|<invoke\b|</invoke>|<function_calls>|"
    r"</?[a-z]*[:\uff1a\u0903\u02d0]parameter\b"
)

class ApplyEditTrialTool:
    """`apply_edit_trial`: one edit of the one artifact, on the held trial,
    `validate_scope` against the live snapshot on EVERY write."""

    name = "apply_edit_trial"
    description = (
        "Apply ONE edit to the session's single artifact on your trial copy. "
        "locator is the dotted path of the field in task_ir.json to edit "
        "(e.g. solver_prompt, reference.sql_by_mart.<mart>). replace needs the exact "
        "existing text in old (occurring exactly once) and a different new; insert "
        "leaves old empty and appends new; delete leaves new empty; replace_json "
        "uses bounded strict canonical JSON text in old/new to replace one exactly "
        "matching typed value. Every write is "
        "scope-checked against the live task: anything outside this route is refused "
        "and rolled back. Nothing is committed here."
    )
    input_schema = MappingProxyType(
        {
            "type": "object",
            "properties": {
                "artifact": {"type": "string", "enum": list(SESSION_ARTIFACTS)},
                "op": {"type": "string", "enum": [op.value for op in RepairEditOp]},
                "locator": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "pattern": _LOCATOR_RE,
                },
                "old": {"type": "string", "maxLength": _MAX_EDIT_TEXT},
                "new": {"type": "string", "maxLength": _MAX_EDIT_TEXT},
                "rationale": {"type": "string", "minLength": 1, "maxLength": _MAX_RATIONALE},
            },
            "required": ["artifact", "op", "locator", "old", "new", "rationale"],
            "additionalProperties": False,
        }
    )
    cost = ToolCost(oracle_bits=0, wall_s=10.0, per_session=6)
    permitted_roles = _RPR
    surface_write = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _session_of(ctx, self.name)
        session.tool_calls += 1
        rp = _rp()
        artifact = str(args.get("artifact", ""))
        if artifact not in SESSION_ARTIFACTS:
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="artifact")
        if session.artifact is not None and artifact != session.artifact:
            # One artifact per session (S8 §3.1 (3); RC §3.2).
            raise ToolProtocolFault(
                "invalid_arguments", tool=self.name, detail="second_artifact_in_one_session"
            )
        locator = str(args.get("locator", ""))
        if locator_argument_problem(locator):
            # An aliasing list index is a probe of the withheld-slot guard,
            # never a slip (the same violation the runner answers at PERMIT).
            raise ForbiddenArgument(tool=self.name, detail=locator_argument_problem(locator))
        if not locator or re.fullmatch(_LOCATOR_RE, locator) is None:
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="locator")
        args = dict(args)
        recovered = _recover_leaked_parameters(args)
        tail_stripped = any(
            recovered.get(field) != args.get(field) for field in _RECOVERABLE_EDIT_FIELDS
        )
        args = recovered
        if (
            tail_stripped
            and str(args.get("op", "")) == RepairEditOp.REPLACE.value
            and str(args.get("old", "")).strip()
            and not str(args.get("new", "")).strip()
        ):
            # The model meant to remove `old`: it closed an empty `new` and
            # went on to its rationale, and the broken framing put the
            # rationale inside `new`.
            args["op"] = RepairEditOp.DELETE.value
            args["new"] = ""
        markup_only_new = bool(
            _LEAKED_TOOL_SYNTAX_RE.search(str(args.get("new", "") or ""))
            and not _LEAKED_TOOL_SYNTAX_RE.sub("", str(args.get("new", ""))).strip()
        )
        if (
            markup_only_new
            and str(args.get("op", "")) == RepairEditOp.REPLACE.value
            and str(args.get("old", "")).strip()
        ):
            # Treat markup-only replacement text as deletion of the matched anchor.
            args["op"] = RepairEditOp.DELETE.value
        if (
            str(args.get("op", "")) == RepairEditOp.DELETE.value
            and markup_only_new
        ):
            # For deletes, discard an empty argument's stray parameter-closing
            # markup; it is framing noise and must never be written.
            args["new"] = ""
        for field in ("old", "new"):
            if _LEAKED_TOOL_SYNTAX_RE.search(str(args.get(field, "") or "")):
                # Tool-call markup is malformed input, not task content.
                raise ToolProtocolFault(
                    "invalid_arguments", tool=self.name, detail=f"{field}_carries_tool_call_syntax"
                )
        if _names_private_field(locator, session.route):
            raise ForbiddenArgument(tool=self.name, detail="private_field")
        if not field_is_editable(locator, session.route):
            # Decide scope from the locator before reading anchor bytes, so
            # forbidden fields cannot become substring oracles. Return the same
            # route-mismatch or allowlist code as accumulated-patch validation.
            observed = rp._route_from_ir_fields([locator, *(e.locator for e in session.edits)])
            if observed is not session.route:
                code = PJ.RejectionCode.SCOPE_ROUTE_MISMATCH.value
                why = f"routes as {observed.value!r}"
            else:
                code = PJ.RejectionCode.SCOPE_FIELD_OUTSIDE_ALLOWLIST.value
                why = "is outside this route's editable fields"
            return rp.project_rejection(
                rp.ScopeViolation(
                    f"locator {locator!r} {why} (route {session.route.value!r})",
                    code=code,
                )
            )
        try:
            edit = RepairEdit(
                op=RepairEditOp(str(args.get("op", ""))),
                locator=locator,
                old=str(args.get("old", "")),
                new=str(args.get("new", "")),
            )
        except (ValidationError, ValueError):
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="edit") from None
        if edit.op is not RepairEditOp.INSERT and (
            not anchor_is_visible(locator, session.route) or session.condition_is_withheld(locator)
        ):
            # Refuse anchored replace/delete on withheld text before reading it,
            # using one code for hits and misses. Unanchored insert remains open.
            return rp.project_rejection(
                rp.PatchApplicationError(
                    f"locator {locator!r}: a replace or delete anchor is refused on a "
                    "field whose text this route's view does not show (use insert)",
                    code=PJ.RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
                )
            )
        rationale = " ".join(str(args.get("rationale", "")).split())[:_MAX_RATIONALE] or "session edit"
        single = RepairPatch(
            route=session.route, artifact=artifact, edits=(edit,), rationale=rationale,
            proposer_role=ROLE,
        )
        try:
            target = rp._resolve_target(session.trial, session.task_id, single)
        except rp.PatchRejected as exc:
            return rp.project_rejection(exc)
        text_before = target.read_text(encoding="utf-8")
        try:
            updated = rp.apply_patch_text(text_before, single)
        except rp.PatchRejected as exc:
            return rp.project_rejection(exc)
        if updated == text_before:
            return rp.project_rejection(
                rp.PatchApplicationError("no-op edit", code=PJ.RejectionCode.PATCH_NOOP.value)
            )
        target.write_text(updated, encoding="utf-8")
        accumulated = RepairPatch(
            route=session.route,
            artifact=artifact,
            edits=(*session.edits, edit),
            rationale=rationale,
            proposer_role=ROLE,
        )
        after = repair_mod.snapshot(session.trial, session.task_id)
        try:
            rp.validate_scope(
                session.trial, session.task_id, accumulated, session.before, after,
                before_ir=session.before_ir,
            )
        except rp.ScopeViolation as exc:
            # The held trial keeps only VALIDATED bytes: roll this edit back.
            target.write_text(text_before, encoding="utf-8")
            return rp.project_rejection(exc)
        session.artifact = artifact
        session.edits.append(edit)
        session.rationales.append(rationale)
        session.state_epoch += 1
        return Diagnostic(
            source=DiagnosticSource.TRIAL,
            ok=True,
            code="applied",
            subject=session.route.value,
            flags={"epoch_bumped": True},
        )


class CheckScopeTool:
    """`check_scope`: `validate_scope` dry run over the accumulated edit."""

    name = "check_scope"
    description = (
        "Dry-run the scope check of everything you have applied so far against the "
        "live task: the route the diff routes as must equal this session's route and "
        "every moved field must be inside its allowlist. Returns scope_ok or the "
        "rejection code."
    )
    input_schema = _NO_ARGS
    cost = ToolCost(oracle_bits=0, wall_s=10.0, per_session=4)
    permitted_roles = _RPR
    validator = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _session_of(ctx, self.name)
        session.tool_calls += 1
        rp = _rp()
        if not session.edits:
            return rp.project_rejection(
                rp.ScopeViolation("no edit applied yet", code=PJ.RejectionCode.PATCH_NOOP.value)
            )
        after = repair_mod.snapshot(session.trial, session.task_id)
        try:
            rp.validate_scope(
                session.trial, session.task_id, session.accumulated_patch(), session.before,
                after, before_ir=session.before_ir,
            )
        except rp.ScopeViolation as exc:
            return rp.project_rejection(exc)
        return Diagnostic(
            source=DiagnosticSource.TRIAL, ok=True, code="scope_ok", subject=session.route.value
        )


class CheckCheapTool:
    """`check_cheap`: the cheap TaskIR gates on the held trial, codes only."""

    name = "check_cheap"
    description = (
        "Run the cheap gates on your trial copy: the IR must load, the prose must "
        "represent every mart item declaratively (check_prose), every referenced "
        "object must exist (check_structure) and, on the population route, the "
        "populations must cover the task. Codes and booleans only, never the text."
    )
    input_schema = _NO_ARGS
    cost = ToolCost(oracle_bits=0, wall_s=10.0, per_session=4)
    permitted_roles = _RPR
    validator = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        from elt_taskgen.review.prose_fidelity import check_prose_fidelity
        from elt_taskgen.verification.structural_completeness import check_structural_completeness

        session = _session_of(ctx, self.name)
        session.tool_calls += 1
        try:
            task = session.trial_task()
        except (ValidationError, ValueError):
            return Diagnostic(
                source=DiagnosticSource.CHEAP, ok=False, code="ir_invalid", flags={"ir_ok": False}
            )
        prose = [str(p) for p in check_prose_fidelity(task)]
        structure = [str(p) for p in check_structural_completeness(task)]
        # The POPULATION route's member (roadmap Phase 3, Table 7; SoT T3
        # `check_cheap` row): `check_population_cheap`, codes only.
        population: Diagnostic | None = None
        if session.route is RepairRoute.POPULATION:
            population = _rp().check_population_cheap(session.trial, task)
        flags: dict[str, bool] = {
            "ir_ok": True,
            "prose_ok": not prose,
            "structure_ok": not structure,
        }
        if population is not None:
            flags["population_ok"] = bool(population.ok)
            flags.update(population.flags)
        # Give the proposer the same item-level codes-only prose projection as
        # the author. It carries public names, never source text or numbers.
        names: list[str] = []
        if prose:
            projected = project_prose_check(prose, task=task)
            flags.update({key: value for key, value in projected.flags.items() if key != "prose_ok"})
            names = [name for name in projected.names if name in session.public or name in _OP_KINDS]
        if prose:
            code = "prose_problems"
        elif structure:
            code = "structure_problems"
        elif population is not None and not population.ok:
            code = population.code
            names.extend(n for n in population.names if n not in names)
        else:
            code = "cheap_green"
        return Diagnostic(
            source=DiagnosticSource.CHEAP,
            ok=code == "cheap_green",
            code=code,
            flags=flags,
            names=_dedupe(names),
        )


#: The Phase 3 sentence the flagged `certify` adds to its description.
_CERTIFY_ATTACK_DESCRIPTION = (
    " On the population and reference routes the attack stage is a member too "
    "(6 oracle bits per run; the attack member is left out, and "
    "model_stages_deferred reports it, when your edit changed a critic's view, "
    "because its review could not be served from memory — the other members "
    "still run), and the discrimination_weakened flag reports whether the "
    "re-measured attack matrix is weaker than the live one — a patch the "
    "certifier would reject."
)


class CertifyTool:
    """`certify`: the provider-free stages on a disposable copy of the held
    trial (`review/tools/certify.py`). The default instance is the Phase 1
    tool (2 bits, 300 s, generate + reference); `CertifyTool(attack_enabled=
    True)` is the flagged instance of `repair.certify.attack_enabled` (6
    bits, 1 200 s, the view-changed refusal declared) — which member set a
    call actually runs is the SESSION's `attack_enabled`, so the two never
    disagree inside one session."""

    name = certify_mod.TOOL_NAME
    _DESCRIPTION = (
        "Run the provider-free stages of this route's rerun set (generate and "
        "reference) on a throwaway copy of your trial. At most 2 per session; costs 2 "
        "oracle bits when it runs. Refused at no cost when the route has no such "
        "stage (every specification failure) or when nothing changed since your last "
        "certify. Returns green, or the first red stage; model-bearing stages are "
        "left to the certifier at submit. certify_refused_resource_budget means a "
        "stage your edit feeds exhausted the run's memory or time budget: that run "
        "was spent, so change the edit before certifying again."
    )
    description = _DESCRIPTION
    input_schema = _NO_ARGS
    #: 2 bits per EXECUTED call; `per_session = max_certify` (SoT T1 RPR:
    #: `certify` <= 2), so the third request is the runner's PERMIT-time
    #: `per_tool_cap` refusal — no worker spawned, no bit charged; and the
    #: two no-cost refusal codes `permit` answers with before dispatch, so
    #: the runner never charges oracle bits for a refused call.
    cost = ToolCost(
        oracle_bits=certify_mod.CERTIFY_ORACLE_BITS,
        wall_s=certify_mod.CERTIFY_DEADLINE_S,
        per_session=certify_mod.MAX_CERTIFY_PER_SESSION,
        no_cost_codes=certify_mod.NO_COST_REFUSAL_CODES,
    )
    permitted_roles = _RPR
    validator = True

    def __init__(self, *, attack_enabled: bool = False) -> None:
        self.attack_enabled = bool(attack_enabled)
        if self.attack_enabled:
            # The flagged declaration (certify addendum §3.1: 6 bits per
            # call, 1 200 s, refusal (3) declared at no cost).
            self.description = self._DESCRIPTION + _CERTIFY_ATTACK_DESCRIPTION
            self.cost = ToolCost(
                oracle_bits=certify_mod.CERTIFY_ATTACK_ORACLE_BITS,
                wall_s=certify_mod.CERTIFY_ATTACK_DEADLINE_S,
                per_session=certify_mod.MAX_CERTIFY_PER_SESSION,
                no_cost_codes=certify_mod.ATTACK_NO_COST_REFUSAL_CODES,
            )

    @staticmethod
    def stages_for(session: ProposerSession) -> tuple[str, ...]:
        return session.certify_stages()

    @staticmethod
    def deferred_for(session: ProposerSession) -> bool:
        return session.certify_deferred()

    def refusal_for(self, session: ProposerSession) -> str:
        """Return a no-cost certification refusal code, or an empty string.

        Refuse empty provider-free sets, inadmissible attack membership, or
        unchanged/unsubmitted edit state before worker dispatch and oracle-bit charging.
        """
        stages = self.stages_for(session)
        if not stages:
            if self.attack_enabled and session.attack_member_dropped_for_view():
                return certify_mod.CODE_REFUSED_REVIEW_VIEW_CHANGED
            return certify_mod.CODE_REFUSED_NO_PROVIDER_FREE_STAGE
        if session.state_epoch == session.certified_epoch:
            return certify_mod.CODE_REFUSED_UNCHANGED_TRIAL
        return ""

    def permit(self, ctx: Any, args: Mapping[str, Any]) -> str:
        """PERMIT hook (`registry.permit_refusal`): the no-cost refusal code
        — one of `cost.no_cost_codes` — a dispatcher answers with BEFORE
        spawning a worker or charging oracle bits, or '' to dispatch. The
        per-session ceiling itself is `cost.per_session`, the runner's own
        PERMIT-time `per_tool_cap` refusal."""
        return self.refusal_for(_session_of(ctx, self.name))

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _session_of(ctx, self.name)
        session.tool_calls += 1
        stages = self.stages_for(session)
        deferred = self.deferred_for(session) if stages else True
        refusal = self.refusal_for(session)
        if refusal:
            # Reached only by a dispatcher that skipped `permit`: the same
            # no-cost answer, so the two paths never disagree.
            session.certify_refusals += 1
            if refusal in certify_mod.ATTACK_NO_COST_REFUSAL_CODES - certify_mod.NO_COST_REFUSAL_CODES:
                return PJ.project_certify_attack(refusal, model_stages_deferred=deferred)
            return PJ.project_certify(refusal, model_stages_deferred=deferred)
        if session.certify_calls >= session.max_certify:
            # The fail-closed backstop behind `cost.per_session` (the runner
            # refuses the third request at PERMIT before this runs) and the
            # binding cap when the role declares a `max_certify` below it.
            raise OracleCapExceeded(tool=self.name, detail="max_certify")
        # The `attack` member's context (the memo-served review, the live
        # baseline), read off the LIVE workspace before anything runs.
        attack = session.attack_context() if certify_mod.CERTIFY_ATTACK_STAGE in stages else None
        # Charged when the worker is SPAWNED, not when it returns: a call
        # the deadline cuts short still executed a stage (it is a paid
        # call, never a free probe).
        session.certify_calls += 1
        session.oracle_bits_used += session.certify_oracle_bits
        session.certified_epoch = session.state_epoch
        # `touched_stages`: a resource-cap exhaustion on a stage the edit
        # feeds is the PAID `certify_refused_resource_budget` (a receipt,
        # the bits above spent), never a harness fault the model could
        # trigger for free; on any other stage it halts as before.
        receipt = certify_mod.certify_disposable_copy(
            session.trial,
            session.task_id,
            stages,
            runners=session.certify_runners,
            deadline_s=session.certify_deadline_s,
            clock=session.clock,
            model_stages_deferred=deferred,
            worker=session.certify_worker,
            attack=attack,
            touched_stages=session.touched_stages(),
        )
        session.certify_receipts.append(receipt)
        return receipt.diagnostic


class SubmitPatchTool:
    """`submit_patch` (terminal, no arguments): marks the accumulated patch
    for the harness. Commits nothing."""

    name = SUBMIT_TOOL
    description = (
        "End the session and hand everything you applied, as one patch, to the "
        "certifier. No arguments. The certifier re-validates on a fresh trial and "
        "commits only on green; you will not see its result in this session."
    )
    input_schema = _NO_ARGS
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _RPR
    terminal = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _session_of(ctx, self.name)
        session.tool_calls += 1
        rp = _rp()
        if not session.edits:
            return rp.project_rejection(
                rp.ScopeViolation("nothing to submit", code=PJ.RejectionCode.PATCH_NOOP.value)
            )
        session.submitted = True
        return Diagnostic(
            source=DiagnosticSource.TRIAL, ok=True, code="submitted", subject=session.route.value
        )


class AbortTool:
    """`abort(reason_code)` (terminal): the SoT T3 reason enum, no free text."""

    name = ABORT_TOOL
    description = abort_tool_wire(ABORT_TOOL)["description"]
    input_schema = MappingProxyType(json.loads(json.dumps(dict(ABORT_TOOL_SCHEMA))))
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _RPR
    terminal = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _session_of(ctx, self.name)
        session.tool_calls += 1
        reason = str(args.get("reason_code", ""))
        if reason not in ABORT_REASON_CODES:
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="reason_code")
        session.abort_reason = reason
        return Diagnostic(
            source=DiagnosticSource.TRIAL, ok=True, code="aborted", subject=session.route.value
        )


def proposer_tools(*, attack_enabled: bool = False) -> tuple[Any, ...]:
    """The proposer's eight tools; `attack_enabled` swaps in the flagged
    `certify` declaration of `repair.certify.attack_enabled` (Phase 3)."""
    return (
        ReadViewTool(),
        ReadFieldTool(),
        ApplyEditTrialTool(),
        CheckScopeTool(),
        CheckCheapTool(),
        CertifyTool(attack_enabled=attack_enabled),
        SubmitPatchTool(),
        AbortTool(),
    )


#: The declared (Phase 1, flag-off) tool set: what the registry declares for
#: the role and what the manifest hashes.
PROPOSER_TOOLS: tuple[Any, ...] = proposer_tools()
#: The flagged tool set (`repair.certify.attack_enabled: true`).
PROPOSER_TOOLS_ATTACK: tuple[Any, ...] = proposer_tools(attack_enabled=True)
PROPOSER_TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in PROPOSER_TOOLS)
MODEL_FACING_TOOL_NAMES: tuple[str, ...] = tuple(
    t.name for t in PROPOSER_TOOLS if not getattr(t, "terminal", False)
)


def proposer_tool(name: str, *, attack_enabled: bool = False) -> Any:
    for tool in PROPOSER_TOOLS_ATTACK if attack_enabled else PROPOSER_TOOLS:
        if tool.name == name:
            return tool
    raise KeyError(name)


def proposer_registry(*, attack_enabled: bool = False) -> ToolRegistry:
    """The declared registry (ungated), for a session the CLI opted into."""
    return ToolRegistry(ROLE, PROPOSER_TOOLS_ATTACK if attack_enabled else PROPOSER_TOOLS)


def proposer_policy(
    limits: SessionLimits | None = None,
    *,
    session_salt: int = 0,
    agents_config: Path | str | None = None,
    attack_enabled: bool = False,
) -> SessionPolicy:
    """Build the bounded repair proposer's session policy.

    Include its tools, terminals, declared limits, and session-wide format defaults.
    Enabling attack certification changes the tool and policy digests, rekeying stored
    sessions.
    """
    if limits is None:
        limits = SessionLimits.from_block({}, session_defaults=session_defaults_for(agents_config))
    elif not limits.session_defaults:
        limits = limits.with_session_defaults(session_defaults_for(agents_config))
    tools = PROPOSER_TOOLS_ATTACK if attack_enabled else PROPOSER_TOOLS
    return SessionPolicy(
        role=ROLE,
        tools=tools,
        submit_tool=SUBMIT_TOOL,
        abort_tool=ABORT_TOOL,
        limits=limits,
        mode="model_driven",
        wire_tools=tuple(proposer_registry(attack_enabled=attack_enabled).wire_tools()),
        session_salt=int(session_salt),
    )


def proposer_validator_worker() -> InProcessValidatorWorker:
    """The D3 worker for a proposer session: the held trial's surface
    fingerprint (R4) and the accumulated patch as the auto-submittable draft."""
    return InProcessValidatorWorker(
        surface_fingerprint=lambda ctx: _session_of(ctx, "worker").surface_fingerprint(),
        current_draft=lambda ctx: _session_of(ctx, "worker").current_draft(),
    )


# Semantic-author sessions expose only submit or abort. The harness installs
# each draft in memory, returns one codes-only prose check, and permits bounded
# revision; only the final draft gets contamination and later review checks.

AUTHOR_ROLE = "semantic_author"
AUTHOR_SUBMIT_TOOL = "submit_prose"
AUTHOR_ABORT_TOOL = "abort"
AUTHOR_CHECK_TOOL = "check_prose"
AUTHOR_PRECHECK_TOOL = "contamination_precheck"
AUTHOR_REPLACE_TOOL = "replace_prose"

#: What one draft may carry (characters): comfortably above the author
#: route's 32 768-token output budget, so the bound is the schema's, never
#: a silent truncation.
MAX_PROSE_CHARS = 200_000

#: `check_prose` codes: the prose half of the cheap-gate vocabulary
#: (`DiagnosticSource.CHEAP`, SoT T3 `check_cheap` row), green or red, with
#: one `prose_<kind>` flag per PROSE kind present and the marts / output
#: columns the problems name in `names`.
PROSE_CHECK_GREEN = "cheap_green"
PROSE_CHECK_RED = "prose_problems"

#: `contamination_precheck` projects onto the `contamination-clean` gate's
#: vocabulary (`DiagnosticSource.GATE`): `{level in {unarmed, name_only,
#: armed}, fatal, kinds}` as flags — never a hash, a corpus name or a prior
#: task id (SoT T3).
CONTAMINATION_GATE = "contamination-clean"
CONTAMINATION_LEVELS: tuple[str, ...] = ("unarmed", "name_only", "armed")

_AUT: frozenset[str] = frozenset({AUTHOR_ROLE})

_PROSE_ARGS: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_PROSE_CHARS}
        },
        "required": ["text"],
        "additionalProperties": False,
    }
)


def author_session_enabled(block: Mapping[str, Any] | None) -> bool:
    """The ONE rule for when the bounded revision runs (roadmap 1.A; SoT T1
    AUT): the role's `session:` block is `enabled` AND `max_revisions > 0`.
    Either key alone (`enabled: false` or `max_revisions: 0`) is the
    rollback that restores `council.author_prose` byte for byte — the wire,
    the key and the ledger row included."""
    if not isinstance(block, Mapping):
        return False
    try:
        revisions = int(block.get("max_revisions", 0) or 0)
    except (TypeError, ValueError):
        return False
    return bool(block.get("enabled", False)) and revisions > 0


class AuthorSession:
    """One author session's HARNESS-side state: the task, the held draft (in
    memory — the author writes no file), every draft installed in order with
    its check, the final-draft precheck and the contamination handle it
    scans against. Never serialized toward the model; the harness reads the
    accepted draft off the `SessionResult` and the counters off this."""

    def __init__(
        self,
        *,
        task: TaskIR,
        max_revisions: int = 0,
        contamination_index: Any = None,
        coverage_requirement: Any = None,
    ) -> None:
        self.task = task
        self.task_id = str(task.task_id)
        self.max_revisions = int(max_revisions)
        #: A harness handle with `scan_pre(task, require=...)`; never a path
        #: the model supplied, never serialized.
        self.contamination_index = contamination_index
        self.coverage_requirement = coverage_requirement
        self.public = PublicIdentifierSet(task)
        self.draft: str | None = None
        self.drafts: list[str] = []
        self.checks: list[Diagnostic] = []
        self.green_draft: str | None = None
        self.precheck: Diagnostic | None = None
        self.state_epoch = 0
        self.tool_calls = 0
        self.abort_reason: str | None = None

    # -- the in-memory write ---------------------------------------------------

    def replace_prose(self, text: str) -> bool:
        """Install `text` as the held draft. True when the bytes moved (the
        state epoch bumps only then — R4's no-op rule)."""
        text = str(text)
        changed = text != self.draft
        self.draft = text
        self.drafts.append(text)
        if changed:
            self.state_epoch += 1
        return changed

    def task_with_draft(self, text: str | None = None) -> TaskIR:
        """`task.model_copy(update={"solver_prompt": draft})`: what every
        validator runs on."""
        draft = self.draft if text is None else text
        return self.task.model_copy(update={"solver_prompt": draft or ""})

    # -- what the harness reads ------------------------------------------------

    @property
    def check_count(self) -> int:
        return len(self.checks)

    @property
    def last_check_green(self) -> bool:
        return bool(self.checks) and bool(self.checks[-1].ok)

    def surface_fingerprint(self) -> str:
        """sha256 of the held draft (the writable surface, R4)."""
        return sha256_hex(self.draft or "")

    def current_draft(self) -> dict | None:
        """The held draft as the auto-submittable payload of a limit stop."""
        return None if self.draft is None else {"text": self.draft}

    def context(self, root: Path) -> "AuthorToolContext":
        return AuthorToolContext(
            root=root, task_id=self.task_id, role=AUTHOR_ROLE, task=self.task, session=self
        )


@dataclass(frozen=True)
class AuthorToolContext(ToolContext):
    """A `ToolContext` for the author (a scratch root: the tools touch no
    file) carrying the session state (non-identity: never compared, never
    serialized)."""

    session: Any = field(default=None, compare=False, repr=False)


def _author_session_of(ctx: Any, tool: str) -> AuthorSession:
    session = getattr(ctx, "session", None)
    if not isinstance(session, AuthorSession):
        raise ToolHarnessFault(tool, code="no_author_session")
    return session


def _draft_of(args: Mapping[str, Any], tool: str) -> str:
    text = args.get("text")
    if not isinstance(text, str) or not text or len(text) > MAX_PROSE_CHARS:
        raise ToolProtocolFault("invalid_arguments", tool=tool, detail="text")
    return text


def _flag_key(prefix: str, value: str) -> str:
    """`<prefix>_<value>` as a `_CODE_RE` flag key (lowercase snake)."""
    body = re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_") or "unknown"
    return f"{prefix}_{body}"[:128]


# -- projections -----------------------------------------------------------------

_AUTHOR_NAME_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]*")


def _author_instruction_names(task: TaskIR, text: str) -> tuple[str, ...]:
    """Task-public identifiers implicated by one prose-gate instruction.

    The prose-fidelity producer builds its problem from TaskIR/MartSpec text.
    Recover only identifiers independently present on those public structures;
    no reference implementation, gold output, population row, or private SQL is
    consulted.  A snake-case public name may occur verbatim or as its ordinary
    space-separated label (``distinct_status_count`` -> "distinct status count").
    """
    public = PublicIdentifierSet(task)
    names: list[str] = []
    for table in task.tables:
        names.append(table.name)
        names.extend(column.name for column in table.columns)
    for mart in task.marts:
        names.append(mart.name)
        names.extend(mart.key_columns)
        names.extend(column.name for column in mart.columns)
        for op in mart.plan.ops:
            # A plan op lists compiler aliases beside public names; only a
            # PUBLIC one may travel (the projector's schema detector refuses
            # any other, as a harness fault).
            names.extend(name for name in op.tables if name in public)
            names.extend(name for name in op.columns if name in public)

    tokens = tuple(_AUTHOR_NAME_TOKEN_RE.findall(text.lower()))
    token_set = frozenset(tokens)

    def represented(name: str) -> bool:
        lowered = name.lower()
        if lowered in token_set:
            return True
        parts = tuple(lowered.split("_"))
        if len(parts) < 2 or len(parts) > len(tokens):
            return False
        return any(
            tokens[index : index + len(parts)] == parts
            for index in range(len(tokens) - len(parts) + 1)
        )

    return _dedupe([name for name in names if represented(name)])


#: Prose flags use closed item, mart, rule, and operator vocabularies without
#: sentences or numbers. Marts containing digits travel in `names` so the
#: numeric canary remains strict.
PROSE_ITEM_FLAG_PREFIX = "prose_item"
PROSE_MART_FLAG_PREFIX = "prose_mart"
PROSE_OPERATOR_FLAG_PREFIX = "prose_operator"

_DIGIT_RE = re.compile(r"[0-9]")


def _mart_flag(mart: str, *parts: str) -> str | None:
    """`prose_mart_<mart>_<part>...` when it is a legal, digit-free flag key
    (`_CODE_RE`, <= 128 chars), else None (the mart travels in `names`)."""
    body = re.sub(r"[^a-z0-9_]+", "_", "_".join((mart, *parts)).lower()).strip("_")
    key = f"{PROSE_MART_FLAG_PREFIX}_{body}"
    if not body or _DIGIT_RE.search(key) or len(key) > 128:
        return None
    return key


def project_prose_check(problems: Sequence[str], *, task: TaskIR) -> Diagnostic:
    """Project prose-fidelity failures into one code-only diagnostic.

    Drop problem sentences and expose only approved gate codes, flags, loci, and
    implicated public identifiers. Never include numbers, reference SQL, or free text.
    """
    from elt_taskgen.review import prose_fidelity as PF  # lazy: the gate imports nothing here

    items = PJ.project_prose_problems(problems, task=task)
    red = bool(problems)
    flags: dict[str, bool] = {"prose_ok": not red}
    public = PublicIdentifierSet(task)
    task_names = _task_names(task)
    order = {mart.name: position for position, mart in enumerate(task.marts, start=1)}
    grouped: dict[str, list[str]] = {}
    marts_implicated: dict[str, None] = {}
    operator_sections: dict[str, str] | None = None
    for item in items:
        flags[_flag_key("prose", item.code)] = True
        locus = PF.problem_locus(item.text)
        mart = locus.mart if locus.mart in public else ""
        if locus.item == "operator":
            flags[_flag_key(PROSE_OPERATOR_FLAG_PREFIX, locus.category or "unknown")] = True
            if not mart:
                if operator_sections is None:
                    operator_sections = _operator_sections(task)
                mart = _locate_operator_fragment(item.text, operator_sections)
        flags[_flag_key(PROSE_ITEM_FLAG_PREFIX, locus.item)] = True
        if mart:
            marts_implicated.setdefault(mart, None)
            key = _mart_flag(mart, locus.item)
            if key is not None:
                flags[key] = True
            if locus.item == "rule" and locus.op_kind in _OP_KINDS:
                rule_key = _mart_flag(mart, "rule", locus.op_kind)
                if rule_key is not None:
                    flags[rule_key] = True
        bucket = grouped.setdefault(mart, [])
        if item.subject:
            bucket.append(item.subject)
        if locus.item == "rule" and locus.op_kind in _OP_KINDS:
            bucket.append(locus.op_kind)  # a MartOpKind value: public vocabulary
        bucket.extend(name for name in locus.identifiers if name in task_names)
        # Missing grain prose often contains non-public words that cannot cross
        # the value-free boundary, so generators reuse concise op wording.
        bucket.extend(item.names)
        bucket.extend(_author_instruction_names(task, item.text))
    if red and not items:
        # Problems that projected to nothing (blank sentences): still red.
        flags[_flag_key("prose", "not_represented")] = True
    names: list[str] = []
    for mart in sorted(grouped, key=lambda name: (order.get(name, 0), name)):
        if mart:
            names.append(mart)
        names.extend(grouped[mart])
    implicated = [m for m in marts_implicated if _names_entry(m)]
    return Diagnostic(
        source=DiagnosticSource.CHEAP,
        ok=not red,
        code=PROSE_CHECK_RED if red else PROSE_CHECK_GREEN,
        subject=implicated[0] if len(implicated) == 1 else "",
        flags=flags,
        names=_dedupe([name for name in names if _names_entry(name)]),
    )


def _task_names(task: TaskIR) -> frozenset[str]:
    """The task's OWN public names (tables, columns, marts, key/output
    columns, relationship endpoints): what a missing-term list may point at.
    The harness vocabulary (`PublicIdentifierSet` also admits op kinds, gate
    and backend names) is left out so an English word of a description that
    happens to be an op kind ('source') is not reported as an identifier."""
    names: set[str] = set()
    for table in task.tables:
        names.add(table.name)
        names.update(column.name for column in table.columns)
    for mart in task.marts:
        names.add(mart.name)
        names.update(mart.key_columns)
        names.update(column.name for column in mart.columns)
    for relationship in task.relationships:
        names.add(relationship.child_table)
        names.add(relationship.parent_table)
        names.update(relationship.child_columns)
        names.update(relationship.parent_columns)
    return frozenset(name for name in names if name)


def _names_entry(name: str) -> bool:
    """Return whether `name` has the diagnostic identifier shape.

    Leading underscores are valid. Bare numbers and paths are excluded; filtering
    them avoids converting a projection limitation into a harness fault.
    """
    return bool(name) and PJ._IDENT_RE.fullmatch(name) is not None  # noqa: SLF001


_OP_KINDS: frozenset[str] = frozenset(kind.value for kind in MartOpKind)

_OPERATOR_EXCERPT_RE = re.compile(r' in "(?:\.\.\.)?(.*?)(?:\.\.\.)?" — ')


def _operator_sections(task: TaskIR) -> dict[str, str]:
    """Each mart's labelled section of the CURRENT draft (`prose_fidelity.
    _mart_sections` over the normalized prose), for locating an operator
    fragment; empty when the draft has no sections."""
    from elt_taskgen.review import prose_fidelity as PF

    prose = PF._normalize(task.solver_prompt or "")  # noqa: SLF001 - the gate's own parser
    if not prose:
        return {}
    sections, _problems = PF._mart_sections(task.marts, prose)  # noqa: SLF001
    return {mart: text for mart, text in sections.items() if text}


def _locate_operator_fragment(sentence: str, sections: Mapping[str, str]) -> str:
    """The ONE mart whose section carries the operator sentence's quoted
    excerpt ('' when none or several do): scopes an operator flag to a mart
    without carrying the excerpt onward."""
    match = _OPERATOR_EXCERPT_RE.search(sentence)
    if match is None:
        return ""
    excerpt = " ".join(match.group(1).split()).lower()
    if len(excerpt) < 8:
        return ""
    hits = [mart for mart, text in sections.items() if excerpt in text]
    return hits[0] if len(hits) == 1 else ""


def project_contamination_precheck(result: Any) -> Diagnostic:
    """`ContaminationIndex.scan_pre` result -> `{level, fatal, kinds}` on the
    `contamination-clean` gate's vocabulary: `ok` iff no fatal collision,
    the coverage level as three booleans, `fatal`, one `kind_<kind>` flag
    per collision kind. Never a fingerprint, a corpus name, a prior task id
    or a count. `None` (no index handle) is the fail-closed reading of
    `check_pre` on an unarmed index: a fatal `index` collision."""
    if result is None:
        level = "unarmed"
        fatal = True
        kinds: list[str] = ["index"]
    else:
        coverage = getattr(result, "coverage", None)
        raw_level = getattr(getattr(coverage, "level", None), "value", None)
        level = str(raw_level) if raw_level in CONTAMINATION_LEVELS else "unarmed"
        collisions = list(getattr(result, "collisions", ()) or ())
        fatal = any(bool(getattr(c, "fatal", False)) for c in collisions)
        kinds = sorted({str(getattr(c, "kind", "") or "unknown") for c in collisions})
    flags: dict[str, bool] = {name: name == level for name in CONTAMINATION_LEVELS}
    flags["fatal"] = fatal
    for kind in kinds:
        flags[_flag_key("kind", kind)] = True
    return Diagnostic(
        source=DiagnosticSource.GATE,
        ok=not fatal,
        code="failed" if fatal else "ok",
        subject=CONTAMINATION_GATE,
        flags=flags,
    )


# -- the tools -------------------------------------------------------------------

class ReplaceProseTool:
    """`replace_prose(text)`: the in-memory write (matrix: A, in memory).
    Harness-only — never on the wire, never model-initiated; `check_prose`
    installs each submitted draft through the same session path."""

    name = AUTHOR_REPLACE_TOOL
    description = (
        "Replace the held solver prose, in memory, with text. Harness-run on every "
        "submitted draft; the prose is never written to a file."
    )
    input_schema = _PROSE_ARGS
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _AUT
    surface_write = True
    harness_only = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _author_session_of(ctx, self.name)
        session.tool_calls += 1
        changed = session.replace_prose(_draft_of(args, self.name))
        return Diagnostic(
            source=DiagnosticSource.TRIAL,
            ok=True,
            code="applied",
            subject="author",
            flags={"accepted": True, "epoch_bumped": changed},
        )


class CheckProseTool:
    """`check_prose`: the harness-run fidelity check of one draft — ONE call
    to `check_prose_fidelity` (declarative prose included), codes only."""

    name = AUTHOR_CHECK_TOOL
    description = (
        "Harness-run on every submitted draft: the prose-fidelity gate (completeness "
        "and declarative prose, one check) on the draft installed as solver_prompt. "
        "Returns cheap_green, or prose_problems with one prose_<kind> flag per "
        "problem kind and the mart / output-column names concerned. Codes only."
    )
    input_schema = _PROSE_ARGS
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _AUT
    validator = True
    harness_only = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        from elt_taskgen.review.prose_fidelity import check_prose_fidelity

        session = _author_session_of(ctx, self.name)
        session.tool_calls += 1
        text = _draft_of(args, self.name)
        # The in-memory write (`replace_prose`'s path), then ONE call to the
        # gate the author stage applies: it already includes
        # `check_declarative_prose`, so it is never called here a second time.
        session.replace_prose(text)
        checked = session.task_with_draft(text)
        problems = [str(p) for p in check_prose_fidelity(checked)]
        # Projected against the task WITH the draft installed: the public
        # identifier set is the same, and an operator fragment is located in
        # the draft's own mart sections (`_operator_sections`).
        diag = project_prose_check(problems, task=checked)
        session.checks.append(diag)
        if diag.ok:
            session.green_draft = text
        return diag


class ContaminationPrecheckTool:
    """`contamination_precheck`: `ContaminationIndex.scan_pre` on the FINAL
    draft, once per session, projected to `{level, fatal, kinds}`."""

    name = AUTHOR_PRECHECK_TOOL
    description = (
        "Harness-run once on the final draft: the contamination firewall's "
        "pre-scan of the task with this prose. Returns the coverage level, whether "
        "a fatal collision exists and the collision kinds. Codes only."
    )
    input_schema = _PROSE_ARGS
    cost = ToolCost(oracle_bits=0, wall_s=10.0, per_session=1)
    permitted_roles = _AUT
    validator = True
    harness_only = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _author_session_of(ctx, self.name)
        session.tool_calls += 1
        text = _draft_of(args, self.name)
        index = session.contamination_index
        scan = getattr(index, "scan_pre", None)
        if not callable(scan):
            result = None
        else:
            result = scan(session.task_with_draft(text), require=session.coverage_requirement)
        diag = project_contamination_precheck(result)
        session.precheck = diag
        return diag


class SubmitProseTool:
    """`submit_prose(text)` (terminal): the draft the harness validates and,
    when green or when the revisions are spent, accepts as the prose the
    author stage's unchanged fidelity gate then decides on."""

    name = AUTHOR_SUBMIT_TOOL
    description = (
        "Submit the COMPLETE prose specification as text (the whole document, every "
        "mart). The harness checks it with the prose-fidelity gate and either accepts "
        "it (cheap_green) or, while revisions remain, answers with the problem codes "
        "so you can resubmit the corrected whole. No fragments, no diffs."
    )
    input_schema = _PROSE_ARGS
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _AUT
    terminal = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _author_session_of(ctx, self.name)
        session.tool_calls += 1
        session.replace_prose(_draft_of(args, self.name))
        return Diagnostic(
            source=DiagnosticSource.TRIAL, ok=True, code="submitted", subject="author"
        )


class AuthorAbortTool:
    """`abort(reason_code)` (terminal) for the author: the SoT T3 reason
    enum, no free text. The stage then fails on today's empty-output route."""

    name = AUTHOR_ABORT_TOOL
    description = abort_tool_wire(AUTHOR_ABORT_TOOL)["description"]
    input_schema = MappingProxyType(json.loads(json.dumps(dict(ABORT_TOOL_SCHEMA))))
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _AUT
    terminal = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _author_session_of(ctx, self.name)
        session.tool_calls += 1
        reason = str(args.get("reason_code", ""))
        if reason not in ABORT_REASON_CODES:
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="reason_code")
        session.abort_reason = reason
        return Diagnostic(
            source=DiagnosticSource.TRIAL, ok=True, code="aborted", subject="author"
        )


AUTHOR_TOOLS: tuple[Any, ...] = (
    ReplaceProseTool(),
    CheckProseTool(),
    ContaminationPrecheckTool(),
    SubmitProseTool(),
    AuthorAbortTool(),
)
AUTHOR_TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in AUTHOR_TOOLS)
#: On the wire: the two terminals only (SoT T1 AUT: no model-initiated tool).
AUTHOR_WIRE_TOOL_NAMES: tuple[str, ...] = tuple(
    t.name for t in AUTHOR_TOOLS if not getattr(t, "harness_only", False)
)
#: Harness-run validators: `check_prose` on every submitted draft (the
#: policy's `harness_validators`), `contamination_precheck` on the final one.
AUTHOR_HARNESS_VALIDATORS: tuple[str, ...] = (AUTHOR_CHECK_TOOL,)


def author_tool(name: str) -> Any:
    for tool in AUTHOR_TOOLS:
        if tool.name == name:
            return tool
    raise KeyError(name)


def author_registry() -> ToolRegistry:
    """The declared registry (ungated): the five author tools; its wire
    manifest carries the two terminals only."""
    return ToolRegistry(AUTHOR_ROLE, AUTHOR_TOOLS)


def session_defaults_for(agents_config: Path | str | None = None) -> dict[str, Any]:
    """The agents document's TOP-LEVEL `session:` mapping (today
    `format_error_disposition`): the session-wide defaults every
    `SessionLimits` built outside `providers.session_policy_for` must still
    carry (`SessionLimits.session_defaults`, un-hashed), read from the same
    document the behaviour manifest hashes (`agents_config` None = the
    repository default, patchable by tests through `providers._agents_doc`).
    `{}` when the document declares none."""
    from elt_taskgen.review import providers  # lazy: providers imports the registry

    document = providers._agents_doc_for(agents_config)  # noqa: SLF001 - the ONE loader
    block = document.get("session") if isinstance(document, Mapping) else None
    return dict(block) if isinstance(block, Mapping) else {}


def author_limits(
    block: Mapping[str, Any] | None = None,
    *,
    session_defaults: Mapping[str, Any] | None = None,
    agents_config: Path | str | None = None,
) -> SessionLimits:
    """Build author limits from the declared session block.

    Derive `harness_validators=[check_prose]` and
    `max_compile_corrections=max_revisions`, and attach un-hashed session-wide format
    defaults. Applying the function to its own output is idempotent.
    """
    from elt_taskgen.review import providers  # lazy: providers imports the registry

    if block is None:
        block = providers.role_loop_limits(AUTHOR_ROLE, agents_config=agents_config)
    plain = dict(block)
    plain["harness_validators"] = list(AUTHOR_HARNESS_VALIDATORS)
    plain["max_compile_corrections"] = int(plain.get("max_revisions", 0) or 0)
    defaults = dict(session_defaults) if session_defaults else session_defaults_for(agents_config)
    return SessionLimits.from_block(plain, session_defaults=defaults)


def author_policy(
    limits: SessionLimits | None = None,
    *,
    session_salt: int = 0,
    agents_config: Path | str | None = None,
) -> SessionPolicy:
    """The session policy the harness-driven author runs under: the five
    tools in the allowlist (the three harness-only ones included, so the
    runner can run them and the manifest names them), `submit_prose` /
    `abort` as terminals, the derived limits (carrying the session-wide
    defaults of `limits`, else the document's), the two terminals on the
    wire."""
    derived = author_limits(
        limits.block if limits is not None else None,
        session_defaults=dict(limits.session_defaults) if limits is not None and limits.session_defaults else None,
        agents_config=agents_config,
    )
    return SessionPolicy(
        role=AUTHOR_ROLE,
        tools=AUTHOR_TOOLS,
        submit_tool=AUTHOR_SUBMIT_TOOL,
        abort_tool=AUTHOR_ABORT_TOOL,
        limits=derived,
        mode="harness_driven",
        wire_tools=tuple(author_registry().wire_tools()),
        session_salt=int(session_salt),
    )


def author_validator_worker() -> InProcessValidatorWorker:
    """The D3 worker for an author session: the held draft's digest (R4) and
    the draft as the auto-submittable payload of a limit stop."""
    return InProcessValidatorWorker(
        surface_fingerprint=lambda ctx: _author_session_of(ctx, "worker").surface_fingerprint(),
        current_draft=lambda ctx: _author_session_of(ctx, "worker").current_draft(),
    )


# DEV/T tools expose only bounded, read-only development data and codes before
# `sql_by_mart` submission. Workers receive public TaskIR, dev paths, and SQL,
# never gold, credentials, pass bits, counts, raw errors, files, or dbt access.

IMPLEMENTER_ROLE = "independent_implementer"
IMPLEMENTER_SUBMIT_TOOL = "submit_sql_by_mart"
IMPLEMENTER_ABORT_TOOL = "abort"

_IMP: frozenset[str] = frozenset({IMPLEMENTER_ROLE})
_NAME_RE = r"^[A-Za-z][A-Za-z0-9_.\-]{0,127}$"
_MAX_SQL_CHARS = 20000

_SQL_ITEM_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "mart": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": _NAME_RE},
            "sql": {"type": "string", "minLength": 1, "maxLength": _MAX_SQL_CHARS},
        },
        "required": ["mart", "sql"],
        "additionalProperties": False,
    }
)
_SQL_BY_MART_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "sql_by_mart": {
                "type": "array",
                "minItems": 1,
                "maxItems": 256,
                "items": dict(_SQL_ITEM_SCHEMA),
            }
        },
        "required": ["sql_by_mart"],
        "additionalProperties": False,
    }
)


def _dev_tool():
    """`training.dev_tool`, imported lazily (it pulls in the reference
    loaders and DuckDB; the registry imports this module eagerly)."""
    from elt_taskgen.training import dev_tool as _dt

    return _dt


def _coerce_sql_by_mart(value: Any) -> dict[str, str]:
    """Accept the wire array form `[{"mart","sql"}, ...]` or a plain mapping
    and return a `{mart: sql}` dict."""
    if isinstance(value, Mapping):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        out: dict[str, str] = {}
        for item in value:
            if isinstance(item, Mapping) and "mart" in item and "sql" in item:
                out[str(item["mart"])] = str(item["sql"])
        return out
    return {}


class ImplementerSession:
    """One implementer session's HARNESS-side state: the workspace, the public
    task, the DEVELOPMENT warehouse (materialized from the DEVELOPMENT rendered
    SOURCE tree, gold-free) and the DEVELOPMENT rendered dir the marts execute
    against, plus the accumulated `sql_by_mart` artifact. Holds NO gold, NO
    package and NO credentials; never serialized toward the model."""

    def __init__(
        self,
        *,
        workspace: Path,
        task: TaskIR,
        warehouse_root: Path | None = None,
        dev_warehouse: Path | None = None,
        rendered_dev: Path | None = None,
    ) -> None:
        dt = _dev_tool()
        self.workspace = Path(workspace).resolve()
        self.task = task
        self.task_id = str(task.task_id)
        # Harness-held trust anchor for exact-path validation.  Production
        # supplies a fresh per-session root; it is not model-visible and is
        # intentionally distinct from the empty ToolContext root.
        self._dev_warehouse_root = (
            self.workspace
            if warehouse_root is None
            else Path(warehouse_root).resolve()
        )
        self.dev_warehouse = (
            Path(dev_warehouse) if dev_warehouse is not None
            else dt.dev_warehouse_path(self._dev_warehouse_root, self.task_id)
        )
        from elt_taskgen.models import PopulationName
        from elt_taskgen.reference.runner import rendered_dir

        self.rendered_dev = (
            Path(rendered_dev) if rendered_dev is not None
            else rendered_dir(self.workspace, self.task_id, PopulationName.DEVELOPMENT)
        )
        self.tool_calls = 0
        self.sql_by_mart: dict[str, str] | None = None
        self.submitted = False
        self.abort_reason: str | None = None
        self.public = PublicIdentifierSet(task)

    @classmethod
    def open(cls, workspace: Path, task: TaskIR, **options: Any) -> "ImplementerSession":
        """Build a session and MATERIALIZE its DEVELOPMENT warehouse from the
        DEVELOPMENT rendered source tree (source BASE TABLEs only).  A caller
        may supply ``warehouse_root`` to keep this disposable derivative away
        from the immutable workspace artifacts."""
        session = cls(workspace=workspace, task=task, **options)
        session.dev_warehouse = _dev_tool().materialize_dev_warehouse(
            task,
            session.workspace,
            warehouse_root=session._dev_warehouse_root,
        )
        return session

    def surface_fingerprint(self) -> str:
        return sha256_hex(canonical_json(self.sql_by_mart or {}))

    def current_draft(self) -> dict | None:
        return None if self.sql_by_mart is None else {"sql_by_mart": dict(self.sql_by_mart)}

    def context(
        self, *, package: Any = None, root: Path | None = None
    ) -> "ImplementerToolContext":
        """Build the model-facing context.

        Production witness sessions pass a disposable scratch ``root`` so a
        normal pipeline workspace below the repository's protected ``runs/``
        tree never becomes a tool-path root.  The default keeps direct tool
        tests and callers on an already-safe workspace backward compatible.
        ``package`` remains intentionally ignored: this worker holds no gold.
        """
        return ImplementerToolContext(
            root=self.workspace if root is None else Path(root),
            task_id=self.task_id, role=IMPLEMENTER_ROLE,
            task=self.task, package=None, session=self,
        )


@dataclass(frozen=True)
class ImplementerToolContext(ToolContext):
    """A `ToolContext` rooted at disposable scratch in production, carrying
    the session state (non-identity: never compared, never serialized).
    `package` is always None: the implementer worker holds no gold."""

    session: Any = field(default=None, compare=False, repr=False)


def _implementer_session_of(ctx: Any, tool: str) -> ImplementerSession:
    session = getattr(ctx, "session", None)
    if not isinstance(session, ImplementerSession):
        raise ToolHarnessFault(tool, code="no_implementer_session")
    return session


class ListSchemasTool:
    """`list_schemas`: the DEVELOPMENT source table names (public), names only."""

    name = "list_schemas"
    description = (
        "List the DEVELOPMENT source table names you may query. Names only — no "
        "row counts, no gold, no marts. The full column schema is in your task view."
    )
    input_schema = _NO_ARGS
    cost = ToolCost(oracle_bits=0, wall_s=10.0, idempotent_read=True)
    permitted_roles = _IMP

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _implementer_session_of(ctx, self.name)
        session.tool_calls += 1
        return PJ.project_list_schemas([t.name for t in session.task.tables])


class DevQueryTool:
    """`dev_query(sql)`: a bounded, read-only query over the DEVELOPMENT
    warehouse (the ported sibling `duckdb_tool`). Returns `DevRows`."""

    name = "dev_query"
    description = (
        "Run ONE read-only SELECT against the DEVELOPMENT warehouse (the same tiny "
        "source rows the solver sees). At most 200 rows and 16 KiB come back. No "
        "ATTACH/SET/PRAGMA, no read_*/scan_* functions, no writes, and no catalogue "
        "queries: information_schema, duckdb_* functions, DESCRIBE and SHOW are "
        "refused and end the session. Column names and types come from "
        "list_schemas and the task material. Only the DEVELOPMENT source tables "
        "exist; there is no gold and no mart to read. Every result column must "
        "be a column name of this task (a source or mart column): a computed "
        "value under any other alias, and a bare `SELECT 1`, come back as "
        "invalid_query, so alias an expression you want to inspect to the "
        "mart column it will produce (`SELECT md5(...) AS keyword_id FROM ...`)."
    )
    input_schema = MappingProxyType(
        {
            "type": "object",
            "properties": {"sql": {"type": "string", "minLength": 1, "maxLength": _MAX_SQL_CHARS}},
            "required": ["sql"],
            "additionalProperties": False,
        }
    )
    #: Per-session cap 8 (SoT T1 IMP `per_tool.dev_query`); 10 s deadline;
    #: idempotent read (exempt from the stuck detector's R1/R3, SoT T5).
    cost = ToolCost(oracle_bits=0, wall_s=10.0, per_session=8, idempotent_read=True)
    permitted_roles = _IMP

    def run(self, ctx: Any, args: Mapping[str, Any]):
        session = _implementer_session_of(ctx, self.name)
        session.tool_calls += 1
        sql = str(args.get("sql", ""))
        if not sql:
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="sql")
        dt = _dev_tool()
        warehouse = dt.assert_development_warehouse(
            session.dev_warehouse,
            session.workspace,
            session.task_id,
            warehouse_root=session._dev_warehouse_root,
        )
        allowed = frozenset(t.name for t in session.task.tables)
        try:
            rows = dt.run_dev_query(warehouse, sql, allowed_tables=allowed)
        except dt.DevQueryError as exc:
            return PJ.project_dev_query_error(exc.code)
        # A column aliased to a NON-PUBLIC identifier (`... AS cid`) would trip
        # the gatekeeper as a LEAK_TRIPWIRE and end the session with reward
        # None; refuse it here as a correctable `invalid_query` instead
        # (finding 3-1). Public DEVELOPMENT column names pass untouched.
        if session.public.unknown(rows.columns):
            return PJ.project_dev_query_error("invalid_query")
        # Refuse path-, secret-, or executor-shaped query cells as correctable
        # invalid_query output before the projection tripwire can void a session.
        if PJ.dev_rows_leak_shape(rows) is not None:
            return PJ.project_dev_query_error("invalid_query")
        return rows


class DryRunSqlTool:
    """`dry_run_sql(mart, sql)`: EXPLAIN over empty typed tables in a 10 s
    spawned worker; `{binds, error_class, columns_match, missing_columns}`."""

    name = "dry_run_sql"
    description = (
        "Dry-run one mart's SQL against EMPTY typed source tables in a throwaway "
        "worker: does it bind, and does it produce exactly the mart's declared "
        "columns? Returns binds, an error class, columns_match and the missing "
        "columns — never the DuckDB error text, never any row."
    )
    input_schema = MappingProxyType(
        {
            "type": "object",
            "properties": {
                "mart": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": _NAME_RE},
                "sql": {"type": "string", "minLength": 1, "maxLength": _MAX_SQL_CHARS},
            },
            "required": ["mart", "sql"],
            "additionalProperties": False,
        }
    )
    cost = ToolCost(oracle_bits=0, wall_s=10.0, per_session=4)
    permitted_roles = _IMP

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _implementer_session_of(ctx, self.name)
        session.tool_calls += 1
        mart_name = str(args.get("mart", ""))
        sql = str(args.get("sql", ""))
        if not sql or not any(m.name == mart_name for m in session.task.marts):
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="mart_or_sql")
        result = _dev_tool().dry_run_in_worker(
            session.task, mart_name, sql, deadline_s=self.cost.wall_s
        )
        return PJ.project_bind(
            binds=bool(result["binds"]),
            error_class=str(result["error_class"]),
            columns_match=bool(result["columns_match"]),
            missing_columns=[str(c) for c in result["missing_columns"]],
            mart=mart_name,
        )


class RunMartSqlDevTool:
    """`run_mart_sql_dev(sql_by_mart)`: DEVELOPMENT-only execution under
    `SemanticLimits` in a gold-free spawned worker; `{mart, code}`, rows and
    counts discarded."""

    name = "run_mart_sql_dev"
    description = (
        "Execute your marts over the DEVELOPMENT source rows in a throwaway "
        "worker to see whether each runs. Rows and row counts are discarded; you "
        "get one {mart, code} for the first mart that does not run (ok when all "
        "run). DEVELOPMENT only; no gold is ever consulted."
    )
    input_schema = MappingProxyType(dict(_SQL_BY_MART_SCHEMA))
    cost = ToolCost(oracle_bits=0, wall_s=60.0, per_session=2)
    permitted_roles = _IMP

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _implementer_session_of(ctx, self.name)
        session.tool_calls += 1
        sql_by_mart = _coerce_sql_by_mart(args.get("sql_by_mart"))
        if not sql_by_mart:
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="sql_by_mart")
        mart, code = _dev_tool().run_marts_dev_in_worker(
            session.task, session.rendered_dev, sql_by_mart, deadline_s=self.cost.wall_s
        )
        return PJ.project_mart_dev(mart=mart, code=code)


class SubmitSqlByMartTool:
    """`submit_sql_by_mart(sql_by_mart)` (terminal): the session's artifact —
    handed to the UNCHANGED certifier after the session ends."""

    name = IMPLEMENTER_SUBMIT_TOOL
    description = (
        "Submit your final SQL for every mart. sql_by_mart is a list of "
        "{mart, sql}; each sql is one standalone DuckDB SELECT producing the mart's "
        "declared columns. The certifier runs it on all five populations; you will "
        "not see the result in this session."
    )
    input_schema = MappingProxyType(dict(_SQL_BY_MART_SCHEMA))
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _IMP
    terminal = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _implementer_session_of(ctx, self.name)
        session.tool_calls += 1
        session.sql_by_mart = _coerce_sql_by_mart(args.get("sql_by_mart"))
        session.submitted = True
        return Diagnostic(source=DiagnosticSource.TRIAL, ok=True, code="submitted")


IMPLEMENTER_CHECK_TOOL = "check_submission"


class CheckSubmissionTool:
    """`check_submission`: the harness-run dry run of every mart in a
    submitted `sql_by_mart` (a validator; never model-initiated). The first
    mart whose statement does not bind, or does not produce the declared
    columns, comes back red as a correction with the mart named, so a
    statement the witness never dry-ran is not scored 0 on every population
    and then resampled (synsql__educational, batch10 run O, 2026-09-11: an
    unbound submission cost one of three sessions). Codes only."""

    name = IMPLEMENTER_CHECK_TOOL
    description = (
        "Harness-run on every submission: dry-runs each mart's SQL against EMPTY "
        "typed source tables. Red, naming the mart, when a statement does not bind "
        "or does not produce the mart's declared columns. Codes only."
    )
    input_schema = MappingProxyType(dict(_SQL_BY_MART_SCHEMA))
    cost = ToolCost(oracle_bits=0, wall_s=40.0)
    permitted_roles = _IMP
    validator = True
    harness_only = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _implementer_session_of(ctx, self.name)
        session.tool_calls += 1
        sql_by_mart = _coerce_sql_by_mart(args.get("sql_by_mart"))
        if not sql_by_mart:
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="sql_by_mart")
        last = ""
        for mart in session.task.marts:
            sql = sql_by_mart.get(mart.name)
            if not sql:
                continue
            last = mart.name
            result = _dev_tool().dry_run_in_worker(
                session.task, mart.name, sql, deadline_s=DryRunSqlTool.cost.wall_s
            )
            binds = bool(result["binds"])
            columns_match = bool(result["columns_match"])
            if not binds or not columns_match:
                diagnostic = PJ.project_bind(
                    binds=binds,
                    error_class=str(result["error_class"]),
                    columns_match=columns_match,
                    missing_columns=[str(c) for c in result["missing_columns"]],
                    mart=mart.name,
                )
                if binds:
                    # Binds, but not the declared columns: red all the same.
                    diagnostic = Diagnostic(
                        source=diagnostic.source, ok=False, code=diagnostic.code,
                        subject=diagnostic.subject, flags=dict(diagnostic.flags),
                        names=tuple(diagnostic.names),
                    )
                return diagnostic
        return PJ.project_bind(
            binds=True, error_class="", columns_match=True, missing_columns=[], mart=last
        )


class ImplementerAbortTool:
    """`abort(reason_code)` (terminal): the SoT T3 reason enum, no free text."""

    name = IMPLEMENTER_ABORT_TOOL
    description = abort_tool_wire(IMPLEMENTER_ABORT_TOOL)["description"]
    input_schema = MappingProxyType(json.loads(json.dumps(dict(ABORT_TOOL_SCHEMA))))
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _IMP
    terminal = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _implementer_session_of(ctx, self.name)
        session.tool_calls += 1
        reason = str(args.get("reason_code", ""))
        if reason not in ABORT_REASON_CODES:
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="reason_code")
        session.abort_reason = reason
        return Diagnostic(source=DiagnosticSource.TRIAL, ok=True, code="aborted")


IMPLEMENTER_TOOLS: tuple[Any, ...] = (
    ListSchemasTool(),
    DevQueryTool(),
    DryRunSqlTool(),
    RunMartSqlDevTool(),
    SubmitSqlByMartTool(),
    ImplementerAbortTool(),
    CheckSubmissionTool(),
)
IMPLEMENTER_TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in IMPLEMENTER_TOOLS)
IMPLEMENTER_MODEL_FACING_TOOL_NAMES: tuple[str, ...] = tuple(
    t.name for t in IMPLEMENTER_TOOLS if not getattr(t, "terminal", False)
)


def implementer_tool(name: str) -> Any:
    for tool in IMPLEMENTER_TOOLS:
        if tool.name == name:
            return tool
    raise KeyError(name)


def implementer_registry() -> ToolRegistry:
    """The declared implementer registry (ungated): its wire manifest carries
    every tool (none are harness-only)."""
    return ToolRegistry(IMPLEMENTER_ROLE, IMPLEMENTER_TOOLS)


def implementer_limits(
    block: Mapping[str, Any] | None = None,
    *,
    session_defaults: Mapping[str, Any] | None = None,
    agents_config: Path | str | None = None,
) -> SessionLimits:
    """The implementer's `SessionLimits` (mirrors `author_limits`): the
    declared `roles.independent_implementer.session` block (None = the
    `agents_config` document's, the repository default's by default), carrying
    the document's session-wide `session:` defaults (un-hashed). The per-tool
    ceilings are the tools' own `cost.per_session` (SoT T1 IMP: `dev_query` 8,
    `dry_run_sql` 4, `run_mart_sql_dev` 2)."""
    from elt_taskgen.review import providers  # lazy: providers imports the registry

    if block is None:
        block = providers.role_loop_limits(IMPLEMENTER_ROLE, agents_config=agents_config)
    defaults = dict(session_defaults) if session_defaults else session_defaults_for(agents_config)
    return SessionLimits.from_block(dict(block), session_defaults=defaults)


def implementer_policy(
    limits: SessionLimits | None = None,
    *,
    session_salt: int = 0,
    agents_config: Path | str | None = None,
) -> SessionPolicy:
    """The model-driven implementer session policy (mirrors `proposer_policy`):
    the six tools, `submit_sql_by_mart` / `abort` as terminals, the declared
    limits carrying the session-wide defaults."""
    if limits is None:
        limits = SessionLimits.from_block({}, session_defaults=session_defaults_for(agents_config))
    elif not limits.session_defaults:
        limits = limits.with_session_defaults(session_defaults_for(agents_config))
    return SessionPolicy(
        role=IMPLEMENTER_ROLE,
        tools=IMPLEMENTER_TOOLS,
        submit_tool=IMPLEMENTER_SUBMIT_TOOL,
        abort_tool=IMPLEMENTER_ABORT_TOOL,
        limits=limits,
        mode="model_driven",
        wire_tools=tuple(implementer_registry().wire_tools()),
        session_salt=int(session_salt),
    )


def implementer_validator_worker() -> InProcessValidatorWorker:
    """The D3 worker for an implementer session (mirrors the proposer worker):
    spawned, gold-free, no provider handle, env replaced and keyless (the
    `dry_run_sql` / `run_mart_sql_dev` sub-workers of `training/dev_tool.py`
    apply the keyless scrub). Its surface is the accumulated `sql_by_mart`."""
    return InProcessValidatorWorker(
        surface_fingerprint=lambda ctx: _implementer_session_of(ctx, "worker").surface_fingerprint(),
        current_draft=lambda ctx: _implementer_session_of(ctx, "worker").current_draft(),
    )


# Loader plans are checked statically for readers, path confinement, and table
# coverage, without execution or counts. Non-public subjects and population
# error records are never returned to the model.

LOADER_ROLE = "independent_loader"
LOADER_SUBMIT_TOOL = "submit_load_plan"
LOADER_ABORT_TOOL = "abort"
LOADER_CHECK_TOOL = "check_load_plan"
LOADER_REPLACE_TOOL = "replace_load_plan"

_LDR: frozenset[str] = frozenset({LOADER_ROLE})
_MAX_PATH_CHARS = 1024

_LOAD_PLAN_ITEM_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "table": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": _NAME_RE},
            "path": {"type": "string", "minLength": 1, "maxLength": _MAX_PATH_CHARS},
            "format": {"type": "string"},
        },
        "required": ["table", "path", "format"],
        "additionalProperties": False,
    }
)
_LOAD_PLAN_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "plan": {
                "type": "array",
                "minItems": 1,
                "maxItems": 256,
                "items": dict(_LOAD_PLAN_ITEM_SCHEMA),
            }
        },
        "required": ["plan"],
        "additionalProperties": False,
    }
)


def _load_formats() -> tuple[str, ...]:
    from elt_taskgen.corpus.calibration import LOAD_FORMATS

    return tuple(LOAD_FORMATS)


def _coerce_load_plan(value: Any) -> dict[str, dict[str, str]]:
    """Accept the wire array form `[{"table","path","format"}, ...]` or a
    `{table: {path, format}}` mapping and return the mapping form.  A table
    named twice in the array form keeps its LAST step here; the static
    check reports the duplicate (`_load_plan_duplicates`) before the plan
    can be submitted, so the seat is told instead of silently losing a
    step."""
    if isinstance(value, Mapping):
        out: dict[str, dict[str, str]] = {}
        for table, step in value.items():
            if isinstance(step, Mapping):
                out[str(table)] = {"path": str(step.get("path", "")), "format": str(step.get("format", ""))}
        return out
    if isinstance(value, (list, tuple)):
        plan: dict[str, dict[str, str]] = {}
        for item in value:
            if isinstance(item, Mapping) and "table" in item:
                plan[str(item["table"])] = {
                    "path": str(item.get("path", "")),
                    "format": str(item.get("format", "")),
                }
        return plan
    return {}


def _load_plan_duplicates(value: Any) -> tuple[str, ...]:
    """The table names the wire array form names more than once, in first-
    occurrence order (a mapping form cannot carry a duplicate key)."""
    if not isinstance(value, (list, tuple)):
        return ()
    seen: dict[str, int] = {}
    for item in value:
        if isinstance(item, Mapping) and "table" in item:
            name = str(item["table"])
            seen[name] = seen.get(name, 0) + 1
    return tuple(name for name, count in seen.items() if count > 1)


def _check_load_plan_static(
    task: TaskIR,
    plan: Mapping[str, Mapping[str, str]],
    source_root: Path,
    *,
    duplicates: Sequence[str] = (),
) -> tuple[str, str]:
    """Validate a load plan without executing readers or returning counts.

    Check reader types, artifact confinement, complete table coverage, unknown tables,
    and duplicates. Return the first `(code, table)` problem.
    """
    formats = _load_formats()
    root = Path(source_root)
    root_resolved = root.resolve()
    declared = {table.name for table in task.tables}
    for name in duplicates:
        return "table_duplicate", (name if name in declared else "")
    for name in plan:
        if name not in declared:
            public = PublicIdentifierSet(task)
            return "table_unknown", (name if name in public else "")
    for table in task.tables:
        step = plan.get(table.name)
        if step is None:
            return "table_uncovered", table.name
        fmt = str(step.get("format", ""))
        if fmt not in formats:
            return "unknown_reader", table.name
        path = str(step.get("path", ""))
        # A NUL byte or backslash crashes `Path(...).resolve()` ('embedded null
        # byte') / is a non-POSIX separator; a malformed path is a path escape,
        # never a harness fault (finding 3-4).
        if "\x00" in path or "\\" in path:
            return "path_escape", table.name
        try:
            candidate = Path(path)
            if not path or candidate.is_absolute() or path.startswith("~") or ".." in candidate.parts:
                return "path_escape", table.name
            resolved = (root / candidate).resolve()
        except (ValueError, OSError):
            return "path_escape", table.name
        if not (resolved == root_resolved or root_resolved in resolved.parents):
            return "path_escape", table.name
        # The s3_jsonl contract wants a PREFIX DIRECTORY; a part FILE
        # under-reads a table larger than one part (execute_load_plan refuses
        # it too). STATIC: a `.jsonl` suffix or an existing file is a file.
        if fmt == "s3_jsonl" and (path.endswith(".jsonl") or resolved.is_file()):
            return "s3_part_file", table.name
    return "ok", ""


class LoaderSession:
    """One loader session's HARNESS-side state: the workspace, the public
    task, the emitted EL bundle dir, the DEVELOPMENT rendered dir (the source
    root confinement is checked against) and the accumulated load plan. Holds
    NO gold and NO credentials; never serialized toward the model."""

    def __init__(
        self,
        *,
        workspace: Path,
        task: TaskIR,
        bundle_dir: Path | None = None,
        rendered_dev: Path | None = None,
    ) -> None:
        from elt_taskgen.models import PopulationName
        from elt_taskgen.reference import independent
        from elt_taskgen.reference.runner import rendered_dir

        self.workspace = Path(workspace).resolve()
        self.task = task
        self.task_id = str(task.task_id)
        self.bundle_dir = (
            Path(bundle_dir) if bundle_dir is not None
            else independent.el_bundle_dir(self.workspace, self.task_id)
        )
        self.rendered_dev = (
            Path(rendered_dev) if rendered_dev is not None
            else rendered_dir(self.workspace, self.task_id, PopulationName.DEVELOPMENT)
        )
        self.plan: dict[str, dict[str, str]] = {}
        self.tool_calls = 0
        self.submitted = False
        self.abort_reason: str | None = None
        self.public = PublicIdentifierSet(task)

    def surface_fingerprint(self) -> str:
        return sha256_hex(canonical_json(self.plan or {}))

    def current_draft(self) -> dict | None:
        return None if not self.plan else {"plan": dict(self.plan)}

    def context(self, *, root: Path | None = None) -> "LoaderToolContext":
        """Build the model-facing context.

        Production witness sessions supply an empty scratch ``root``; the
        model's logical load-plan paths remain independently confined to
        ``rendered_dev`` by ``_check_load_plan_static``.  The default retains
        the direct-tool API for callers whose workspace is already safe.
        """
        return LoaderToolContext(
            root=self.workspace if root is None else Path(root),
            task_id=self.task_id, role=LOADER_ROLE,
            task=self.task, package=None, session=self,
        )


@dataclass(frozen=True)
class LoaderToolContext(ToolContext):
    """A `ToolContext` rooted at disposable scratch in production, carrying
    the loader session state (non-identity)."""

    session: Any = field(default=None, compare=False, repr=False)


def _loader_session_of(ctx: Any, tool: str) -> LoaderSession:
    session = getattr(ctx, "session", None)
    if not isinstance(session, LoaderSession):
        raise ToolHarnessFault(tool, code="no_loader_session")
    return session


class ReplaceLoadPlanTool:
    """`replace_load_plan(plan)`: set the session's load plan, in memory;
    `check_load_plan` auto-runs on every write."""

    name = LOADER_REPLACE_TOOL
    description = (
        "Set your load plan: a list of {table, path, format}, one per source "
        "table, with a path relative to the source root and a reader format. The "
        "harness immediately checks it statically (reader validity, path "
        "confinement, table coverage) and returns the result; nothing is executed."
    )
    input_schema = MappingProxyType(dict(_LOAD_PLAN_SCHEMA))
    cost = ToolCost(oracle_bits=0, wall_s=10.0, per_session=1)
    permitted_roles = _LDR
    surface_write = True
    auto_validators = (LOADER_CHECK_TOOL,)

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _loader_session_of(ctx, self.name)
        session.tool_calls += 1
        session.plan = _coerce_load_plan(args.get("plan"))
        return Diagnostic(
            source=DiagnosticSource.TRIAL, ok=True, code="applied",
            flags={"accepted": True},
        )


class CheckLoadPlanTool:
    """`check_load_plan`: STATIC — reader validity, confinement and table
    coverage. Harness-run on every `replace_load_plan`; never on the wire,
    never model-initiated. Executes nothing, returns no count."""

    name = LOADER_CHECK_TOOL
    description = (
        "Harness-run on every load plan: reader validity, path confinement inside "
        "the source root and table coverage. Returns ok or a code with the "
        "offending table. Executes nothing and returns no row count."
    )
    input_schema = MappingProxyType(dict(_LOAD_PLAN_SCHEMA))
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _LDR
    validator = True
    harness_only = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _loader_session_of(ctx, self.name)
        session.tool_calls += 1
        raw = args.get("plan")
        plan = _coerce_load_plan(raw) if raw is not None else session.plan
        code, table = _check_load_plan_static(
            session.task, plan, session.rendered_dev,
            duplicates=_load_plan_duplicates(raw) if raw is not None else (),
        )
        return PJ.project_load_plan(code=code, table=table)


class SubmitLoadPlanTool:
    """`submit_load_plan` (terminal, no arguments): hand the accumulated load
    plan to the UNCHANGED certifier."""

    name = LOADER_SUBMIT_TOOL
    description = (
        "End the session and submit your load plan. No arguments. The certifier "
        "executes it with its own trusted readers on all five populations; you will "
        "not see the result in this session."
    )
    input_schema = _NO_ARGS
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _LDR
    terminal = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _loader_session_of(ctx, self.name)
        session.tool_calls += 1
        session.submitted = True
        return Diagnostic(source=DiagnosticSource.TRIAL, ok=True, code="submitted")


class LoaderAbortTool:
    """`abort(reason_code)` (terminal): the SoT T3 reason enum, no free text."""

    name = LOADER_ABORT_TOOL
    description = abort_tool_wire(LOADER_ABORT_TOOL)["description"]
    input_schema = MappingProxyType(json.loads(json.dumps(dict(ABORT_TOOL_SCHEMA))))
    cost = ToolCost(oracle_bits=0, wall_s=10.0)
    permitted_roles = _LDR
    terminal = True

    def run(self, ctx: Any, args: Mapping[str, Any]) -> Diagnostic:
        session = _loader_session_of(ctx, self.name)
        session.tool_calls += 1
        reason = str(args.get("reason_code", ""))
        if reason not in ABORT_REASON_CODES:
            raise ToolProtocolFault("invalid_arguments", tool=self.name, detail="reason_code")
        session.abort_reason = reason
        return Diagnostic(source=DiagnosticSource.TRIAL, ok=True, code="aborted")


LOADER_TOOLS: tuple[Any, ...] = (
    ReplaceLoadPlanTool(),
    CheckLoadPlanTool(),
    SubmitLoadPlanTool(),
    LoaderAbortTool(),
)
LOADER_TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in LOADER_TOOLS)
#: On the wire: `replace_load_plan`, `submit_load_plan`, `abort` — the static
#: `check_load_plan` validator is harness-run, never model-initiated.
LOADER_WIRE_TOOL_NAMES: tuple[str, ...] = tuple(
    t.name for t in LOADER_TOOLS if not getattr(t, "harness_only", False)
)
LOADER_MODEL_FACING_TOOL_NAMES: tuple[str, ...] = tuple(
    t.name for t in LOADER_TOOLS
    if not getattr(t, "terminal", False) and not getattr(t, "harness_only", False)
)


def loader_tool(name: str) -> Any:
    for tool in LOADER_TOOLS:
        if tool.name == name:
            return tool
    raise KeyError(name)


def loader_registry() -> ToolRegistry:
    """The declared loader registry (ungated): its wire manifest carries the
    two terminals plus `replace_load_plan` (the static check is harness-only)."""
    return ToolRegistry(LOADER_ROLE, LOADER_TOOLS)


def loader_limits(
    block: Mapping[str, Any] | None = None,
    *,
    session_defaults: Mapping[str, Any] | None = None,
    agents_config: Path | str | None = None,
) -> SessionLimits:
    """The loader's `SessionLimits` (mirrors `author_limits`): the declared
    `roles.independent_loader.session` block (None = the `agents_config`
    document's), carrying the session-wide `session:` defaults (un-hashed)."""
    from elt_taskgen.review import providers  # lazy: providers imports the registry

    if block is None:
        block = providers.role_loop_limits(LOADER_ROLE, agents_config=agents_config)
    defaults = dict(session_defaults) if session_defaults else session_defaults_for(agents_config)
    return SessionLimits.from_block(dict(block), session_defaults=defaults)


def loader_policy(
    limits: SessionLimits | None = None,
    *,
    session_salt: int = 0,
    agents_config: Path | str | None = None,
) -> SessionPolicy:
    """The model-driven loader session policy (mirrors `author_policy`): the
    four tools (the harness-only static check included, so the runner can
    auto-run it and the manifest names it), `submit_load_plan` / `abort` as
    terminals, the two terminals plus `replace_load_plan` on the wire."""
    if limits is None:
        limits = SessionLimits.from_block({}, session_defaults=session_defaults_for(agents_config))
    elif not limits.session_defaults:
        limits = limits.with_session_defaults(session_defaults_for(agents_config))
    return SessionPolicy(
        role=LOADER_ROLE,
        tools=LOADER_TOOLS,
        submit_tool=LOADER_SUBMIT_TOOL,
        abort_tool=LOADER_ABORT_TOOL,
        limits=limits,
        mode="model_driven",
        wire_tools=tuple(loader_registry().wire_tools()),
        session_salt=int(session_salt),
    )


def loader_validator_worker() -> InProcessValidatorWorker:
    """The D3 worker for a loader session (mirrors the proposer worker):
    gold-free, no provider handle, keyless. Its surface is the load plan."""
    return InProcessValidatorWorker(
        surface_fingerprint=lambda ctx: _loader_session_of(ctx, "worker").surface_fingerprint(),
        current_draft=lambda ctx: _loader_session_of(ctx, "worker").current_draft(),
    )

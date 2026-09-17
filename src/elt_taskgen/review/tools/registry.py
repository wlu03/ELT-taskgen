"""Register and dispatch bounded tools by role.

Tools run against confined `ToolContext` roots and return projected values rather than
raw validator results. The registry also defines the exact wire manifest and its digest.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from elt_taskgen import workspace as _workspace
from elt_taskgen.models import PopulationName, canonical_json, sha256_hex
from elt_taskgen.review.session import (
    ForbiddenArgument,
    PolicyFault,
    SessionFault,
    ToolHarnessFault,
    ToolNotPermitted,
    ToolProtocolFault,
)
from elt_taskgen.review.tools.projection import DevRows, Diagnostic, DiagnosticText
from elt_taskgen.training.models import _CODE_RE

__all__ = [
    "DENIED_BASENAME_RE",
    "DENIED_PATH_COMPONENTS",
    "Tool",
    "ToolContext",
    "ToolCost",
    "ToolLookupError",
    "ToolPathDenied",
    "ToolRegistry",
    "check_tool_path",
    "path_under_release_root",
    "path_under_runs",
    "permit_refusal",
    "repository_runs_root",
]

#: Path components no tool may name or resolve through: the operator's `runs/`
#: tree (credentials, ledgers, live drives), the private task trees, DuckDB
#: oracles and rendered populations, workspace runtime state, secrets and
#: Terraform state.
DENIED_PATH_COMPONENTS: frozenset[str] = frozenset(
    {
        "runs",
        "answer_key",
        "gold",
        "populations",
        "attack_cases",
        "attacks",
        "private",
        "secrets",
        "oracle",
        "rendered",
        ".workspace-runtime",
        ".git",
        ".terraform",
        "target",
    }
)

#: Basenames no tool may name: credential files, DuckDB files, Terraform state,
#: dbt profiles and env files.
DENIED_BASENAME_RE = re.compile(
    r"(?:_credential\.json|\.duckdb|\.tfstate(?:\.backup)?|profiles\.yml|\.env)$",
    re.IGNORECASE,
)

_RELEASE_MARKER = "release_manifest.json"


class ToolPathDenied(ValueError):
    """A path outside the surface the role may touch.

    Raised for a refused context ROOT (the controller's own error) and for a
    refused tool-supplied path. The Phase 1 runner maps the latter onto
    `review.session.ForbiddenArgument(PolicyFault)` (terminal
    `POLICY_VIOLATION` with a security event); the class itself stays a
    `ValueError` because a bad root is never the model's doing.
    """


class ToolLookupError(LookupError):
    """`dispatch` found no such tool for this role. `kind` is `unknown_tool`
    (no role registers the name), `tool_not_permitted` (another role does) or
    `role_mismatch` (the context was built for another role).

    `as_policy_fault()` is the SoT T6 mapping the Phase 1 runner applies."""

    def __init__(self, kind: str, name: str, role: str) -> None:
        self.kind = str(kind)
        self.name = str(name)
        self.role = str(role)
        super().__init__(f"{self.kind}: tool {self.name!r} for role {self.role!r}")

    def as_policy_fault(self) -> PolicyFault | None:
        """The model-caused fault this refusal is: an unknown name is a
        `ToolProtocolFault` correction, another role's tool is a terminal
        `ToolNotPermitted`. A role mismatch is the controller's own error, not
        the model's, and maps to nothing."""
        if self.kind == "unknown_tool":
            return ToolProtocolFault("unknown_tool", tool=self.name)
        if self.kind == "tool_not_permitted":
            return ToolNotPermitted(self.name)
        return None


@dataclass(frozen=True)
class ToolCost:
    """Declare the limits and accounting for one tool call.

    `oracle_bits` contributes to the session cap; `per_session` limits executions;
    `deadline_s` bounds runtime. Idempotent reads may be exempt from stuck detection,
    and declared no-cost refusal codes must be unique valid diagnostic codes.
    """

    oracle_bits: int = 0
    wall_s: float = 10.0
    per_session: int | None = None
    idempotent_read: bool = False
    idempotent_poll: bool = False
    no_cost_codes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if int(self.oracle_bits) < 0:
            raise ValueError("oracle_bits must be >= 0")
        if float(self.wall_s) <= 0:
            raise ValueError("wall_s must be > 0")
        if self.per_session is not None and int(self.per_session) < 1:
            raise ValueError("per_session must be >= 1 when set")
        codes = frozenset(str(code) for code in (self.no_cost_codes or ()))
        for code in codes:
            if _CODE_RE.fullmatch(code) is None:
                raise ValueError(f"no_cost_codes entry {code!r} must be a lowercase stable code")
        object.__setattr__(self, "no_cost_codes", codes)


def repository_runs_root() -> Path:
    """The repository's own `runs/` directory, resolved: the parent of
    `workspace.DEFAULT_WORKSPACE` under `workspace.repo_root()`. Looked up per
    call so tests can patch `workspace.repo_root` onto a temporary tree."""
    return (_workspace.repo_root() / _workspace.DEFAULT_WORKSPACE.parent).resolve()


def path_under_runs(path: Path) -> bool:
    """Return whether a resolved path is the repository runs tree or a descendant.

    Use component-aware resolution rather than string-prefix matching, and fail closed
    when repository-root discovery is unavailable.
    """
    resolved = Path(path).resolve()
    runs = repository_runs_root()
    return resolved == runs or runs in resolved.parents


def path_under_release_root(path: Path) -> bool:
    """Does `path` lie in a tree that carries `release_manifest.json` (a
    release root, the tree `release`/`export` publish)? The marker is looked
    for on the path itself and on every ancestor."""
    path = Path(path)
    for candidate in (path, *path.parents):
        if (candidate / _RELEASE_MARKER).is_file():
            return True
    return False


_under_runs = path_under_runs
_under_release_root = path_under_release_root


@dataclass(frozen=True)
class ToolContext:
    """Define the confined task context in which a tool runs.

    Reject roots under repository runs, release trees, or credential-shaped files. Paths
    are resolved without permitting denied components, symlink escapes, or another
    role's private surface.
    """

    root: Path
    task_id: str
    role: str
    task: Any = field(default=None, compare=False, repr=False)
    route: Any = field(default=None, compare=False, repr=False)
    package: Any = field(default=None, compare=False, repr=False)

    population: ClassVar[str] = PopulationName.DEVELOPMENT.value

    def __post_init__(self) -> None:
        root = Path(self.root).resolve()
        if not root.is_absolute():  # pragma: no cover - resolve() always absolutizes
            raise ToolPathDenied("tool context root must be absolute")
        if _under_runs(root):
            raise ToolPathDenied(
                "tool context root may not lie under the repository's runs/ "
                "directory (operator drives, ledgers and credentials live there)"
            )
        if DENIED_BASENAME_RE.search(root.name):
            raise ToolPathDenied("tool context root may not be a credential-shaped file")
        if _under_release_root(root):
            raise ToolPathDenied("tool context root may not lie under a release root")
        if not self.task_id or not self.role:
            raise ValueError("tool context needs a task_id and a role")
        object.__setattr__(self, "root", root)

    def resolve(self, candidate: str) -> Path:
        """Resolve one tool-supplied path inside the root, or refuse."""
        return check_tool_path(self, candidate)


def check_tool_path(ctx: ToolContext, candidate: str) -> Path:
    """The tool path policy: relative, confined, never through a denied
    component, never a denied basename, never a symlink escape.

    Resolved-path equality decides confinement, not string prefixes."""
    if not isinstance(candidate, str) or not candidate:
        raise ToolPathDenied("a tool path must be a non-empty string")
    if "\\" in candidate or "\x00" in candidate:
        raise ToolPathDenied("a tool path must use POSIX separators")
    relative = Path(candidate)
    if relative.is_absolute() or candidate.startswith("~"):
        raise ToolPathDenied("a tool path must be relative to the context root")
    parts = relative.parts
    if any(part in ("..", "") for part in parts):
        raise ToolPathDenied("a tool path may not climb out of the context root")
    if any(part in DENIED_PATH_COMPONENTS for part in parts):
        raise ToolPathDenied("a tool path names a denied tree")
    if DENIED_BASENAME_RE.search(relative.name):
        raise ToolPathDenied("a tool path names a denied file")
    resolved = (ctx.root / relative).resolve()
    if resolved != ctx.root and ctx.root not in resolved.parents:
        raise ToolPathDenied("a tool path resolved outside the context root")
    # The root itself was vetted at construction; a symlink may still land the
    # RESOLVED path in a denied subtree of the root, which is what this catches.
    inside = resolved.relative_to(ctx.root).parts
    if any(part in DENIED_PATH_COMPONENTS for part in inside):
        raise ToolPathDenied("a tool path resolved into a denied tree")
    return resolved


@runtime_checkable
class Tool(Protocol):
    """Declare one bounded verb backed by a trusted validator.

    The declaration binds schema, roles, projection, cost, trust domain, wire
    visibility, and terminal/write behavior.
    """

    name: str
    input_schema: Mapping[str, Any]
    cost: ToolCost
    permitted_roles: frozenset[str]

    def run(self, ctx: ToolContext, args: Mapping[str, Any]) -> Diagnostic | DevRows: ...


def permit_refusal(tool: Any, ctx: ToolContext, args: Mapping[str, Any]) -> str:
    """Return a tool's no-cost permit refusal code, or an empty string.

    The code must be declared in the tool cost and is evaluated before worker dispatch
    or oracle-bit charging.
    """
    hook = getattr(tool, "permit", None)
    if not callable(hook):
        return ""
    name = str(getattr(tool, "name", "") or "")
    try:
        code = str(hook(ctx, dict(args)) or "")
    except (SessionFault, PolicyFault):
        raise
    except Exception as exc:  # noqa: BLE001 - the hook crashed: a harness fault, output withheld
        raise ToolHarnessFault.from_exception(name, exc, code="permit_hook_failed") from exc
    if not code:
        return ""
    declared = frozenset(getattr(getattr(tool, "cost", None), "no_cost_codes", ()) or ())
    if code not in declared:
        raise ToolHarnessFault(name, code="undeclared_refusal_code")
    return code


#: role -> tools EXPLICITLY registered (ungated). Empty at baseline for every
#: role; a test patches it to make a role agentic; the Phase 1 proposer tools
#: are DECLARED instead (below) and gated on the role's `session.enabled`.
_ROLE_TOOLS: dict[str, tuple[Tool, ...]] = {}

#: Tool-bearing roles come from the permission matrix, never prompts. Resolve
#: them lazily and keep them equal to `providers.SESSION_RUNNER_ROLES`.
_DECLARED_ROLES: tuple[str, ...] = (
    "repair_proposer",
    "semantic_author",
    "independent_implementer",
    "independent_loader",
)

#: Critic harness validators are never model-facing and register only when the
#: seat session is enabled. Keep them separate from bounded-runner roles.
_DECLARED_VALIDATOR_ROLES: tuple[str, ...] = (
    "population_adversary",
    "shortcut_attacker",
)


def _declared_tools(role: str) -> tuple[Tool, ...]:
    """The tools the permission matrix declares for `role` (Phase 1: the
    repair proposer's eight, the semantic author's five; Phase 2: the DEV/T
    implementer's six, the EL loader's four), ungated."""
    if role == "repair_proposer":
        from elt_taskgen.review.tools import validators  # lazy: it imports this module

        return tuple(validators.PROPOSER_TOOLS)
    if role == "semantic_author":
        from elt_taskgen.review.tools import validators  # lazy: it imports this module

        return tuple(validators.AUTHOR_TOOLS)
    if role == "independent_implementer":
        from elt_taskgen.review.tools import validators  # lazy: it imports this module

        return tuple(validators.IMPLEMENTER_TOOLS)
    if role == "independent_loader":
        from elt_taskgen.review.tools import validators  # lazy: it imports this module

        return tuple(validators.LOADER_TOOLS)
    return ()


def _declared_validators(role: str) -> tuple[Tool, ...]:
    """Return enabled harness validators declared for a critic role.

    Disabled session blocks contribute none. These validators run in the harness and
    never enter the model-facing wire manifest.
    """
    if role not in _DECLARED_VALIDATOR_ROLES:
        return ()
    from elt_taskgen.review.tools import critic_validators  # lazy: it imports this module

    return tuple(critic_validators.declared_validators(role))


def _role_gate(role: str, block: Mapping[str, Any]) -> bool:
    """A role's OWN enablement rule beyond `enabled` (roadmap 1.A rollback
    rule: the author's session runs only with `max_revisions > 0`, so
    `max_revisions: 0` alone keeps its tools off the wire and the one-shot
    author byte-identical). Every other role is `enabled` alone."""
    if role == "semantic_author":
        from elt_taskgen.review.tools import validators  # lazy: it imports this module

        return validators.author_session_enabled(block)
    return True


def _session_enabled(role: str) -> bool:
    """Is `role`'s `session:` block enabled in the loaded agents document
    (and, for a role with its own gate, does that gate open)? False for a
    role without one, and fail-closed on any loader error."""
    try:
        from elt_taskgen.review import providers  # lazy: providers imports this module

        block = providers.role_loop_limits(role)
    except Exception:  # noqa: BLE001 - an unreadable declaration enables nothing
        return False
    if not (isinstance(block, Mapping) and block.get("enabled", False)):
        return False
    return bool(_role_gate(role, block))


def _registered_names() -> frozenset[str]:
    """Every tool name SOME role registers or declares: naming one from
    another role is `tool_not_permitted` (a violation), not `unknown_tool`."""
    names = {tool.name for tools in _ROLE_TOOLS.values() for tool in tools}
    for role in _DECLARED_ROLES:
        names.update(tool.name for tool in _declared_tools(role))
    for role in _DECLARED_VALIDATOR_ROLES:
        names.update(tool.name for tool in _declared_validators(role))
    return frozenset(names)


class ToolRegistry:
    """The tools ONE role may call, with the wire manifest and its digest."""

    def __init__(self, role: str, tools: Sequence[Tool] = ()) -> None:
        if not role or not isinstance(role, str):
            raise ValueError("a registry needs a role name")
        self.role = role
        by_name: dict[str, Tool] = {}
        for tool in tools:
            name = getattr(tool, "name", "")
            if not isinstance(name, str) or _CODE_RE.fullmatch(name) is None:
                raise ValueError(f"tool name {name!r} must be a lowercase stable code")
            if name in by_name:
                raise ValueError(f"tool {name!r} registered twice for role {role!r}")
            if role not in tool.permitted_roles:
                raise ValueError(f"tool {name!r} is not permitted for role {role!r}")
            by_name[name] = tool
        self._tools: dict[str, Tool] = dict(sorted(by_name.items()))

    @classmethod
    def for_role(cls, role: str) -> "ToolRegistry":
        """The registry the controller dispatches through for `role`: its
        explicit registration when one is set, else its DECLARED tools (a
        runner role) or DECLARED harness validators (a critic seat) while
        the role's `session:` block is enabled, else empty."""
        tools = _ROLE_TOOLS.get(role)
        if tools is None and role in _DECLARED_ROLES and _session_enabled(role):
            tools = _declared_tools(role)
        elif tools is None and role in _DECLARED_VALIDATOR_ROLES and _session_enabled(role):
            # A critic seat's harness validators (all `harness_only`): in
            # the registry the runner dispatches through, never on the wire.
            tools = _declared_validators(role)
        return cls(role, tools or ())

    @classmethod
    def declared_for_role(cls, role: str) -> "ToolRegistry":
        """The registry of `role`'s DECLARED tools, ungated: what a bounded
        session (the CLI default for repair proposals) builds its policy from,
        independent of the config's `enabled` flag."""
        tools = _ROLE_TOOLS.get(role)
        return cls(role, tools if tools is not None else _declared_tools(role))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def wire_tools(self) -> list[dict]:
        """The `tools[]` objects the provider payload sends, byte-equal to what
        `AnthropicBackend._payload` builds for a forced tool: name, description,
        strict, input_schema. A `harness_only` tool (a validator the harness
        runs, never the model: the author's `check_prose`) is in the registry
        but never on the wire."""
        out: list[dict] = []
        for name, tool in self._tools.items():
            if bool(getattr(tool, "harness_only", False)):
                continue
            out.append(
                {
                    "name": name,
                    "description": str(getattr(tool, "description", "") or ""),
                    "strict": True,
                    "input_schema": dict(tool.input_schema),
                }
            )
        return out

    def manifest_sha256(self) -> str:
        """sha256 of the canonical wire manifest (`tools_sha256`)."""
        return sha256_hex(canonical_json({"role": self.role, "tools": self.wire_tools()}))

    def lookup(self, ctx: ToolContext, name: str) -> Tool:
        """The PERMIT half of `dispatch`: the tool `name` resolves to for this
        role, WITHOUT running it. Raises `ToolLookupError` (`role_mismatch`,
        `tool_not_permitted` for a name another role registers, `unknown_tool`
        for a name no role registers), so the runner can decide correction
        versus violation before any worker is spawned."""
        if ctx.role != self.role:
            raise ToolLookupError("role_mismatch", name, self.role)
        tool = self._tools.get(name)
        if tool is None:
            kind = "tool_not_permitted" if name in _registered_names() else "unknown_tool"
            raise ToolLookupError(kind, name, self.role)
        return tool

    def dispatch(
        self, ctx: ToolContext, name: str, args: Mapping[str, Any]
    ) -> Diagnostic | DevRows | DiagnosticText:
        """Run one permitted tool through its declared validator and projector.

        Unknown or unauthorized tools raise `ToolLookupError`. Validator crashes become
        text-free harness faults; raw results are never returned directly.
        """
        tool = self.lookup(ctx, name)
        if not isinstance(args, Mapping):
            raise TypeError("tool arguments must be a mapping")
        try:
            result = tool.run(ctx, dict(args))
        except (SessionFault, PolicyFault):
            raise  # already typed by the boundary that caught it
        except ToolPathDenied as exc:
            # The path came from the model's arguments (the context ROOT was
            # vetted at construction): a violation, and the message — which
            # may echo the path — stays with the traceback.
            raise ForbiddenArgument(tool=name, detail="tool path denied") from exc
        except Exception as exc:  # noqa: BLE001 - the validator crashed: its output is withheld
            # The fault names the exception CLASS only; its text (DuckDB
            # errors, paths, values) stays with the traceback and never
            # reaches a model.
            raise ToolHarnessFault.from_exception(name, exc) from exc
        if not isinstance(result, (Diagnostic, DevRows, DiagnosticText)):
            raise ToolHarnessFault(
                name, code="non_projection_result", cause_type=type(result).__name__
            )
        return result

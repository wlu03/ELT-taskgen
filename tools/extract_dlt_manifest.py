#!/usr/bin/env python
"""Vendored dlt connector -> curator-reviewable endpoint-graph manifest (AST only).

WHY THIS EXISTS
The dlt pool vendors all 29 officially listed verified-source connectors at one
pinned commit. Only a reviewed subset is task-admissible; every connector's
real content is a
Python `@dlt.source` / `@dlt.resource` graph — the endpoint names, primary keys,
write dispositions, parent (transformer) edges and incremental cursors that make
an ELT task's Extract+Load half non-trivial. `adapters/dlt.py` consumes a
DECLARATIVE manifest, so something has to turn connector code into that
manifest, and there are exactly two ways to do it:

  * import the connector and introspect the DltSource objects — which requires
    `dlt` plus every connector's own dependencies (pyairtable, google-analytics,
    pendulum, ...) installed, and EXECUTES vendored third-party module bodies
    inside our process. Two of these connectors call an API at source-build
    time. We do not import them.
  * read the code as data. This module parses each connector with `ast` and
    NEVER imports it: no third-party dependency, no code execution, no network,
    and it works on a connector whose module body raises.

The output is a YAML per connector under `config/dlt_connectors/`, committed and
reviewable by a human before it is ever ingested. The extractor is deliberately
HONEST rather than complete: anything it cannot statically resolve (a resource
whose name is an Airtable table fetched at runtime, a cursor field read from a
config query) is written into an `unresolved:` block instead of being guessed,
and connectors whose whole resource set is runtime-defined come out with zero
endpoints so `adapters/dlt.py` fails closed on them.

WHAT IS STATICALLY RESOLVED
  decorator form   @dlt.source/@dlt.resource/@dlt.transformer(...)  (bare or called)
  factory form     dlt.resource(fn, name=..., ...)  /  dlt.transformer(...)
  loop expansion   `for e in DEFAULT_ENDPOINTS: ... name=e` and
                   `for k, v in RECENTS_ENTITIES.items(): ... name=v`, including
                   f-string names (`name=f"jobs_{sub}"`), where the iterable is a
                   module constant of this connector (imports of `.settings`
                   are followed by PATH, not by import)
  parent edges     `@dlt.transformer(data_from=other_resource)` and the pipe
                   form `parent | dlt.transformer(...)`, where the left side is
                   a resource function, a variable bound to one, or a
                   `dict["name"]` of resources (dlt connectors key those dicts
                   by resource name)
  cursors          `dlt.sources.incremental("<field>", ...)` in the resource
                   function's parameter defaults or at its call site
  auth/config      parameters defaulted to `dlt.secrets.value` / `dlt.config.value`
  pagination       per-resource: pagination tokens in the resource body and in
                   connector-local functions it calls
  paths            literal endpoint/url/path/resource arguments of the client
                   call inside the resource body; else inferred as `/<name>`

DELIBERATE HEURISTICS (recorded per endpoint so a curator can overrule them)
  * a resource declaring `write_disposition="merge"` with no statically
    resolvable key is written as `append` + a note: a keyless merge has no
    defined dedup key, and inventing one would fabricate task semantics.
  * a conditional write disposition (`"append" if x is None else "merge"`) is
    written as the first branch — the value taken with default arguments — plus
    a note naming the other branch.
  * `selected=False` resources are kept in the manifest (they are part of the
    graph) but the adapter does not turn them into loaded warehouse tables.

KNOWN LIMITATIONS (each shows up as an empty/incomplete manifest, never as a
guess): `async def` resources are not walked (no vendored connector uses one);
dlt's declarative `rest_api_resources(config)` sources are not decoded, so a
connector built that way (pipedrive's v2 source) contributes no endpoints and
loses to its sibling source; a resource whose name is a runtime value is
recorded as `unresolved` even when its shape is otherwise fully readable.

CONTAMINATION: the tool refuses to emit a task manifest for a connector whose
canonical family name is on a benchmark deny list in
verification/contamination.py (the one place those lists live). Contaminated
connectors remain vendored for provenance and extraction-mechanics audits, but
must never enter `config/dlt_connectors/` or become tasks.

DETERMINISM: pure AST + sorted traversal. No clock, no RNG, no network, no
absolute paths in the emitted YAML (paths are relative to the pool root), so
re-running the tool on the same pinned commit reproduces the files byte for byte.

USAGE
    python tools/extract_dlt_manifest.py --all
    python tools/extract_dlt_manifest.py --connector dlt_freshdesk
    python tools/extract_dlt_manifest.py --connector dlt_freshdesk --include-excluded
    python tools/extract_dlt_manifest.py --all --out config/dlt_connectors
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import yaml

from elt_taskgen.catalog import PoolSource, load_source_catalog
from elt_taskgen.models import slugify_family
from elt_taskgen.verification import contamination as cont

#: Bumped whenever the extraction semantics change; recorded in every manifest
#: so a stale manifest is recognizable without diffing it against the source.
#: "3": `if`-guards are recorded (Endpoint.guard + a note naming the source
#: parameter's default) and duplicate resource names are noted instead of
#: silently dropped.
EXTRACTOR_VERSION = "3"

#: Substrings that mark a resource body as paginating (case-insensitive match
#: against identifiers, attribute names, keyword names and string literals).
PAGINATION_TOKENS: tuple[str, ...] = (
    "paginat",
    "per_page",
    "page_size",
    "rows_per_page",
    "items_per_page",
    "next_page",
    "next_cursor",
    "has_more",
    "offset",
    "get_pages",
    "starting_after",
)

#: dlt `columns={"c": {"data_type": ...}}` spellings -> manifest type tokens
#: (adapters/dlt.py maps these onto models.ColumnType).
_DATA_TYPE_TOKENS: frozenset[str] = frozenset(
    {
        "text", "bigint", "double", "bool", "timestamp", "date", "time",
        "decimal", "wei", "json", "complex", "binary",
    }
)

_MAX_LOOP_EXPANSION = 64


# ---------------------------------------------------------------------------
# Extraction result types (plain dataclasses: this is a tool, not the IR)
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    name: str
    kind: str                      # "resource" | "transformer"
    file: str
    line: int
    func: str = ""
    primary_key: tuple[str, ...] = ()
    cursor: str | None = None
    write_disposition: str = "append"
    declared_write_disposition: str = ""
    selected: bool = True
    parent: str | None = None
    path: str = ""
    path_source: str = "inferred"  # "literal" | "resolved" | "inferred"
    paginated: bool = False
    column_hints: dict[str, str] = field(default_factory=dict)
    #: The `if` condition(s) the definition sits under, joined with " and ";
    #: "" when unconditional. Descriptive (`ast.unparse` of the test), never
    #: evaluated: workable's jobs_*/candidates_* transformers carry
    #: `load_details`, whose source-parameter default is False.
    guard: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class SourceFn:
    name: str
    func: str
    file: str
    line: int
    #: identifiers referenced anywhere in the source body (used to attribute
    #: module-level resources to the source that yields them).
    referenced: set[str] = field(default_factory=set)
    #: resource names created lexically inside this source function.
    owned: list[str] = field(default_factory=list)


@dataclass
class Unresolved:
    file: str
    line: int
    form: str
    reason: str


@dataclass
class ConnectorExtract:
    record: str
    endpoints: list[Endpoint]
    sources: list[SourceFn]
    primary_source: str
    secrets: list[str]
    config: list[str]
    pagination_hints: list[str]
    unresolved: list[Unresolved]
    files: list[str]
    notes: list[str]


class ExtractionError(RuntimeError):
    """The connector could not be read at all (unparseable / no source dir)."""


# ---------------------------------------------------------------------------
# Static value resolution
# ---------------------------------------------------------------------------

_UNKNOWN = object()


class Scope:
    """Lexical scope: literal bindings, function defs, resource-valued names."""

    def __init__(self, parent: "Scope | None" = None):
        self.parent = parent
        self.vars: dict[str, Any] = {}
        self.funcs: dict[str, ast.FunctionDef] = {}
        #: variable name -> resource name (a name bound to a dlt resource)
        self.resource_vars: dict[str, str] = {}
        #: dict variable -> {key: resource name} (`resources["jobs"] = ...`)
        self.resource_dicts: dict[str, dict[str, str]] = {}
        #: `if` guards currently OPEN in this scope, innermost last, as
        #: (condition text, curator note or ""). Pushed/popped by `_walk_stmt`
        #: around an `ast.If` body/orelse; child scopes see their parents'
        #: guards through `effective_guards`.
        self.guards: list[tuple[str, str]] = []
        #: parameter name -> literal default of the enclosing @dlt.source
        #: function (only for parameters with a literal default). Lets a bare
        #: `if <param>:` guard say what a default-argument run does.
        self.source_params: dict[str, Any] = {}

    def child(self) -> "Scope":
        return Scope(self)

    def _chain(self) -> Iterator["Scope"]:
        node: Scope | None = self
        while node is not None:
            yield node
            node = node.parent

    def effective_guards(self) -> list[tuple[str, str]]:
        """Every open guard from the outermost scope in, in source order."""
        chain = list(self._chain())
        out: list[tuple[str, str]] = []
        for scope in reversed(chain):
            out.extend(scope.guards)
        return out

    def lookup_source_param(self, name: str) -> Any:
        for s in self._chain():
            if name in s.source_params:
                return s.source_params[name]
        return _UNKNOWN

    def lookup_var(self, name: str) -> Any:
        for s in self._chain():
            if name in s.vars:
                return s.vars[name]
        return _UNKNOWN

    def lookup_func(self, name: str) -> ast.FunctionDef | None:
        for s in self._chain():
            if name in s.funcs:
                return s.funcs[name]
        return None

    def lookup_resource_var(self, name: str) -> str | None:
        for s in self._chain():
            if name in s.resource_vars:
                return s.resource_vars[name]
        return None

    def lookup_resource_dict(self, name: str) -> dict[str, str] | None:
        for s in self._chain():
            if name in s.resource_dicts:
                return s.resource_dicts[name]
        return None


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        return _UNKNOWN


def _literal_parameter_defaults(fn: ast.FunctionDef) -> dict[str, Any]:
    """Parameter name -> literal default, for the parameters that have one."""
    args = fn.args
    out: dict[str, Any] = {}
    positional = list(args.posonlyargs) + list(args.args)
    pad = len(positional) - len(args.defaults)
    for i, arg in enumerate(positional):
        if i >= pad:
            value = _literal(args.defaults[i - pad])
            if value is not _UNKNOWN:
                out[arg.arg] = value
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if default is not None:
            value = _literal(default)
            if value is not _UNKNOWN:
                out[arg.arg] = value
    return out


def _dlt_kind(node: ast.AST) -> str | None:
    """'source'|'resource'|'transformer' if `node` is that dlt attribute."""
    if isinstance(node, ast.Attribute) and node.attr in {
        "source", "resource", "transformer"
    }:
        base = node.value
        if isinstance(base, ast.Name) and base.id == "dlt":
            return node.attr
    return None


def _is_dlt_value(node: ast.AST, kind: str) -> bool:
    """True for `dlt.secrets.value` / `dlt.config.value`."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "value"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == kind
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "dlt"
    )


def _incremental_call(node: ast.AST) -> ast.Call | None:
    """`dlt.sources.incremental(...)` call node, if that is what `node` is."""
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    if isinstance(fn, ast.Attribute) and fn.attr == "incremental":
        inner = fn.value
        if (
            isinstance(inner, ast.Attribute)
            and inner.attr == "sources"
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "dlt"
        ):
            return node
    return None


class Resolver:
    """Resolves AST expressions to python values using module + local scopes."""

    def __init__(self, consts: dict[str, Any]):
        self.consts = consts

    def value(self, node: ast.AST | None, scope: Scope) -> Any:
        if node is None:
            return _UNKNOWN
        lit = _literal(node)
        if lit is not _UNKNOWN:
            return lit
        if isinstance(node, ast.Name):
            v = scope.lookup_var(node.id)
            if v is not _UNKNOWN:
                return v
            return self.consts.get(node.id, _UNKNOWN)
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
                elif isinstance(piece, ast.FormattedValue):
                    inner = self.value(piece.value, scope)
                    if isinstance(inner, (str, int)):
                        parts.append(str(inner))
                    else:
                        return _UNKNOWN
                else:
                    return _UNKNOWN
            return "".join(parts)
        if isinstance(node, ast.Subscript):
            base = self.value(node.value, scope)
            key = self.value(node.slice, scope)
            if base is _UNKNOWN or key is _UNKNOWN:
                return _UNKNOWN
            try:
                return base[key]  # type: ignore[index]
            except Exception:
                return _UNKNOWN
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            # `endpoints = endpoints or DEFAULT_ENDPOINTS`: the fallback is the
            # value a default-argument run actually uses.
            for operand in reversed(node.values):
                v = self.value(operand, scope)
                if v is not _UNKNOWN:
                    return v
            return _UNKNOWN
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr in {"items", "values", "keys"}:
                base = self.value(fn.value, scope)
                if isinstance(base, dict):
                    if fn.attr == "items":
                        return [tuple(kv) for kv in base.items()]
                    if fn.attr == "values":
                        return list(base.values())
                    return list(base.keys())
            if isinstance(fn, ast.Name) and fn.id in {"list", "tuple", "sorted"}:
                if node.args:
                    base = self.value(node.args[0], scope)
                    if isinstance(base, (list, tuple, dict)):
                        seq = list(base)
                        return sorted(seq) if fn.id == "sorted" else seq
        return _UNKNOWN

    def string(self, node: ast.AST | None, scope: Scope) -> str | None:
        v = self.value(node, scope)
        return v if isinstance(v, str) and v else None

    def sequence(self, node: ast.AST | None, scope: Scope) -> list[Any] | None:
        v = self.value(node, scope)
        if isinstance(v, (list, tuple)):
            return list(v)
        if isinstance(v, dict):
            return list(v.keys())
        return None

    def name_tuple(self, node: ast.AST | None, scope: Scope) -> tuple[str, ...] | None:
        """Resolve a primary_key-ish argument to a tuple of column names."""
        v = self.value(node, scope)
        if isinstance(v, str):
            return (v,)
        if isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v):
            return tuple(v)
        return None


# ---------------------------------------------------------------------------
# Module constant tables (imports followed by PATH, never executed)
# ---------------------------------------------------------------------------

def _contains_dlt_factory(body: Sequence[ast.stmt]) -> bool:
    """True if a `dlt.resource/transformer/source` construct appears in `body`."""
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and _dlt_kind(node.func) is not None:
                return True
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if _dlt_kind(dec) is not None:
                        return True
                    if isinstance(dec, ast.Call) and _dlt_kind(dec.func) is not None:
                        return True
    return False


def _module_consts(tree: ast.Module) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for t in targets:
            if isinstance(t, ast.Name):
                v = _literal(value) if value is not None else _UNKNOWN
                if v is not _UNKNOWN:
                    out[t.id] = v
    return out


def _relative_module_files(rel_file: str, level: int, module: str | None) -> list[str]:
    """Candidate files (relative to the source dir) a `from . import` names."""
    # `from .settings import X` in `pkg/__init__.py` names `pkg/settings.py`;
    # each extra dot climbs one directory.
    base = list(Path(rel_file).parts[:-1])
    for _ in range(max(level - 1, 0)):
        if base:
            base.pop()
    tail = module.split(".") if module else []
    stem = base + tail
    return [
        "/".join(stem) + ".py",
        "/".join(stem + ["__init__.py"]),
    ]


def _link_imports(
    trees: dict[str, ast.Module], consts: dict[str, dict[str, Any]]
) -> None:
    """Copy imported module constants into the importing file's const table."""
    for _ in range(2):  # two rounds settle `a imports b imports c` chains
        for rel, tree in sorted(trees.items()):
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level == 0:
                    continue
                for cand in _relative_module_files(rel, node.level, node.module):
                    src = consts.get(cand)
                    if not src:
                        continue
                    for alias in node.names:
                        if alias.name in src:
                            consts.setdefault(rel, {})[alias.asname or alias.name] = (
                                src[alias.name]
                            )
                    break


# ---------------------------------------------------------------------------
# The per-file walker
# ---------------------------------------------------------------------------

class _FileWalker:
    """Walks one connector file, emitting endpoints/sources/unresolved rows."""

    def __init__(self, ctx: "_ConnectorWalker", rel: str, tree: ast.Module):
        self.ctx = ctx
        self.rel = rel
        self.tree = tree
        self.resolver = Resolver(ctx.consts.get(rel, {}))

    # -- entry ------------------------------------------------------------

    def run(self) -> None:
        scope = Scope()
        self._collect_defs(self.tree.body, scope)
        self._walk_body(self.tree.body, scope, source=None)

    # -- helpers ----------------------------------------------------------

    def _collect_defs(self, body: Sequence[ast.stmt], scope: Scope) -> None:
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef):
                scope.funcs[stmt.name] = stmt

    def _note_unresolved(self, node: ast.AST, form: str, reason: str) -> None:
        self.ctx.unresolved.append(
            Unresolved(
                file=self.rel,
                line=getattr(node, "lineno", 0),
                form=form,
                reason=reason,
            )
        )

    # -- statement walk ---------------------------------------------------

    def _walk_body(
        self, body: Sequence[ast.stmt], scope: Scope, source: SourceFn | None
    ) -> None:
        self._collect_defs(body, scope)
        for stmt in body:
            self._walk_stmt(stmt, scope, source)

    def _walk_stmt(self, stmt: ast.stmt, scope: Scope, source: SourceFn | None) -> None:
        if isinstance(stmt, ast.FunctionDef):
            self._walk_funcdef(stmt, scope, source)
            return
        if isinstance(stmt, ast.For):
            self._walk_for(stmt, scope, source)
            return
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            self._walk_assign(stmt, scope, source)
            return
        if isinstance(stmt, ast.If):
            self._walk_if(stmt, scope, source)
            return
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.stmt):
                self._walk_stmt(child, scope, source)
            else:
                self._walk_expr(child, scope, source)

    def _walk_if(self, stmt: ast.If, scope: Scope, source: SourceFn | None) -> None:
        """Walk both branches with the condition RECORDED as an open guard.

        The SAME scope is used (guards are pushed and popped around each
        branch) so name binding is exactly what the generic recursion did
        before guards were tracked — only the annotation is new. A resource
        emitted inside the body carries `guard`; when the test is a bare
        Name that is a parameter of the enclosing @dlt.source with a literal
        default, the note says whether a default-argument run loads it.
        """
        self._walk_expr(stmt.test, scope, source)
        guard = ast.unparse(stmt.test)
        note = ""
        default = (
            scope.lookup_source_param(stmt.test.id)
            if isinstance(stmt.test, ast.Name)
            else _UNKNOWN
        )
        if default is not _UNKNOWN:
            note = (
                f"resource is emitted only under `if {guard}`; source parameter "
                f"default is {default!r}, so a default-argument run "
                f"{'loads' if default else 'does not load'} it"
            )
        scope.guards.append((guard, note))
        try:
            self._walk_body(stmt.body, scope, source)
        finally:
            scope.guards.pop()
        if stmt.orelse:
            scope.guards.append((f"not ({guard})", ""))
            try:
                self._walk_body(stmt.orelse, scope, source)
            finally:
                scope.guards.pop()

    def _walk_funcdef(
        self, fn: ast.FunctionDef, scope: Scope, source: SourceFn | None
    ) -> None:
        scope.funcs[fn.name] = fn
        kind, call = self.ctx.dlt_decoration(fn)
        inner_source = source
        if kind == "source":
            inner_source = self._emit_source(fn, call, scope)
        elif kind in {"resource", "transformer"}:
            self._emit_endpoint(
                fn=fn,
                factory=call,
                kind=kind,
                scope=scope,
                source=source,
                extra_calls=(),
                node=fn,
            )
        body_scope = scope.child()
        if kind == "source":
            body_scope.source_params = _literal_parameter_defaults(fn)
        self._walk_body(fn.body, body_scope, inner_source)

    def _walk_for(self, stmt: ast.For, scope: Scope, source: SourceFn | None) -> None:
        values = self.resolver.sequence(stmt.iter, scope)
        if values is None or not values or len(values) > _MAX_LOOP_EXPANSION:
            # Only a loop that actually BUILDS resources is worth a curator's
            # attention; every connector also loops over response pages.
            if values is None and _contains_dlt_factory(stmt.body):
                self._note_unresolved(
                    stmt,
                    "for-loop",
                    "loop iterable is a runtime value; resources created inside "
                    "it cannot be named statically",
                )
            body_scope = scope.child()
            self._bind_target(stmt.target, _UNKNOWN, body_scope)
            self._walk_body(stmt.body, body_scope, source)
            self._walk_body(stmt.orelse, scope.child(), source)
            return
        for value in values:
            body_scope = scope.child()
            self._bind_target(stmt.target, value, body_scope)
            self._walk_body(stmt.body, body_scope, source)
        self._walk_body(stmt.orelse, scope.child(), source)

    def _bind_target(self, target: ast.expr, value: Any, scope: Scope) -> None:
        if isinstance(target, ast.Name):
            if value is not _UNKNOWN:
                scope.vars[target.id] = value
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            items = list(value) if isinstance(value, (list, tuple)) else None
            for i, elt in enumerate(target.elts):
                self._bind_target(
                    elt, items[i] if items is not None and i < len(items) else _UNKNOWN,
                    scope,
                )

    def _walk_assign(
        self, stmt: ast.Assign | ast.AnnAssign, scope: Scope, source: SourceFn | None
    ) -> None:
        value = stmt.value
        targets = list(stmt.targets) if isinstance(stmt, ast.Assign) else [stmt.target]
        if value is not None:
            self._walk_expr(value, scope, source)
        produced = self._resource_name_of(value, scope) if value is not None else None
        for t in targets:
            if isinstance(t, ast.Name):
                lit = self.resolver.value(value, scope) if value is not None else _UNKNOWN
                if lit is not _UNKNOWN:
                    scope.vars[t.id] = lit
                if produced:
                    scope.resource_vars[t.id] = produced
            elif isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                key = self.resolver.string(t.slice, scope)
                if key and produced:
                    scope.resource_dicts.setdefault(t.value.id, {})[key] = produced

    # -- expression walk --------------------------------------------------

    def _walk_expr(self, node: ast.AST, scope: Scope, source: SourceFn | None) -> None:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            if self._handle_pipe(node, scope, source):
                return
        if isinstance(node, ast.Call):
            factory, extra = self._unwrap_factory(node)
            if factory is not None:
                kind = _dlt_kind(factory.func)
                if kind in {"resource", "transformer"}:
                    self._emit_factory_endpoint(
                        factory, extra, kind, scope, source, parent=None
                    )
                    for call in (factory, *extra):
                        for arg in list(call.args) + [k.value for k in call.keywords]:
                            self._walk_expr(arg, scope, source)
                    return
        if isinstance(node, ast.Name) and source is not None:
            source.referenced.add(node.id)
        if isinstance(node, ast.stmt):
            self._walk_stmt(node, scope, source)
            return
        for child in ast.iter_child_nodes(node):
            self._walk_expr(child, scope, source)

    def _unwrap_factory(self, node: ast.Call) -> tuple[ast.Call | None, tuple[ast.Call, ...]]:
        """`dlt.resource(...)(fn)(args)` -> (the dlt.resource call, outer calls)."""
        chain: list[ast.Call] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Call):
            if _dlt_kind(cur.func) in {"resource", "transformer", "source"}:
                return cur, tuple(reversed(chain))
            chain.append(cur)
            cur = cur.func
        return None, ()

    def _handle_pipe(
        self, node: ast.BinOp, scope: Scope, source: SourceFn | None
    ) -> bool:
        """`parent | dlt.transformer(...)` — the left side names the parent."""
        right = node.right
        factory: ast.Call | None = None
        extra: tuple[ast.Call, ...] = ()
        if isinstance(right, ast.Call):
            factory, extra = self._unwrap_factory(right)
        parent = self._resource_name_of(node.left, scope)
        if factory is None:
            # `visits | get_unique_visitors(...)`: the right side is a decorated
            # transformer already recorded via its own decorator.
            self._walk_expr(node.left, scope, source)
            self._walk_expr(node.right, scope, source)
            return True
        kind = _dlt_kind(factory.func)
        if kind not in {"resource", "transformer"}:
            return False
        if parent is None:
            self._note_unresolved(
                node,
                "pipe transformer",
                "parent of a piped transformer is a runtime value",
            )
        self._emit_factory_endpoint(factory, extra, kind, scope, source, parent=parent)
        self._walk_expr(node.left, scope, source)
        return True

    # -- resource-name resolution ----------------------------------------

    def _resource_name_of(self, node: ast.AST | None, scope: Scope) -> str | None:
        """The resource NAME an expression evaluates to, when knowable."""
        if node is None:
            return None
        if isinstance(node, ast.Name):
            via_var = scope.lookup_resource_var(node.id)
            if via_var:
                return via_var
            return self.ctx.resource_name_of_func(node.id)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            table = scope.lookup_resource_dict(node.value.id)
            key = self.resolver.string(node.slice, scope)
            if key is not None:
                if table and key in table:
                    return table[key]
                # dlt connectors key their resource dicts BY resource name;
                # accept the literal key as the name when the dict itself was
                # not tracked (e.g. built in a loop we could not expand).
                return key
            return None
        if isinstance(node, ast.Call):
            factory, _ = self._unwrap_factory(node)
            if factory is not None:
                nm = self.resolver.string(self.ctx.kwarg(factory, "name"), scope)
                if nm:
                    return nm
                if factory.args:
                    return self._resource_name_of(factory.args[0], scope)
                return None
            return self._resource_name_of(node.func, scope)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._resource_name_of(node.right, scope)
        return None

    # -- emission ---------------------------------------------------------

    def _emit_source(
        self, fn: ast.FunctionDef, call: ast.Call | None, scope: Scope
    ) -> SourceFn:
        name = None
        if call is not None:
            name = self.resolver.string(self.ctx.kwarg(call, "name"), scope)
        src = SourceFn(
            name=name or fn.name,
            func=fn.name,
            file=self.rel,
            line=fn.lineno,
        )
        self.ctx.sources.append(src)
        self.ctx.collect_credentials(fn)
        return src

    def _emit_factory_endpoint(
        self,
        factory: ast.Call,
        extra: tuple[ast.Call, ...],
        kind: str,
        scope: Scope,
        source: SourceFn | None,
        *,
        parent: str | None,
    ) -> None:
        target_fn: ast.FunctionDef | None = None
        if factory.args and isinstance(factory.args[0], ast.Name):
            target_fn = scope.lookup_func(factory.args[0].id) or self.ctx.func_by_name(
                factory.args[0].id
            )
        for call in extra:
            if call.args and isinstance(call.args[0], ast.Name) and target_fn is None:
                target_fn = scope.lookup_func(call.args[0].id) or self.ctx.func_by_name(
                    call.args[0].id
                )
        self._emit_endpoint(
            fn=target_fn,
            factory=factory,
            kind=kind,
            scope=scope,
            source=source,
            extra_calls=extra,
            node=factory,
            parent=parent,
        )

    def _emit_endpoint(
        self,
        *,
        fn: ast.FunctionDef | None,
        factory: ast.Call | None,
        kind: str,
        scope: Scope,
        source: SourceFn | None,
        extra_calls: tuple[ast.Call, ...],
        node: ast.AST,
        parent: str | None = None,
    ) -> None:
        notes: list[str] = []
        name = None
        if factory is not None:
            name = self.resolver.string(self.ctx.kwarg(factory, "name"), scope)
        if name is None and factory is not None and self.ctx.kwarg(factory, "name") is not None:
            self._note_unresolved(
                node,
                f"dlt.{kind}(name=...)",
                "resource name is a runtime value (config/API-defined)",
            )
            return
        if name is None:
            if fn is None:
                self._note_unresolved(
                    node, f"dlt.{kind}(...)", "resource has neither a literal name nor a named function"
                )
                return
            name = fn.name

        # primary key
        pk: tuple[str, ...] = ()
        if factory is not None:
            pk_node = self.ctx.kwarg(factory, "primary_key")
            if pk_node is not None:
                resolved = self.resolver.name_tuple(pk_node, scope)
                if resolved is None:
                    notes.append("primary_key is a runtime value; recorded as empty")
                else:
                    pk = resolved

        # write disposition
        declared = ""
        disposition = "append"
        if factory is not None:
            wd_node = self.ctx.kwarg(factory, "write_disposition")
            if wd_node is not None:
                wd = self.resolver.string(wd_node, scope)
                if wd is not None:
                    declared = wd
                    disposition = wd
                elif isinstance(wd_node, ast.IfExp):
                    branches = [
                        self.resolver.string(wd_node.body, scope),
                        self.resolver.string(wd_node.orelse, scope),
                    ]
                    if branches[0]:
                        declared = "|".join(b for b in branches if b)
                        disposition = branches[0]
                        notes.append(
                            "write_disposition is conditional "
                            f"({declared}); recorded as the default-argument "
                            f"branch {branches[0]!r}"
                        )
                else:
                    resolved_var = self._resolve_conditional_var(wd_node, scope)
                    if resolved_var:
                        declared = "|".join(resolved_var)
                        disposition = resolved_var[0]
                        notes.append(
                            "write_disposition is a conditional variable "
                            f"({declared}); recorded as the default-argument "
                            f"branch {resolved_var[0]!r}"
                        )
                    else:
                        notes.append(
                            "write_disposition is a runtime value; recorded as 'append'"
                        )

        # selected
        selected = True
        if factory is not None:
            sel = self.ctx.kwarg(factory, "selected")
            if sel is not None:
                v = self.resolver.value(sel, scope)
                if isinstance(v, bool):
                    selected = v

        # column type hints from columns={...}
        hints: dict[str, str] = {}
        if factory is not None:
            cols = self.ctx.kwarg(factory, "columns")
            v = self.resolver.value(cols, scope) if cols is not None else _UNKNOWN
            if isinstance(v, dict):
                for col, spec in sorted(v.items()):
                    if isinstance(col, str) and isinstance(spec, dict):
                        dt = spec.get("data_type")
                        if isinstance(dt, str) and dt in _DATA_TYPE_TOKENS:
                            hints[col] = dt

        # parent edge: data_from=... beats a pipe
        if factory is not None:
            data_from = self.ctx.kwarg(factory, "data_from")
            if data_from is not None:
                resolved_parent = self._resource_name_of(data_from, scope)
                if resolved_parent:
                    parent = resolved_parent
                elif parent is None:
                    notes.append("data_from is a runtime value; parent unresolved")

        # incremental cursor: parameter defaults of the resource function, and
        # the call site that invokes the factory-produced resource.
        cursor = None
        if fn is not None:
            cursor = self._cursor_from_defaults(fn, scope)
        if cursor is None:
            for call in extra_calls:
                cursor = self._cursor_from_call(call, scope)
                if cursor is not None:
                    break
        if cursor is None and factory is not None:
            cursor = self._cursor_from_call(factory, scope)

        paginated, path, path_source = self._body_signals(fn, scope, name)

        if disposition == "merge" and not pk:
            notes.append(
                "declared write_disposition='merge' with no statically resolvable "
                "primary key; recorded as 'append' because a keyless merge has no "
                "defined dedup key — supply the key to restore merge semantics"
            )
            if not declared:
                declared = "merge"
            disposition = "append"

        guards = scope.effective_guards()
        notes.extend(n for _g, n in guards if n)
        ep = Endpoint(
            name=name,
            kind=kind,
            file=self.rel,
            line=getattr(node, "lineno", 0),
            func=fn.name if fn is not None else "",
            primary_key=pk,
            cursor=cursor,
            write_disposition=disposition,
            declared_write_disposition=declared or disposition,
            selected=selected,
            parent=parent,
            path=path,
            path_source=path_source,
            paginated=paginated,
            column_hints=hints,
            guard=" and ".join(g for g, _n in guards),
            notes=notes,
        )
        self.ctx.add_endpoint(ep, source)
        if fn is not None:
            self.ctx.register_resource_func(fn.name, name)
            self.ctx.collect_credentials(fn)

    def _resolve_conditional_var(self, node: ast.AST, scope: Scope) -> list[str] | None:
        """`write_disposition` bound to an `X if c else Y` earlier in the body."""
        if not isinstance(node, ast.Name):
            return None
        for assign in self.ctx.conditional_str_assignments.get(node.id, []):
            branches = [b for b in assign if b]
            if branches:
                return branches
        return None

    # -- body signals (pagination + path) ---------------------------------

    def _body_signals(
        self, fn: ast.FunctionDef | None, scope: Scope, name: str
    ) -> tuple[bool, str, str]:
        paginated = False
        path: str | None = None
        path_source = "inferred"
        bodies: list[ast.FunctionDef] = []
        if fn is not None:
            bodies.append(fn)
            for called in self._called_function_names(fn):
                target = scope.lookup_func(called) or self.ctx.func_by_name(called)
                if target is not None and target is not fn:
                    bodies.append(target)
        for body in bodies:
            if not paginated and self._has_pagination(body):
                paginated = True
            if path is None:
                found = self._path_literal(body, scope)
                if found is not None:
                    path, path_source = found
        if path is None:
            path = "/" + name
        return paginated, path, path_source

    def _called_function_names(self, fn: ast.FunctionDef) -> list[str]:
        out: list[str] = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                out.append(node.func.id)
        return sorted(set(out))

    def _has_pagination(self, fn: ast.FunctionDef) -> bool:
        for node in ast.walk(fn):
            token = None
            if isinstance(node, ast.Name):
                token = node.id
            elif isinstance(node, ast.Attribute):
                token = node.attr
            elif isinstance(node, ast.keyword):
                token = node.arg
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                token = node.value
            if token and any(t in token.lower() for t in PAGINATION_TOKENS):
                return True
            if isinstance(node, ast.While):
                return True
        return False

    _PATH_KWARGS = ("endpoint", "url", "path", "resource", "method")
    _CLIENT_CALLS = (
        "get_pages", "paginated_response", "get_method", "get", "request",
        "fetch_resource", "get_path_with_retry", "get_url_with_retry",
        "pagination", "details_from_endpoint", "search",
    )

    def _local_string_scope(self, fn: ast.FunctionDef, scope: Scope) -> Scope:
        """Scope extended with the function's own literal string assignments.

        `url = f"{API_BASE_URL}/video/v1/assets"` then `requests.get(url)` is the
        common shape; without this the path would fall back to `/<name>`.
        """
        local = scope.child()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    v = self.resolver.value(node.value, local)
                    if isinstance(v, str):
                        local.vars.setdefault(target.id, v)
        return local

    def _path_literal(
        self, fn: ast.FunctionDef, scope: Scope
    ) -> tuple[str, str] | None:
        scope = self._local_string_scope(fn, scope)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            fname = None
            if isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            elif isinstance(node.func, ast.Name):
                fname = node.func.id
            if fname not in self._CLIENT_CALLS:
                continue
            for kw in node.keywords:
                if kw.arg in self._PATH_KWARGS:
                    literal = isinstance(kw.value, (ast.Constant, ast.JoinedStr))
                    v = self.resolver.string(kw.value, scope)
                    if v:
                        return self._normalize_path(v), (
                            "literal" if literal else "resolved"
                        )
            for arg in node.args:
                literal = isinstance(arg, (ast.Constant, ast.JoinedStr))
                v = self.resolver.string(arg, scope)
                if v and ("/" in v or "." in v) and " " not in v:
                    return self._normalize_path(v), (
                        "literal" if literal else "resolved"
                    )
        return None

    @staticmethod
    def _normalize_path(raw: str) -> str:
        text = raw.strip()
        for scheme in ("https://", "http://"):
            if text.startswith(scheme):
                rest = text[len(scheme):]
                text = rest.split("/", 1)[1] if "/" in rest else rest
        if not text.startswith("/"):
            text = "/" + text
        return text

    # -- cursor -----------------------------------------------------------

    def _cursor_from_defaults(self, fn: ast.FunctionDef, scope: Scope) -> str | None:
        defaults: list[ast.expr | None] = list(fn.args.defaults) + list(
            fn.args.kw_defaults
        )
        for default in defaults:
            if default is None:
                continue
            call = _incremental_call(default)
            if call is not None:
                return self._cursor_field(call, scope)
        return None

    def _cursor_from_call(self, call: ast.Call, scope: Scope) -> str | None:
        for node in list(call.args) + [kw.value for kw in call.keywords]:
            inc = _incremental_call(node)
            if inc is not None:
                return self._cursor_field(inc, scope)
        return None

    def _cursor_field(self, call: ast.Call, scope: Scope) -> str | None:
        for kw in call.keywords:
            if kw.arg == "cursor_path":
                return self.resolver.string(kw.value, scope)
        if call.args:
            return self.resolver.string(call.args[0], scope)
        return None


# ---------------------------------------------------------------------------
# Connector-level walker
# ---------------------------------------------------------------------------

class _ConnectorWalker:
    def __init__(self, record: str, source_dir: Path):
        self.record = record
        self.source_dir = source_dir
        self.consts: dict[str, dict[str, Any]] = {}
        self.trees: dict[str, ast.Module] = {}
        self.endpoints: dict[str, Endpoint] = {}
        self.endpoint_owner: dict[str, str] = {}   # endpoint name -> source name
        self.sources: list[SourceFn] = []
        self.unresolved: list[Unresolved] = []
        self.secrets: set[str] = set()
        self.config: set[str] = set()
        self.pagination_hints: set[str] = set()
        self.notes: list[str] = []
        self._funcs: dict[str, ast.FunctionDef] = {}
        self._resource_func_names: dict[str, str] = {}
        self.conditional_str_assignments: dict[str, list[list[str | None]]] = {}

    # -- shared lookups ---------------------------------------------------

    @staticmethod
    def kwarg(call: ast.Call, name: str) -> ast.expr | None:
        for kw in call.keywords:
            if kw.arg == name:
                return kw.value
        return None

    @staticmethod
    def dlt_decoration(fn: ast.FunctionDef) -> tuple[str | None, ast.Call | None]:
        for dec in fn.decorator_list:
            kind = _dlt_kind(dec)
            if kind is not None:
                return kind, None
            if isinstance(dec, ast.Call):
                kind = _dlt_kind(dec.func)
                if kind is not None:
                    return kind, dec
        return None, None

    def func_by_name(self, name: str) -> ast.FunctionDef | None:
        return self._funcs.get(name)

    def register_resource_func(self, func_name: str, resource_name: str) -> None:
        self._resource_func_names.setdefault(func_name, resource_name)

    def resource_name_of_func(self, func_name: str) -> str | None:
        return self._resource_func_names.get(func_name)

    def add_endpoint(self, ep: Endpoint, source: SourceFn | None) -> None:
        if ep.name in self.endpoints:
            # FIRST DEFINITION WINS, and says so: a second `@dlt.resource`
            # under the same name (jira defines `issues` in two @dlt.source
            # functions) used to vanish without a trace. Recorded on the kept
            # endpoint AND in `unresolved`, so the curator sees the choice.
            kept = self.endpoints[ep.name]
            message = (
                f"duplicate resource name {ep.name!r} at {ep.file}:{ep.line}; "
                f"first definition ({kept.file}:{kept.line}) kept"
            )
            kept.notes.append(message)
            self.unresolved.append(
                Unresolved(
                    file=ep.file,
                    line=ep.line,
                    form=f"dlt.{ep.kind}(name={ep.name!r})",
                    reason=message,
                )
            )
            return
        self.endpoints[ep.name] = ep
        if source is not None:
            source.owned.append(ep.name)
            self.endpoint_owner[ep.name] = source.name

    def collect_credentials(self, fn: ast.FunctionDef) -> None:
        args = fn.args
        pairs: list[tuple[ast.arg, ast.expr | None]] = []
        positional = list(args.posonlyargs) + list(args.args)
        pad = len(positional) - len(args.defaults)
        for i, a in enumerate(positional):
            pairs.append((a, args.defaults[i - pad] if i >= pad else None))
        for a, d in zip(args.kwonlyargs, args.kw_defaults):
            pairs.append((a, d))
        for arg, default in pairs:
            if default is None:
                continue
            if _is_dlt_value(default, "secrets"):
                self.secrets.add(arg.arg)
            elif _is_dlt_value(default, "config"):
                self.config.add(arg.arg)

    # -- driver -----------------------------------------------------------

    def load(self) -> None:
        if not self.source_dir.is_dir():
            raise ExtractionError(f"no source/ directory at {self.source_dir}")
        files = sorted(
            p for p in self.source_dir.rglob("*.py") if "tests" not in p.parts
        )
        if not files:
            raise ExtractionError(f"no python files under {self.source_dir}")
        for path in files:
            rel = path.relative_to(self.source_dir).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(text, filename=rel)
            except SyntaxError as exc:  # fail closed, but keep going per-file
                raise ExtractionError(f"{rel}: cannot parse ({exc})") from exc
            self.trees[rel] = tree
            self.consts[rel] = _module_consts(tree)
        _link_imports(self.trees, self.consts)
        for rel, tree in sorted(self.trees.items()):
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    self._funcs.setdefault(node.name, node)
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
                    node.value, ast.IfExp
                ):
                    ifexp = node.value
                    branches = [
                        _literal(ifexp.body) if isinstance(ifexp.body, ast.Constant) else None,
                        _literal(ifexp.orelse) if isinstance(ifexp.orelse, ast.Constant) else None,
                    ]
                    strs = [b if isinstance(b, str) else None for b in branches]
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    for t in targets:
                        if isinstance(t, ast.Name) and any(strs):
                            self.conditional_str_assignments.setdefault(
                                t.id, []
                            ).append(strs)
                if isinstance(node, ast.Name):
                    low = node.id.lower()
                    for token in PAGINATION_TOKENS:
                        if token in low:
                            self.pagination_hints.add(node.id)
                if isinstance(node, ast.Attribute):
                    low = node.attr.lower()
                    for token in PAGINATION_TOKENS:
                        if token in low:
                            self.pagination_hints.add(node.attr)

    def walk(self) -> None:
        # `__init__.py` first: it defines the sources every other file feeds.
        order = sorted(self.trees, key=lambda r: (r != "__init__.py", r))
        for rel in order:
            _FileWalker(self, rel, self.trees[rel]).run()

    def _cursors_from_call_sites(self) -> None:
        """Cursors passed where a decorated resource is CALLED, not declared.

        `get_last_visits(client=..., last_date=dlt.sources.incremental("serverTimestamp"))`
        in matomo: the resource function declares the parameter with no default,
        so the cursor field only exists at the source's call site.
        """
        for rel, tree in sorted(self.trees.items()):
            resolver = Resolver(self.consts.get(rel, {}))
            scope = Scope()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                resource_name = self._resource_func_names.get(node.func.id)
                if resource_name is None:
                    continue
                ep = self.endpoints.get(resource_name)
                if ep is None or ep.cursor:
                    continue
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    inc = _incremental_call(arg)
                    if inc is None:
                        continue
                    field_node: ast.expr | None = None
                    for kw in inc.keywords:
                        if kw.arg == "cursor_path":
                            field_node = kw.value
                    if field_node is None and inc.args:
                        field_node = inc.args[0]
                    cursor = resolver.string(field_node, scope)
                    if cursor:
                        ep.cursor = cursor
                        ep.notes.append(
                            "incremental cursor declared at the call site, not in "
                            "the resource signature"
                        )
                        break

    def finish(self) -> ConnectorExtract:
        self._cursors_from_call_sites()
        # Attribute module-level resources to the source(s) that reference them.
        module_level = SourceFn(
            name="(module-level)", func="", file="", line=0
        )
        for name, ep in self.endpoints.items():
            if name in self.endpoint_owner:
                continue
            owners = [
                s for s in self.sources
                if ep.func and ep.func in s.referenced
            ]
            if owners:
                for s in owners:
                    s.owned.append(name)
                self.endpoint_owner[name] = owners[0].name
            else:
                module_level.owned.append(name)
                self.endpoint_owner[name] = module_level.name

        candidates = list(self.sources)
        if module_level.owned:
            candidates.append(module_level)
        primary = ""
        if candidates:
            best_count = max(len(set(s.owned)) for s in candidates)
            tied = sorted(
                (s for s in candidates if len(set(s.owned)) == best_count),
                key=lambda s: s.name,
            )
            primary = tied[0].name

        owned = {
            name for name, owner in self.endpoint_owner.items() if owner == primary
        }
        endpoints = [self.endpoints[n] for n in sorted(owned)]
        # A parent outside the primary source cannot be a table in this task.
        names = {e.name for e in endpoints}
        for ep in endpoints:
            if ep.parent is not None and ep.parent not in names:
                ep.notes.append(
                    f"parent resource {ep.parent!r} is not part of source "
                    f"{primary!r}; edge dropped"
                )
                ep.parent = None
        return ConnectorExtract(
            record=self.record,
            endpoints=endpoints,
            sources=sorted(self.sources, key=lambda s: s.name)
            + ([module_level] if module_level.owned else []),
            primary_source=primary,
            secrets=sorted(self.secrets),
            config=sorted(self.config),
            pagination_hints=sorted(self.pagination_hints),
            unresolved=sorted(self.unresolved, key=lambda u: (u.file, u.line, u.form)),
            files=sorted(self.trees),
            notes=self.notes,
        )


def extract_connector(record: str, source_dir: Path) -> ConnectorExtract:
    """Parse one vendored connector's `source/` tree. NEVER imports it."""
    walker = _ConnectorWalker(record, Path(source_dir))
    walker.load()
    walker.walk()
    return walker.finish()


# ---------------------------------------------------------------------------
# Manifest rendering
# ---------------------------------------------------------------------------

def connector_slug(record: str) -> str:
    """`dlt_freshdesk` -> `freshdesk` (the family segment, pool-namespaced later)."""
    stem = record[4:] if record.startswith("dlt_") else record
    # Upstream directory suffixes describe packaging, not distinct families.
    # Canonicalize them BEFORE contamination checks so a naive checkout named
    # `dlt_asana_dlt` cannot evade the held-out `asana` family guard.
    aliases = {
        "asana_dlt": "asana",
        "shopify_dlt": "shopify",
        "stripe_analytics": "stripe",
    }
    return slugify_family(aliases.get(stem, stem))


def assert_not_denylisted(slug: str) -> None:
    """Refuse connectors whose family is on a benchmark deny list (fail closed).

    Uses the embedded lists in verification/contamination.py — the ONE place
    those names live — so a future `sources/zendesk` vendoring cannot become a
    task by way of this tool. The full fingerprint check still runs at ingest.
    """
    denied = {
        cont.normalize_name(f)
        for fams in (
            cont.ELTBENCH_FAMILIES,
            cont.SPIDER2_DBT_FAMILIES,
            cont.ADE_BENCH_FAMILIES,
        )
        for f in fams
    }
    if cont.normalize_name(slug) in denied:
        raise ExtractionError(
            f"connector {slug!r} collides with a benchmark family in the "
            "contamination deny lists; it must never become a task"
        )


_HEADER = """\
# GENERATED by tools/extract_dlt_manifest.py from the pinned dlt checkout.
# The extractor parses AST only and never imports connector code.
# Regeneration overwrites other edits, preserves `curated_columns`, and fails
# on orphaned curated blocks. `notes` mark inferences; `unresolved` resources
# require curator input.
"""

#: Printed immediately above every preserved `curated_columns` block so a
#: reader of the YAML can tell curator-authored schema from extractor output.
_CURATOR_BANNER = (
    "# CURATOR-AUTHORED: preserved across regeneration"
)


def read_curated_blocks(path: Path) -> dict[str, list[dict[str, Any]]]:
    """`curated_columns` blocks of an existing manifest, keyed by endpoint name.

    WHY THIS EXISTS (blocker2 T2): `manifest_document` builds endpoints purely
    from the AST extract, so without a read-back every regeneration would
    silently delete the curator-authored columns — and with them the task's
    chain evidence — with nothing in any ledger showing it. Reading the
    TARGET file (not some other copy) is what makes regeneration idempotent
    over curation.
    """
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    blocks: dict[str, list[dict[str, Any]]] = {}
    for endpoint in raw.get("endpoints") or ():
        if isinstance(endpoint, dict) and endpoint.get("curated_columns"):
            blocks[str(endpoint.get("name"))] = endpoint["curated_columns"]
    return blocks


def inject_curated_blocks(
    doc: dict[str, Any], blocks: dict[str, list[dict[str, Any]]]
) -> None:
    """Re-inject preserved curator blocks into a freshly extracted document.

    A block whose endpoint no longer exists upstream is a HARD failure naming
    the orphan: a silently dropped curated block reverts a task's schema (and
    therefore its chains), so the curator must resolve the orphan by hand —
    the manifest is not rewritten until they do.
    """
    known = {endpoint["name"] for endpoint in doc["endpoints"]}
    orphans = sorted(set(blocks) - known)
    if orphans:
        raise ExtractionError(
            f"curated_columns block(s) for endpoint(s) {orphans} no longer "
            "match any extracted endpoint; refusing to regenerate — a silent "
            "drop would revert the task's schema. Resolve the orphan(s) in "
            "the committed manifest first."
        )
    for endpoint in doc["endpoints"]:
        if endpoint["name"] in blocks:
            endpoint["curated_columns"] = blocks[endpoint["name"]]


def manifest_document(
    extract: ConnectorExtract, pool: PoolSource
) -> dict[str, Any]:
    """The manifest mapping, ready for `yaml.safe_dump(..., sort_keys=True)`."""
    slug = connector_slug(extract.record)
    assert_not_denylisted(slug)
    prov = pool.provenance(extract.record)
    endpoints: list[dict[str, Any]] = []
    for ep in extract.endpoints:
        row: dict[str, Any] = {
            "name": ep.name,
            "path": ep.path,
            "kind": ep.kind,
            "write_disposition": ep.write_disposition,
            "declared_write_disposition": ep.declared_write_disposition,
            "selected": ep.selected,
            "paginated": ep.paginated,
            "path_source": ep.path_source,
            "defined_in": f"{ep.file}:{ep.line}",
        }
        if ep.primary_key:
            row["primary_key"] = list(ep.primary_key)
        if ep.cursor:
            row["cursor"] = ep.cursor
        if ep.parent:
            row["parent"] = ep.parent
        if ep.column_hints:
            row["column_hints"] = [
                {"name": c, "type": t} for c, t in sorted(ep.column_hints.items())
            ]
        if ep.guard:
            row["guard"] = ep.guard
        if ep.notes:
            row["notes"] = list(ep.notes)
        endpoints.append(row)

    doc: dict[str, Any] = {
        "connector": slug,
        "record": extract.record,
        "license": pool.license,
        "attribution": pool.attribution_for(extract.record),
        "source_dir": f"{extract.record}/source",
        "extractor_version": EXTRACTOR_VERSION,
        "primary_source": extract.primary_source,
        "auth": {"secrets": extract.secrets, "config": extract.config},
        "pagination_hints": extract.pagination_hints,
        "sources": [
            {
                "name": s.name,
                "function": s.func,
                "defined_in": f"{s.file}:{s.line}" if s.file else "",
                "resources": sorted(set(s.owned)),
            }
            for s in extract.sources
        ],
        "files": extract.files,
        "endpoints": endpoints,
        "unresolved": [
            {
                "defined_in": f"{u.file}:{u.line}",
                "form": u.form,
                "reason": u.reason,
            }
            for u in extract.unresolved
        ],
    }
    if prov.get("upstream"):
        doc["upstream"] = prov["upstream"]
    if prov.get("commit"):
        doc["commit"] = prov["commit"]
    return doc


def render_manifest(doc: dict[str, Any]) -> str:
    text = _HEADER + yaml.safe_dump(
        doc, sort_keys=True, default_flow_style=False, allow_unicode=True, width=100
    )
    # Re-add a banner to each curated_columns block after YAML rendering,
    # handling both normal and list-item key forms.
    return re.sub(
        r"(?m)^([ ]*)((?:- )?)curated_columns:",
        lambda m: (
            f"{m.group(1)}{_CURATOR_BANNER}\n"
            f"{m.group(1)}{m.group(2)}curated_columns:"
        ),
        text,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_out_dir() -> Path:
    return _repo_root() / "config" / "dlt_connectors"


def discover_records(pool: PoolSource, *, include_excluded: bool = False) -> list[str]:
    """Vendored records eligible for task-manifest extraction.

    `include_excluded` is a maintenance aid for ordinary policy-excluded
    connectors whose review manifests already exist. It never bypasses the
    contamination guard in `manifest_document`.
    """
    root = pool.root_path()
    if not root.is_dir():
        raise ExtractionError(f"dlt pool root not found: {root} (fail closed)")
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir()
        and p.name.startswith("dlt_")
        and (include_excluded or p.name not in pool.excluded)
    )


def summarize(extract: ConnectorExtract) -> dict[str, int]:
    eps = extract.endpoints
    return {
        "resources": len(eps),
        "with_primary_key": sum(1 for e in eps if e.primary_key),
        "parent_dependent": sum(1 for e in eps if e.parent),
        "with_cursor": sum(1 for e in eps if e.cursor),
        "paginated": sum(1 for e in eps if e.paginated),
        "unselected": sum(1 for e in eps if not e.selected),
        "unresolved": len(extract.unresolved),
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="vendored dlt connector -> config/dlt_connectors/<name>.yaml"
    )
    ap.add_argument("--connector", help="record dir name, e.g. dlt_freshdesk")
    ap.add_argument(
        "--all", action="store_true", help="every catalog-admitted vendored connector"
    )
    ap.add_argument(
        "--include-excluded",
        action="store_true",
        help=(
            "maintenance only: extract catalog-excluded connectors too; "
            "contaminated families still fail and catalog admission is unchanged"
        ),
    )
    ap.add_argument("--out", type=Path, default=None, help="output directory")
    ap.add_argument("--pool", default="dlt", help="catalog pool name")
    ap.add_argument(
        "--dry-run", action="store_true", help="print, do not write the YAML"
    )
    ap.add_argument(
        "--quiet", action="store_true", help="suppress the per-connector table"
    )
    args = ap.parse_args(argv)

    if not args.all and not args.connector:
        ap.error("pass --connector <record> or --all")

    catalog = load_source_catalog()
    pool = catalog.pool(args.pool)
    records = (
        discover_records(pool, include_excluded=args.include_excluded)
        if args.all
        else [args.connector]
    )
    out_dir = Path(args.out) if args.out else default_out_dir()
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    #: hard failures (unreadable connector) vs flags (readable, but its
    #: resources are runtime-defined). Only the former is a nonzero exit.
    failures: list[tuple[str, str]] = []
    flags: list[tuple[str, str]] = []
    rows: list[tuple[str, dict[str, int]]] = []
    for record in records:
        if record in pool.excluded:
            # Catalog exclusion is an ingest-admission decision, not a way to
            # hide a held-out family. A targeted contaminated record must
            # still fail loudly; otherwise `--connector dlt_zendesk` would
            # look like a benign skipped maintenance fixture.
            try:
                assert_not_denylisted(connector_slug(record))
            except ExtractionError as exc:
                failures.append((record, str(exc)))
                continue
            if not args.include_excluded:
                # Deliberately not emitted as a task manifest. The raw
                # connector remains vendored and is covered by
                # audit_dlt_inventory.py; ordinary policy exclusions are a
                # flag, unlike contamination.
                flags.append((record, "excluded by the source catalog"))
                continue
        source_dir = pool.record_path(record) / "source"
        try:
            extract = extract_connector(record, source_dir)
            doc = manifest_document(extract, pool)
            # T2: regeneration must not destroy curation — re-inject the
            # target's curator-authored blocks before rendering; an orphaned
            # block is a hard failure, never a silent drop.
            inject_curated_blocks(
                doc, read_curated_blocks(out_dir / f"{doc['connector']}.yaml")
            )
        except ExtractionError as exc:
            failures.append((record, str(exc)))
            continue
        text = render_manifest(doc)
        target = out_dir / f"{doc['connector']}.yaml"
        if args.dry_run:
            print(f"--- {target} ---")
            print(text)
        else:
            target.write_text(text, encoding="utf-8")
        rows.append((doc["connector"], summarize(extract)))
        if not extract.endpoints:
            flags.append(
                (record, "no statically resolvable resources (runtime-defined)")
            )

    if not args.quiet:
        print(
            f"{'connector':<20}{'res':>5}{'pk':>5}{'child':>7}{'cursor':>8}"
            f"{'pagin':>7}{'unsel':>7}{'unres':>7}"
        )
        for name, s in rows:
            print(
                f"{name:<20}{s['resources']:>5}{s['with_primary_key']:>5}"
                f"{s['parent_dependent']:>7}{s['with_cursor']:>8}"
                f"{s['paginated']:>7}{s['unselected']:>7}{s['unresolved']:>7}"
            )
    for record, reason in flags:
        print(f"FLAG {record}: {reason}", file=sys.stderr)
    for record, reason in failures:
        print(f"ERROR {record}: {reason}", file=sys.stderr)
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

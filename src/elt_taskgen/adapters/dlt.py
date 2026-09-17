"""Convert a curated dlt endpoint graph into one complete ``TaskIR``.

Reject empty selections, keyless merges, unknown parents, and contamination.
License and attribution come from ``config/sources.yaml``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from elt_taskgen.generation.mart_plan import (
    MIN_MART_COLUMNS,
    Measure,
    StarJoin,
    StarShape,
    build_star,
    quote,
    relation_identifier,
)
from elt_taskgen.generation.difficulty_profiles import (
    GenerationDifficultyProfile,
    STANDARD_DIFFICULTY_PROFILE,
)
from elt_taskgen.adapters.evidence import (
    build_marts,
    chain_candidates,
    declared_key_parents,
)
from elt_taskgen.generation.populations import derive_populations_and_attacks
from elt_taskgen.reference.solution import attach_reference
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartColumn,
    MartColumnKind,
    MartSpec,
    Origin,
    PoolSelection,
    Relationship,
    TableSpec,
    TaskIR,
    derive_seed,
)

_WRITE_DISPOSITIONS = frozenset({"append", "replace", "merge"})
_ENDPOINT_KINDS = frozenset({"resource", "transformer"})

#: dlt/JSON-schema-ish type spellings -> canonical ColumnType.
_TYPE_MAP: dict[str, ColumnType] = {
    "text": ColumnType.TEXT,
    "string": ColumnType.TEXT,
    "varchar": ColumnType.TEXT,
    "binary": ColumnType.TEXT,
    "integer": ColumnType.INTEGER,
    "int": ColumnType.INTEGER,
    "bigint": ColumnType.BIGINT,
    "wei": ColumnType.BIGINT,
    "float": ColumnType.FLOAT,
    "double": ColumnType.FLOAT,
    "decimal": ColumnType.DECIMAL,
    "numeric": ColumnType.DECIMAL,
    "bool": ColumnType.BOOLEAN,
    "boolean": ColumnType.BOOLEAN,
    "date": ColumnType.DATE,
    "time": ColumnType.TIMESTAMP,
    "timestamp": ColumnType.TIMESTAMP,
    "datetime": ColumnType.TIMESTAMP,
    "json": ColumnType.JSON,
    "complex": ColumnType.JSON,
}

#: Name suffixes that imply a temporal column when a schema is synthesized.
_TIME_SUFFIXES = ("_at", "_time", "_date", "_ts", "timestamp")


def _infer_type(name: str) -> ColumnType:
    """Type of a synthesized column, inferred from its NAME only; the column
    description records it as a heuristic so nothing mistakes it for observed
    data.
    """
    low = name.lower()
    if low == "ts" or low.endswith(_TIME_SUFFIXES):
        return ColumnType.TIMESTAMP
    if low == "id" or low.endswith("_id"):
        return ColumnType.BIGINT
    return ColumnType.TEXT


class DltColumn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    type: str = "text"
    nullable: bool = True

    def to_column_spec(self, *, description: str = "") -> ColumnSpec:
        ct = _TYPE_MAP.get(self.type.lower())
        if ct is None:
            raise ValueError(f"dlt column {self.name!r}: unknown type {self.type!r}")
        return ColumnSpec(
            name=self.name, type=ct, nullable=self.nullable, description=description
        )


#: Enumerated provenance kinds a curated column may declare, printed per column.
_CURATED_SOURCE_PREFIXES = ("fixture:", "readme:", "ast:")
_CURATED_SOURCE_INVENTED = "curator-invented"


class DltCuratedColumn(DltColumn):
    """Curator-authored column with required enumerated provenance."""

    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_source(self) -> "DltCuratedColumn":
        ok = self.source == _CURATED_SOURCE_INVENTED or any(
            self.source.startswith(prefix) and len(self.source) > len(prefix)
            for prefix in _CURATED_SOURCE_PREFIXES
        )
        if not ok:
            raise ValueError(
                f"curated column {self.name!r}: source {self.source!r} is not an "
                f"enumerated provenance kind (one of "
                f"{[p + '<ref>' for p in _CURATED_SOURCE_PREFIXES]} or "
                f"{_CURATED_SOURCE_INVENTED!r})"
            )
        return self


class DltEndpoint(BaseModel):
    """One resource/transformer of a connector, as the manifest declares it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    primary_key: tuple[str, ...] = ()
    cursor: str | None = None
    write_disposition: str = "append"
    parent: str | None = None
    parent_key: str | None = None
    columns: tuple[DltColumn, ...] = ()

    # --- extractor-supplied provenance/annotation (all optional) ---
    kind: str = "resource"
    #: dlt `selected=False`: in the graph, never loaded — never a source table.
    selected: bool = True
    #: Paginated resources land on the `rest` backend, one-shot ones on `files`.
    paginated: bool = True
    #: The disposition the connector declared, when the validated one differs.
    declared_write_disposition: str = ""
    #: "literal" | "resolved" | "inferred" — how `path` was obtained.
    path_source: str = "inferred"
    #: "<file>:<line>" inside the connector's source dir.
    defined_in: str = ""
    #: dlt type hints, merged INTO a synthesized schema (`columns` replaces it).
    column_hints: tuple[DltColumn, ...] = ()
    #: Curator-authored columns merged in, each with a required `source:`.
    curated_columns: tuple[DltCuratedColumn, ...] = ()
    #: `if` condition(s) guarding the resource; empty means unconditional.
    guard: str = ""
    #: Where the extractor chose rather than read. Curator-facing.
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "DltEndpoint":
        if self.write_disposition not in _WRITE_DISPOSITIONS:
            raise ValueError(
                f"endpoint {self.name!r}: write_disposition {self.write_disposition!r} "
                f"not in {sorted(_WRITE_DISPOSITIONS)}"
            )
        if self.write_disposition == "merge" and not self.primary_key:
            raise ValueError(f"endpoint {self.name!r}: merge disposition requires a primary_key")
        if self.kind not in _ENDPOINT_KINDS:
            raise ValueError(
                f"endpoint {self.name!r}: kind {self.kind!r} not in {sorted(_ENDPOINT_KINDS)}"
            )
        if self.parent is None and self.parent_key is not None:
            raise ValueError(
                f"endpoint {self.name!r}: parent_key without a parent endpoint"
            )
        if self.curated_columns:
            # A curated column must ADD schema, never shadow a synthesized
            # (PK / link / cursor) or declared one: fail closed on collision.
            curated = [c.name for c in self.curated_columns]
            if len(curated) != len(set(curated)):
                raise ValueError(
                    f"endpoint {self.name!r}: duplicate curated column names"
                )
            reserved: dict[str, str] = {pk: "primary_key" for pk in self.primary_key}
            if self.link_column is not None:
                reserved.setdefault(self.link_column, "parent link")
            if self.cursor_column is not None:
                reserved.setdefault(self.cursor_column, "cursor")
            for hint in self.column_hints:
                reserved.setdefault(hint.name, "column_hints")
            for name in curated:
                if name in reserved:
                    raise ValueError(
                        f"endpoint {self.name!r}: curated column {name!r} "
                        f"collides with the {reserved[name]} column"
                    )
        return self

    @property
    def cursor_column(self) -> str | None:
        """Return the warehouse column for the first cursor-path alternative."""
        if self.cursor is None:
            return None
        head = self.cursor.split("|", 1)[0].strip()
        head = head.split(".")[-1].lstrip("$").strip("[]'\" ")
        cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in head)
        cleaned = cleaned.strip("_")
        return cleaned or None

    @property
    def link_column(self) -> str | None:
        """Child column referencing the parent: `parent_key`, else a synthesized
        `_<parent>_id` (dlt's include_from_parent convention), so the implied FK
        edge is materialized.
        """
        if self.parent is None:
            return None
        return self.parent_key or f"_{self.parent}_id"


class DltSourceFn(BaseModel):
    """One `@dlt.source` function and the resources attributed to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = ""
    function: str = ""
    defined_in: str = ""
    resources: tuple[str, ...] = ()


class DltUnresolved(BaseModel):
    """A construct the AST extractor saw but could not resolve statically."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    defined_in: str = ""
    form: str = ""
    reason: str = ""


class DltAuth(BaseModel):
    """Credential/config names the connector expects from dlt secrets/config."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    secrets: tuple[str, ...] = ()
    config: tuple[str, ...] = ()


class DltManifest(BaseModel):
    """Declarative connector manifest: endpoint graph, cursors, dispositions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    license: str = "unspecified"
    endpoints: tuple[DltEndpoint, ...] = ()

    # --- extractor provenance (optional; absent in hand-written manifests) ---
    #: The vendored directory name — the catalog selector, NOT `connector`.
    record: str = ""
    attribution: str = ""
    source_dir: str = ""
    upstream: str = ""
    commit: str = ""
    extractor_version: str = ""
    primary_source: str = ""
    auth: DltAuth = Field(default_factory=DltAuth)
    pagination_hints: tuple[str, ...] = ()
    sources: tuple[DltSourceFn, ...] = ()
    files: tuple[str, ...] = ()
    unresolved: tuple[DltUnresolved, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_graph(self) -> "DltManifest":
        names = [e.name for e in self.endpoints]
        if len(names) != len(set(names)):
            raise ValueError("duplicate endpoint names")
        known = set(names)
        for e in self.endpoints:
            if e.parent is not None:
                if e.parent not in known:
                    raise ValueError(f"endpoint {e.name!r}: unknown parent {e.parent!r}")
                if e.parent == e.name:
                    raise ValueError(f"endpoint {e.name!r}: endpoint is its own parent")
        return self

    def endpoint(self, name: str) -> DltEndpoint:
        for e in self.endpoints:
            if e.name == name:
                return e
        raise KeyError(f"no endpoint {name!r}")

    def loadable(self) -> tuple[DltEndpoint, ...]:
        """Endpoints dlt would actually LOAD (`selected=False` is extract-only)."""
        return tuple(e for e in self.endpoints if e.selected)

    @property
    def selector(self) -> str:
        """The pool record key: the vendored dir name when known."""
        return self.record or self.connector


class NoStaticResources(ValueError):
    """The manifest is well formed but names no resource (runtime-defined).

    A distinct type: batch ingest skips this known state, while a malformed
    manifest must still stop the run.
    """


def load_connector(spec_path: Path) -> DltManifest:
    """Parse a declarative connector manifest (.yaml/.yml/.json). Fails closed."""
    spec_path = Path(spec_path)
    if not spec_path.is_file():
        raise FileNotFoundError(f"dlt manifest not found: {spec_path} (fail closed)")
    text = spec_path.read_text(encoding="utf-8")
    raw = json.loads(text) if spec_path.suffix == ".json" else yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{spec_path}: manifest must be a mapping")
    manifest = DltManifest.model_validate(raw)
    if not manifest.endpoints:
        unresolved = "; ".join(
            f"{u.defined_in} {u.form}: {u.reason}" for u in manifest.unresolved
        )
        raise NoStaticResources(
            f"{spec_path}: connector {manifest.connector!r} declares no endpoints — "
            "its resources are defined at runtime and a curator must name them "
            f"before it can become a task. Unresolved: {unresolved or 'none recorded'}"
        )
    return manifest


def load_all_connectors(manifest_dir: Path) -> list[tuple[Path, DltManifest]]:
    """Load sorted manifests, skipping only empty runtime-defined connectors."""
    out: list[tuple[Path, DltManifest]] = []
    for path in list_manifests(manifest_dir):
        try:
            out.append((path, load_connector(path)))
        except NoStaticResources:
            continue
    return out


def list_manifests(manifest_dir: Path) -> list[Path]:
    d = Path(manifest_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"manifest dir not found: {d} (fail closed)")
    return sorted(p for p in d.glob("*.yaml") if p.is_file())


# --- Schema synthesis ---

_SYNTH = "synthesized from the dlt manifest (no observed schema)"


def _pk_column_type(ep: DltEndpoint) -> ColumnType | None:
    """Return the inferred or declared type of a single-column primary key."""
    if len(ep.primary_key) != 1:
        return None
    pk = ep.primary_key[0]
    if ep.columns:
        for column in ep.columns:
            if column.name == pk:
                return column.to_column_spec().type
        raise ValueError(
            f"endpoint {ep.name!r}: primary_key column {pk!r} is not among its "
            "declared columns"
        )
    return _infer_type(pk)


def _endpoint_columns(
    ep: DltEndpoint,
    *,
    surrogate_pk: str | None = None,
    link_type: ColumnType | None = None,
    link_typed_after: str = "",
) -> tuple[ColumnSpec, ...]:
    """Return declared columns or a minimal graph-derived schema.

    ``surrogate_pk`` is described explicitly; ``link_type`` overrides name
    inference and ``link_typed_after`` records any difference.
    """
    if ep.columns:
        if ep.curated_columns:
            raise ValueError(
                f"endpoint {ep.name!r}: curated_columns alongside a wholesale "
                "`columns` declaration is ambiguous — the curator already owns "
                "`columns`; declare the extra columns there instead"
            )
        cols = tuple(c.to_column_spec() for c in ep.columns)
    else:
        synth: list[ColumnSpec] = [
            ColumnSpec(
                name=pk,
                type=_infer_type(pk),
                nullable=False,
                description=(
                    f"deterministic surrogate primary key of dlt resource "
                    f"{ep.name!r}, synthesized because the connector declares "
                    f"none ({_SYNTH})"
                    if pk == surrogate_pk
                    else f"primary key of dlt resource {ep.name!r} ({_SYNTH})"
                ),
            )
            for pk in ep.primary_key
        ]
        seen = {c.name for c in synth}
        link = ep.link_column
        if link is not None and link not in seen:
            inferred = _infer_type(link)
            resolved = link_type if link_type is not None else inferred
            synth.append(
                ColumnSpec(
                    name=link,
                    type=resolved,
                    nullable=False,
                    description=(
                        f"parent link to {ep.parent!r} "
                        + (
                            "declared by the manifest"
                            if ep.parent_key
                            else "(dlt include_from_parent convention; " + _SYNTH + ")"
                        )
                        + (
                            f"; typed after {link_typed_after}"
                            if link_typed_after and resolved is not inferred
                            else ""
                        )
                    ),
                )
            )
            seen.add(link)
        cursor_col = ep.cursor_column
        if cursor_col is not None and cursor_col not in seen:
            synth.append(
                ColumnSpec(
                    name=cursor_col,
                    type=ColumnType.TIMESTAMP,
                    nullable=False,
                    description=(
                        f"incremental cursor of dlt resource {ep.name!r} "
                        f"(dlt cursor path {ep.cursor!r})"
                    ),
                )
            )
            seen.add(cursor_col)
        for hint in ep.column_hints:
            if hint.name not in seen:
                synth.append(
                    hint.to_column_spec(
                        description=f"dlt column hint (data_type={hint.type})"
                    )
                )
                seen.add(hint.name)
        for curated in ep.curated_columns:
            if curated.name in seen:
                # The validator rejects declared collisions; this catches the
                # synthesized ones (e.g. a surrogate key).
                raise ValueError(
                    f"endpoint {ep.name!r}: curated column {curated.name!r} "
                    "collides with a synthesized column"
                )
            synth.append(
                curated.to_column_spec(
                    # Distinct prefix on purpose: a curator-authored column must
                    # never read as an extractor-read hint.
                    description=(
                        f"curator-authored column of dlt {ep.kind} {ep.name!r} "
                        f"(source: {curated.source})"
                    )
                )
            )
            seen.add(curated.name)
        synth.append(
            ColumnSpec(
                name="payload",
                type=ColumnType.JSON,
                nullable=True,
                description="raw endpoint record body (synthesized schema)",
            )
        )
        cols = tuple(synth)
    have = {c.name for c in cols}
    for label, needed in (
        ("primary_key", ep.primary_key),
        ("cursor", (ep.cursor_column,) if ep.cursor_column else ()),
        # The LINK column, not just a declared parent_key: a verbatim `columns`
        # manifest must still carry the column the FK edge is built on.
        ("parent link", (ep.link_column,) if ep.link_column else ()),
    ):
        missing = [c for c in needed if c not in have]
        if missing:
            raise ValueError(f"endpoint {ep.name!r}: {label} columns {missing} not in columns")
    return cols


#: Backends a NON-paginated dlt resource rotates over; `rest` is excluded because
#: its paginated fixture would claim a transport the connector never declared.
_NON_REST_BACKENDS: tuple[Backend, ...] = (
    Backend.FILES,
    Backend.POSTGRES,
    Backend.S3,
    Backend.MONGODB,
)

#: Backends for paginated resources; pagination metadata remains in options.
_PAGINATED_BACKENDS: tuple[Backend, ...] = (
    Backend.FILES,
    Backend.MONGODB,
    Backend.POSTGRES,
    Backend.REST,
    Backend.S3,
)


def _assign_backends(
    endpoints: Iterable[DltEndpoint], family_id: str
) -> dict[str, Backend]:
    """Assign deterministic backends while retaining paginated REST.

    The first paginated endpoint by name is always pinned to REST.
    """
    assigned: dict[str, Backend] = {}
    paginated_names: list[str] = []
    for ep in endpoints:
        pool = _PAGINATED_BACKENDS if ep.paginated else _NON_REST_BACKENDS
        assigned[ep.name] = pool[
            derive_seed("dlt-backend", family_id, ep.name) % len(pool)
        ]
        if ep.paginated:
            paginated_names.append(ep.name)
    paginated_names.sort()
    if paginated_names and not any(
        assigned[name] is Backend.REST for name in paginated_names
    ):
        assigned[paginated_names[0]] = Backend.REST
    return assigned


#: Primary counts force three parent pages and eleven child pages at 100 rows each.
DLT_PAGE_SIZE = 100
DLT_PARENT_ROWS = 260
DLT_CHILD_ROWS = 1_040


def _dlt_scale_hint(
    tables: tuple[TableSpec, ...], rels: tuple[Relationship, ...]
) -> dict[str, int]:
    children = {r.child_table for r in rels}
    return {
        t.name: (DLT_CHILD_ROWS if t.name in children else DLT_PARENT_ROWS)
        for t in sorted(tables, key=lambda t: t.name)
    }


def _backend_options(ep: DltEndpoint, manifest: DltManifest) -> dict[str, str]:
    options: dict[str, str] = {
        "path": ep.path,
        "write_disposition": ep.write_disposition,
        "resource_kind": ep.kind,
        "paginated": "true" if ep.paginated else "false",
    }
    if ep.cursor_column is not None:
        options["cursor"] = ep.cursor_column
        if ep.cursor != ep.cursor_column:
            options["cursor_path"] = ep.cursor or ""
    if ep.parent is not None:
        options["parent"] = ep.parent
    if ep.declared_write_disposition and ep.declared_write_disposition != ep.write_disposition:
        options["declared_write_disposition"] = ep.declared_write_disposition
    if manifest.auth.secrets:
        options["auth_secrets"] = ",".join(manifest.auth.secrets)
    if manifest.auth.config:
        options["auth_config"] = ",".join(manifest.auth.config)
    return options


# --- Mart derivation from the entity graph ---

def _column_type(table: TableSpec, name: str) -> ColumnType:
    return table.column(name).type


def _entity_mart(
    parent: DltEndpoint,
    parent_table: TableSpec,
    child: DltEndpoint | None,
    child_table: TableSpec | None,
    child_link: str | None,
    *,
    minimum_columns: int = 0,
) -> tuple[MartSpec, StarShape] | None:
    """`dim_<parent>`: one row per parent entity, with its child count.

    Built through generation/mart_plan.py::build_star so the plan compiles.
    """
    if not parent.primary_key:
        return None
    name = f"dim_{parent.name}"
    cols: list[MartColumn] = [
        MartColumn(
            name=pk,
            type=_column_type(parent_table, pk),
            description=f"{parent.name} primary key column {pk!r}",
            kind=MartColumnKind.PASSTHROUGH,
        )
        for pk in parent.primary_key
    ]
    used = {c.name for c in cols}
    joins: list[StarJoin] = []
    measures: list[Measure] = []
    parent_carry: list[tuple[str, str]] = []

    def unused(base: str) -> str:
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    # Only count children across an FK the emitted schema actually declares:
    # a composite parent key yields no single-column FK, so no join exists.
    if child is not None and child_link is not None and len(parent.primary_key) == 1:
        count_col = f"{child.name}_count"
        if count_col in used:
            count_col = f"{child.name}_row_count"
        cols.append(
            MartColumn(
                name=count_col,
                type=ColumnType.BIGINT,
                description=(
                    f"number of {child.name!r} records extracted for this "
                    f"{parent.name!r} (0 when none)"
                ),
                kind=MartColumnKind.AGGREGATED,
            )
        )
        used.add(count_col)
        link_alias = f"{child.name}__{child_link}"
        joins.append(
            StarJoin(
                table=child.name,
                on_pairs=((parent.primary_key[0], child_link),),
                carry=((child_link, link_alias),),
                rel_columns=(parent.primary_key[0], child_link),
                # Outcome wording, not operator vocabulary: declarative_prose
                # bans it here. build_star still emits LEFT and the ON pairs.
                description=(
                    f"Bring in {child.name}: every {parent.name} record appears "
                    f"whether or not it has matching {child.name} records."
                ),
            )
        )
        measures.append(
            Measure(column=count_col, expr=f"COUNT({quote(link_alias)})")
        )

        # Profile a non-JSON child attribute so the mart tests more than row count.
        if child_table is not None and minimum_columns:
            excluded = {child_link, "payload"}
            candidates = [
                column
                for column in child_table.columns
                if column.name not in excluded
                and column.type
                in {
                    ColumnType.INTEGER,
                    ColumnType.BIGINT,
                    ColumnType.FLOAT,
                    ColumnType.DECIMAL,
                    ColumnType.DATE,
                    ColumnType.TIMESTAMP,
                    ColumnType.TEXT,
                }
            ]
            candidates.sort(
                key=lambda column: (
                    column.name not in child_table.primary_key,
                    column.type not in {
                        ColumnType.INTEGER,
                        ColumnType.BIGINT,
                        ColumnType.FLOAT,
                        ColumnType.DECIMAL,
                    },
                    column.name,
                )
            )
            profile = candidates[0] if candidates else None
            if profile is not None:
                profile_alias = f"{child.name}__profile_value"
                joins[0] = StarJoin(
                    table=joins[0].table,
                    on_pairs=joins[0].on_pairs,
                    carry=joins[0].carry + ((profile.name, profile_alias),),
                    rel_columns=joins[0].rel_columns,
                    description=joins[0].description,
                )
                metric_specs = (
                    (
                        unused(f"distinct_{child.name}_count"),
                        ColumnType.BIGINT,
                        f"COUNT(DISTINCT {quote(profile_alias)})",
                        f"number of different non-missing {child.name}.{profile.name} values",
                    ),
                    (
                        unused(f"non_null_{profile.name}_count"),
                        ColumnType.BIGINT,
                        f"COUNT({quote(profile_alias)})",
                        f"number of {child.name} rows whose {profile.name} value is present",
                    ),
                    (
                        unused(f"first_{profile.name}"),
                        profile.type,
                        f"MIN({quote(profile_alias)})",
                        f"smallest {child.name}.{profile.name} value; missing when none is present",
                    ),
                    (
                        unused(f"last_{profile.name}"),
                        profile.type,
                        f"MAX({quote(profile_alias)})",
                        f"largest {child.name}.{profile.name} value; missing when none is present",
                    ),
                )
                for metric_name, metric_type, expression, description in metric_specs:
                    cols.append(
                        MartColumn(
                            name=metric_name,
                            type=metric_type,
                            description=description,
                            kind=MartColumnKind.AGGREGATED,
                        )
                    )
                    used.add(metric_name)
                    measures.append(Measure(column=metric_name, expr=expression))

    # MAX(cursor) is a no-op when the cursor is itself part of the grain.
    parent_cursor = parent.cursor_column
    if parent_cursor is not None and parent_cursor not in parent.primary_key:
        last_col = f"last_{parent_cursor}"
        if last_col not in used:
            cols.append(
                MartColumn(
                    name=last_col,
                    type=ColumnType.TIMESTAMP,
                    description=(
                        f"latest {parent_cursor!r} seen for this {parent.name!r} "
                        "record (the incremental cursor of the resource)"
                    ),
                    kind=MartColumnKind.AGGREGATED,
                )
            )
            used.add(last_col)
            cursor_alias = f"{parent.name}__{parent_cursor}"
            parent_carry.append((parent_cursor, cursor_alias))
            measures.append(
                Measure(column=last_col, expr=f"MAX({quote(cursor_alias)})")
            )
    if len(cols) == len(parent.primary_key):
        # Nothing but the key survived: a key-only mart is SELECT DISTINCT,
        # not a task.
        return None
    # Omit the optional mart when it cannot meet the six-column floor meaningfully.
    if minimum_columns and len(cols) < minimum_columns:
        return None
    built = build_star(
        mart=name,
        parent=parent.name,
        parent_keys=parent.primary_key,
        key_columns=parent.primary_key,
        parent_carry=tuple(parent_carry),
        joins=tuple(joins),
        measures=tuple(measures),
        notes="entity dimension derived from the connector's resource graph",
    )
    return (
        MartSpec(
            name=name,
            description=f"one row per {parent.name!r} record extracted from the API",
            grain=f"one row per {parent.name} ({', '.join(parent.primary_key)})",
            key_columns=parent.primary_key,
            columns=tuple(cols),
            plan=built.plan,
        ),
        built.shape,
    )


def _activity_mart(
    event: DltEndpoint,
    event_table: TableSpec,
    link: str | None,
    *,
    minimum_columns: int = 0,
) -> tuple[MartSpec, StarShape] | None:
    """`<event>_activity`: a per-day summary over the child/event resource."""
    cursor_col = event.cursor_column
    if cursor_col is None:
        return None
    name = f"{event.name}_activity"
    cols: list[MartColumn] = []
    keys: list[str] = []
    parent_keys: list[str] = []
    key_exprs: list[str] = []
    if link is not None and link in {c.name for c in event_table.columns}:
        cols.append(
            MartColumn(
                name=link,
                type=_column_type(event_table, link),
                description=f"parent entity this {event.name!r} record belongs to",
                kind=MartColumnKind.PASSTHROUGH,
            )
        )
        keys.append(link)
        parent_keys.append(link)
        key_exprs.append(f"{relation_identifier(event.name)}.{quote(link)}")
    date_col = "activity_date"
    if date_col in {c.name for c in cols}:
        date_col = f"{event.name}_date"
    cols.append(
        MartColumn(
            name=date_col,
            type=ColumnType.DATE,
            description=f"calendar day of {cursor_col!r} (UTC date part)",
            kind=MartColumnKind.DERIVED,
        )
    )
    keys.append(date_col)
    parent_keys.append(cursor_col)
    key_exprs.append(
        f"CAST({relation_identifier(event.name)}.{quote(cursor_col)} AS DATE)"
    )
    cols.append(
        MartColumn(
            name="record_count",
            type=ColumnType.BIGINT,
            description=f"number of {event.name!r} records on that day",
            kind=MartColumnKind.AGGREGATED,
        )
    )

    parent_carry: tuple[tuple[str, str], ...] = ()
    activity_measures: list[Measure] = [
        Measure(column="record_count", expr="COUNT(*)")
    ]
    used = {column.name for column in cols}

    def unused(base: str) -> str:
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        return candidate

    if minimum_columns:
        identity = next(
            (
                column
                for name in event_table.primary_key
                for column in event_table.columns
                if column.name == name and column.name != link
            ),
            None,
        )
        if identity is None:
            identity = next(
                (
                    column
                    for column in event_table.columns
                    if column.name not in {link, cursor_col, "payload"}
                    and column.type is not ColumnType.JSON
                ),
                event_table.column(cursor_col),
            )
        cursor_alias = f"{event.name}__activity_cursor"
        identity_alias = f"{event.name}__activity_identity"
        parent_carry = (
            (cursor_col, cursor_alias),
            (identity.name, identity_alias),
        )
        distinct_col = unused("distinct_record_count")
        non_null_col = unused(f"non_null_{cursor_col}_count")
        first_col = unused(f"first_{cursor_col}")
        last_col = unused(f"last_{cursor_col}")
        for metric_name, metric_type, description in (
            (
                distinct_col,
                ColumnType.BIGINT,
                f"number of different {identity.name} values represented that day",
            ),
            (
                non_null_col,
                ColumnType.BIGINT,
                f"number of records whose {cursor_col} value is present that day",
            ),
            (
                first_col,
                event_table.column(cursor_col).type,
                f"earliest {cursor_col} value on that day; missing when none is present",
            ),
            (
                last_col,
                event_table.column(cursor_col).type,
                f"latest {cursor_col} value on that day; missing when none is present",
            ),
        ):
            cols.append(
                MartColumn(
                    name=metric_name,
                    type=metric_type,
                    description=description,
                    kind=MartColumnKind.AGGREGATED,
                )
            )
        activity_measures.extend(
            (
                Measure(
                    column=distinct_col,
                    expr=f"COUNT(DISTINCT {quote(identity_alias)})",
                ),
                Measure(column=non_null_col, expr=f"COUNT({quote(cursor_alias)})"),
                Measure(column=first_col, expr=f"MIN({quote(cursor_alias)})"),
                Measure(column=last_col, expr=f"MAX({quote(cursor_alias)})"),
            )
        )
    built = build_star(
        mart=name,
        parent=event.name,
        parent_keys=tuple(parent_keys),
        key_columns=tuple(keys),
        key_exprs=tuple(key_exprs),
        parent_carry=parent_carry,
        measures=tuple(activity_measures),
        grain_description=(
            f"The grain is {', '.join(keys)} — {date_col} is the UTC date "
            f"part of the incremental cursor {cursor_col!r}."
        ),
        notes=(
            "summary over the child/event resource; the cursor column makes "
            "incremental-load mistakes visible in the output"
        ),
    )
    grain = (
        f"one row per {keys[0]} per calendar day of {cursor_col}"
        if len(keys) == 2
        else f"one row per calendar day of {cursor_col}"
    )
    return (
        MartSpec(
            name=name,
            description=f"daily activity summary of the {event.name!r} resource",
            grain=grain,
            key_columns=tuple(keys),
            columns=tuple(cols),
            plan=built.plan,
        ),
        built.shape,
    )


def _entity_rank(ep: DltEndpoint, child_count: int) -> tuple:
    """Sort key picking the connector's most entity-like keyed resource:
    (1) most children, (2) has a cursor, (3) paginated, (4) name — so the
    choice is total and reproducible.
    """
    return (-child_count, ep.cursor is None, not ep.paginated, ep.name)


def _pick_entity_graph(
    endpoints: tuple[DltEndpoint, ...]
) -> tuple[DltEndpoint | None, DltEndpoint | None]:
    """Choose the highest-ranked keyed parent and its preferred declared child."""
    by_name = {e.name: e for e in endpoints}
    children: dict[str, list[DltEndpoint]] = {}
    for e in sorted(endpoints, key=lambda x: x.name):
        if e.parent is not None and e.parent in by_name:
            children.setdefault(e.parent, []).append(e)

    keyed = [e for e in endpoints if e.primary_key]
    if not keyed:
        return None, None
    parent = sorted(keyed, key=lambda e: _entity_rank(e, len(children.get(e.name, ()))))[0]
    kids = children.get(parent.name, [])
    if not kids:
        return parent, None
    with_cursor = [k for k in kids if k.cursor is not None]
    return parent, (with_cursor or kids)[0]


#: Mart budget: a third library mart would overshoot the anchor's p75 of 3.
_MAX_LIBRARY_MARTS = 2

_MART_PROSE: dict[str, str] = {
    "by_period": (
        "Per-({parent}, calendar month of {cursor}) activity grid over the "
        "{bridge} resource. The month comes from the resource's own INCREMENTAL "
        "CURSOR, so a load that ignores the cursor is visible in the output."
    ),
    "rollup": (
        "Per-{parent} roll-up of extracted {bridge} records, following the "
        "resource graph on to {child}."
    ),
    "top": (
        "Per-{parent} extremes over extracted {bridge} records: WHICH record is "
        "largest, not how large it is."
    ),
    "bands": "Per-{parent} banding of extracted {bridge} records.",
    "cohorts": (
        "Per-({parent}, status cohort) summary of extracted {bridge} records."
    ),
    "snapshot": (
        "Per-{parent} latest-record snapshot over extracted {bridge} records, "
        "ordered by the declared incremental cursor and row key."
    ),
    "distribution": (
        "Per-({parent}, measure state) distribution of extracted {bridge} records."
    ),
}


def _library_marts(
    endpoints: tuple[DltEndpoint, ...],
    tables: dict[str, TableSpec],
    relationships: tuple[Relationship, ...],
    *,
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> tuple[tuple[MartSpec, ...], tuple[StarShape, ...]]:
    """Build funded marts, trying cursor-based ``temporal_grid`` first.

    Missing attributes are never invented; only curated columns may supply them.
    """
    table_specs = tuple(tables[name] for name in sorted(tables))
    # Keyness basis passed explicitly so the grain gate is never read as
    # disarmed; a PK-less transformer may fall back to its `_<parent>_id` link.
    candidates = chain_candidates(
        table_specs, relationships, key_parents=declared_key_parents(table_specs)
    )
    if not candidates:
        return (), ()
    cursors = {e.name: e.cursor_column for e in endpoints if e.cursor_column}
    max_marts, spread_grains, max_per_shape = difficulty_profile.mart_parameters(
        pool="dlt", default_budget=_MAX_LIBRARY_MARTS
    )
    marts, shapes, _names = build_marts(
        candidates,
        prefix=lambda e, suffix: f"{e.bridge}_{suffix}",
        max_marts=max_marts,
        spread_grains=spread_grains,
        max_per_shape=max_per_shape,
        description=lambda suffix, e: _MART_PROSE[suffix].format(
            parent=e.parent,
            bridge=e.bridge,
            child=e.child or e.bridge,
            cursor=cursors.get(e.bridge, e.bridge_timestamp),
        ),
        notes=(
            "derived from the connector's declared resource graph, primary "
            "keys and incremental cursors"
        ),
    )
    return marts, shapes


def _derive_marts(
    endpoints: tuple[DltEndpoint, ...],
    tables: dict[str, TableSpec],
    relationships: tuple[Relationship, ...] = (),
    *,
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> tuple[tuple[MartSpec, ...], tuple[StarShape, ...]]:
    """Library marts (temporal grid first) + entity dimension + activity.

    REFUSES a connector whose only mart would echo the stage-1 row counts —
    see the raise below.
    """
    parent, child = _pick_entity_graph(endpoints)
    marts: list[MartSpec] = []
    shapes: list[StarShape] = []
    lib_marts, lib_shapes = _library_marts(
        endpoints,
        tables,
        relationships,
        difficulty_profile=difficulty_profile,
    )
    marts.extend(lib_marts)
    shapes.extend(lib_shapes)
    if parent is not None:
        built = _entity_mart(
            parent,
            tables[parent.name],
            child,
            tables[child.name] if child is not None else None,
            child.link_column if child is not None else None,
            minimum_columns=(
                MIN_MART_COLUMNS
                if difficulty_profile.name == "challenging"
                else 0
            ),
        )
        if built is not None and all(m.name != built[0].name for m in marts):
            marts.append(built[0])
            shapes.append(built[1])
    # Summarize over the child/event resource when it carries the cursor, else
    # over the parent — the only other cursor that makes a time slice meaningful.
    activity: DltEndpoint | None = None
    if child is not None and child.cursor is not None:
        activity = child
    elif parent is not None and parent.cursor is not None:
        activity = parent
    if activity is not None:
        built = _activity_mart(
            activity,
            tables[activity.name],
            activity.link_column if activity.parent is not None else None,
            minimum_columns=(
                MIN_MART_COLUMNS
                if difficulty_profile.name == "challenging"
                else 0
            ),
        )
        if built is not None and all(m.name != built[0].name for m in marts):
            marts.append(built[0])
            shapes.append(built[1])
    # Refuse count-only marts because stage 1 already grades row counts.
    if not marts:
        raise ValueError(
            "no transform mart could be built from this connector's endpoint "
            "graph — a task whose only mart would echo the stage-1 row "
            "counts has no transform half to grade (the extract-load reward "
            "already measures extraction; add the connector to "
            "config/sources.yaml `excluded` or fund a real mart via curated "
            "columns)"
        )
    return tuple(marts), tuple(shapes)


# --- The IR bridge ---

def _selection_for(manifest: DltManifest, pool: str) -> PoolSelection:
    """Derive fallback identity and licensing from the manifest."""
    return PoolSelection.for_record(
        pool=pool,
        selector=manifest.selector,
        origin=Origin.DLT,
        license=manifest.license,
        attribution=manifest.attribution or f"dlt connector manifest {manifest.connector!r}",
        family=manifest.connector,
    )


def to_task_ir(
    manifest: DltManifest,
    *,
    pool: str = "dlt",
    selection: PoolSelection | None = None,
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> TaskIR:
    """Convert resources and transformer edges into a complete data project."""
    endpoints = manifest.loadable()
    if not endpoints:
        raise ValueError(
            f"connector {manifest.connector!r}: every endpoint is selected=False "
            "(extract-only); there is nothing to load, so there is no task"
        )
    sel = selection if selection is not None else _selection_for(manifest, pool)
    if sel.origin is not Origin.DLT:
        raise ValueError(
            f"connector {manifest.connector!r}: selection origin is "
            f"{sel.origin.value!r}, expected {Origin.DLT.value!r}"
        )

    loadable_names = {e.name for e in endpoints}

    # Synthesize keys only for keyless parents; composite keys are not replaced.
    surrogate_pks: dict[str, str] = {}
    for ep in endpoints:
        if ep.parent is None or ep.parent not in loadable_names:
            continue
        parent_ep = manifest.endpoint(ep.parent)
        if not parent_ep.primary_key:
            surrogate_pks[parent_ep.name] = f"_{parent_ep.name}_surrogate_id"
    if surrogate_pks:
        endpoints = tuple(
            e.model_copy(update={"primary_key": (surrogate_pks[e.name],)})
            if e.name in surrogate_pks
            else e
            for e in endpoints
        )
    by_name = {e.name: e for e in endpoints}

    tables: dict[str, TableSpec] = {}
    backends: list[BackendAssignment] = []
    relationships: list[Relationship] = []
    backend_by_table = _assign_backends(endpoints, sel.family_id)

    for ep in endpoints:
        desc = (
            f"dlt {ep.kind} {ep.name!r} at {ep.path} "
            f"(write_disposition={ep.write_disposition}"
            + (f", cursor={ep.cursor}" if ep.cursor else "")
            # Honest provenance: a guarded resource is emitted only under that
            # condition, so the docs must not present it as unconditional.
            + (f", opt-in: emitted only when {ep.guard}" if ep.guard else "")
            + ")"
        )
        link_type: ColumnType | None = None
        link_typed_after = ""
        if ep.parent is not None and ep.parent in by_name:
            link_type = _pk_column_type(by_name[ep.parent])
            if link_type is not None:
                link_typed_after = f"{ep.parent}.{by_name[ep.parent].primary_key[0]}"
        tables[ep.name] = TableSpec(
            name=ep.name,
            description=desc,
            columns=_endpoint_columns(
                ep,
                surrogate_pk=surrogate_pks.get(ep.name),
                link_type=link_type,
                link_typed_after=link_typed_after,
            ),
            primary_key=ep.primary_key,
        )
        backends.append(
            BackendAssignment(
                table=ep.name,
                backend=backend_by_table[ep.name],
                options=_backend_options(ep, manifest),
            )
        )

    for ep in endpoints:
        if ep.parent is None or ep.parent not in loadable_names:
            continue
        parent = by_name[ep.parent]
        if len(parent.primary_key) != 1:
            # A composite/absent parent key cannot be a single FK column; the
            # extraction dependency stays recorded in the backend options.
            continue
        link = ep.link_column
        if link is None:  # unreachable while ep.parent is set; never assert
            continue
        relationships.append(
            Relationship(
                child_table=ep.name,
                child_columns=(link,),
                parent_table=parent.name,
                parent_columns=(parent.primary_key[0],),
                required=True,
            )
        )

    table_specs = tuple(tables[name] for name in sorted(tables))
    rel_specs = tuple(
        sorted(relationships, key=lambda r: (r.child_table, r.parent_table))
    )
    _assert_relationship_types(manifest.connector, tables, rel_specs)
    marts, shapes = _derive_marts(
        endpoints,
        tables,
        rel_specs,
        difficulty_profile=difficulty_profile,
    )
    populations, attacks = derive_populations_and_attacks(
        task_id=sel.family_id,
        tables=table_specs,
        relationships=rel_specs,
        shapes=shapes,
        scale_hint=_dlt_scale_hint(table_specs, rel_specs),
        policy_conditions=(
            "SYNTHETIC data: a dlt connector manifest describes ENDPOINTS, not "
            "rows, so every row is generated from the declared schema.",
            f"Every REST resource ships MORE THAN {DLT_PAGE_SIZE} rows, so its "
            "fixture is served as several pages and the extraction must follow "
            "the pagination to completion; a loader that reads only the first "
            "page produces a mart that is short by construction.",
        ),
        backends=len({b.backend for b in backends}),
        # The load-side catalogue needs the ASSIGNMENTS, not just the count:
        # which attacks are honest depends on which table sits on which backend.
        backend_assignments=backends,
    )

    # task/cluster ids follow the SELECTION's family id: one connector is one
    # family, and a curator-supplied selection must not split them.
    task = TaskIR(
        task_id=sel.family_id,
        cluster_id=sel.family_id,
        title=f"dlt extraction: {manifest.connector}",
        tables=table_specs,
        relationships=rel_specs,
        backends=tuple(sorted(backends, key=lambda b: b.table)),
        marts=marts,
        populations=populations,
        attack_cases=attacks,
        **sel.ir_identity(),
    )
    # attacks.py mutates TaskIR.reference and has nothing to mutate without it.
    return attach_reference(task)


def _assert_relationship_types(
    connector: str,
    tables: dict[str, TableSpec],
    relationships: tuple[Relationship, ...],
) -> None:
    """Reject synthesized FK columns whose types differ from parent keys."""
    for rel in relationships:
        child, parent = tables[rel.child_table], tables[rel.parent_table]
        for ccol, pcol in zip(rel.child_columns, rel.parent_columns, strict=True):
            ct, pt = child.column(ccol).type, parent.column(pcol).type
            if ct is not pt:
                raise ValueError(
                    f"connector {connector!r}: FK {child.name}.{ccol} ({ct.value}) "
                    f"does not match {parent.name}.{pcol} ({pt.value}) — a link "
                    "column must carry its parent key's type (fail closed at "
                    "ingest, not at the reference load)"
                )


class DltContaminationError(ValueError):
    """Fatal dlt contamination carrying fatal and borderline collisions."""

    def __init__(self, message: str, collisions: tuple = ()) -> None:
        super().__init__(message)
        self.collisions = collisions


def assert_uncontaminated(task: TaskIR, index_dir: Path) -> list:
    """Return borderline collisions and raise on any fatal contamination."""
    from elt_taskgen.verification.contamination import ContaminationIndex

    collisions = ContaminationIndex(Path(index_dir)).check_pre(task)
    fatal = [c for c in collisions if c.fatal]
    if fatal:
        detail = "; ".join(f"[{c.kind}/{c.against}] {c.detail}" for c in fatal)
        raise DltContaminationError(
            f"contamination pre-check REJECTED {task.task_id!r}: {detail}",
            tuple(collisions),
        )
    return [c for c in collisions if not c.fatal]

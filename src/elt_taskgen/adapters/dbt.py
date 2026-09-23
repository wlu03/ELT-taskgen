"""Convert Fivetran/dbt manifests into complete connected-subgraph tasks.

Mart keys require evidence and realizable data; otherwise the mart is omitted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

import sqlglot
from sqlglot import exp
from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.generation.mart_plan import (
    AllNullAggregateWitness,
    KeyColumn,
    Measure,
    Passthrough,
    RoundBeforeSumWitness,
    StarJoin,
    StarShape,
    budget_problems,
    build_projection,
    build_rollup,
    guard_conditions,
    quote,
)
from elt_taskgen.generation.populations import derive_populations_and_attacks
from elt_taskgen.adapters.dbt_joins import recover_join_relationships
from elt_taskgen.reference.solution import attach_reference
from elt_taskgen.adapters import reassign_unsafe_file_tables
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    JoinType,
    MartColumn,
    MartColumnKind,
    MartOpKind,
    MartSpec,
    Origin,
    Relationship,
    TableSpec,
    TaskIR,
    canonical_json,
    derive_seed,
    sha256_hex,
)

# Deterministic backend rotation domain (order is part of the contract: changing
# it changes task content hashes).
_BACKEND_CYCLE: tuple[Backend, ...] = (
    Backend.POSTGRES,
    Backend.MONGODB,
    Backend.REST,
    Backend.S3,
    Backend.FILES,
)

_TYPE_MAP: dict[str, ColumnType] = {
    "int": ColumnType.INTEGER,
    "integer": ColumnType.INTEGER,
    "smallint": ColumnType.INTEGER,
    "tinyint": ColumnType.INTEGER,
    "serial": ColumnType.INTEGER,
    "bigint": ColumnType.BIGINT,
    "bigserial": ColumnType.BIGINT,
    "float": ColumnType.FLOAT,
    "float4": ColumnType.FLOAT,
    "float8": ColumnType.FLOAT,
    "real": ColumnType.FLOAT,
    "double": ColumnType.FLOAT,
    "double precision": ColumnType.FLOAT,
    "numeric": ColumnType.DECIMAL,
    "decimal": ColumnType.DECIMAL,
    "number": ColumnType.DECIMAL,
    "money": ColumnType.DECIMAL,
    "text": ColumnType.TEXT,
    "string": ColumnType.TEXT,
    "varchar": ColumnType.TEXT,
    "char": ColumnType.TEXT,
    "character varying": ColumnType.TEXT,
    "bool": ColumnType.BOOLEAN,
    "boolean": ColumnType.BOOLEAN,
    "date": ColumnType.DATE,
    "datetime": ColumnType.TIMESTAMP,
    "timestamp": ColumnType.TIMESTAMP,
    "timestamptz": ColumnType.TIMESTAMP,
    "timestamp_tz": ColumnType.TIMESTAMP,
    "timestamp_ntz": ColumnType.TIMESTAMP,
    "json": ColumnType.JSON,
    "jsonb": ColumnType.JSON,
    "variant": ColumnType.JSON,
    "object": ColumnType.JSON,
    "array": ColumnType.JSON,
}


def _map_type(data_type: str | None) -> ColumnType:
    """Map a dbt/warehouse type string to the canonical logical ColumnType."""
    if not data_type:
        return ColumnType.TEXT
    base = data_type.strip().lower()
    base = re.sub(r"\(.*\)$", "", base).strip()  # varchar(255) -> varchar
    return _TYPE_MAP.get(base, ColumnType.TEXT)


def _slug(text: str) -> str:
    """Lowercase identifier slug safe for family/task ids (never empty)."""
    s = re.sub(r"[^a-z0-9_.-]+", "_", text.strip().lower()).strip("_.-")
    return s or "x"


# Load-metadata columns — NEVER key candidates: Fivetran/dbt bookkeeping
# describes THE LOAD, not the business row, so a grain stated in terms of one is
# a false claim. `source_relation` is deliberately NOT excluded — in union
# models it discriminates the connector and appears in real GROUP BY grains.

#: Column-name prefix that marks a Fivetran load-metadata column.
LOAD_METADATA_PREFIX = "_fivetran"
#: Substrings that mark a load-metadata column wherever they appear in a name.
LOAD_METADATA_SUBSTRINGS: frozenset[str] = frozenset(
    {"fivetran_synced", "fivetran_deleted"}
)
#: Exact (lowercased) load-metadata column names.
LOAD_METADATA_COLUMNS: frozenset[str] = frozenset(
    {
        "fivetran_id",
        "fivetran_synced",
        "fivetran_deleted",
        "fivetran_active",
        "dbt_run_date",
        "dbt_updated_at",
        "dbt_valid_from",
        "dbt_valid_to",
        "dbt_scd_id",
        "_file",
        "_line",
        "_modified",
        "_loaded_at",
        "_etl_loaded_at",
    }
)


def is_load_metadata_column(name: str) -> bool:
    """True iff `name` is Fivetran/dbt LOAD bookkeeping, not business data.

    Load metadata can NEVER be a key column: not stable across loads, not part
    of the business grain.
    """
    lowered = name.strip().lower()
    if lowered.startswith(LOAD_METADATA_PREFIX):
        return True
    if lowered in LOAD_METADATA_COLUMNS:
        return True
    return any(s in lowered for s in LOAD_METADATA_SUBSTRINGS)


# CandidateSpec — module-owned raw view of the manifest

class DbtColumn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    data_type: str | None = None


class DbtNode(BaseModel):
    """One source table or model, reduced to what task extraction needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unique_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    package_name: str = ""
    resource_type: str = Field(min_length=1)  # 'source' | 'model'
    depends_on: tuple[str, ...] = ()          # upstream unique_ids (models only)
    columns: tuple[DbtColumn, ...] = ()
    description: str = ""
    #: The model's SQL AS WRITTEN — Jinja and all. Never emitted into a TaskIR.
    raw_code: str = ""
    #: The SQL with Jinja RENDERED — only on a `dbt compile` manifest, and what
    #: every recovery pass needs since sqlglot cannot parse Jinja. Never emitted.
    compiled_code: str = ""

    @property
    def sql_text(self) -> str:
        """The best SQL available for this model: compiled if dbt rendered it.

        ONE accessor, so recovery passes cannot disagree about which text.
        """
        return self.compiled_code or self.raw_code


class DbtTest(BaseModel):
    """One schema test: unique / not_null / relationships / composite unique."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unique_id: str = Field(min_length=1)
    #: 'unique'|'not_null'|'relationships'|'unique_combination_of_columns'
    test_name: str = Field(min_length=1)
    attached_to: str = Field(min_length=1)    # unique_id of the tested node
    column_name: str | None = None
    to: str | None = None                     # parent unique_id (relationships)
    to_field: str | None = None               # parent column (relationships)
    #: Composite key columns of a dbt_utils.unique_combination_of_columns test.
    columns: tuple[str, ...] = ()


class CandidateSpec(BaseModel):
    """Raw models/deps/tests/sources of one dbt package, pre-extraction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package_name: str = Field(min_length=1)
    sources: tuple[DbtNode, ...] = ()
    models: tuple[DbtNode, ...] = ()
    tests: tuple[DbtTest, ...] = ()
    #: SPDX id of the package if known; manifests do not carry one.
    license: str = "unspecified"
    #: ``{source table: {column: raw datatype text}}`` from the package's own
    #: ``get_<table>_columns`` macros. Raw text on purpose: `staging_type_map`
    #: is the ONE place it becomes a ColumnType.
    macro_column_types: dict[str, dict[str, str]] = Field(default_factory=dict)

    def node_by_id(self) -> dict[str, DbtNode]:
        return {n.unique_id: n for n in (*self.sources, *self.models)}


# Manifest parsing

_REF_RE = re.compile(r"ref\(\s*'(?P<name>[^']+)'\s*\)")
_SOURCE_RE = re.compile(r"source\(\s*'(?P<src>[^']+)'\s*,\s*'(?P<name>[^']+)'\s*\)")

#: dbt_utils' COMPOSITE uniqueness test: how a package declares a multi-col key.
COMPOSITE_UNIQUE_TEST = "unique_combination_of_columns"
_CONSUMED_TESTS = frozenset(
    {"unique", "not_null", "relationships", COMPOSITE_UNIQUE_TEST}
)


def _parse_columns(raw_columns: dict) -> tuple[DbtColumn, ...]:
    cols = []
    for cname, cinfo in raw_columns.items():
        cinfo = cinfo or {}
        cols.append(
            DbtColumn(
                name=cinfo.get("name") or cname,
                description=cinfo.get("description") or "",
                data_type=cinfo.get("data_type"),
            )
        )
    return tuple(sorted(cols, key=lambda c: c.name))


def _resolve_test_target(
    kwargs: dict, depends: list[str], attached: str | None,
    name_index: dict[str, list[str]],
) -> tuple[str | None, str | None]:
    """Resolve (attached child uid, parent uid) for a relationships test."""
    to_expr = str(kwargs.get("to") or "")
    parent_uid: str | None = None
    m = _REF_RE.search(to_expr) or _SOURCE_RE.search(to_expr)
    if m:
        target_name = m.group("name")
        for uid in name_index.get(target_name, []):
            if uid in depends:
                parent_uid = uid
                break
        if parent_uid is None:
            candidates = name_index.get(target_name, [])
            parent_uid = candidates[0] if candidates else None
    if attached is None:
        others = [d for d in depends if d != parent_uid]
        attached = others[0] if others else None
    return attached, parent_uid


#: The suffix dbt gives the project of a Fivetran package's CI harness.
INTEGRATION_TESTS_SUFFIX = "_integration_tests"

#: `macro.<pkg>.get_<table>_columns` — the Fivetran convention for the staging
#: column declaration of ONE source table.
_COLUMNS_MACRO_RE = re.compile(r"^macro\.[^.]+\.get_(?P<table>\w+)_columns$")
#: One ``{"name": ..., "datatype": ...}`` entry of that macro's Jinja list; a
#: datatype that is neither a literal nor `dbt.type_*()` is left unread.
_COLUMNS_MACRO_ENTRY_RE = re.compile(
    r"\{\s*['\"]name['\"]\s*:\s*['\"](?P<name>\w+)['\"]\s*,\s*"
    r"['\"]datatype['\"]\s*:\s*(?P<type>dbt\.type_\w+\(\)|['\"][^'\"]*['\"])"
)
#: `dbt.type_*()` cross-database macros -> the warehouse type they render to.
_DBT_TYPE_MACROS: dict[str, str] = {
    "dbt.type_string()": "text",
    "dbt.type_timestamp()": "timestamp",
    "dbt.type_int()": "integer",
    "dbt.type_bigint()": "bigint",
    "dbt.type_float()": "float",
    "dbt.type_numeric()": "numeric",
    "dbt.type_boolean()": "boolean",
}


def _macro_column_types(macros: dict) -> dict[str, dict[str, str]]:
    """Return raw macro-declared column types, keeping the first duplicate."""
    out: dict[str, dict[str, str]] = {}
    for uid in sorted(macros):
        m = _COLUMNS_MACRO_RE.match(uid)
        if m is None:
            continue
        body = (macros[uid] or {}).get("macro_sql") or ""
        for entry in _COLUMNS_MACRO_ENTRY_RE.finditer(body):
            raw_type = entry.group("type").strip()
            mapped = _DBT_TYPE_MACROS.get(raw_type, raw_type.strip("'\""))
            out.setdefault(m.group("table"), {}).setdefault(entry.group("name"), mapped)
    return out


def load_manifest(manifest_path: Path) -> CandidateSpec:
    """Parse a manifest, rejecting unreadable bodies or unresolved project names."""
    raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"manifest {manifest_path} is not a JSON object")

    sources: list[DbtNode] = []
    for uid, s in sorted((raw.get("sources") or {}).items()):
        sources.append(
            DbtNode(
                unique_id=uid,
                name=s.get("name") or uid.rsplit(".", 1)[-1],
                package_name=s.get("package_name") or "",
                resource_type="source",
                columns=_parse_columns(s.get("columns") or {}),
                description=s.get("description") or "",
            )
        )

    models: list[DbtNode] = []
    raw_tests: list[dict] = []
    for uid, n in sorted((raw.get("nodes") or {}).items()):
        rt = n.get("resource_type")
        if rt == "model":
            deps = tuple(sorted((n.get("depends_on") or {}).get("nodes") or []))
            models.append(
                DbtNode(
                    unique_id=uid,
                    name=n.get("name") or uid.rsplit(".", 1)[-1],
                    package_name=n.get("package_name") or "",
                    resource_type="model",
                    depends_on=deps,
                    columns=_parse_columns(n.get("columns") or {}),
                    description=n.get("description") or "",
                    raw_code=n.get("raw_code") or "",
                    compiled_code=n.get("compiled_code") or "",
                )
            )
        elif rt == "test" and n.get("test_metadata"):
            raw_tests.append({"unique_id": uid, **n})

    name_index: dict[str, list[str]] = {}
    for node in (*sources, *models):
        name_index.setdefault(node.name, []).append(node.unique_id)
    for uids in name_index.values():
        uids.sort()

    tests: list[DbtTest] = []
    for t in raw_tests:
        meta = t["test_metadata"]
        tname = meta.get("name") or ""
        if tname not in _CONSUMED_TESTS:
            continue  # custom tests carry no schema semantics we consume
        kwargs = meta.get("kwargs") or {}
        depends = sorted((t.get("depends_on") or {}).get("nodes") or [])
        attached = t.get("attached_node")
        parent_uid: str | None = None
        if tname == "relationships":
            attached, parent_uid = _resolve_test_target(
                kwargs, depends, attached, name_index
            )
        elif attached is None:
            attached = depends[0] if depends else None
        if attached is None:
            continue  # unattachable test: no schema fact to record
        combo = kwargs.get("combination_of_columns")
        combo_cols = (
            tuple(str(c) for c in combo if str(c).strip())
            if isinstance(combo, (list, tuple))
            else ()
        )
        if tname == COMPOSITE_UNIQUE_TEST and not combo_cols:
            continue  # a composite test naming no columns asserts nothing
        tests.append(
            DbtTest(
                unique_id=t["unique_id"],
                test_name=tname,
                attached_to=attached,
                column_name=kwargs.get("column_name"),
                to=parent_uid,
                to_field=kwargs.get("field"),
                columns=combo_cols,
            )
        )

    package = (raw.get("metadata") or {}).get("project_name") or next(
        (n.package_name for n in models if n.package_name), ""
    ) or next((n.package_name for n in sources if n.package_name), "")
    if not package:
        raise ValueError(f"manifest {manifest_path}: no project/package name found")
    if package.endswith(INTEGRATION_TESTS_SUFFIX) and len(package) > len(
        INTEGRATION_TESTS_SUFFIX
    ):
        # The family must follow the PACKAGE, not the CI harness around it:
        # `<pkg>_integration_tests` evades the contamination family check that
        # `<pkg>` trips.
        package = package[: -len(INTEGRATION_TESTS_SUFFIX)]

    return CandidateSpec(
        package_name=package,
        sources=tuple(sorted(sources, key=lambda n: n.unique_id)),
        models=tuple(sorted(models, key=lambda n: n.unique_id)),
        tests=tuple(sorted(tests, key=lambda t: t.unique_id)),
        macro_column_types=_macro_column_types(raw.get("macros") or {}),
    )


# Connected-subgraph task extraction

def _connected_components(spec: CandidateSpec) -> list[list[str]]:
    """Undirected connected components over sources+models (sorted, stable)."""
    nodes = sorted(spec.node_by_id())
    adj: dict[str, set[str]] = {uid: set() for uid in nodes}
    known = set(nodes)
    for m in spec.models:
        for dep in m.depends_on:
            if dep in known:
                adj[m.unique_id].add(dep)
                adj[dep].add(m.unique_id)
    seen: set[str] = set()
    components: list[list[str]] = []
    for start in nodes:
        if start in seen:
            continue
        comp: list[str] = []
        frontier = [start]
        seen.add(start)
        while frontier:
            cur = frontier.pop()
            comp.append(cur)
            for nb in sorted(adj[cur]):
                if nb not in seen:
                    seen.add(nb)
                    frontier.append(nb)
        components.append(sorted(comp))
    return components


def _source_closure(model_uid: str, spec: CandidateSpec) -> set[str]:
    """All source unique_ids the model transitively depends on."""
    by_id = spec.node_by_id()
    closure: set[str] = set()
    stack = [model_uid]
    visited: set[str] = set()
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        node = by_id.get(cur)
        if node is None:
            continue
        if node.resource_type == "source":
            closure.add(cur)
        else:
            stack.extend(node.depends_on)
    return closure


def _ancestors(model_uid: str, spec: CandidateSpec) -> set[str]:
    """`model_uid` plus EVERY node it transitively depends on (sources included).

    The full closure a cut must keep to be a complete data project.
    """
    by_id = spec.node_by_id()
    seen: set[str] = set()
    stack = [model_uid]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in by_id:
            continue
        seen.add(cur)
        stack.extend(by_id[cur].depends_on)
    return seen


def _marts_stay_connected(ancestors: dict[str, set[str]]) -> bool:
    """Return whether surviving marts remain connected through shared ancestors."""
    remaining = sorted(ancestors)
    if not remaining:
        return False
    group = {remaining[0]}
    covered = set(ancestors[remaining[0]])
    changed = True
    while changed:
        changed = False
        for uid in remaining:
            if uid in group:
                continue
            if ancestors[uid] & covered:
                group.add(uid)
                covered |= ancestors[uid]
                changed = True
    return len(group) == len(remaining)


def _require_mart_columns(model: DbtNode) -> None:
    """Fail closed on a mart with no columns: the grading surface is undefined.

    Checked BEFORE key derivation, so it reports as a manifest defect rather
    than as a merely keyless mart.
    """
    if not model.columns:
        raise ValueError(
            f"mart model {model.unique_id!r} declares no columns; the grading "
            "surface would be undefined"
        )


def _tests_for(spec: CandidateSpec, uid: str, test_name: str) -> list[DbtTest]:
    return [t for t in spec.tests if t.attached_to == uid and t.test_name == test_name]


#: Suffix of the ONE value added to an adopted domain so the negative class is
#: inhabited; derived from the column's own name.
_OUT_OF_DOMAIN_SUFFIX = "_other"


def _adopted_domain(column: str, selected: set[str]) -> tuple[str, ...]:
    """Return selected literals followed by one residual domain value.

    This ordering exercises both guarded and unguarded rows in small populations.
    """
    filler = f"{column}{_OUT_OF_DOMAIN_SUFFIX}"
    ordered = tuple(sorted(selected - {filler}))
    return ordered + (filler,)


def _adopt_column_type(
    column: DbtColumn,
    staging: ColumnType | None,
    required: ColumnType | None,
) -> ColumnType:
    """Choose a source type by declared, non-text cast, use, text cast, then text."""
    if column.data_type:
        return _map_type(column.data_type)
    if staging is not None and staging is not ColumnType.TEXT:
        return staging
    if required is not None:
        return required
    if staging is not None:
        return staging
    return ColumnType.TEXT


def _column_types(
    node: DbtNode,
    staging_types: dict[str, ColumnType] | None,
    type_overrides: dict[str, ColumnType] | None,
) -> dict[str, ColumnType]:
    """column -> the type `_source_to_table` will ship it with (same rule)."""
    staging = staging_types or {}
    overrides = type_overrides or {}
    return {
        c.name: _adopt_column_type(c, staging.get(c.name), overrides.get(c.name))
        for c in node.columns
    }


def _source_to_table(
    spec: CandidateSpec,
    node: DbtNode,
    *,
    grounded_keys: tuple[str, ...] = (),
    grain_not_null: tuple[str, ...] = (),
    type_overrides: dict[str, ColumnType] | None = None,
    domain_overrides: dict[str, tuple[str, ...]] | None = None,
    staging_types: dict[str, ColumnType] | None = None,
    lookup_keys: tuple[str, ...] = (),
    relationships: tuple[Relationship, ...] = (),
) -> TableSpec:
    """Build a source table whose generated data satisfies grounded grains.

    Grounded grain columns are unique and non-null. Lookup parents are unique
    unless the key is an FK, which may repeat. Type overrides apply only when
    metadata is absent; text domain overrides contain only recovered literals.
    """
    if not node.columns:
        raise ValueError(
            f"source {node.unique_id!r} declares no columns; cannot build a TableSpec"
        )
    not_null = {t.column_name for t in _tests_for(spec, node.unique_id, "not_null")}
    unique = {t.column_name for t in _tests_for(spec, node.unique_id, "unique")}
    # Same exclusion as mart keys: a load-generated surrogate is not a key.
    unique = {c for c in unique if c and not is_load_metadata_column(c)}
    pk_candidates = [c.name for c in node.columns if c.name in unique and c.name in not_null]
    declared = {col.name for col in node.columns}
    grain_unique = tuple(c for c in grounded_keys if c in declared)
    fk_columns = {
        c
        for rel in relationships
        if rel.child_table == node.name
        for c in rel.child_columns
    }
    # Declared column order, so the pick does not depend on hop build order.
    lookup_unique = tuple(
        c.name
        for c in node.columns
        if c.name in set(lookup_keys) and c.name not in fk_columns
    )
    tested_pk = tuple(pk_candidates[:1])
    # A grain-bearing column becomes the PRIMARY KEY, not merely a business
    # key, when the manifest declares none: `source_data` injects duplicate
    # rows into the stress population of every PK-less table, and the primary
    # key is the only identity claim the solver bundle prints.
    primary_key = tested_pk or tuple(grain_unique[:1]) or tuple(lookup_unique[:1])
    tested_key = tuple(c for c in sorted(unique - set(primary_key)) if c is not None)[:1]
    # Every remaining grain-bearing column must still be minted, so the UNION:
    # one table can host two marts grained differently.
    business_key = tuple(
        sorted(
            (set(tested_key) | set(grain_unique) | set(lookup_unique))
            - set(primary_key)
        )
    )
    types = _column_types(node, staging_types, type_overrides)
    domains = domain_overrides or {}
    identity = set(primary_key) | set(business_key)

    def _column(c: DbtColumn) -> ColumnSpec:
        ctype = types[c.name]
        # An identity column is minted, never drawn from a domain: a domain
        # would collapse the key space and destroy the grain.
        domain = domains.get(c.name)
        if domain is not None and (ctype is not ColumnType.TEXT or c.name in identity):
            domain = None
        return ColumnSpec(
            name=c.name,
            type=ctype,
            nullable=not (
                c.name in not_null
                or c.name in identity
                or c.name in set(grain_not_null)
            ),
            description=c.description or f"Column {c.name} of source table {node.name}.",
            enum_values=domain,
        )

    columns = tuple(_column(c) for c in node.columns)
    table_description = node.description or f"Source table {node.name}."
    if re.search(r"\bversions?\b", table_description, re.IGNORECASE):
        table_description = table_description.rstrip()
        if primary_key:
            identity_text = ", ".join(primary_key)
            table_description += (
                f" In this generated task, {identity_text} uniquely identifies a "
                f"row and at most one row per {identity_text} is present."
            )
        table_description += (
            " Rows are used exactly as supplied in this generated task; no "
            "history-version selection or version deduplication is performed."
        )
    return TableSpec(
        name=node.name,
        description=table_description,
        columns=columns,
        primary_key=primary_key,
        business_key=business_key,
    )


def _component_relationships(
    spec: CandidateSpec, source_uids: set[str]
) -> tuple[Relationship, ...]:
    by_id = spec.node_by_id()
    rels: list[Relationship] = []
    seen: set[tuple] = set()
    for t in spec.tests:
        if t.test_name != "relationships" or t.to is None:
            continue
        if t.attached_to not in source_uids or t.to not in source_uids:
            continue
        if not t.column_name or not t.to_field:
            continue
        child = by_id[t.attached_to]
        parent = by_id[t.to]
        child_not_null = {
            x.column_name for x in _tests_for(spec, t.attached_to, "not_null")
        }
        key = (child.name, t.column_name, parent.name, t.to_field)
        if key in seen:
            continue
        seen.add(key)
        rels.append(
            Relationship(
                child_table=child.name,
                child_columns=(t.column_name,),
                parent_table=parent.name,
                parent_columns=(t.to_field,),
                required=t.column_name in child_not_null,
            )
        )
    # Union in the join-predicate recovery, restricted to the CUT: a
    # relationship naming an outside table would be unloadable. A declared
    # `relationships` test wins, carrying the vendor's not_null evidence.
    allowed = {by_id[u].name for u in source_uids if u in by_id}
    for rel in recover_join_relationships(spec):
        if rel.child_table not in allowed or rel.parent_table not in allowed:
            continue
        key = (
            rel.child_table,
            rel.child_columns[0] if len(rel.child_columns) == 1 else rel.child_columns,
            rel.parent_table,
            rel.parent_columns[0] if len(rel.parent_columns) == 1 else rel.parent_columns,
        )
        if key in seen:
            continue
        seen.add(key)
        rels.append(rel)
    return tuple(sorted(rels, key=lambda r: (r.child_table, r.child_columns, r.parent_table)))


# Mart key derivation — a ranked ladder of manifest evidence

#: SkippedCut.kind values.
UNUSABLE_CUT_SKIP = "unusable-cut"
KEYLESS_MART_SKIP = "keyless-mart"
#: Source tables no SURVIVING mart reads; pruned rather than shipped unread.
STRANDED_SOURCE_SKIP = "stranded-source"

#: SkippedCut.scope values — WHAT was dropped, so no caller has to sniff
#: `reason` text.
CUT_SCOPE = "cut"
MART_SCOPE = "mart"
SOURCE_SCOPE = "source"


class KeylessMartError(ValueError):
    """No defensible grain could be derived for a mart model.

    A ValueError subclass so it travels the normal skip path. Fail closed: an
    unknown grain is not an excuse to invent one.
    """


class UnprovableGrainError(KeylessMartError):
    """Raised when evidenced grain cannot be realized by generated data."""


class DegenerateMartError(KeylessMartError):
    """Raised when a rollup's grain covers every projected column."""


class DeclaredGrainViolation(ValueError):
    """Raised when a built plan does not produce its declared grain."""


def _grouped_grain(grain: str, attributes: tuple[str, ...]) -> str:
    """Describe the full grouping key, including carried attributes."""
    if not attributes:
        return grain
    head = grain.rstrip()
    sep = "" if head.endswith(".") else "."
    # No "grouped": the author treats it as operator vocabulary and wrote
    # "carried together" instead, failing grain fidelity on "grouped" in
    # two runs (dbt__twitter_ads, batch10 runs K and M, 2026-09-11).
    return (
        f"{head}{sep} The carried columns {', '.join(attributes)} are part of "
        "this grain together with these keys, so rows that agree on the keys "
        "but differ in one of them are separate output rows."
    )


def _declared_grain(key_columns: tuple[str, ...], description: str) -> str:
    """Describe the derived key columns, followed by vendor grain prose."""
    head = f"One row per {', '.join(key_columns)}."
    tail = " ".join((description or "").split())
    return f"{head} {tail}".strip()


#: Role suffixes Fivetran appends to a mart name after the entity it is about
#: (`servicenow__incident_enhanced` -> entity `incident`).
_ENTITY_ROLE_SUFFIXES: tuple[str, ...] = (
    "_enhanced",
    "_enriched",
    "_report",
    "_summary",
    "_overview",
    "_details",
    "_detail",
    "_history",
)

_JINJA_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.S)
_JINJA_STATEMENT = re.compile(r"\{%.*?%\}", re.S)
#: Placeholder an inlined `{{ ... }}` becomes so the residue still parses.
_JINJA_PLACEHOLDER = "_dbt_jinja_expr"


def _group_by_grain(model: DbtNode, columns: set[str]) -> list[str]:
    """Return recoverable output columns of the outermost ``GROUP BY``."""
    sql = _JINJA_EXPRESSION.sub(_JINJA_PLACEHOLDER, model.sql_text or "")
    if not sql.strip() or _JINJA_STATEMENT.search(sql):
        return []  # jinja control flow: the emitted SQL is not this text
    try:
        parsed = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception:  # noqa: BLE001 — any parse failure is "no evidence"
        return []
    if not isinstance(parsed, exp.Select):
        return []
    group = parsed.args.get("group")
    if group is None:
        return []
    projections = parsed.expressions
    grain: list[str] = []
    for item in group.expressions:
        if isinstance(item, exp.Literal) and item.is_int:
            index = int(item.name) - 1     # `group by 1,2,3` is 1-based
            if not 0 <= index < len(projections):
                return []
            name = projections[index].alias_or_name
        else:
            name = item.alias_or_name
        if not name or name not in columns:
            return []  # grouped on something the mart does not output
        grain.append(name)
    return grain


def _dataflow_group_by_grain(
    model: DbtNode,
    columns: set[str],
    *,
    enforce_key_width: bool = True,
) -> list[str]:
    """Return the first inner grouping reached through pure pass-through selects.

    Any join, lateral, set operation, or ambiguous path refuses recovery. The
    mart-key gate later verifies uniqueness on generated rows.
    """
    ast = parse_model_sql(model)
    if ast is None or not isinstance(ast, exp.Select):
        return []
    if ast.args.get("group") is not None:
        # The ROOT aggregates, so its GROUP BY is the grain (rung 3's claim).
        # Walking past would name an INNER grain whose tuples stay distinct in
        # the output, which not even the mart-key-unique gate can catch.
        return []
    ctes = {c.alias_or_name: c.this for c in ast.find_all(exp.CTE)}

    def from_relation(select: exp.Select) -> str | None:
        """The single FROM relation name of a pass-through select, else None."""
        if select.args.get("joins") or select.args.get("laterals"):
            return None
        frm = select.args.get("from") or select.args.get("from_")
        if frm is None:
            return None
        this = frm.this
        if isinstance(this, exp.Table):
            return this.name
        return None

    select: exp.Select | None = ast
    seen: set[str] = set()
    for _ in range(len(ctes) + 2):  # bounded: a cycle yields [] via `seen`
        if select is None or not isinstance(select, exp.Select):
            return []
        if select.args.get("group") is not None and select is not ast:
            break  # the grouping select
        name = from_relation(select)
        if name is None or name in seen or name not in ctes:
            return []
        seen.add(name)
        select = ctes[name]
    else:
        return []

    group = select.args.get("group")
    projections = select.expressions
    grain: list[str] = []
    for item in group.expressions:
        if isinstance(item, exp.Literal) and item.is_int:
            index = int(item.name) - 1
            if not 0 <= index < len(projections):
                return []
            name = projections[index].alias_or_name
        elif isinstance(item, exp.Column):
            name = item.name
        else:
            return []
        if not name:
            return []
        if name == "source_relation" or is_load_metadata_column(name):
            # One distinct literal per package, so keeping it would widen every
            # grain by a constant column. Stripped, never a refusal.
            continue
        if name not in columns:
            return []  # grouped on something the mart does not output
        grain.append(name)
    if not grain or (enforce_key_width and len(grain) * 2 >= len(columns)):
        # Width gate: a grain covering half the mart makes `keys_only`
        # near-identity and squeezes info-content.
        return []
    return grain


def _entity_id_columns(model_name: str, columns: set[str]) -> list[str]:
    """Return the unambiguous ``<entity>_id`` implied by the model name."""
    stem = model_name.split("__")[-1]
    for suffix in _ENTITY_ROLE_SUFFIXES:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            stem = stem[: -len(suffix)]
            break
    stems = [stem] + ([stem[:-1]] if stem.endswith("s") and len(stem) > 1 else [])
    for candidate_stem in stems:
        for candidate in (f"{candidate_stem}_id", f"{candidate_stem}_key"):
            if candidate in columns:
                return [candidate]
    for candidate_stem in stems:
        matches = sorted(c for c in columns if c.endswith(f"_{candidate_stem}_id"))
        if len(matches) == 1:
            return matches
    return []


def _upstream_identity_columns(
    spec: CandidateSpec, model: DbtNode, columns: set[str]
) -> list[str]:
    """Identifier columns the mart CARRIES from an upstream declared key.

    Restricted to `*_id`/`*_key` names so a merely-not-null attribute cannot
    become a grain claim.
    """
    by_id = spec.node_by_id()
    declared: dict[str, set[str]] = {}
    for test in spec.tests:
        if test.test_name in {"unique", "not_null"} and test.column_name:
            declared.setdefault(test.attached_to, set()).add(test.column_name)
        for column in test.columns:
            declared.setdefault(test.attached_to, set()).add(column)

    seen: set[str] = set()
    stack = [model.unique_id]
    found: set[str] = set()
    while stack:
        uid = stack.pop()
        if uid in seen:
            continue
        seen.add(uid)
        found |= declared.get(uid, set())
        node = by_id.get(uid)
        if node is not None:
            stack.extend(node.depends_on)
    return sorted(
        c
        for c in found
        if c in columns and (c.endswith("_id") or c.endswith("_key"))
    )


def mart_key_columns(spec: CandidateSpec, model: DbtNode) -> tuple[tuple[str, ...], str]:
    """Return mart keys and their highest-ranked evidence rule.

    Precedence is composite-unique test, unique test, outer grouping, non-null
    test, model-derived entity ID, then upstream key. Load metadata is excluded,
    and at least one non-key column must remain. Otherwise raise
    ``KeylessMartError``.
    """
    col_names = [c.name for c in model.columns]
    columns = set(col_names)
    business = {c for c in col_names if not is_load_metadata_column(c)}

    def composite() -> list[str]:
        for test in _tests_for(spec, model.unique_id, COMPOSITE_UNIQUE_TEST):
            # All-or-nothing: a composite key missing a column, or holding a
            # metadata column, is not this mart's key.
            if all(c in business for c in test.columns):
                return list(test.columns)
        return []

    def tested(test_name: str) -> list[str]:
        named = {t.column_name for t in _tests_for(spec, model.unique_id, test_name)}
        return [c for c in col_names if c in named and c in business]

    #: (evidence name, lazy producer) — in order, so the sqlglot parse and the
    #: dependency walk only run when reached.
    rules: tuple[tuple[str, Callable[[], list[str]]], ...] = (
        ("dbt_utils.unique_combination_of_columns test", composite),
        ("unique test", lambda: tested("unique")),
        ("model GROUP BY grain",
         lambda: [c for c in _group_by_grain(model, columns) if c in business]),
        ("not_null test", lambda: tested("not_null")),
        # A rescue below every test-backed rule and above the naming
        # conventions; the mart-key-unique gate verifies it against real rows.
        ("inner GROUP BY grain (evidence, not proof)",
         lambda: [c for c in _dataflow_group_by_grain(model, columns) if c in business]),
        ("<entity>_id naming convention",
         lambda: _entity_id_columns(model.name, business)),
        ("upstream declared key carried into the mart",
         lambda: _upstream_identity_columns(spec, model, business)),
    )

    tried: list[str] = []
    for rule, candidates_of in rules:
        chosen = [c for c in col_names if c in set(candidates_of())]
        if not chosen:
            tried.append(f"{rule}: no candidate")
            continue
        if len(chosen) >= len(col_names):
            # Every column a "key" leaves nothing informative, and the
            # keys_only shortcut probe becomes the identity query.
            tried.append(f"{rule}: covers every column")
            continue
        return tuple(chosen), rule
    raise KeylessMartError(
        f"mart model {model.unique_id!r} declares no defensible key: "
        f"{'; '.join(tried)}. Load-metadata columns "
        f"({', '.join(sorted(c for c in col_names if is_load_metadata_column(c))) or 'none'}) "
        "are excluded from key candidacy; the mart's grain is unknown, so the "
        "mart is dropped — and its cut refused when no keyed mart remains — "
        "rather than keyed arbitrarily"
    )


def parse_model_sql(model: DbtNode) -> exp.Expression | None:
    """Parse model SQL, returning no evidence for Jinja or invalid syntax."""
    text = (model.sql_text or "").strip()
    if not text:
        return None
    if _JINJA_STATEMENT.search(text) or _JINJA_EXPRESSION.search(text):
        return None
    try:
        return sqlglot.parse_one(text, read="duckdb")
    except Exception:  # noqa: BLE001 — any parse failure is "no evidence"
        return None


#: Single-argument functions that RENAME a column without changing what it
#: holds. A CASE or arithmetic expression is NOT one — it would ground a
#: derived flag onto the column it happens to read.
_RENAME_WRAPPERS = (exp.Cast, exp.TryCast, exp.Trim, exp.Lower, exp.Upper)


def _rename_ref(body: exp.Expression) -> str | None:
    """The source column this projection RENAMES, or None if it computes."""
    node = body
    while isinstance(node, _RENAME_WRAPPERS):
        node = node.this
    return node.name if isinstance(node, exp.Column) and node.name else None


def _aliased_projection_refs(model: DbtNode) -> tuple[tuple[str, str], ...]:
    """Return alias/source pairs for rename-only projections."""
    ast = parse_model_sql(model)
    if ast is None:
        return ()
    out: list[tuple[str, str]] = []
    for select in ast.find_all(exp.Select):
        for projection in select.expressions:
            if not isinstance(projection, exp.Alias):
                continue
            alias = projection.alias_or_name
            ref = _rename_ref(projection.this)
            if alias and ref:
                out.append((alias, ref))
    return tuple(out)


def _source_uids_by_name(spec: CandidateSpec) -> dict[str, list[str]]:
    """source table name -> unique_ids declaring it (sorted; >1 = ambiguous)."""
    out: dict[str, list[str]] = {}
    for node in spec.sources:
        out.setdefault(node.name, []).append(node.unique_id)
    for uids in out.values():
        uids.sort()
    return out


def _source_resolver(
    spec: CandidateSpec,
    model: DbtNode,
    tree: exp.Expression | None = None,
) -> Callable[[str], frozenset[str]]:
    """Resolve relation names to source tables, marking ambiguity as unknown."""
    by_id = spec.node_by_id()
    models_by_name = {m.name: m for m in spec.models}
    uids_by_name = _source_uids_by_name(spec)
    if tree is None:
        tree = parse_model_sql(model)
    ctes: dict[str, exp.CTE] = (
        {c.alias_or_name: c for c in tree.find_all(exp.CTE)} if tree is not None else {}
    )
    memo: dict[str, frozenset[str]] = {}

    def resolve(name: str, seen: frozenset[str] = frozenset()) -> frozenset[str]:
        if name in memo:
            return memo[name]
        if name in seen:
            return frozenset()
        seen = seen | {name}
        if name in ctes:
            found: set[str] = set()
            for t in ctes[name].find_all(exp.Table):
                found |= resolve(t.name, seen)
            result = frozenset(found)
        elif name in models_by_name:
            result = frozenset(
                by_id[u].name for u in _source_closure(models_by_name[name].unique_id, spec)
            )
        elif len(uids_by_name.get(name, [])) == 1:
            result = frozenset({name})
        else:
            result = frozenset({f"?{name}"})
        if not seen - {name}:
            memo[name] = result  # only top-level results are cycle-free facts
        return result

    return resolve


def _from_and_joins(select: exp.Select) -> list[exp.Expression]:
    """The FROM relation and every JOIN relation of one SELECT, in order.

    Both `from` and `from_` are read: sqlglot renamed the arg between releases.
    """
    out: list[exp.Expression] = []
    frm = select.args.get("from") or select.args.get("from_")
    if frm is not None and frm.this is not None:
        out.append(frm.this)
    for join in select.args.get("joins") or ():
        if join.this is not None:
            out.append(join.this)
    return out


def _select_scope(
    select: exp.Select, resolver: Callable[[str], frozenset[str]]
) -> dict[str, frozenset[str]]:
    """Map a select's relation aliases to resolved source tables or unknowns."""
    scope: dict[str, frozenset[str]] = {}
    for relation in _from_and_joins(select):
        if isinstance(relation, exp.Table):
            scope[relation.alias_or_name] = resolver(relation.name)
        elif isinstance(relation, exp.Subquery):
            found: set[str] = set()
            for t in relation.find_all(exp.Table):
                found |= resolver(t.name)
            scope[relation.alias_or_name or "?subquery"] = frozenset(found) or frozenset(
                {"?subquery"}
            )
        else:
            key = relation.alias_or_name or f"?{type(relation).__name__.lower()}"
            scope[key] = frozenset({f"?{type(relation).__name__.lower()}"})
    return scope


#: One `<expr> as <alias>` projection of a staging model, jinja and all. Only
#: single-column renames: an alias over an expression is not column lineage.
_STAGING_ALIAS_RE = re.compile(
    r"^\s*(?:cast\(\s*)?([a-z_][a-z0-9_]*)"
    r"(?:\s+as\s+\{\{[^}]*\}\}\s*\))?"
    r"\s+as\s+([a-z_][a-z0-9_]*)\s*,?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def staging_alias_map(spec: CandidateSpec) -> dict[str, dict[str, str]]:
    """Recover rename lineage from parsed SQL and recognized Jinja casts.

    Accept only one source table and declared source columns.
    """
    by_id = spec.node_by_id()
    #: Keyed by unique_id, not name: two same-named sources must not merge
    #: their column sets. Consumers still see table NAMES.
    source_cols_by_uid = {n.unique_id: {c.name for c in n.columns} for n in spec.sources}
    uids_by_name = _source_uids_by_name(spec)
    out: dict[str, dict[str, str]] = {}
    for model in sorted(spec.models, key=lambda m: m.unique_id):
        closure = _source_closure(model.unique_id, spec)
        if len(closure) != 1:
            continue
        source_uid = next(iter(closure))
        table = by_id[source_uid].name
        known = source_cols_by_uid.get(source_uid, set())

        def _record(src: str, alias: str, *, _table=table, _known=known) -> None:
            src, alias = src.lower(), alias.lower()
            if src == alias or src not in _known:
                return
            out.setdefault(alias, {}).setdefault(_table, src)

        for alias, ref in _aliased_projection_refs(model):
            _record(ref, alias)
        for src, alias in _STAGING_ALIAS_RE.findall(model.raw_code or ""):
            _record(src, alias)

    # MART-LEVEL hop-alias lineage, which the single-source read above cannot
    # see: recover `<join_alias>.<col> AS <mart_col>` from MULTI-source models
    # where the qualifier resolves to EXACTLY ONE source table and the column
    # is (or staging-renames to) a declared column of it.
    for model in sorted(spec.models, key=lambda m: m.unique_id):
        closure = _source_closure(model.unique_id, spec)
        if len(closure) <= 1:
            continue  # the single-source read above already covered it
        sql = model.compiled_code
        if not sql.strip():
            continue
        try:
            tree = sqlglot.parse_one(sql, read="duckdb")
        except Exception:
            continue
        # Only a source this model's closure reads, and only when its name is
        # unambiguous in the package, may receive a rename.
        source_names = {
            by_id[u].name for u in closure if len(uids_by_name.get(by_id[u].name, [])) == 1
        }
        resolve = _source_resolver(spec, model, tree)

        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                if not isinstance(projection, exp.Alias):
                    continue
                inner = projection.this
                if not isinstance(inner, exp.Column) or not inner.table:
                    continue
                resolved = resolve(inner.table)
                if len(resolved) != 1:
                    continue
                table = next(iter(resolved))
                if table.startswith("?") or table not in source_names:
                    continue
                col = inner.name.lower()
                known = source_cols_by_uid.get(uids_by_name[table][0], set())
                src = col if col in known else out.get(col, {}).get(table)
                if src is None:
                    continue
                mart_col = projection.alias_or_name.lower()
                if mart_col and src != mart_col:
                    out.setdefault(mart_col, {}).setdefault(table, src)
    return out


#: How deep a staging expression may be inlined. ONE level: a second would need
#: cycle detection and produce lineage nobody can audit.
STAGING_EXPANSION_DEPTH = 1


def staging_expression_map(
    spec: CandidateSpec,
) -> dict[str, dict[str, tuple[str, tuple[str, ...]]]]:
    """Recover one-level staging expressions and their source-column references.

    Accept one source table, declared references, and nonconstant expressions.
    """
    by_id = spec.node_by_id()
    out: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {}
    for model in sorted(spec.models, key=lambda m: m.unique_id):
        closure = _source_closure(model.unique_id, spec)
        if len(closure) != 1:
            continue
        source = by_id[next(iter(closure))]
        table = source.name
        known = {c.name for c in source.columns}  # THIS source's columns
        ast = parse_model_sql(model)
        if ast is None:
            continue
        for select in ast.find_all(exp.Select):
            for projection in select.expressions:
                if not isinstance(projection, exp.Alias):
                    continue
                alias = projection.alias_or_name
                body = projection.this
                refs = tuple(
                    sorted({c.name for c in body.find_all(exp.Column) if c.name})
                )
                if not alias or not refs or not all(r in known for r in refs):
                    continue
                if _rename_ref(body) is not None:
                    continue  # a rename, not a definition: `staging_alias_map` owns it
                out.setdefault(alias, {}).setdefault(
                    table, (_widen_float_casts(body).sql(dialect="duckdb"), refs)
                )
    return out


def _widen_float_casts(body: exp.Expression) -> exp.Expression:
    """Rewrite a lifted 32-bit float cast to DOUBLE.

    DuckDB's FLOAT (rendered REAL) holds 4 bytes; the same text on Snowflake
    holds 8. Leaving it narrow rounds the reference result and not the
    submission's, so every mart column built from it mismatches.
    """

    def _widen(node: exp.Expression) -> exp.Expression:
        if (
            isinstance(node, (exp.Cast, exp.TryCast))
            and node.to.this is exp.DataType.Type.FLOAT
        ):
            node.set("to", exp.DataType.build("DOUBLE"))
        return node

    return body.transform(_widen)


def _cast_of(body: exp.Expression) -> exp.Cast | None:
    """The outermost CAST a rename-only projection wraps, or None."""
    node = body
    while isinstance(node, _RENAME_WRAPPERS):
        if isinstance(node, (exp.Cast, exp.TryCast)):
            return node
        node = node.this
    return None


def _macro_type(text: str) -> ColumnType | None:
    """A `get_<table>_columns` datatype text as a ColumnType, or None if unknown."""
    base = re.sub(r"\(.*\)$", "", text.strip().lower()).strip()
    return _TYPE_MAP.get(base)


def staging_type_map(spec: CandidateSpec) -> dict[str, dict[str, ColumnType]]:
    """Recover staging types from compiled casts, then column macros.

    Conflicting non-text types and unmapped macro types produce no evidence.
    """
    by_id = spec.node_by_id()
    seen: dict[str, dict[str, set[ColumnType]]] = {}
    for model in sorted(spec.models, key=lambda m: m.unique_id):
        closure = _source_closure(model.unique_id, spec)
        if len(closure) != 1:
            continue
        source = by_id[next(iter(closure))]
        known = {c.name for c in source.columns}
        ast = parse_model_sql(model)
        if ast is None:
            continue
        for select in ast.find_all(exp.Select):
            for projection in select.expressions:
                if not isinstance(projection, exp.Alias):
                    continue
                cast = _cast_of(projection.this)
                if cast is None or cast.to is None:
                    continue
                if isinstance(cast.this, exp.Null):
                    column = projection.alias_or_name
                else:
                    column = _rename_ref(cast.this)
                if not column or column not in known:
                    continue
                ctype = _map_type(cast.to.sql(dialect="duckdb"))
                seen.setdefault(source.name, {}).setdefault(column, set()).add(ctype)

    out: dict[str, dict[str, ColumnType]] = {}
    for table, columns in sorted(seen.items()):
        for column, types in sorted(columns.items()):
            non_text = types - {ColumnType.TEXT}
            if len(non_text) > 1:
                continue  # contradictory casts: no evidence, never a guess
            out.setdefault(table, {})[column] = (
                next(iter(non_text)) if non_text else ColumnType.TEXT
            )

    uids_by_name = _source_uids_by_name(spec)
    for table, columns in sorted(spec.macro_column_types.items()):
        uids = uids_by_name.get(table, [])
        if len(uids) != 1:
            continue  # no such source, or an ambiguous name
        known = {c.name for c in by_id[uids[0]].columns}
        typed = out.setdefault(table, {})
        for column, text in sorted(columns.items()):
            if column not in known or column in typed:
                continue
            ctype = _macro_type(text)
            if ctype is not None:
                typed[column] = ctype
        if not typed:
            out.pop(table, None)
    return out


class UngroundedMartError(KeylessMartError):
    """Raised when declared mart grain cannot be grounded in source columns."""


class ProjectionKind(str, Enum):
    """Taxonomy for one recovered mart projection."""

    PASSTHROUGH = "passthrough"                  # a column, a rename, a cast
    AGGREGATE = "aggregate"                      # SUM/COUNT/AVG over a group
    FILTERED_AGGREGATE = "filtered_aggregate"    # aggregate over a CASE body
    DISTINCT_AGGREGATE = "distinct_aggregate"    # COUNT(DISTINCT x)
    EXTREMA = "extrema"                          # MIN/MAX over a group
    RATIO = "ratio"                              # a division
    WINDOW = "window"                            # an OVER() frame
    CONDITIONAL = "conditional"                  # a CASE ladder, not aggregated
    NULL_DEFAULT = "null_default"                # COALESCE / NULLIF / IFNULL
    ARITHMETIC = "arithmetic"                    # +-*, date arithmetic, concat


#: The kinds that are NOT a projection of one source column.
COMPUTED_PROJECTION_KINDS = frozenset(
    k for k in ProjectionKind if k is not ProjectionKind.PASSTHROUGH
)


@dataclass(frozen=True)
class _Projection:
    """Recovered output expression, classification, and referenced columns."""

    column: str
    expr: str
    refs: tuple[str, ...]
    kind: ProjectionKind
    #: The ROUTING fact (`kind` is the MEASUREMENT fact): a ratio of two SUMs
    #: is `kind=RATIO` and `is_aggregate=True`.
    is_aggregate: bool = False
    #: Every construct present, not just the headline `kind`, so no attack
    #: surface is lost to the label.
    features: frozenset[str] = frozenset()
    #: SOURCE LINEAGE per ref, ``((ref, (source table, ...)), ...)``: the tables
    #: the model's SQL reads that ref from, `?name` marking an unresolvable
    #: relation. Grounding binds a ref ONLY within its lineage. Never persisted.
    lineage: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def computed(self) -> bool:
        return self.kind in COMPUTED_PROJECTION_KINDS


@dataclass(frozen=True)
class _Measure:
    """One aggregate column of a dbt mart, recovered from the model's own SQL."""

    column: str
    #: The aggregate expression, verbatim from the model (DuckDB-normalized).
    expr: str
    #: Column names the expression references.
    refs: tuple[str, ...]
    #: What the anchor taxonomy calls this column (see `ProjectionKind`).
    kind: ProjectionKind = ProjectionKind.AGGREGATE
    #: Source lineage per ref, carried from the `_Projection` (same shape).
    lineage: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _classify_projection(body: exp.Expression) -> tuple[ProjectionKind, bool, frozenset[str]]:
    """(kind, is_aggregate, features) for one projection body.

    Reads the AST, never the text; most-specific-first.
    """
    features: set[str] = set()
    aggs = list(body.find_all(exp.AggFunc))
    windows = list(body.find_all(exp.Window))
    has_case = bool(list(body.find_all(exp.Case)))
    has_div = bool(list(body.find_all(exp.Div)))
    has_null_default = bool(
        list(body.find_all(exp.Coalesce)) or list(body.find_all(exp.Nullif))
    )
    distinct = any(a.args.get("distinct") or a.find(exp.Distinct) for a in aggs)
    filtered = any(a.find(exp.Case) is not None for a in aggs)
    extrema = any(isinstance(a, (exp.Min, exp.Max)) for a in aggs)

    if aggs:
        features.add("aggregate")
    if windows:
        features.add("window")
    if has_case:
        features.add("conditional")
    if has_div:
        features.add("ratio")
    if has_null_default:
        features.add("null_default")
    if distinct:
        features.add("distinct")
    if filtered:
        features.add("filtered")
    if extrema:
        features.add("extrema")

    is_aggregate = bool(aggs)
    if windows:
        return (ProjectionKind.WINDOW, is_aggregate, frozenset(features))
    if aggs:
        if distinct:
            return (ProjectionKind.DISTINCT_AGGREGATE, True, frozenset(features))
        if filtered:
            return (ProjectionKind.FILTERED_AGGREGATE, True, frozenset(features))
        if has_div:
            return (ProjectionKind.RATIO, True, frozenset(features))
        if extrema:
            return (ProjectionKind.EXTREMA, True, frozenset(features))
        return (ProjectionKind.AGGREGATE, True, frozenset(features))
    if has_case:
        return (ProjectionKind.CONDITIONAL, False, frozenset(features))
    if has_div:
        return (ProjectionKind.RATIO, False, frozenset(features))
    if has_null_default:
        return (ProjectionKind.NULL_DEFAULT, False, frozenset(features))
    inner = body.this if isinstance(body, exp.Cast) else body
    if isinstance(inner, exp.Column):
        return (ProjectionKind.PASSTHROUGH, False, frozenset(features))
    return (ProjectionKind.ARITHMETIC, False, frozenset(features))


#: Meta key stamped on every Column an inlining COPIES: the SELECT it was
#: written in, so lineage reads THAT select's scope, not the outer one.
_LINEAGE_SELECT_META = "lineage_select"

#: Sentinel: a ref names an alias two relations of one SELECT both define.
_AMBIGUOUS_DEFINITION = object()


def _output_select_chain(ast: exp.Expression) -> list[exp.Select]:
    """Follow a bounded chain of bare ``SELECT * FROM cte`` output selects."""
    if not isinstance(ast, exp.Select):
        return []
    ctes = {c.alias_or_name: c for c in ast.find_all(exp.CTE)}
    chain: list[exp.Select] = [ast]
    seen: set[str] = set()
    current = ast
    for _ in range(len(ctes) + 1):
        projections = current.expressions
        if len(projections) != 1 or not isinstance(projections[0], exp.Star):
            break
        relations = _from_and_joins(current)
        if len(relations) != 1 or not isinstance(relations[0], exp.Table):
            break
        name = relations[0].name
        if name in seen or name not in ctes:
            break
        seen.add(name)
        inner = ctes[name].this
        if not isinstance(inner, exp.Select):
            break
        chain.append(inner)
        current = inner
    return chain


def _column_definitions(
    ast: exp.Expression,
) -> Callable[[exp.Select], tuple[dict[str, object], dict[str, dict[str, object]]]]:
    """Map each select to recursively defined CTE aliases and upstream scopes."""
    ctes: dict[str, exp.CTE] = {c.alias_or_name: c for c in ast.find_all(exp.CTE)}
    memo: dict[str, dict[str, object]] = {}

    def of_cte(name: str, seen: frozenset[str]) -> dict[str, object]:
        if name in memo:
            return memo[name]
        if name in seen or name not in ctes:
            return {}
        select = ctes[name].this
        result = (
            of_select(select, seen | {name})[0] if isinstance(select, exp.Select) else {}
        )
        memo[name] = result
        return result

    def of_select(
        select: exp.Select, seen: frozenset[str] = frozenset()
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        upstream: dict[str, dict[str, object]] = {}
        for relation in _from_and_joins(select):
            if isinstance(relation, exp.Table):
                upstream[relation.alias_or_name] = of_cte(relation.name, seen)
        defined: dict[str, object] = {}

        def merge(alias: str, definition: object) -> None:
            have = defined.get(alias)
            if have is None:
                defined[alias] = definition
            elif have is not definition:
                defined[alias] = _AMBIGUOUS_DEFINITION

        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                for definitions in upstream.values():
                    for alias, definition in definitions.items():
                        merge(alias, definition)
            elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                for alias, definition in upstream.get(projection.table, {}).items():
                    merge(alias, definition)
            elif isinstance(projection, exp.Column):
                definition = _upstream_definition(projection, upstream)
                if definition is not None:
                    merge(projection.name, definition)
            elif isinstance(projection, exp.Alias):
                alias = projection.alias_or_name
                body = projection.this
                if not alias:
                    continue
                if isinstance(body, exp.Column) and not isinstance(body.this, exp.Star):
                    definition = _upstream_definition(body, upstream)
                    if definition is not None:
                        merge(alias, definition)  # chase the rename upstream
                    elif body.name != alias:
                        merge(alias, (body, select))  # a rename of a source column
                    continue
                merge(alias, (body, select))
        return defined, upstream

    return of_select


def _upstream_definition(
    column: exp.Column, upstream: dict[str, dict[str, object]]
) -> object | None:
    """Resolve a CTE-defined column, returning ambiguity explicitly."""
    if column.table:
        return upstream.get(column.table, {}).get(column.name)
    hits = [d[column.name] for d in upstream.values() if column.name in d]
    if not hits:
        return None
    if len(hits) > 1 and any(h is not hits[0] for h in hits[1:]):
        return _AMBIGUOUS_DEFINITION
    return hits[0]


def _inline_definitions(
    body: exp.Expression,
    select: exp.Select,
    definitions_of: Callable[
        [exp.Select], tuple[dict[str, object], dict[str, dict[str, object]]]
    ],
    depth: int = 0,
) -> exp.Expression | None:
    """Inline unambiguous, nonaggregate CTE definitions one level at a time."""
    if depth > 32:
        return None
    _, upstream = definitions_of(select)
    if not any(upstream.values()):
        return body
    refused = False
    substituted = False

    def _sub(node: exp.Expression) -> exp.Expression:
        nonlocal refused, substituted
        if refused or not isinstance(node, exp.Column) or not node.name:
            return node
        if isinstance(node.this, exp.Star):
            return node
        definition = _upstream_definition(node, upstream)
        if definition is None:
            return node
        if definition is _AMBIGUOUS_DEFINITION:
            refused = True
            return node
        def_body, def_select = definition
        if def_body.find(exp.AggFunc) is not None or def_body.find(exp.Window) is not None:
            refused = True
            return node
        copy = def_body.copy()
        for column in copy.find_all(exp.Column):
            column.meta[_LINEAGE_SELECT_META] = def_select
        inner = _inline_definitions(copy, def_select, definitions_of, depth + 1)
        if inner is None:
            refused = True
            return node
        substituted = True
        return inner

    out = body.transform(_sub, copy=True)
    if refused:
        return None
    return out if substituted else body


def _column_lineage(
    column: exp.Column,
    owner: exp.Select,
    resolver: Callable[[str], frozenset[str]],
    scopes: dict[int, dict[str, frozenset[str]]],
) -> frozenset[str]:
    """Source tables ONE column ref reads from (see `_Projection.lineage`).

    Scope precedence: the nearest enclosing SELECT, else the select an inlined
    definition was written in, else `owner` — an inlined body is a detached
    copy, so ancestry alone cannot find it.
    """
    select = (
        column.find_ancestor(exp.Select)
        or column.meta.get(_LINEAGE_SELECT_META)
        or owner
    )
    scope = scopes.get(id(select))
    if scope is None:
        scope = _select_scope(select, resolver)
        scopes[id(select)] = scope
    if column.table:
        if column.table in scope:
            return scope[column.table]
        return resolver(column.table)
    if not scope:
        return frozenset({"?scope"})
    found: set[str] = set()
    for tables in scope.values():
        found |= tables
    return frozenset(found)


def recover_projections(
    model: DbtNode,
    resolver: Callable[[str], frozenset[str]] | None = None,
) -> tuple[_Projection, ...]:
    """Recover declared mart columns from model SQL projections.

    Read the output select chain first, then breadth-first. Inline references to
    non-aggregate CTE aliases and recover aggregate aliases at their defining
    expression. A resolver attaches source lineage and prevents out-of-lineage
    grounding. Jinja-bearing or unparseable SQL yields no projections.
    """
    ast = parse_model_sql(model)
    if ast is None:
        return ()
    declared = {c.name for c in model.columns}
    chain = _output_select_chain(ast)
    ordered: list[exp.Select] = list(chain)
    chain_ids = {id(s) for s in chain}
    ordered.extend(s for s in ast.find_all(exp.Select) if id(s) not in chain_ids)
    definitions_of = _column_definitions(ast)
    scopes: dict[int, dict[str, frozenset[str]]] = {}
    out: list[_Projection] = []
    seen: set[str] = set()
    for select in ordered:
        for projection in select.expressions:
            alias = projection.alias_or_name
            if alias not in declared or alias in seen:
                continue
            body = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(body, exp.Star) or (
                isinstance(body, exp.Column) and isinstance(body.this, exp.Star)
            ):
                continue
            inlined = _inline_definitions(body, select, definitions_of)
            if inlined is None:
                continue  # an aggregate of an aggregate: recovered where stated
            body = inlined
            kind, is_aggregate, features = _classify_projection(body)
            columns = [c for c in body.find_all(exp.Column) if c.name]
            if isinstance(body, exp.Column) and body.name:
                columns = [body]
            refs = tuple(sorted({c.name for c in columns}))
            lineage: tuple[tuple[str, tuple[str, ...]], ...] = ()
            if resolver is not None:
                by_ref: dict[str, set[str]] = {}
                for column in columns:
                    by_ref.setdefault(column.name, set()).update(
                        _column_lineage(column, select, resolver, scopes)
                    )
                lineage = tuple(
                    (name, tuple(sorted(tables))) for name, tables in sorted(by_ref.items())
                )
            seen.add(alias)
            out.append(
                _Projection(
                    column=alias,
                    expr=_widen_float_casts(body).sql(dialect="duckdb"),
                    refs=refs,
                    kind=kind,
                    is_aggregate=is_aggregate,
                    features=features,
                    lineage=lineage,
                )
            )
    return tuple(sorted(out, key=lambda p: p.column))


def recover_measures(model: DbtNode) -> tuple[_Measure, ...]:
    """The AGGREGATE-bearing recovered projections, as `_Measure` records.

    Separate from `recover_projections` because the plan routes on
    `is_aggregate`, not on the taxonomy label `kind`.
    """
    return tuple(
        _Measure(column=p.column, expr=p.expr, refs=p.refs, kind=p.kind, lineage=p.lineage)
        for p in recover_projections(model)
        if p.is_aggregate
    )


@dataclass(frozen=True)
class _Grounding:
    """What of a dbt mart's declared contract is derivable from its sources.

    Carries the FULL accounting, not just survivors: `dropped` names a reason
    per column, because a drop nobody can name is a drop nobody can fix.
    """

    base_table: str
    #: (source column, mart column) in mart declaration order, ON THE BASE TABLE.
    select_map: tuple[tuple[str, str], ...]
    #: Mart columns forming the grain, all present in `select_map`.
    key_columns: tuple[str, ...]
    #: Source column each key column grounds to (positionally aligned).
    key_source_columns: tuple[str, ...]
    #: 'projection' (one row per source row) or 'aggregate' (a GROUP BY grain).
    mode: str
    #: Aggregate columns recovered from the model SQL and fully groundable.
    measures: tuple[_Measure, ...] = ()
    #: NON-aggregate computed columns whose every ref grounds. Emitted as a
    #: DERIVE/WINDOW op, never as a GROUP BY measure.
    derived: tuple[_Projection, ...] = ()
    #: (table, source column, mart column) for passthroughs grounded in a
    #: JOINED table rather than the base — aggregate mode only.
    joined_columns: tuple[tuple[str, str, str], ...] = ()
    #: Tables joined to reach `joined_columns`/measure refs, as (table,
    #: base-side cols, joined-side cols) of a DECLARED relationship whose
    #: PARENT is the joined table — a many-to-one lookup that cannot change the
    #: row count.
    joins: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = ()
    #: Declared mart columns nothing in this cut produces.
    dropped: tuple[str, ...] = ()
    #: (column, reason) for every entry of `dropped` — same order.
    drop_reasons: tuple[tuple[str, str], ...] = ()
    #: Declared mart columns of the model, before any narrowing.
    declared: int = 0
    #: (source table, source column, type the recovered SQL demands). Sources
    #: declare no types, so without this the pool is TEXT and any recovered SUM
    #: or boolean flag fails to execute.
    type_requirements: tuple[tuple[str, str, ColumnType], ...] = ()
    #: (source table, source column, literal) the recovered SQL SELECTS ON by
    #: equality. Undeclared, no row qualifies and every guarded measure ships a
    #: constant 0. Adopted as `ColumnSpec.enum_values`.
    domain_requirements: tuple[tuple[str, str, str], ...] = ()

    @property
    def grounded(self) -> int:
        return (
            len(self.select_map)
            + len(self.joined_columns)
            + len(self.measures)
            + len(self.derived)
        )

    @property
    def computed(self) -> int:
        """Grounded columns that are NOT a projection of one source column."""
        return len(self.measures) + len(self.derived)


def _ground_column(
    name: str, table: str, source_columns: dict[str, set[str]], aliases: dict[str, dict[str, str]]
) -> str | None:
    known = source_columns.get(table, set())
    if name in known:
        return name
    src = aliases.get(name, {}).get(table)
    # The alias map is package-wide and keyed by table NAME, so the renamed
    # column must be a column of THIS table (fail closed, never merged).
    return src if src is not None and src in known else None


#: Strongest-first: when two recovered expressions demand different types of
#: one source column, the strongest wins and the measures needing a weaker one
#: are DROPPED (never silently retyped).
_TYPE_STRENGTH: tuple[ColumnType, ...] = (
    ColumnType.BOOLEAN,
    ColumnType.TIMESTAMP,
    ColumnType.DATE,
    ColumnType.FLOAT,
    ColumnType.BIGINT,
    ColumnType.TEXT,
)

#: Which ADOPTED types satisfy a demanded one. A demand is a lower bound, not
#: an identity: a SUM demanding BIGINT runs over FLOAT, TIMESTAMP over DATE.
#: Anything unlisted must match exactly.
_TYPE_SATISFIERS: dict[ColumnType, frozenset[ColumnType]] = {
    ColumnType.BIGINT: frozenset(
        {ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.FLOAT, ColumnType.DECIMAL}
    ),
    ColumnType.FLOAT: frozenset(
        {ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.FLOAT, ColumnType.DECIMAL}
    ),
    ColumnType.TIMESTAMP: frozenset({ColumnType.TIMESTAMP, ColumnType.DATE}),
    ColumnType.BOOLEAN: frozenset({ColumnType.BOOLEAN}),
    ColumnType.TEXT: frozenset({ColumnType.TEXT}),
}


def _type_satisfies(adopted: ColumnType, want: ColumnType) -> bool:
    """True iff a column shipped as `adopted` can serve a use demanding `want`."""
    return adopted in _TYPE_SATISFIERS.get(want, frozenset({want}))


_NUMERIC_PARENTS = (exp.Sum, exp.Avg, exp.Add, exp.Sub, exp.Mul, exp.Round, exp.Abs)
_FLOAT_PARENTS = (exp.Avg, exp.Div)
_DATE_PARENTS = (
    exp.DateDiff,
    exp.DateAdd,
    exp.DateSub,
    exp.DateTrunc,
    exp.TimestampTrunc,
    exp.TsOrDsToDate,
)


def _required_type(column: exp.Column) -> ColumnType | None:
    """What type this USE of a column demands, or None when it demands nothing.

    Read off the parent node: with sources declaring no types, the package's
    own SQL is the only evidence, and `SUM(x)` over TEXT has no computable gold.
    """
    # Climb the TRANSPARENT wrappers first: `SUM(COALESCE(a, b))` types BOTH
    # operands numeric, and DuckDB refuses to mix BIGINT and VARCHAR there.
    node: exp.Expression = column
    parent = node.parent
    while parent is not None:
        if isinstance(parent, (exp.Cast, exp.TryCast)):
            # An explicit cast is the strongest available evidence: without
            # adopting its input domain, synthetic TEXT such as ``metric_42``
            # reaches the cast and fails before the mart can run.
            return _map_type(parent.to.sql(dialect="duckdb"))
        if not isinstance(
            parent,
            (exp.Coalesce, exp.Nullif, exp.Paren, exp.Distinct, exp.Case, exp.If),
        ):
            break
        if isinstance(parent, exp.If) and parent.this is node:
            return ColumnType.BOOLEAN     # `CASE WHEN <expr> THEN ...`
        node, parent = parent, parent.parent
    if parent is None:
        return None
    if isinstance(parent, _FLOAT_PARENTS):
        return ColumnType.FLOAT
    if isinstance(parent, _NUMERIC_PARENTS):
        return ColumnType.BIGINT
    if isinstance(parent, _DATE_PARENTS):
        return ColumnType.TIMESTAMP
    if isinstance(parent, (exp.Not, exp.And, exp.Or)):
        return ColumnType.BOOLEAN
    if isinstance(parent, (exp.EQ, exp.NEQ, exp.Is)):
        other = parent.expression if parent.this is node else parent.this
        if isinstance(other, exp.Boolean):
            return ColumnType.BOOLEAN
        if isinstance(other, exp.Literal):
            return ColumnType.BIGINT if other.is_number else ColumnType.TEXT
        return None
    if isinstance(parent, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
        other = parent.expression if parent.this is node else parent.this
        if isinstance(other, exp.Literal) and other.is_number:
            return ColumnType.BIGINT
        return None
    return None


def _required_types(expr: str) -> dict[str, ColumnType]:
    """Column name -> the strongest type the expression's USES of it demand."""
    node = _parse_sql_expression(expr)
    if node is None:
        return {}
    out: dict[str, ColumnType] = {}
    for column in node.find_all(exp.Column):
        want = _required_type(column)
        if want is None:
            continue
        have = out.get(column.name)
        if have is None or _TYPE_STRENGTH.index(want) < _TYPE_STRENGTH.index(have):
            out[column.name] = want
    return out


def _required_domain(expr: str) -> list[tuple[str, str]]:
    """(column name, string literal) every EQUALITY in `expr` selects on.

    The VALUE analogue of `_required_types`: unless the column can HOLD the
    literal, the WHEN never fires and the measure is a constant 0. EQUALITY
    ONLY — an inequality or LIKE names a range, so taking a literal from one
    would guess at the domain rather than read it.
    """
    node = _parse_sql_expression(expr)
    if node is None:
        return []
    out: list[tuple[str, str]] = []
    for cmp_node in node.find_all(exp.EQ, exp.NEQ, exp.In):
        column = cmp_node.this
        if not isinstance(column, exp.Column):
            continue
        others = (
            list(cmp_node.expressions)
            if isinstance(cmp_node, exp.In)
            else [cmp_node.expression]
        )
        for other in others:
            if isinstance(other, exp.Literal) and other.is_string:
                out.append((column.name, other.this))
    return out


def _unqualify(expr: str) -> str | None:
    """Drop table qualifiers from a recovered expression, or refuse.

    A plan's aggregate runs over bare carried aliases, so a qualifier binds to
    nothing. Safe ONLY when no bare name appears under two DIFFERENT
    qualifiers; otherwise REFUSED rather than bound to whichever arrives first.
    Lineage is certified BEFORE this runs, so the binding stays correct.
    """
    node = _parse_sql_expression(expr)
    if node is None:
        return None
    qualifiers: dict[str, set[str]] = {}
    for column in node.find_all(exp.Column):
        qualifiers.setdefault(column.name, set()).add(column.table or "")
    for name, quals in qualifiers.items():
        if len({q for q in quals if q}) > 1:
            return None
    return node.transform(
        lambda n: exp.column(n.name) if isinstance(n, exp.Column) else n
    ).sql(dialect="duckdb")


def _expand_expression(expr: str, expansions: dict[str, str]) -> str | None:
    """Inline staging definitions into a mart expression, ONE level deep.

    Structural, via sqlglot: a textual substitution would corrupt any
    identifier containing another identifier's name. Returns None when anything
    fails to parse — "no evidence", never a guess.
    """
    if not expansions:
        return expr
    root = _parse_sql_expression(expr)
    if root is None:
        return None
    parsed: dict[str, exp.Expression] = {}
    for name, text in expansions.items():
        node = _parse_sql_expression(text)
        if node is None:
            return None
        parsed[name] = node

    def _sub(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and node.name in parsed:
            return parsed[node.name].copy()
        return node

    return root.transform(_sub).sql(dialect="duckdb")


#: How many tables may be joined to the base to widen a mart's lineage. A cap,
#: not a target: every extra table is an extra LEFT JOIN in the gold.
MAX_LINEAGE_JOINS = 3


def _lookup_joins(
    base: str,
    candidates: set[str],
    relationships: tuple[Relationship, ...],
) -> tuple[dict[str, tuple[tuple[str, ...], tuple[str, ...]]], dict[str, str]]:
    """Tables reachable from `base` by a MANY-TO-ONE declared relationship.

    ONLY base=child -> other=PARENT: the parent side is minted unique on the
    join key, so the join cannot fan out or change the row count. REFUSED, fail
    closed, is a hop whose parent-side columns are ALL foreign keys of the
    parent — drawn WITH repetition, so no minting makes the parent unique and
    every measure over the join would double. Returns
    ``({parent: (base cols, parent cols)}, {refused parent: reason})``.
    """
    fk_columns: dict[str, set[str]] = {}
    for rel in relationships:
        fk_columns.setdefault(rel.child_table, set()).update(rel.child_columns)
    out: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    refused: dict[str, str] = {}
    for rel in sorted(
        relationships, key=lambda r: (r.parent_table, r.child_table, r.child_columns)
    ):
        if rel.child_table != base or rel.parent_table not in candidates:
            continue
        if rel.parent_table in out:
            continue
        parent_fks = fk_columns.get(rel.parent_table, set())
        if all(c in parent_fks for c in rel.parent_columns):
            refused.setdefault(
                rel.parent_table,
                f"joined table {rel.parent_table} is keyed on "
                f"({', '.join(rel.parent_columns)}) which are all foreign keys "
                "drawn with repetition; not provably one row per key",
            )
            continue
        out[rel.parent_table] = (tuple(rel.child_columns), tuple(rel.parent_columns))
    for table in out:
        refused.pop(table, None)  # another relationship to it was mintable
    return out, refused


def _lineage_tables(
    ref: str,
    tables: tuple[str, ...],
    lineage: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[str, ...]:
    """The subset of `tables` a recovered ref may bind to, by its lineage.

    A KNOWN lineage entry limits candidates to the tables it names, possibly
    none; an absent or `?` entry keeps every table. This is the fail-closed
    half of `ground_mart`'s contract — a ref resolving to one table may never
    bind to a same-named column of another.
    """
    for name, sources in lineage:
        if name != ref:
            continue
        if not sources or any(s.startswith("?") for s in sources):
            return tables
        allowed = set(sources)
        return tuple(t for t in tables if t in allowed)
    return tables


def _fans_out(
    projection: _Projection, base: str, hops: tuple[str, ...]
) -> str:
    """Why an ADDITIVE aggregate over lookup-table columns is refused, or ''.

    A hop is many-to-one FROM the base, so `SUM(parent.x)` adds `parent.x` once
    per BASE row, not once per parent row as the package computes it. Refused
    for non-DISTINCT SUM/AVG/COUNT whose every column lies on hop tables by
    KNOWN lineage; MIN/MAX/COUNT DISTINCT and anything also reading a base
    column are row-level facts and stay. Unknown lineage keeps the measure.
    """
    if not projection.is_aggregate or not hops:
        return ""
    node = _parse_sql_expression(projection.expr)
    if node is None:
        return ""
    hop_set = set(hops)
    for agg in node.find_all(exp.AggFunc):
        if not isinstance(agg, (exp.Sum, exp.Avg, exp.Count)):
            continue
        if agg.args.get("distinct") or agg.find(exp.Distinct) is not None:
            continue
        cols = sorted({c.name for c in agg.find_all(exp.Column) if c.name})
        if not cols:
            continue  # COUNT(*): counts base rows, a base-level fact
        origins: set[str] = set()
        hop_only = True
        for name in cols:
            lineage = _lineage_of(name, projection.lineage)
            if (
                not lineage
                or any(s.startswith("?") for s in lineage)
                or base in lineage
                or not set(lineage) <= hop_set
            ):
                hop_only = False
                break
            origins |= set(lineage)
        if hop_only:
            return (
                f"recovered {projection.kind.value} expression "
                f"{agg.sql(dialect='duckdb')} adds up {', '.join(cols)} of "
                f"{', '.join(sorted(origins))}, a lookup table joined "
                f"many-to-one from {base}: the value would be counted once per "
                f"{base} row (fan-out), not once per {', '.join(sorted(origins))} "
                "row as the package computes it"
            )
    return ""


def _lineage_of(
    ref: str, lineage: tuple[tuple[str, tuple[str, ...]], ...]
) -> tuple[str, ...]:
    """The recorded lineage tables of `ref`, or () when unknown."""
    for name, sources in lineage:
        if name == ref:
            return sources
    return ()


def ground_mart(
    model: DbtNode,
    key_columns: tuple[str, ...],
    closure_tables: list[str],
    source_columns: dict[str, set[str]],
    aliases: dict[str, dict[str, str]],
    relationships: tuple[Relationship, ...] = (),
    expressions: dict[str, dict[str, tuple[str, tuple[str, ...]]]] | None = None,
    resolver: Callable[[str], frozenset[str]] | None = None,
) -> _Grounding:
    """Ground a dbt mart contract in the sources shipped by this cut.

    Try recovered expressions within their SQL lineage, staging names or
    renames, then many-to-one lookup tables for aggregate marts. Do not infer
    relationships from mart SQL. A grain that is ungrounded, partially
    grounded, or covers every column raises ``UngroundedMartError`` and drops
    the whole mart. Report every dropped column and reason.
    """
    projections = {p.column: p for p in recover_projections(model, resolver)}
    declared_names = [c.name for c in model.columns]
    staged = expressions or {}

    def _grounds_in(name: str, tables: tuple[str, ...]) -> str | None:
        """The first of `tables` that produces `name` as a COLUMN."""
        for table in tables:
            hit = _ground_column(name, table, source_columns, aliases)
            if hit is not None:
                return table
        return None

    def _ref_grounds_in(name: str, tables: tuple[str, ...]) -> str | None:
        """The first of `tables` producing `name` as a column OR expression.

        A name a staging model DEFINES grounds when the definition's own refs
        are columns of that table.
        """
        direct = _grounds_in(name, tables)
        if direct is not None:
            return direct
        for table in tables:
            definition = staged.get(name, {}).get(table)
            if definition is None:
                continue
            _, refs = definition
            if all(
                _ground_column(r, table, source_columns, aliases) is not None
                for r in refs
            ):
                return table
        return None

    def _expansions(
        refs: tuple[str, ...],
        tables: tuple[str, ...],
        lineage: tuple[tuple[str, tuple[str, ...]], ...],
    ) -> dict[str, str]:
        """Staging definitions to inline for `refs` (empty when none is needed)."""
        out: dict[str, str] = {}
        for ref in refs:
            allowed = _lineage_tables(ref, tables, lineage)
            # Prefer the staging definition even when it keeps the raw column's
            # name.  ``coalesce(cast(metric ...), 0) AS metric`` is semantic
            # work, not a rename; binding the mart's ``metric`` directly to the
            # same-named raw column silently discards its NULL/type behavior.
            for table in allowed:
                definition = staged.get(ref, {}).get(table)
                if definition is None:
                    continue
                expr, sub_refs = definition
                if all(
                    _ground_column(r, table, source_columns, aliases) is not None
                    for r in sub_refs
                ):
                    out[ref] = expr
                    break
        return out

    def _staged_projection(
        name: str, tables: tuple[str, ...]
    ) -> tuple[str, str, tuple[str, ...]] | None:
        """One unambiguous computed staging column behind a mart passthrough.

        A final model commonly groups on ``date_day`` while its raw source has
        only ``date`` and a staging model defines the day truncation.  Treating
        the final ``report.date_day`` as a direct rename drops the grain column.
        This resolver admits the definition only when exactly one reachable
        source produces it and every referenced raw column exists there.
        """
        hits: list[tuple[str, str, tuple[str, ...]]] = []
        for table in tables:
            definition = staged.get(name, {}).get(table)
            if definition is None:
                continue
            expr, refs = definition
            if refs and all(
                _ground_column(ref, table, source_columns, aliases) is not None
                for ref in refs
            ):
                hits.append((table, expr, refs))
        return hits[0] if len(hits) == 1 else None

    def _hits(tables: tuple[str, ...]) -> int:
        """Declared mart columns `tables` can produce between them."""
        total = 0
        for name in declared_names:
            projection = projections.get(name)
            if projection is not None and projection.computed:
                if (
                    projection.refs
                    and all(
                        _ref_grounds_in(r, _lineage_tables(r, tables, projection.lineage))
                        for r in projection.refs
                    )
                    and not _fans_out(projection, tables[0], tables[1:])
                ):
                    total += 1
                continue
            if (
                _grounds_in(name, tables) is not None
                or _staged_projection(name, tables) is not None
            ):
                total += 1
        return total

    scored = sorted((-_hits((t,)), t) for t in sorted(closure_tables))
    if not scored or -scored[0][0] == 0:
        raise UngroundedMartError(
            f"mart model {model.unique_id!r}: none of its {len(model.columns)} "
            f"declared columns is produced by any of its {len(closure_tables)} "
            "source tables, directly or through a recovered staging rename; the "
            "mart cannot be executed from the sources this cut ships"
        )
    base = scored[0][1]

    # Only a GROUP BY mart may take lookup joins: build_projection is
    # single-table by contract.
    has_aggregate = any(p.is_aggregate for p in projections.values())
    selected: list[str] = [base]
    joins: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    refused_hops: dict[str, str] = {}
    offered_hops: set[str] = set()
    if has_aggregate and relationships:
        available = {t for t in closure_tables if t != base}
        while len(joins) < MAX_LINEAGE_JOINS:
            offers, refused = _lookup_joins(base, available, relationships)
            refused_hops.update(refused)
            offered_hops.update(offers)
            gains = [
                (
                    -(_hits(tuple(selected) + (table,)) - _hits(tuple(selected))),
                    table,
                )
                for table in sorted(offers)
            ]
            gains = [(g, t) for g, t in sorted(gains) if g < 0]
            if not gains:
                break
            table = gains[0][1]
            base_cols, other_cols = offers[table]
            joins.append((table, base_cols, other_cols))
            selected.append(table)
            available.discard(table)
    reach = tuple(selected)

    select_map: list[tuple[str, str]] = []
    joined_columns: list[tuple[str, str, str]] = []
    kept_measures: list[_Measure] = []
    derived: list[_Projection] = []
    dropped: list[str] = []
    drop_reasons: list[tuple[str, str]] = []
    requirements: list[tuple[str, str, ColumnType]] = []
    domains: list[tuple[str, str, str]] = []

    def _drop(name: str, reason: str) -> None:
        dropped.append(name)
        drop_reasons.append((name, reason))

    def _unreachable(elsewhere: list[str]) -> str:
        """Why tables that DO produce a column are not in reach."""
        refusals = [refused_hops[t] for t in elsewhere if t in refused_hops]
        head = (
            f"grounds only in {', '.join(elsewhere)}, which no declared "
            f"many-to-one relationship reaches from {base}"
        )
        return head + ("; " + "; ".join(refusals) if refusals else "")

    for name in declared_names:
        projection = projections.get(name)
        if projection is not None and projection.computed:
            if not projection.refs:
                _drop(
                    name,
                    f"recovered expression {projection.expr!r} references no "
                    "column, so it is a constant this factory will not certify",
                )
                continue
            disjoint = [
                r
                for r in projection.refs
                if not _lineage_tables(r, reach, projection.lineage)
            ]
            if disjoint:
                # Lineage names a table not in reach: dropped, never re-bound
                # to a same-named column. If the origin was an offered hop the
                # measure would fan out over, say that instead.
                ref = disjoint[0]
                origin = _lineage_of(ref, projection.lineage)
                fan_out = (
                    _fans_out(projection, base, origin)
                    if set(origin) <= offered_hops | set(reach[1:])
                    else ""
                )
                refusals = [refused_hops[t] for t in origin if t in refused_hops]
                _drop(
                    name,
                    fan_out
                    or (
                        f"recovered {projection.kind.value} expression reads {ref} "
                        f"from {', '.join(origin)}, "
                        f"which is not in reach ({', '.join(reach)}); a same-named "
                        f"column of {reach[0]} is not that column"
                        + ("; " + "; ".join(refusals) if refusals else "")
                    ),
                )
                continue
            ungrounded = [
                r
                for r in projection.refs
                if _ref_grounds_in(r, _lineage_tables(r, reach, projection.lineage))
                is None
            ]
            if ungrounded:
                _drop(
                    name,
                    f"recovered {projection.kind.value} expression references "
                    f"{', '.join(ungrounded)}, which ground in none of "
                    f"{', '.join(reach)}",
                )
                continue
            fan_out = _fans_out(projection, base, reach[1:])
            if fan_out:
                _drop(name, fan_out)
                continue
            inline = _expansions(projection.refs, reach, projection.lineage)
            expr = _expand_expression(projection.expr, inline)
            if expr is not None:
                expr = _unqualify(expr)
            if expr is None:
                _drop(
                    name,
                    f"recovered {projection.kind.value} expression could not be "
                    f"rewritten over the source columns behind {', '.join(inline)}",
                )
                continue
            node = _parse_sql_expression(expr)
            refs = (
                tuple(sorted({c.name for c in node.find_all(exp.Column) if c.name}))
                if node is not None
                else projection.refs
            )
            for ref, want in sorted(_required_types(expr).items()):
                table = _grounds_in(ref, _lineage_tables(ref, reach, projection.lineage))
                src = (
                    _ground_column(ref, table, source_columns, aliases)
                    if table is not None
                    else None
                )
                if src is not None:
                    requirements.append((table, src, want))
            # The VALUES the expression selects on, grounded in the same pass
            # as the types it demands: both are facts the package's SQL states
            # about its sources, both useless unless the generator honours them.
            for ref, literal in sorted(set(_required_domain(expr))):
                table = _grounds_in(ref, _lineage_tables(ref, reach, projection.lineage))
                src = (
                    _ground_column(ref, table, source_columns, aliases)
                    if table is not None
                    else None
                )
                if src is not None:
                    domains.append((table, src, literal))
            if projection.is_aggregate:
                kept_measures.append(
                    _Measure(
                        column=projection.column,
                        expr=expr,
                        refs=refs,
                        kind=projection.kind,
                        lineage=projection.lineage,
                    )
                )
            else:
                derived.append(replace(projection, expr=expr, refs=refs))
            continue
        table = _grounds_in(name, reach)
        if table is None:
            staged_projection = _staged_projection(name, reach)
            if staged_projection is not None:
                staged_table, staged_expr, staged_refs = staged_projection
                if staged_table != base:
                    _drop(
                        name,
                        f"computed staging column grounds in {staged_table}, "
                        "reachable only through a lookup; computed lookup "
                        "attributes are not carried into the mart grain",
                    )
                    continue
                node = _parse_sql_expression(staged_expr)
                if node is None:
                    _drop(name, "computed staging expression does not parse")
                    continue
                kind, is_aggregate, features = _classify_projection(node)
                if is_aggregate:
                    _drop(
                        name,
                        "computed staging expression contains an aggregate; "
                        "a staging aggregate cannot be replayed as a row-level "
                        "grain column",
                    )
                    continue
                derived.append(
                    _Projection(
                        column=name,
                        expr=staged_expr,
                        refs=staged_refs,
                        kind=kind,
                        is_aggregate=False,
                        features=features,
                        lineage=tuple(
                            (ref, (staged_table,)) for ref in staged_refs
                        ),
                    )
                )
                for ref, want in sorted(_required_types(staged_expr).items()):
                    source = _ground_column(ref, staged_table, source_columns, aliases)
                    if source is not None:
                        requirements.append((staged_table, source, want))
                continue
            elsewhere = [
                t for t in sorted(closure_tables)
                if _ground_column(name, t, source_columns, aliases) is not None
            ]
            _drop(
                name,
                (
                    _unreachable(elsewhere)
                    if elsewhere
                    else "no source column, staging rename or recovered "
                    "expression in this cut produces it"
                ),
            )
            continue
        grounded = _ground_column(name, table, source_columns, aliases)
        assert grounded is not None  # `_grounds_in` just said so
        if table == base:
            select_map.append((grounded, name))
        else:
            joined_columns.append((table, grounded, name))

    grounded_names = {dst for _, dst in select_map}
    derived_names = {projection.column for projection in derived}
    grounded_key = tuple(k for k in key_columns if k in grounded_names)
    if not grounded_key and not (set(key_columns) & derived_names):
        raise UngroundedMartError(
            f"mart model {model.unique_id!r}: its declared grain "
            f"({', '.join(key_columns)}) grounds in no column of {base!r} "
            "(directly or through a recovered staging rename); the mart has no "
            "gradeable grain and is dropped"
        )
    # A key column carried from a LOOKUP HOP is reproducible too: it joins the
    # GROUP BY as a passthrough and re-enters the declared grain.
    hop_grounded = {dst for _, _, dst in joined_columns}
    missing = tuple(
        k
        for k in key_columns
        if k not in grounded_names
        and k not in hop_grounded
        and k not in derived_names
    )
    if missing:
        # PARTIAL grain: shipping the grounded subset would declare a COARSER
        # grain than the package's, so the mart is dropped whole.
        raise UngroundedMartError(
            f"mart model {model.unique_id!r}: declared grain "
            f"({', '.join(key_columns)}) only partially grounds in {base!r} — "
            f"{', '.join(missing)} has no lineage to a shipped source column; a "
            "grain the factory cannot reproduce is not shipped"
        )
    if not [c for c in model.columns if c.name not in set(key_columns)]:
        # Standing guard on `mart_key_columns`' invariant: without a non-key
        # column the `keys_only` probe is the IDENTITY query, so the mart is
        # DROPPED rather than shipped un-gatable.
        raise UngroundedMartError(
            f"mart model {model.unique_id!r}: derived key "
            f"({', '.join(key_columns)}) covers every declared column; no "
            "informative column would remain"
        )
    by_dst = {dst: src for src, dst in select_map}
    mode = (
        "projection"
        if (
            not kept_measures
            and len(key_columns) == 1
            and len(grounded_key) == 1
            and len(select_map) >= 2
        )
        else "aggregate"
    )
    # Emit a non-aggregate expression before aggregation only when the recovered
    # grain names it. Drop unsupported post-aggregate expressions rather than
    # promoting them to keys and changing the grain.
    kept_derived: list[_Projection] = []
    mechanical_group = set(_group_by_grain(model, set(declared_names)))
    if not mechanical_group:
        mechanical_group = set(
            _dataflow_group_by_grain(
                model,
                set(declared_names),
                enforce_key_width=False,
            )
        )
    declared_key_set = set(key_columns) | mechanical_group
    for projection in derived:
        if mode == "aggregate" and projection.column in declared_key_set:
            kept_derived.append(projection)
            continue
        _drop(
            projection.column,
            (
                "recovered non-aggregate expression is not part of the mart's "
                "declared GROUP BY/key grain; this plan builder cannot reproduce "
                "a post-aggregation expression without changing the grain"
                if mode == "aggregate"
                else "computed output is not representable by the single-table "
                "projection plan without a DERIVE stage"
            ),
        )
    derived = kept_derived
    if mode == "projection":
        # build_projection is single-table by contract, so report a join-reached
        # column rather than keep a join the plan will not carry.
        for table, src, dst in joined_columns:
            _drop(dst, f"grounds in {table}, reachable only by a join a projection mart does not carry")
        joined_columns = []
        joins = []
    return _Grounding(
        base_table=base,
        select_map=tuple(select_map),
        key_columns=grounded_key,
        key_source_columns=tuple(by_dst[k] for k in grounded_key),
        mode=mode,
        measures=tuple(kept_measures),
        derived=tuple(derived),
        joined_columns=tuple(joined_columns),
        joins=tuple(joins),
        dropped=tuple(dropped),
        drop_reasons=tuple(drop_reasons),
        declared=len(model.columns),
        type_requirements=tuple(sorted(set(requirements))),
        domain_requirements=tuple(sorted(set(domains))),
    )


def _predicate_of(measures: tuple) -> str:
    """WHICH MEASURES the recovered filters govern, and on what condition.

    The solver must be TOLD which rows count, so FILTERED_AGGREGATE refuses to
    ship without a predicate, read off the recovered SQL and never invented.
    THE SCOPE IS PART OF THE PREDICATE: a vendored rollup is MIXED, and a
    predicate naming no scope claims something the SQL does not implement.
    """
    by_condition: dict[str, list[str]] = {}
    unguarded: list[str] = []
    for measure in measures:
        # The SAME reading `build_rollup` and `op_problems` use: a second
        # implementation could classify a measure one way in the prose and the
        # other in the gate.
        conditions = guard_conditions(measure.expr)
        if not conditions:
            unguarded.append(measure.column)
            continue
        key = " OR ".join(conditions)
        by_condition.setdefault(key, []).append(measure.column)
    if not by_condition:
        return ""
    clauses = [
        f"{', '.join(columns)} count only rows where {condition}"
        for condition, columns in by_condition.items()
    ]
    if unguarded:
        clauses.append(
            f"{', '.join(unguarded)} count every row of the group"
        )
    return "; ".join(clauses)


def _parse_sql_expression(text: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(text, read="duckdb")
    except Exception:  # noqa: BLE001 — unparseable is "no evidence"
        return None


def _column_meta(
    model: DbtNode,
    name: str,
    fallback: str,
    default: ColumnType | None = None,
) -> tuple[ColumnType, str]:
    """(type, description) the dbt model declares for one of its columns.

    A DECLARED `data_type` always wins; `default` is what the caller knows the
    column to be from the plan, used only when the model is silent.
    """
    for column in model.columns:
        if column.name == name:
            declared = _map_type(column.data_type) if column.data_type else None
            return (
                declared or default or ColumnType.TEXT,
                column.description or fallback,
            )
    return (default or ColumnType.TEXT, fallback)


def _derived_result_type(
    expression: str, bound_types: dict[str, ColumnType | None]
) -> ColumnType | None:
    """Infer the public result type of a recovered scalar grain expression.

    The dbt manifests we ingest commonly omit ``data_type`` for derived
    columns. Falling back to TEXT is not harmless for a graded key: a day
    truncated from a timestamp is still a timestamp, and describing it as text
    leaves its exact serialized value ambiguous. Keep this deliberately closed
    and return no evidence for expression families we have not modeled.
    """
    node = _parse_sql_expression(expression)
    if node is None:
        return None
    if isinstance(node, (exp.Cast, exp.TryCast)):
        return _map_type(node.to.sql(dialect="duckdb"))
    if isinstance(node, exp.TimestampTrunc):
        return ColumnType.TIMESTAMP
    if isinstance(node, exp.DateTrunc):
        columns = tuple(node.find_all(exp.Column))
        if len(columns) != 1:
            return None
        source_type = bound_types.get(columns[0].name)
        # DuckDB's DATE_TRUNC returns TIMESTAMP for both DATE and TIMESTAMP
        # inputs.  The generated reference SQL runs in DuckDB, so reflecting
        # the input DATE here would publish a false output contract.
        if source_type in {ColumnType.DATE, ColumnType.TIMESTAMP}:
            return ColumnType.TIMESTAMP
    if isinstance(node, exp.MD5):
        return ColumnType.TEXT
    return None


def _flatten_text_concat(node: exp.Expression) -> tuple[exp.Expression, ...]:
    """Return ``||`` operands in their semantic left-to-right order."""
    if isinstance(node, exp.DPipe):
        return _flatten_text_concat(node.this) + _flatten_text_concat(node.expression)
    return (node,)


def _flatten_add(node: exp.Expression) -> tuple[exp.Expression, ...]:
    """Return ``+`` operands in their semantic left-to-right order."""
    if isinstance(node, exp.Paren):
        return _flatten_add(node.this)
    if isinstance(node, exp.Add):
        return _flatten_add(node.this) + _flatten_add(node.expression)
    return (node,)


def _columns_left_to_right(node: exp.Expression) -> tuple[exp.Column, ...]:
    """Columns below ``node`` in SQL expression order, not traversal order."""
    if isinstance(node, exp.Column):
        return (node,)
    return tuple(
        column
        for child in node.iter_expressions()
        for column in _columns_left_to_right(child)
    )


def _md5_surrogate_key_contract(expression: str, output: str) -> str | None:
    """Describe a dbt-utils-style MD5 key without exposing SQL syntax.

    Exact key bytes are graded output. Merely listing the referenced columns
    loses their order, delimiter, NULL sentinel and digest encoding, so an
    otherwise reasonable solver cannot reproduce the frozen reference.
    """
    node = _parse_sql_expression(expression)
    if not isinstance(node, exp.MD5):
        return None
    body = node.this
    if isinstance(body, (exp.Cast, exp.TryCast)):
        if _map_type(body.to.sql(dialect="duckdb")) is not ColumnType.TEXT:
            return None
        body = body.this
    terms = _flatten_text_concat(body)
    if len(terms) < 3 or len(terms) % 2 == 0:
        return None

    columns: list[str] = []
    sentinels: list[str] = []
    separators: list[str] = []
    for index, term in enumerate(terms):
        if index % 2:
            if not isinstance(term, exp.Literal) or not term.is_string:
                return None
            separators.append(term.name)
            continue
        if not isinstance(term, exp.Coalesce):
            return None
        value = term.this
        if isinstance(value, (exp.Cast, exp.TryCast)):
            if _map_type(value.to.sql(dialect="duckdb")) is not ColumnType.TEXT:
                return None
            value = value.this
        if not isinstance(value, exp.Column) or value.table:
            return None
        fallbacks = tuple(term.expressions)
        if (
            len(fallbacks) != 1
            or not isinstance(fallbacks[0], exp.Literal)
            or not fallbacks[0].is_string
        ):
            return None
        columns.append(value.name)
        sentinels.append(fallbacks[0].name)

    if len(set(separators)) != 1 or len(set(sentinels)) != 1:
        return None
    ordered = ", ".join(columns)
    return (
        f"To form {output}, take {ordered} in that order, render each value as "
        f"text, replace a missing value with {sentinels[0]!r}, and place "
        f"{separators[0]!r} between adjacent values. The result is the lowercase "
        "32-character hexadecimal MD5 digest of that combined text. It is "
        "computed before accumulation and remains part of the mart grain."
    )


#: Aggregates whose result type is that of the aggregated value.
_VALUE_TYPED_AGGREGATES = (exp.Sum, exp.Min, exp.Max)


def _measure_result_type(
    expr: str, bound_types: dict[str, ColumnType]
) -> ColumnType:
    """The type a recovered aggregate PRODUCES, from what it aggregates.

    COUNT -> BIGINT; AVG or any division -> FLOAT; SUM/MIN/MAX -> the type of
    the value aggregated (mixed numerics promote); anything unreadable -> TEXT.
    `bound_types` maps a bare ref to the type the cut adopted for it.
    """
    node = _parse_sql_expression(expr)
    if node is None:
        return ColumnType.TEXT
    aggs = list(node.find_all(exp.AggFunc))
    if not aggs:
        return ColumnType.TEXT
    if any(isinstance(a, exp.Count) for a in aggs) and len(aggs) == 1:
        return ColumnType.BIGINT
    if node.find(exp.Div) is not None or any(isinstance(a, exp.Avg) for a in aggs):
        return ColumnType.FLOAT
    if not all(isinstance(a, _VALUE_TYPED_AGGREGATES) for a in aggs):
        return ColumnType.TEXT

    def _values(value: exp.Expression) -> list[exp.Expression]:
        """The leaf VALUE nodes an aggregated expression can evaluate to."""
        if isinstance(value, exp.Paren):
            return _values(value.this)
        if isinstance(value, exp.Coalesce):
            return [v for arg in (value.this, *value.expressions) for v in _values(arg)]
        if isinstance(value, exp.Case):
            out: list[exp.Expression] = []
            for branch in value.args.get("ifs") or ():
                out.extend(_values(branch.args["true"]))
            default = value.args.get("default")
            if default is not None:
                out.extend(_values(default))
            return out
        if isinstance(value, (exp.Cast, exp.TryCast)):
            return [value]
        return [value]

    types: set[ColumnType] = set()
    for agg in aggs:
        for value in _values(agg.this):
            if isinstance(value, exp.Boolean):
                types.add(ColumnType.BOOLEAN)
            elif isinstance(value, exp.Literal):
                if value.is_string:
                    types.add(ColumnType.TEXT)
                elif "." in value.name:
                    types.add(ColumnType.FLOAT)
                else:
                    types.add(ColumnType.BIGINT)
            elif isinstance(value, (exp.Cast, exp.TryCast)):
                types.add(_map_type(value.to.sql(dialect="duckdb")))
            elif isinstance(value, exp.Column):
                bound = bound_types.get(value.name)
                if bound is None:
                    return ColumnType.TEXT
                types.add(bound)
            else:
                # Arithmetic over columns: numeric if every column is.
                cols = [c for c in value.find_all(exp.Column) if c.name]
                inner = {bound_types.get(c.name) for c in cols}
                if not cols or None in inner:
                    return ColumnType.TEXT
                types |= inner
    numeric = {ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.FLOAT, ColumnType.DECIMAL}
    if not types:
        return ColumnType.TEXT
    if types <= numeric:
        if ColumnType.FLOAT in types:
            return ColumnType.FLOAT
        if ColumnType.DECIMAL in types:
            return ColumnType.DECIMAL
        return ColumnType.BIGINT
    if len(types) == 1:
        return next(iter(types))
    return ColumnType.TEXT


def _describe_component_nulls(description: str, expression: str) -> str:
    """State row-level NULL handling for a recovered sum of components.

    Package SQL often defines each staging metric as zero when absent and then
    adds those metrics inside a final sum.  If recovery drops the staging
    wrappers, ``SUM(a + b)`` instead skips a row whenever either value is NULL.
    The expression expansion preserves the wrappers; this helper makes that
    otherwise invisible behavior part of the public column contract without
    prescribing SQL syntax.
    """
    node = _parse_sql_expression(expression)
    if node is None:
        return description
    for aggregate in node.find_all(exp.Sum):
        body = aggregate.this
        # ``Expression.find_all`` is breadth-first: on a left-associated ADD
        # tree it visits the rightmost component before the earlier ones.  The
        # order is graded prose here ("uses exactly a + b + c"), so flatten
        # the ADD tree and walk each term in SQL order instead.
        columns = [
            column
            for term in _flatten_add(body)
            for column in _columns_left_to_right(term)
        ]
        if len({column.name for column in columns}) < 2 or body.find(exp.Add) is None:
            continue

        ordered_names = tuple(dict.fromkeys(column.name for column in columns))
        names = ", ".join(ordered_names)
        exact_sum = " + ".join(ordered_names)
        resolved = re.sub(
            r"\(\s*default\s*=\s*[^)]*\)",
            f"(this mart uses exactly {exact_sum})",
            description.rstrip(),
            flags=re.IGNORECASE,
        )
        if resolved == description.rstrip():
            resolved += f" This mart uses exactly {exact_sum} per input row."
        resolved += (
            " This exact per-input component list is complete and authoritative "
            "for this column; every named component remains an input even if it "
            "is not emitted as a separate mart output."
        )

        def defaulted_to_zero(column: exp.Column) -> bool:
            parent = column.parent
            while parent is not None and parent is not aggregate:
                if isinstance(parent, exp.Coalesce):
                    values = (parent.this, *parent.expressions)
                    return any(
                        isinstance(value, exp.Literal)
                        and not value.is_string
                        and float(value.name) == 0.0
                        for value in values
                    )
                parent = parent.parent
            return False

        if columns and all(defaulted_to_zero(column) for column in columns):
            return (
                resolved
                + f" For each input row, missing {names} values contribute 0 "
                "before the components are added; the resulting row values are "
                "then accumulated for the mart grain."
            )
        # No zero default around every leaf: SQL addition becomes missing when
        # ANY component remains missing, and SUM then skips that row.  State
        # that outcome explicitly; otherwise both "missing means zero" and
        # "missing row is skipped" are reasonable readings of "sum of A+B".
        return (
            resolved
            + " For each input row, the stated fallback values are chosen "
            "before the components are added. If any added component still "
            "has no value after those fallbacks, that row's combined value is "
            "missing and does not contribute to the accumulated result."
        )
    return description


def _null_propagating_recovered_aggregate(expression: str) -> bool:
    """Can NULL source refs force this recovered aggregate to return NULL?

    Deliberately closed to aggregate/scalar forms whose NULL behavior is part
    of ordinary SQL arithmetic.  This is witness admission, not expression
    recovery: an unfamiliar function simply gets no all-NULL population claim.
    ``COALESCE``/``CASE`` are excluded to match ``build_rollup``'s public
    undefaulted-measure rule and avoid claiming that a fallback-preserving
    expression returns NULL.
    """

    node = _parse_sql_expression(expression)
    if not isinstance(node, (exp.Sum, exp.Avg, exp.Min, exp.Max)):
        return False
    if any(isinstance(part, (exp.Coalesce, exp.Case)) for part in node.walk()):
        return False

    def propagates(part: exp.Expression) -> bool:
        if isinstance(part, exp.Column):
            return True
        if isinstance(
            part,
            (
                exp.Paren,
                exp.Cast,
                exp.TryCast,
                exp.Round,
                exp.Neg,
                exp.Nullif,
            ),
        ):
            return isinstance(part.this, exp.Expression) and propagates(part.this)
        if isinstance(part, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Pow)):
            return any(propagates(child) for child in part.iter_expressions())
        return False

    return isinstance(node.this, exp.Expression) and propagates(node.this)


def _round_before_sum_signature(
    expression: str,
) -> tuple[str, Decimal, int] | None:
    """Recognize exactly ``SUM(ROUND(column / positive_literal, places))``.

    This is evidence admission, not a general algebraic rewrite.  Casts,
    arithmetic around the input, multiple references, implicit precision, and
    non-literal divisors are all rejected so the population never promises a
    rounding-order witness for semantics it did not recover exactly.
    """

    node = _parse_sql_expression(expression)
    if not isinstance(node, exp.Sum) or not isinstance(node.this, exp.Round):
        return None
    rounded = node.this
    divided = rounded.this
    places_node = rounded.args.get("decimals")
    if (
        not isinstance(divided, exp.Div)
        or not isinstance(divided.this, exp.Column)
        or not isinstance(divided.expression, exp.Literal)
        or divided.expression.is_string
        or not isinstance(places_node, exp.Literal)
        or places_node.is_string
    ):
        return None
    places_text = places_node.name
    if not re.fullmatch(r"\d+", places_text):
        return None
    places = int(places_text)
    if not 0 <= places <= 6:
        return None
    try:
        divisor = Decimal(divided.expression.name)
    except (InvalidOperation, ValueError):
        return None
    if not divisor.is_finite() or divisor <= 0:
        return None
    return divided.this.name, divisor, places


#: (pattern, canonical noun) for the unit phrases the vendored sources actually
#: state. Deliberately CLOSED: an open-ended "mentions a unit" heuristic fires
#: on names like `pricing_unit` that state no unit of a summed NUMBER.
_SOURCE_UNITS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"micro[-\s]?dollars?", re.I), "microdollars"),
    (re.compile(r"micro[-\s]?currency", re.I), "micro-currency"),
    # The Fivetran twitter_ads sources say plainly "in micros", which none of
    # the patterns above match: `billed_charge_local_micro` shipped with no
    # unit on either the source column or the mart column built from it
    # (batch10 2026-09-11). Anchored to the preposition so it states the unit
    # of a number rather than merely naming a unit column.
    (re.compile(r"\bin\s+micros\b", re.I), "micros"),
    (re.compile(r"\bmilliseconds?\b", re.I), "milliseconds"),
)

#: A mart description CLAIMING the value was converted out of its source unit.
#: Worse than silence when no model divides: it tells the independent
#: implementer to divide a number gold never divided.
_CONVERSION_CLAIM = re.compile(
    r"converted\s+(?:out\s+of|from)|/\s*1[,.]?0{3}[,.]?0{3}", re.I
)


def _stated_unit(description: str) -> str | None:
    """The unit this description states, or None. First match wins (stable)."""
    for pattern, unit in _SOURCE_UNITS:
        if pattern.search(description):
            return unit
    return None


def _source_descriptions(spec: CandidateSpec) -> dict[str, dict[str, str]]:
    """source table name -> column name -> the manifest's own description.

    The SAME text `_source_to_table` publishes, so a unit carried from here
    says exactly what the solver's bundle says.
    """
    out: dict[str, dict[str, str]] = {}
    for node in spec.sources:
        out[node.name] = {c.name: (c.description or "") for c in node.columns}
    return out


#: Sentence appended to a hop passthrough so the column STATES its origin,
#: phrased as the plan library phrases its own so one wording means one thing.
_LINEAGE_TEMPLATE = "Taken as the `{column}` of the matching `{table}` record."


def _states_origin(description: str, table: str) -> bool:
    """Does this description already bind the column to `table`?"""
    return f"`{table}`" in description


def _carry_lineage(description: str, table: str, column: str) -> str:
    """Make a hop passthrough state WHICH source record it is taken from.

    Without it, same-named columns from different tables ship byte-identical
    descriptions and the author has no way to recover the binding. States a
    fact grounding already proved, never inference. Idempotent: a description
    already naming the table is returned unchanged.
    """
    if not table or not column:
        return description
    text = (description or "").strip()
    if _states_origin(text, table):
        return description
    sentence = _LINEAGE_TEMPLATE.format(column=column, table=table)
    if not text:
        return sentence
    return f"{text.rstrip('.')}. {sentence}"


#: The outer aggregate of a recovered measure, in words a solver can act on.
_AGGREGATION_PHRASES: dict[type, str] = {
    exp.Sum: "added up over the source rows that share the mart row's grain",
    exp.Count: "counted over the source rows that share the mart row's grain",
    exp.Max: "the largest value over the source rows that share the mart row's grain",
    exp.Min: "the smallest value over the source rows that share the mart row's grain",
    exp.Avg: "averaged over the source rows that share the mart row's grain",
}


def _aggregation_clause(expression: str) -> str:
    """How a recovered measure combines its source rows, or "".

    twitter_ads shipped six measures whose descriptions said only "`X` is the
    value of source column `T.X`"; the ambiguity critic read that as
    "taken per row" against the aggregate rule's "for that row's matching
    rows" and filed the accumulation as unstated (batch10 2026-09-11). The
    outer aggregate of the emitted expression says which it is.
    """
    try:
        tree = sqlglot.parse_one(str(expression))
    except (sqlglot.errors.ParseError, ValueError, TypeError):
        return ""
    outer = tree.this if isinstance(tree, exp.Alias) else tree
    while isinstance(outer, (exp.Cast, exp.Coalesce, exp.Round, exp.Paren)):
        outer = outer.this
    for kind, phrase in _AGGREGATION_PHRASES.items():
        if isinstance(outer, kind):
            return phrase
    return ""


def _carry_measure_lineage(
    description: str,
    bindings: tuple[tuple[str, str, str], ...],
    expression: str = "",
) -> str:
    """Publish the source column behind every recovered per-input name.

    A staging rename can leave a measure expression reading an alias that does
    not exist in any published source schema (for example ``spend_micro`` over
    ``billed_charge_local_micro``). Grounding already proves the exact binding;
    carry that evidence into the mart-column contract so an independent solver
    can construct every input value from the public schemas. The order follows
    the recovered expression's stable ref order and duplicate bindings collapse.
    """
    unique = tuple(dict.fromkeys(bindings))
    if not unique:
        return description
    mappings = "; ".join(
        f"`{alias}` is the value of source column `{table}.{source}`"
        for table, source, alias in unique
    )
    # A measure whose conversion sentence already says how the values are
    # combined ("each source value is divided ... and those values are then
    # added together") must not get a second, cruder statement here: "added
    # up over the source rows" beside it read as "sum the raw micros, then
    # divide", a different number (twitter_ads `spend`, batch10 2026-09-11).
    converts = bool(expression) and _conversion_facts(expression) is not None
    combined = _aggregation_clause(expression) if expression and not converts else ""
    tail = f", {combined}" if combined else ""
    return f"{description.rstrip()} Per-input source lineage: {mappings}{tail}."


#: Aggregates a conversion can sit inside or outside of. Which side it sits on
#: is the whole difference between "each source value is divided, then they are
#: added together" and "the added-up total is divided", and a rounded division
#: makes the two disagree on real data.
_CONVERSION_AGGREGATES = (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count)


def _inside_aggregate(node) -> bool:
    """Does an aggregate enclose this node?"""
    node = node.parent
    while node is not None:
        if isinstance(node, _CONVERSION_AGGREGATES):
            return True
        node = node.parent
    return False


def _conversion_facts(expression: str):
    """`(divisor, places, div_inside, round_inside, has_aggregate)`, or None.

    None for anything but ONE division by ONE numeric literal with at most one
    ROUND: a compound expression is better described by silence than by a
    claim that is only half true.
    """
    try:
        tree = sqlglot.parse_one(str(expression))
    except (sqlglot.errors.ParseError, ValueError, TypeError):
        return None
    divisions = list(tree.find_all(exp.Div))
    if len(divisions) != 1:
        return None
    denominator = divisions[0].expression
    if not isinstance(denominator, exp.Literal) or not denominator.is_number:
        return None
    roundings = list(tree.find_all(exp.Round))
    if len(roundings) > 1:
        return None
    places = None
    round_inside = False
    if roundings:
        decimals = roundings[0].args.get("decimals")
        if isinstance(decimals, exp.Literal) and decimals.is_number:
            places = int(float(decimals.name))
        round_inside = _inside_aggregate(roundings[0])
    return (
        denominator.name,
        places,
        _inside_aggregate(divisions[0]),
        round_inside,
        any(True for _ in tree.find_all(*_CONVERSION_AGGREGATES)),
    )


def _sum_proven_non_null(expression: str) -> bool:
    """Is this aggregate expression proven non-NULL for a non-empty group
    (a COALESCE around it, or a 0-substituted argument)? Unknown parses count
    as nullable, so the empty-total clause is stated."""
    from elt_taskgen.generation.mart_plan import _expression_proven_non_null, _parse_expression

    parsed = _parse_expression(expression)
    if parsed is None:
        return False
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(str(expression), read="duckdb")
        # A COALESCE anywhere inside the aggregate's argument (a 0-substituted
        # input) or around the aggregate itself: never an empty result.
        for coalesce in tree.find_all(exp.Coalesce):
            defaults = list(coalesce.expressions)
            if defaults and all(isinstance(d, exp.Literal) and not d.is_string for d in defaults):
                return True
        return bool(_expression_proven_non_null(parsed, non_empty_group=True))
    except Exception:  # noqa: BLE001 - an unreadable expression is nullable
        return False


def _conversion_sentence(expression: str, unit: str, origin: str) -> str:
    """Describe the unit conversion applied by an expression, or return ``""``.

    State conversions explicitly when an output and its source use different
    units, including division placement, aggregation, rounding, and null
    behavior. This prevents descriptions from omitting a scale conversion that
    is present in the gold expression.
    """
    facts = _conversion_facts(expression)
    if facts is None:
        return ""
    divisor, places, div_inside, round_inside, has_aggregate = facts
    try:
        readable = f"{int(float(divisor)):,}"
    except (TypeError, ValueError):
        readable = str(divisor)
    rounding = f" and rounded to {places} decimal places" if places is not None else ""
    if not has_aggregate:
        what = f"the value is divided by {readable}{rounding}"
    elif div_inside and (places is None or round_inside):
        what = (
            f"each source value is divided by {readable}{rounding}, and those "
            "values are then added together, a source row with no value adding "
            "nothing to the total"
        )
        if not _sum_proven_non_null(expression):
            # The reasoning witness wrapped `spend` in a 0 default where gold
            # keeps an all-missing total empty (dbt__twitter_ads, batch10
            # run P, 2026-09-11): the column says so itself now.
            what += (
                ", and a mart row whose source rows all lack a value reports an "
                "empty total, not 0"
            )
    elif not div_inside and (places is None or not round_inside):
        what = f"the added-up total is divided by {readable}{rounding}"
    else:
        # Division and rounding sit on opposite sides of the aggregate. Say
        # where each one happens instead of picking one order for both.
        inner = "each source value" if div_inside else "the added-up total"
        outer = "each source value" if round_inside else "the added-up total"
        what = (
            f"{inner} is divided by {readable}, and {outer} is rounded to "
            f"{places} decimal places"
        )
    source = f"the {unit} of {origin}" if unit else str(origin)
    return f" Units: converted out of {source} — {what}."


def _carry_units(
    description: str,
    origins: tuple[tuple[str, str], ...],
    source_descriptions: dict[str, dict[str, str]],
    *,
    converts: str = "",
) -> str:
    """Make a mart column state the UNIT its own gold is in, or return it as-is.

    Undeclared units let a witness divide where gold does not, so the unit the
    SOURCE states is carried onto the mart column and both builders read one
    claim. Evidence only, and only where the mart is SILENT. The inverse — a
    description claiming a conversion the expression does not apply — is
    contradicted in place, not deleted, so the reader sees both.
    """
    stated: list[tuple[str, str, str]] = []
    for table, column in origins:
        text = source_descriptions.get(table, {}).get(column, "")
        unit = _stated_unit(text)
        if unit is not None:
            stated.append((table, column, unit))
    # WHETHER A CONVERSION IS APPLIED IS A FACT ABOUT THE EXPRESSION, not
    # about whether an expression was passed. Branching on the truthiness of
    # `converts` made every measure look like a conversion and silenced the
    # "carried unchanged" sentence on `spend_micro`, whose expression divides
    # by nothing.
    converted = (
        _conversion_sentence(
            converts,
            "",
            ", ".join(f"{t}.{c}" for t, c in origins),
        )
        if converts
        else ""
    )
    if converted and not stated:
        # A conversion is a fork whether or not the SOURCE names a unit, so
        # the arithmetic is stated even when there is no unit to name.
        return f"{description.rstrip()}{converted}"
    if not stated:
        return description
    units = sorted({u for _, _, u in stated})
    if len(units) != 1:
        # Two units in one expression: say nothing rather than pick one.
        return description
    unit = units[0]
    origin = ", ".join(f"{t}.{c}" for t, c, _ in stated)
    claims_conversion = bool(_CONVERSION_CLAIM.search(description))
    if converted:
        # STATE THE CONVERSION. Silence here is what left `spend` and
        # `spend_micro` reading as the same number (see _conversion_sentence).
        if claims_conversion and _stated_unit(description) is not None:
            return description
        return f"{description.rstrip()}{_conversion_sentence(converts, unit, origin)}"
    if claims_conversion:
        return (
            f"{description.rstrip()} NOTE: this task's reference applies NO "
            f"such conversion — the value is the raw {unit} figure carried "
            f"from {origin}."
        )
    if _stated_unit(description) is not None:
        return description
    return (
        f"{description.rstrip()} Units: {unit}, carried unchanged from "
        f"{origin} (the reference applies no unit conversion)."
    )


def _model_to_mart(
    spec: CandidateSpec,
    model: DbtNode,
    closure_tables: list[str],
    relationships: tuple[Relationship, ...],
    grounding: _Grounding,
    key_evidence: str,
    source_columns: dict[str, set[str]],
    aliases: dict[str, dict[str, str]],
    resolved_types: dict[str, dict[str, ColumnType]] | None = None,
) -> tuple[MartSpec, StarShape]:
    """Build ``MartSpec`` from the grounded contract.

    Drop measures incompatible with adopted ``resolved_types``. Emit either a
    one-row-per-source projection or an aggregate whose operation kind comes
    from recovered measures. Declare a source operation for every closure
    table, and derive each emitted ``MartColumnKind`` from SQL rather than
    vendor prose.
    """
    types = {c.name: c for c in model.columns}
    #: The published source prose, read ONCE. A mart column whose source states
    #: a unit must state it too, or the dual-build gate refuses the task.
    source_descriptions = _source_descriptions(spec)

    def _notes(drop_reasons: tuple[tuple[str, str], ...]) -> str:
        """The plan note, including the FULL grounding account."""
        head = (
            f"Recovered from dbt model {model.unique_id}, grounded in source "
            f"table {grounding.base_table} ({grounding.mode}). "
            f"Key evidence: {key_evidence}."
        )
        if not drop_reasons:
            return head + (
                f" GROUNDING: {grounding.declared} declared, all grounded."
            )
        kept = grounding.declared - len(drop_reasons)
        return head + (
            f" GROUNDING: {grounding.declared} declared, {kept} grounded, "
            f"{len(drop_reasons)} dropped — "
            + "; ".join(f"{c}: {r}" for c, r in drop_reasons)
            + "."
        )

    notes = _notes(grounding.drop_reasons)
    extra_sources = tuple(t for t in sorted(closure_tables))

    adopted = resolved_types or {}

    def _adopted(table: str, column: str) -> ColumnType | None:
        return adopted.get(table, {}).get(column)

    if grounding.mode == "projection":
        mart_columns = tuple(
            MartColumn(
                name=dst,
                type=_column_meta(
                    model, dst, "", default=_adopted(grounding.base_table, src)
                )[0],
                description=_carry_units(
                    types[dst].description or f"Column {dst} of mart {model.name}.",
                    ((grounding.base_table, src),),
                    source_descriptions,
                ),
                kind=MartColumnKind.PASSTHROUGH,
            )
            for src, dst in grounding.select_map
        )
        built = build_projection(
            mart=model.name,
            table=grounding.base_table,
            select_map=grounding.select_map,
            key_columns=grounding.key_columns,
            extra_sources=extra_sources,
            description=(
                f"Project {grounding.base_table} to the mart contract, applying the "
                "staging renames the dbt package declares."
            ),
            notes=notes,
        )
        return (
            MartSpec(
                name=model.name,
                description=(
                    model.description
                    or f"Mart {model.name} from package {spec.package_name}."
                ),
                grain=_declared_grain(grounding.key_columns, model.description),
                key_columns=grounding.key_columns,
                columns=mart_columns,
                plan=built.plan,
            ),
            built.shape,
        )

    base = grounding.base_table
    evidence_key = grounding.key_columns
    grain_source: dict[str, str] = dict(
        zip(evidence_key, grounding.key_source_columns)
    )

    # THE DECLARED GRAIN OF A ROLLUP IS ITS GROUP BY. Carried attributes join
    # the GROUP BY, so one row per `evidence_key` holds only if each is
    # functionally dependent on it — which no manifest proves. They therefore
    # become KEY columns, declaring the grain the GROUP BY actually produces.
    attribute_aliases = tuple(
        dst for _, dst in grounding.select_map if dst not in set(evidence_key)
    )
    derived_grain = grounding.derived
    key_aliases = (
        evidence_key
        + attribute_aliases
        + tuple(projection.column for projection in derived_grain)
    )
    for src, dst in grounding.select_map:
        grain_source[dst] = src

    direct_keys = tuple(
        KeyColumn(
            column=k,
            type=_column_meta(
                model,
                k,
                f"Grain column {k} of mart {model.name}.",
                default=_adopted(base, grain_source[k]),
            )[0],
            description=_carry_units(
                _column_meta(model, k, f"Grain column {k} of mart {model.name}.")[1],
                ((base, grain_source[k]),),
                source_descriptions,
            ),
            source=grain_source[k],
        )
        for k in evidence_key + attribute_aliases
    )
    computed_key_items: list[KeyColumn] = []
    for projection in derived_grain:
        expression_node = _parse_sql_expression(projection.expr)
        inferred_type = _derived_result_type(
            projection.expr,
            {ref: _adopted(base, ref) for ref in projection.refs},
        )
        ctype, declared_description = _column_meta(
            model,
            projection.column,
            f"Computed grain column {projection.column} of mart {model.name}.",
            default=inferred_type,
        )
        exact_key_contract = _md5_surrogate_key_contract(
            projection.expr, projection.column
        )
        if exact_key_contract is not None:
            description = exact_key_contract
        elif (
            isinstance(expression_node, (exp.DateTrunc, exp.TimestampTrunc))
            and len(projection.refs) == 1
        ):
            if ctype is ColumnType.TIMESTAMP:
                outcome = (
                    f"It is {projection.refs[0]} truncated to the start of its "
                    "calendar day at 00:00:00"
                )
            else:
                outcome = f"It is the calendar date containing {projection.refs[0]}"
            description = (
                declared_description.rstrip()
                + f" {outcome} and remains part of the mart grain."
            )
        else:
            description = (
                declared_description.rstrip()
                + " It is computed before aggregation from "
                + ", ".join(projection.refs)
                + " and remains part of the mart grain."
            )
        computed_key_items.append(
            KeyColumn(
                column=projection.column,
                type=ctype,
                description=description,
                expr=projection.expr,
                kind=MartColumnKind.DERIVED,
            )
        )
    computed_keys = tuple(computed_key_items)
    keys = direct_keys + computed_keys

    passthrough: list[Passthrough] = []
    hop_carry: dict[str, list[tuple[str, str]]] = {}
    for table, src, dst in grounding.joined_columns:
        ctype, desc = _column_meta(
            model,
            dst,
            f"Attribute {dst} of mart {model.name}.",
            default=_adopted(table, src),
        )
        passthrough.append(
            Passthrough(
                column=dst,
                type=ctype,
                description=_carry_lineage(
                    _carry_units(desc, ((table, src),), source_descriptions),
                    table,
                    src,
                ),
                source=src,
                from_hop=table,
            )
        )
        hop_carry.setdefault(table, []).append((src, dst))
        grain_source[dst] = src

    grain_names = {k.column for k in keys} | {p.column for p in passthrough}

    # Every measure ref must reach the aggregate wearing its own name; a ref
    # colliding with a grain column bound elsewhere drops the measure. Keyed by
    # ALIAS, never source column: one column can arrive under TWO names.
    parent_carry: dict[str, str] = {}          # alias -> parent column
    hop_ref_carry: dict[str, dict[str, str]] = {}   # table -> {alias: column}
    join_key_alias: dict[str, str] = {}        # parent column -> carried alias
    measures: list[Measure] = []
    all_null_witnesses: list[AllNullAggregateWitness] = []
    round_before_sum_witnesses: list[RoundBeforeSumWitness] = []
    drop_reasons = list(grounding.drop_reasons)

    def _join_alias(col: str) -> str:
        """The alias the base projection carries parent column `col` under.

        ONE projection per source column, so a join key already projected joins
        on THAT alias; only a column projected nowhere else gets a new carry.
        """
        have = join_key_alias.get(col)
        if have is not None:
            return have
        for k in key_aliases:
            if grain_source.get(k) == col:
                join_key_alias[col] = k
                return k
        for alias, src in sorted(parent_carry.items()):
            if src == col:
                join_key_alias[col] = alias
                return alias
        alias = f"{base}__{col}"
        parent_carry.setdefault(alias, col)
        join_key_alias[col] = alias
        return alias

    def _drop(name: str, reason: str) -> None:
        """Record a column this BUILDER drops, on top of what grounding dropped."""
        drop_reasons.append((name, reason))

    join_tables = {table: (b, o) for table, b, o in grounding.joins}
    for measure in grounding.measures:
        if measure.kind is ProjectionKind.WINDOW:
            _drop(
                measure.column,
                "recovered expression is an OVER() window, which cannot be a "
                "GROUP BY measure; the plan library's post_windows stage owns it",
            )
            continue
        bindings: list[tuple[str, str, str]] = []  # (table, source column, ref)
        conflict = ""
        for ref in measure.refs:
            if ref in grain_names:
                if grain_source.get(ref) != ref:
                    conflict = (
                        f"references {ref}, which the grain already binds to "
                        f"{grain_source.get(ref)!r}"
                    )
                    break
                continue
            table = None
            # Bind within the ref's SOURCE LINEAGE only: a ref read from a hop
            # table never binds to a same-named base column, or vice versa.
            candidates = _lineage_tables(
                ref, (base, *sorted(join_tables)), measure.lineage
            )
            for candidate in candidates:
                if _ground_column(ref, candidate, source_columns, aliases) is not None:
                    table = candidate
                    break
            if table is None:
                conflict = f"references {ref}, which grounds in no table in scope"
                break
            src = _ground_column(ref, table, source_columns, aliases)
            assert src is not None
            bindings.append((table, src, ref))
        bound_types: dict[str, ColumnType] = {}
        if not conflict:
            # A measure demanding a type the CUT did not adopt cannot run, so
            # it is dropped: retyping one column for one mart would make two
            # marts disagree about what the same source holds.
            wants = _required_types(measure.expr)
            for table, src, ref in bindings:
                want = wants.get(ref)
                have = _adopted(table, src)
                if have is not None:
                    bound_types[ref] = have
                if want is not None and have is not None and not _type_satisfies(have, want):
                    conflict = (
                        f"needs {src} of {table} to be {want.value}, but the cut "
                        f"adopted {have.value} (declared, cast by staging, or "
                        "demanded by another mart's SQL)"
                    )
                    break
        if conflict:
            _drop(measure.column, f"recovered aggregate {conflict}")
            continue
        for table, src, ref in bindings:
            if table == base:
                parent_carry[ref] = src
            else:
                hop_ref_carry.setdefault(table, {})[ref] = src
        ctype, desc = _column_meta(
            model,
            measure.column,
            f"Measure {measure.column} of mart {model.name}.",
            default=_measure_result_type(measure.expr, bound_types),
        )
        measure_description = _carry_units(
            desc,
            tuple((t, s) for t, s, _ in bindings),
            source_descriptions,
            converts=measure.expr,
        )
        bound_refs = {ref for _table, _source, ref in bindings}
        bound_sources = tuple(
            dict.fromkeys(source for _table, source, _ref in bindings)
        )
        direct_group_sources = tuple(
            dict.fromkeys(source for source, _alias in grounding.select_map)
        )
        if (
            bindings
            and set(measure.refs) == bound_refs
            and all(table == base for table, _source, _ref in bindings)
            and direct_group_sources
            and not set(bound_sources) & set(direct_group_sources)
            and _null_propagating_recovered_aggregate(measure.expr)
        ):
            all_null_witnesses.append(
                AllNullAggregateWitness(
                    source_table=base,
                    input_columns=bound_sources,
                    measure_columns=(measure.column,),
                    direct_group_columns=direct_group_sources,
                )
            )
        rounding_signature = _round_before_sum_signature(measure.expr)
        if rounding_signature is not None:
            rounding_ref, divisor, decimal_places = rounding_signature
            exact_binding = next(
                (
                    (table, source)
                    for table, source, ref in bindings
                    if ref == rounding_ref
                ),
                None,
            )
            if (
                len(bindings) == 1
                and set(measure.refs) == {rounding_ref}
                and exact_binding is not None
                and exact_binding[0] == base
                and direct_group_sources
                and exact_binding[1] not in set(direct_group_sources)
                and bound_types.get(rounding_ref)
                in {
                    ColumnType.INTEGER,
                    ColumnType.BIGINT,
                    ColumnType.FLOAT,
                    ColumnType.DECIMAL,
                }
            ):
                round_before_sum_witnesses.append(
                    RoundBeforeSumWitness(
                        source_table=base,
                        input_column=exact_binding[1],
                        measure_columns=(measure.column,),
                        direct_group_columns=direct_group_sources,
                        divisor=divisor,
                        decimal_places=decimal_places,
                    )
                )
        measures.append(
            Measure(
                column=measure.column,
                expr=measure.expr,
                type=ctype,
                # `converts` reads the EMITTED expression, not vendor prose: a
                # division here means the result left the source's unit.
                description=_carry_measure_lineage(
                    _describe_component_nulls(measure_description, measure.expr),
                    tuple(bindings),
                    measure.expr,
                ),
                kind=MartColumnKind.AGGREGATED,
            )
        )

    hops: list[StarJoin] = []
    join_edges: list[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = []
    for table, base_cols, other_cols in grounding.joins:
        carry = sorted(set(hop_carry.get(table, ())) | set(
            (src, alias) for alias, src in hop_ref_carry.get(table, {}).items()
        ))
        if not carry:
            continue
        for col in base_cols:
            _join_alias(col)
        hops.append(
            StarJoin(
                table=table,
                on_pairs=tuple(
                    (join_key_alias[b], o) for b, o in zip(base_cols, other_cols)
                ),
                carry=tuple(carry),
                rel_columns=tuple(base_cols) + tuple(other_cols),
                description=(
                    f"Each {base} row is matched to its {table} row across "
                    f"the relationship the package declares; {table} is the "
                    f"parent side, so every {base} row appears exactly once, "
                    f"including {base} rows with no {table} row."
                ),
            )
        )
        join_edges.append((base, tuple(base_cols), table, tuple(other_cols)))

    if not measures:
        # No recoverable measure: take the project's own join surface, so the
        # mart still measures something and the relationship stays in the plan.
        link = _first_related(base, relationships, set(closure_tables))
        if link is not None:
            other, base_cols, other_cols = link
            for col in base_cols:
                _join_alias(col)
            link_alias = f"{other}__{other_cols[0]}"
            hops.append(
                StarJoin(
                    table=other,
                    on_pairs=tuple(
                        (join_key_alias[b], o) for b, o in zip(base_cols, other_cols)
                    ),
                    carry=((other_cols[0], link_alias),),
                    rel_columns=tuple(base_cols) + tuple(other_cols),
                    description=(
                        f"{base} rows are matched to {other} rows across the "
                        f"relationship the package declares; {base} rows with "
                        f"no matching {other} rows are retained."
                    ),
                )
            )
            join_edges.append((base, tuple(base_cols), other, tuple(other_cols)))
            count_col = f"{other}_count"
            measures.append(
                Measure(
                    column=count_col,
                    expr=f"COUNT({quote(link_alias)})",
                    type=ColumnType.BIGINT,
                    description=(
                        f"Number of {other} rows related to this {base} row; 0 when none."
                    ),
                    kind=MartColumnKind.AGGREGATED,
                )
            )
        else:
            # No measure and nothing to count over: the grain is every column,
            # so `COUNT(*)` is 1 on every row. Pass 1 predicts this and drops
            # the mart alone; reaching it here refuses the whole CUT.
            raise DegenerateMartError(
                f"mart model {model.unique_id!r} recovered no measure and has "
                f"no declared relationship to count over, so its grain "
                f"({', '.join(key_aliases)}) covers every column it projects: "
                "the mart is SELECT DISTINCT plus a constant 1, which "
                "gates.info-content refuses at stage 9. Dropped at ingest"
            )

    notes = _notes(tuple(drop_reasons))

    built = build_rollup(
        mart=model.name,
        shape_name="dbt_recovered_rollup",
        parent=base,
        keys=keys,
        passthrough=tuple(passthrough),
        parent_carry=tuple(
            (src, alias) for alias, src in sorted(parent_carry.items())
        ),
        hops=tuple(hops),
        measures=tuple(measures),
        # SURVIVING measures only: a predicate naming a dropped measure would
        # describe a column the mart never ships.
        aggregate_predicate=_predicate_of(tuple(measures)),
        extra_sources=extra_sources,
        notes=notes,
        # The mart contract is the PACKAGE's: deleting a small vendor mart over
        # a budget it never agreed to throws away real transformation logic.
        # The budget is REPORTED on the plan notes instead.
        enforce_budget=False,
    )
    budget = budget_problems(built)
    plan = built.plan
    if budget:
        plan = plan.model_copy(
            update={
                "notes": plan.notes
                + " BUDGET: "
                + "; ".join(budget)
                + " (recovered from the package as-is; not padded)."
            }
        )
    # Omit hop-carried attributes from the logical grain only when current keys
    # determine every lookup key; otherwise retain them in the contract.
    grouped_hop_aliases = {p.column for p in passthrough if p.from_hop}
    determined_aliases = set(key_aliases)
    retained_hop_aliases: list[str] = []
    counterfactual_join_edges: list[
        tuple[str, tuple[str, ...], str, tuple[str, ...]]
    ] = []
    for hop, edge in zip(hops, join_edges, strict=True):
        carried_group_aliases = tuple(
            alias for _source, alias in hop.carry if alias in grouped_hop_aliases
        )
        hop_is_determined = (
            set(left for left, _right in hop.on_pairs) <= determined_aliases
        )
        if not hop_is_determined:
            retained_hop_aliases.extend(carried_group_aliases)
            relation = next(
                (
                    relationship
                    for relationship in relationships
                    if relationship.child_table == base
                    and relationship.parent_table == hop.table
                    and set(
                        zip(
                            relationship.child_columns,
                            relationship.parent_columns,
                        )
                    )
                    == set(
                        (
                            parent_carry.get(
                                left, grain_source.get(left, left)
                            ),
                            right,
                        )
                        for left, right in hop.on_pairs
                    )
                ),
                None,
            )
            if carried_group_aliases and (
                relation is None or not relation.required
            ):
                raise UnprovableGrainError(
                    f"mart model {model.unique_id!r}: hop-carried grain "
                    f"column(s) {', '.join(carried_group_aliases)} are not "
                    f"functionally determined by ({', '.join(key_aliases)}), "
                    f"and the LEFT lookup from {base} to {hop.table} is "
                    "optional; an unmatched row would NULL-extend a logical "
                    "grain key. This post-construction proof failure refuses "
                    "the candidate cut rather than publishing a nullable "
                    "identity"
                )
        # An INNER_JOIN counterfactual may orphan this edge only when doing so
        # cannot NULL-extend a retained logical key.  Hops carrying no grouped
        # attribute are safe too: they affect measures, not the grain.
        if hop_is_determined or not carried_group_aliases:
            counterfactual_join_edges.append(edge)
        # Whether inherited by FD or retained explicitly, these aliases are
        # determined for any later builder-representable lookup hop.
        determined_aliases.update(carried_group_aliases)
    declared_grain = key_aliases + tuple(
        dict.fromkeys(
            alias for alias in retained_hop_aliases if alias not in set(key_aliases)
        )
    )
    return (
        MartSpec(
            name=model.name,
            description=(
                model.description
                or f"Mart {model.name} from package {spec.package_name}."
            ),
            # A rollup whose GROUP BY carries attribute columns beside the
            # keys says so in its grain: "one row per <keys>" alone read
            # against a rule grouping by the attributes too was a fork
            # (twitter_ads, batch10 run J, 2026-09-11).
            grain=_grouped_grain(
                _declared_grain(declared_grain, model.description),
                tuple(item.column for item in tuple(passthrough)),
            ),
            key_columns=declared_grain,
            columns=built.columns,
            plan=plan,
        ),
        # A RECOVERED rollup's counterfactual is generic constructed rows, not
        # the library's duplicate-child witness, so `no_dedup` must claim
        # STRESS rather than the counterfactual.
        replace(
            built.shape,
            dedupe_counterfactual_witness=False,
            join_counterfactual_witness=False,
            join_edges=tuple(counterfactual_join_edges),
            all_null_aggregate_witnesses=tuple(all_null_witnesses),
            round_before_sum_witnesses=tuple(round_before_sum_witnesses),
        ),
    )


def _first_related(
    table: str, relationships: tuple[Relationship, ...], closure: set[str]
) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    """The first declared relationship touching `table`, deterministically.

    Returns (other table, this side's columns, the other side's columns).
    """
    for rel in sorted(
        relationships, key=lambda r: (r.child_table, r.parent_table, r.child_columns)
    ):
        if rel.parent_table == table and rel.child_table in closure:
            return rel.child_table, tuple(rel.parent_columns), tuple(rel.child_columns)
        if rel.child_table == table and rel.parent_table in closure:
            return rel.parent_table, tuple(rel.child_columns), tuple(rel.parent_columns)
    return None


def _assign_backends(family_id: str, table_names: list[str]) -> tuple[BackendAssignment, ...]:
    return tuple(
        BackendAssignment(
            table=t,
            backend=_BACKEND_CYCLE[derive_seed("dbt-backend", family_id, t) % len(_BACKEND_CYCLE)],
        )
        for t in table_names
    )


class SkippedCut(BaseModel):
    """Something that did NOT reach a task, and why.

    Recorded rather than swallowed; `--strict` fails on any. ONE mechanism for
    all three granularities, so a per-mart drop cannot travel a quieter channel.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Sorted unique_ids of what was dropped.
    members: tuple[str, ...] = Field(min_length=1)
    #: Human-readable rejection reason (the strict path's ValueError text).
    reason: str = Field(min_length=1)
    #: 'keyless-mart' | 'stranded-source' | 'unusable-cut'. Typed so callers
    #: need not sniff `reason` text.
    kind: str = "unusable-cut"
    #: 'cut' | 'mart' | 'source' — the GRANULARITY of the drop.
    scope: str = CUT_SCOPE
    #: For 'mart'/'source' skips: the task the drop came OUT of.
    task_id: str = ""


class ExtractionResult(BaseModel):
    """Usable cuts plus every rejected one — the whole-package view."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tasks: tuple[TaskIR, ...] = ()
    skipped: tuple[SkippedCut, ...] = ()


def _projection_grain_problem(
    model: DbtNode,
    grounding: _Grounding,
    relationships: tuple[Relationship, ...],
) -> str:
    """Why a PROJECTION mart's declared grain cannot be made true, or ''.

    Minting can make any key column identify a base row except one: a column
    that is also the CHILD side of a declared relationship is drawn from the
    parent's pool WITH repetition, so it duplicates by construction and the
    mart is refused rather than keyed and hoped over.
    """
    fk_columns = {
        c
        for rel in relationships
        if rel.child_table == grounding.base_table
        for c in rel.child_columns
    }
    offenders = sorted(set(grounding.key_source_columns) & fk_columns)
    if not offenders:
        return ""
    return (
        f"mart model {model.unique_id!r} declares grain "
        f"({', '.join(grounding.key_columns)}), which grounds to "
        f"{', '.join(offenders)} of {grounding.base_table!r} — the CHILD side "
        "of a declared relationship, i.e. a foreign key drawn from the parent "
        "with repetition. A projection mart is one row per source row, so this "
        "grain duplicates by construction and cannot be minted unique; the "
        "mart is dropped rather than declaring a grain its data cannot honour"
    )


def _degenerate_rollup_problem(
    model: DbtNode,
    grounding: _Grounding,
    relationships: tuple[Relationship, ...],
    closure_tables: set[str],
) -> str:
    """Why this rollup would measure nothing, or '' — see `DegenerateMartError`.

    Predicts in PASS 1, where the mart can still be dropped alone, the same
    condition `_model_to_mart` raises on: no measure and nothing to count over.
    """
    if grounding.mode != "aggregate" or grounding.measures:
        return ""
    if _first_related(grounding.base_table, relationships, closure_tables) is not None:
        return ""
    return (
        f"mart model {model.unique_id!r} recovered no measure from its own SQL "
        f"and its cut declares no relationship to count over. Its grain is now "
        f"its whole GROUP BY, i.e. every column it projects "
        f"({', '.join(grounding.key_columns)} + "
        f"{len(grounding.select_map) - len(grounding.key_columns)} attribute(s)), "
        "so the mart is SELECT DISTINCT plus a COUNT(*) that is 1 on every row: "
        "it measures nothing and gates.info-content refuses it at stage 9. "
        "Dropped at ingest instead"
    )


#: `<table>."<src>" AS "<dst>"` — how every builder writes a plan projection.
_SELECT_BINDING = re.compile(r'(\w+)\."((?:[^"]|"")*)"\s+AS\s+"((?:[^"]|"")*)"')
#: A quoted identifier inside `details['group_by']`.
_QUOTED_IDENT = re.compile(r'"((?:[^"]|"")*)"')


def _grain_bindings(mart: MartSpec, table_names: set[str]) -> dict[str, tuple[str, str]]:
    """mart column -> (source table, source column), read off the PLAN itself."""
    out: dict[str, tuple[str, str]] = {}
    for op in mart.plan.ops:
        for table, src, dst in _SELECT_BINDING.findall(op.details.get("select", "")):
            if table in table_names:
                out.setdefault(dst.replace('""', '"'), (table, src.replace('""', '"')))
    return out


def _derived_grain_bindings(
    mart: MartSpec, table_names: set[str]
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Derived grain column -> every raw source column it depends on.

    Direct projections are handled by `_grain_bindings`.  This companion reads
    only a DERIVE whose input is one real source table, which is exactly how
    `build_rollup` emits a computed key such as ``date_day``.  Intermediate
    relations are ignored; treating one as a raw table would make the
    non-nullness proof circular.
    """
    out: dict[str, tuple[tuple[str, str], ...]] = {}
    for op in mart.plan.ops:
        if (
            op.kind is not MartOpKind.DERIVE
            or len(op.tables) != 1
            or op.tables[0] not in table_names
        ):
            continue
        source = op.tables[0]
        select = op.details.get("select", "").strip()
        if not select:
            continue
        try:
            parsed = sqlglot.parse_one(f"SELECT {select}", read="duckdb")
        except Exception:  # noqa: BLE001 — verifier fails on the missing binding
            continue
        if not isinstance(parsed, exp.Select):
            continue
        for projection in parsed.expressions:
            alias = projection.alias_or_name
            body = projection.this if isinstance(projection, exp.Alias) else projection
            if not alias or isinstance(body, exp.Column):
                continue
            refs = tuple(
                dict.fromkeys(
                    (source, column.name)
                    for column in body.find_all(exp.Column)
                    if column.name and (not column.table or column.table == source)
                )
            )
            if refs:
                out[alias] = refs
    return out


def _mechanical_grain(mart: MartSpec) -> tuple[str, ...] | None:
    """The GROUP BY the plan emits, or None when the mart is a projection."""
    for op in mart.plan.ops:
        if op.kind in (
            MartOpKind.AGGREGATE,
            MartOpKind.FILTERED_AGGREGATE,
            MartOpKind.DISTINCT,
        ):
            group_by = op.details.get("group_by", "").strip()
            if group_by:
                return tuple(
                    ident.replace('""', '"')
                    for ident in _QUOTED_IDENT.findall(group_by)
                )
    return None


@dataclass(frozen=True)
class _ExactJoinPredicate:
    """One structurally exact builder JOIN before relationship orientation."""

    op_index: int
    join_type: JoinType
    left_table: str
    left_columns: tuple[str, ...]
    left_aliases: tuple[str, ...]
    right_table: str
    right_columns: tuple[str, ...]
    carried_aliases: tuple[str, ...]


@dataclass(frozen=True)
class _LookupJoinProof:
    """One emitted JOIN proved to be an exact relationship-parent lookup."""

    op_index: int
    join_type: JoinType
    required: bool
    child_table: str
    child_columns: tuple[str, ...]
    left_aliases: tuple[str, ...]
    parent_table: str
    parent_columns: tuple[str, ...]
    carried_aliases: tuple[str, ...]


def _and_conjuncts(node: exp.Expression) -> tuple[exp.Expression, ...]:
    """Flatten an AND tree while preserving predicate order."""
    if isinstance(node, exp.Paren):
        return _and_conjuncts(node.this)
    if isinstance(node, exp.And):
        return _and_conjuncts(node.this) + _and_conjuncts(node.expression)
    return (node,)


def _exact_join_predicates(
    mart: MartSpec,
    table_names: set[str],
) -> tuple[_ExactJoinPredicate, ...]:
    """Parse only exact builder-representable JOIN predicate structures."""
    # Bindings are operation-order-sensitive: a left alias must have been
    # projected before this JOIN. A right-table projection made by the JOIN
    # itself cannot retroactively prove the provenance of its own predicate.
    bindings: dict[str, tuple[str, str]] = {}
    exact: list[_ExactJoinPredicate] = []
    for op_index, op in enumerate(mart.plan.ops):
        prior_bindings = dict(bindings)
        for table, source, alias in _SELECT_BINDING.findall(
            op.details.get("select", "")
        ):
            if table in table_names:
                bindings.setdefault(
                    alias.replace('""', '"'),
                    (table, source.replace('""', '"')),
                )
        if (
            op.kind is not MartOpKind.JOIN
            or op.join_type not in {JoinType.LEFT, JoinType.INNER}
            or len(op.tables) != 2
        ):
            continue
        left_relation, right_table = op.tables
        if right_table not in table_names or left_relation == right_table:
            continue
        predicate = _parse_sql_expression(op.predicate)
        if predicate is None:
            continue

        pairs: list[tuple[str, str]] = []  # (left alias, right column)
        valid = True
        for term in _and_conjuncts(predicate):
            if isinstance(term, exp.Paren):
                term = term.this
            if not isinstance(term, exp.EQ):
                valid = False
                break
            lhs, rhs = term.this, term.expression
            if not isinstance(lhs, exp.Column) or not isinstance(rhs, exp.Column):
                valid = False
                break
            if lhs.db or lhs.catalog or rhs.db or rhs.catalog:
                valid = False
                break
            if lhs.table == right_table and rhs.table == left_relation:
                pairs.append((rhs.name, lhs.name))
            elif rhs.table == right_table and lhs.table == left_relation:
                pairs.append((lhs.name, rhs.name))
            else:
                valid = False
                break
        left_aliases = tuple(left for left, _right in pairs)
        right_columns = tuple(right for _left, right in pairs)
        if (
            not valid
            or not pairs
            or len(set(left_aliases)) != len(left_aliases)
            or len(set(right_columns)) != len(right_columns)
        ):
            continue

        bound_pairs: list[tuple[str, str]] = []
        left_tables: set[str] = set()
        for left_alias, right_column in pairs:
            binding = prior_bindings.get(left_alias)
            if binding is None:
                valid = False
                break
            left_table, left_column = binding
            left_tables.add(left_table)
            bound_pairs.append((left_column, right_column))
        if not valid or len(left_tables) != 1:
            continue
        carried_aliases = tuple(
            dict.fromkeys(
                alias.replace('""', '"')
                for table, _source, alias in _SELECT_BINDING.findall(
                    op.details.get("select", "")
                )
                if table == right_table
            )
        )
        exact.append(
            _ExactJoinPredicate(
                op_index=op_index,
                join_type=op.join_type,
                left_table=next(iter(left_tables)),
                left_columns=tuple(left for left, _right in bound_pairs),
                left_aliases=left_aliases,
                right_table=right_table,
                right_columns=right_columns,
                carried_aliases=carried_aliases,
            )
        )
    return tuple(exact)


def _same_relationship_pairs(
    actual: tuple[tuple[str, str], ...],
    expected_left: tuple[str, ...],
    expected_right: tuple[str, ...],
) -> bool:
    """Whether ``actual`` is one-to-one and exactly the declared mapping."""
    expected = tuple(zip(expected_left, expected_right, strict=True))
    return (
        len(actual) == len(expected)
        and len(set(expected)) == len(expected)
        and set(actual) == set(expected)
    )


def _lookup_join_proofs(
    mart: MartSpec,
    relationships: tuple[Relationship, ...],
    table_names: set[str],
    exact_joins: tuple[_ExactJoinPredicate, ...] | None = None,
) -> tuple[_LookupJoinProof, ...]:
    """Prove builder-representable lookup joins in ``mart``.

    Accept only two-relation LEFT/INNER joins whose predicate is an AND of
    bijective column equalities matching one declared child-to-parent
    relationship. Reject other join types, expressions, constants, residuals,
    duplicate columns, and unqualified columns as dependency evidence. Parent
    uniqueness and grain closure consume the same proof records.
    """
    proofs: list[_LookupJoinProof] = []
    joins = (
        exact_joins
        if exact_joins is not None
        else _exact_join_predicates(mart, table_names)
    )
    for join in joins:
        relationship = next(
            (
                relation
                for relation in relationships
                if relation.child_table == join.left_table
                and relation.parent_table == join.right_table
                and _same_relationship_pairs(
                    tuple(zip(join.left_columns, join.right_columns, strict=True)),
                    relation.child_columns,
                    relation.parent_columns,
                )
            ),
            None,
        )
        if relationship is None:
            continue
        proofs.append(
            _LookupJoinProof(
                op_index=join.op_index,
                join_type=join.join_type,
                required=relationship.required,
                child_table=join.left_table,
                child_columns=tuple(relationship.child_columns),
                left_aliases=join.left_aliases,
                parent_table=join.right_table,
                parent_columns=tuple(relationship.parent_columns),
                carried_aliases=join.carried_aliases,
            )
        )
    return tuple(proofs)


def _counted_child_join_indexes(
    exact_joins: tuple[_ExactJoinPredicate, ...],
    relationships: tuple[Relationship, ...],
) -> set[int]:
    """Exact parent->child fan-out joins used by the counted-join fallback."""
    return {
        join.op_index
        for join in exact_joins
        if any(
            relationship.parent_table == join.left_table
            and relationship.child_table == join.right_table
            and _same_relationship_pairs(
                tuple(zip(join.left_columns, join.right_columns, strict=True)),
                relationship.parent_columns,
                relationship.child_columns,
            )
            for relationship in relationships
        )
    }


def _lookup_hops(
    mart: MartSpec,
    relationships: tuple[Relationship, ...],
    table_names: set[str],
) -> list[tuple[str, tuple[str, ...]]]:
    """The JOIN ops of `mart` that are LOOKUPS: the right table is the PARENT
    side of a declared relationship on exactly the joined columns. A join onto
    a CHILD side (the counted-join fallback's fan-out) is not one."""
    return [
        (proof.parent_table, proof.parent_columns)
        for proof in _lookup_join_proofs(mart, relationships, table_names)
    ]


def _lookup_parent_is_unique(
    proof: _LookupJoinProof, tables: dict[str, TableSpec]
) -> bool:
    """Whether the proven lookup parent is minted unique on its exact key."""
    parent = tables[proof.parent_table]
    joined = set(proof.parent_columns)
    # A PK forbids physical duplicate rows. A business key only forbids two
    # *distinct* rows with the same key in this package's population contract;
    # PK-less stress data may intentionally contain a byte-identical duplicate.
    # Such a duplicate still fans a SQL lookup out, so BK proves lookup
    # uniqueness only when some PK also prevents physical duplicates.
    return bool(
        parent.primary_key
        and (
            set(parent.primary_key) <= joined
            or (
                parent.business_key
                and set(parent.business_key) <= joined
            )
        )
    )


def _lookup_determined_group_columns(
    mart: MartSpec,
    relationships: tuple[Relationship, ...],
    tables: dict[str, TableSpec],
    declared: tuple[str, ...],
    proofs: tuple[_LookupJoinProof, ...] | None = None,
) -> set[str]:
    """Return group columns functionally determined by the logical grain.

    Walk joins from the declared grain. When every left key is determined and
    the right side is a declared relationship parent, include its carried
    attributes. Use the same exact lookup and parent-uniqueness proofs as
    ``verify_grain``; malformed or approximate joins confer no dependency.
    """
    table_names = set(tables)
    lookup_proofs = (
        proofs
        if proofs is not None
        else _lookup_join_proofs(mart, relationships, table_names)
    )
    determined = set(declared)
    bindings = _grain_bindings(mart, table_names)

    def expand_source_identities() -> None:
        """Close aliases projected from any already-determined source row."""
        changed = True
        while changed:
            changed = False
            for table_name, table in tables.items():
                determined_sources = {
                    source
                    for alias in determined
                    for bound_table, source in (bindings.get(alias, ("", "")),)
                    if bound_table == table_name
                }
                identities = tuple(
                    identity
                    for identity in (table.primary_key, table.business_key)
                    if identity
                )
                if not any(
                    set(identity) <= determined_sources for identity in identities
                ):
                    continue
                aliases = {
                    alias
                    for alias, (bound_table, _source) in bindings.items()
                    if bound_table == table_name
                }
                if not aliases <= determined:
                    determined.update(aliases)
                    changed = True

    expand_source_identities()
    for proof in lookup_proofs:
        if (
            set(proof.left_aliases) <= determined
            and _lookup_parent_is_unique(proof, tables)
        ):
            determined.update(proof.carried_aliases)
            expand_source_identities()
    return determined


def verify_grain(task: TaskIR) -> None:
    """Reject cuts whose plan or data cannot honor the declared mart grain.

    Re-derive checks from the emitted plan. Aggregate keys must fit GROUP BY
    plus proven lookup dependencies; projection keys must contain a complete
    minted identity; grain columns must ground to non-null sources; and lookup
    parents must be unique on join keys. Counted child-side joins are exempt.
    Any violation rejects the whole cut.
    """
    table_names = {t.name for t in task.tables}
    by_name = {t.name: t for t in task.tables}
    for mart in task.marts:
        exact_joins = _exact_join_predicates(mart, table_names)
        lookup_proofs = _lookup_join_proofs(
            mart,
            task.relationships,
            table_names,
            exact_joins=exact_joins,
        )
        legitimate_join_indexes = {
            proof.op_index for proof in lookup_proofs
        } | _counted_child_join_indexes(exact_joins, task.relationships)
        source_right_join_indexes = {
            index
            for index, op in enumerate(mart.plan.ops)
            if op.kind is MartOpKind.JOIN
            and op.tables
            and op.tables[-1] in table_names
        }
        malformed_join_indexes = sorted(
            source_right_join_indexes - legitimate_join_indexes
        )
        if malformed_join_indexes:
            raise DeclaredGrainViolation(
                f"mart {mart.name!r} of task {task.task_id!r} has source-side "
                f"JOIN op(s) {malformed_join_indexes} that are neither an exact "
                "LEFT/INNER equality lookup matching a declared relationship "
                "nor an exact relationship child-side counted join; they cannot "
                "confer functional dependency or bypass lookup fan-out checking"
            )
        for proof in lookup_proofs:
            if not _lookup_parent_is_unique(proof, by_name):
                parent = by_name[proof.parent_table]
                raise DeclaredGrainViolation(
                    f"mart {mart.name!r} of task {task.task_id!r} joins "
                    f"{proof.parent_table} as a lookup on "
                    f"({', '.join(proof.parent_columns)}), but "
                    f"{proof.parent_table} is not minted unique on that key "
                    f"(primary key "
                    f"{list(parent.primary_key)}, business key "
                    f"{list(parent.business_key)}): the join could fan the base "
                    "out and the mart's 'exactly once' claim would be false"
                )
        declared = tuple(mart.key_columns)
        for proof in lookup_proofs:
            null_extended_keys = sorted(
                set(declared) & set(proof.carried_aliases)
            )
            if (
                null_extended_keys
                and proof.join_type is JoinType.LEFT
                and not proof.required
            ):
                raise DeclaredGrainViolation(
                    f"mart {mart.name!r} of task {task.task_id!r} declares "
                    f"hop-carried {null_extended_keys} as logical grain keys, "
                    f"but its optional LEFT lookup to {proof.parent_table} can "
                    "NULL-extend them on an unmatched row even when the parent "
                    "source columns are NOT NULL"
                )
        bindings = _grain_bindings(mart, table_names)
        derived_bindings = _derived_grain_bindings(mart, table_names)
        group_by = _mechanical_grain(mart)
        if group_by is not None:
            mechanical = set(group_by)
            logical = set(declared)
            determined = _lookup_determined_group_columns(
                mart,
                task.relationships,
                by_name,
                declared,
                proofs=lookup_proofs,
            )
            if not logical <= mechanical or not (mechanical - logical) <= determined:
                raise DeclaredGrainViolation(
                    f"mart {mart.name!r} of task {task.task_id!r} declares grain "
                    f"{list(declared)} but its plan groups by {list(group_by)}: "
                    "every extra grouping column must be provably determined by "
                    "the declared grain through a unique relationship-parent "
                    "lookup, or gold could carry duplicate keys"
                )
        else:
            unresolved = sorted(column for column in declared if column not in bindings)
            if unresolved:
                raise DeclaredGrainViolation(
                    f"mart {mart.name!r} of task {task.task_id!r} is a projection "
                    f"whose declared grain {list(declared)} has no direct source "
                    f"binding for {unresolved}"
                )
            base_tables = {bindings[k][0] for k in declared}
            if len(base_tables) != 1:
                raise DeclaredGrainViolation(
                    f"mart {mart.name!r} of task {task.task_id!r} is a projection "
                    f"whose declared grain {list(declared)} does not resolve to "
                    f"exactly one source table (resolved: {sorted(base_tables)})"
                )
            base = by_name[next(iter(base_tables))]
            declared_sources = {bindings[column][1] for column in declared}
            identities = tuple(
                identity
                for identity in (base.primary_key, base.business_key)
                if identity
            )
            # A business key constrains distinct generated rows, but this
            # package deliberately injects byte-identical duplicates for a
            # PK-less table in stress data.  A projection preserves those
            # physical duplicates, so it needs some PK plus a complete PK/BK
            # tuple in the declared output grain (same rule as lookup fanout).
            physically_unique = bool(
                base.primary_key
                and any(
                    set(identity) <= declared_sources for identity in identities
                )
            )
            if not physically_unique:
                raise DeclaredGrainViolation(
                    f"mart {mart.name!r} of task {task.task_id!r} declares grain "
                    f"{list(declared)}, grounded to "
                    f"{sorted(declared_sources)} of {base.name!r}, which does "
                    "not contain a complete identity protected from physical "
                    "duplicate rows by the generator "
                    f"(primary key {list(base.primary_key)}, business key "
                    f"{list(base.business_key)}). One row per source row is not "
                    "one row per that key"
                )
        for column in declared:
            sources = (
                (bindings[column],)
                if column in bindings
                else derived_bindings.get(column, ())
            )
            if not sources:
                raise DeclaredGrainViolation(
                    f"mart {mart.name!r} of task {task.task_id!r}: grain column "
                    f"{column!r} is not traceable to any source column in its "
                    "own plan, so nothing can guarantee it is non-empty"
                )
            for table, source in sources:
                if by_name[table].column(source).nullable:
                    raise DeclaredGrainViolation(
                        f"mart {mart.name!r} of task {task.task_id!r}: grain column "
                        f"{column!r} grounds to {table}.{source}, which is NULLABLE. "
                        "A NULL grain key is not an identity (and becomes '' through "
                        "a CSV backend), so the source column must be declared NOT "
                        "NULL or the mart must not claim that grain"
                    )


def extract_candidates(spec: CandidateSpec, *, pool: str = "dbt") -> ExtractionResult:
    """Extract usable connected subgraphs and report skipped cuts.

    Each cut retains full source and dependency closure. Skip invalid
    components, including those without terminal models, enough sources, valid
    columns, multi-table marts, or any keyed mart. A keyless mart may be dropped
    whole if the remaining project stays nonempty and connected; prune and
    report sources used only by dropped marts. Other mart failures reject the
    cut. Record every drop as ``SkippedCut``.
    """
    by_id = spec.node_by_id()
    consumed: set[str] = set()
    for m in spec.models:
        for dep in m.depends_on:
            if dep in by_id and by_id[dep].resource_type == "model":
                consumed.add(dep)
    terminal_models = {m.unique_id for m in spec.models if m.unique_id not in consumed}

    family_id = f"{pool}__{_slug(spec.package_name)}"
    aliases = staging_alias_map(spec)
    expressions = staging_expression_map(spec)
    staging_types = staging_type_map(spec)
    tasks: list[TaskIR] = []
    skipped: list[SkippedCut] = []
    for comp in _connected_components(spec):
        comp_set = set(comp)
        if not any(by_id[uid].resource_type == "model" for uid in comp):
            continue  # orphan sources: not a candidate cut
        comp_sources = sorted(
            (uid for uid in comp if by_id[uid].resource_type == "source"),
            key=lambda uid: by_id[uid].name,
        )
        comp_marts = sorted(
            (uid for uid in comp if uid in terminal_models),
            key=lambda uid: by_id[uid].name,
        )
        # TaskIR table names must be unique and every recovery pass speaks
        # names, so two same-named sources in one component are refused rather
        # than silently merged.
        same_named = [
            (a, b)
            for a, b in zip(comp_sources, comp_sources[1:], strict=False)
            if by_id[a].name == by_id[b].name
        ]
        if same_named:
            a, b = same_named[0]
            skipped.append(
                SkippedCut(
                    members=tuple(comp),
                    reason=(
                        f"same-named sources {a}, {b}: TaskIR table names must "
                        "be unique"
                    ),
                )
            )
            continue
        source_columns = {
            by_id[uid].name: {c.name for c in by_id[uid].columns} for uid in comp_sources
        }
        if not comp_marts:
            skipped.append(
                SkippedCut(
                    members=tuple(comp),
                    reason=f"trivial cut {sorted(comp_set)}: no mart (terminal) models",
                )
            )
            continue
        if len(comp_sources) < 2:
            skipped.append(
                SkippedCut(
                    members=tuple(comp),
                    reason=(
                        f"trivial cut {sorted(comp_set)}: fewer than 2 source "
                        "tables (no join surface)"
                    ),
                )
            )
            continue

        dropped_marts: list[tuple[str, KeylessMartError]] = []
        stranded: list[str] = []
        try:
            # Pass 1 — per-mart admission. Keylessness DROPS the mart; anything
            # else kills the cut.
            kept_marts: list[str] = []
            keys: dict[str, tuple[tuple[str, ...], str]] = {}
            groundings: dict[str, _Grounding] = {}
            # Pass 2's relationship set (over SURVIVING sources) is not known
            # yet. The component's own set is safe here: a table a mart grounds
            # a column in is in its closure, so it can never be pruned.
            comp_relationships = _component_relationships(
                spec, {uid for uid in comp_sources}
            )
            for mart_uid in comp_marts:
                closure = _source_closure(mart_uid, spec)
                if len(closure) < 2:
                    raise ValueError(
                        f"trivial mart {mart_uid!r}: source closure has "
                        f"{len(closure)} table(s); a task mart must join at least 2"
                    )
                _require_mart_columns(by_id[mart_uid])
                try:
                    keys[mart_uid] = mart_key_columns(spec, by_id[mart_uid])
                    # A mart grounding in no source column cannot be executed
                    # from this cut: same admission decision as keylessness.
                    groundings[mart_uid] = ground_mart(
                        by_id[mart_uid],
                        keys[mart_uid][0],
                        sorted(by_id[uid].name for uid in closure),
                        source_columns,
                        aliases,
                        comp_relationships,
                        expressions,
                        resolver=_source_resolver(spec, by_id[mart_uid]),
                    )
                    # Grain honesty, admission half: a projection keyed on a
                    # foreign key can never be minted unique, so it is dropped
                    # in pass 1, where a per-mart drop is still legal.
                    if groundings[mart_uid].mode == "projection":
                        problem = _projection_grain_problem(
                            by_id[mart_uid], groundings[mart_uid], comp_relationships
                        )
                        if problem:
                            raise UnprovableGrainError(problem)
                    else:
                        problem = _degenerate_rollup_problem(
                            by_id[mart_uid],
                            groundings[mart_uid],
                            comp_relationships,
                            {by_id[uid].name for uid in closure},
                        )
                        if problem:
                            raise DegenerateMartError(problem)
                except KeylessMartError as exc:
                    dropped_marts.append((mart_uid, exc))
                    continue
                kept_marts.append(mart_uid)
            if not kept_marts:
                # No gradeable mart left: refuse the cut with the first reason.
                raise dropped_marts[0][1]

            # Is what remains still ONE COMPLETE project?
            ancestors = {uid: _ancestors(uid, spec) for uid in kept_marts}
            kept_members: set[str] = set().union(*ancestors.values())
            kept_sources = sorted(
                (uid for uid in comp_sources if uid in kept_members),
                key=lambda uid: by_id[uid].name,
            )
            stranded = [uid for uid in comp_sources if uid not in kept_members]
            if len(kept_sources) < 2:
                # Standing guard: every kept mart passed closure>=2, so this
                # can only fire if that invariant moves. Fail closed rather
                # than emit a join-free cut.
                raise ValueError(
                    f"cut {sorted(comp_set)}: after dropping "
                    f"{len(dropped_marts)} keyless mart(s) fewer than 2 source "
                    "tables remain (no join surface)"
                )
            if dropped_marts and not _marts_stay_connected(ancestors):
                raise KeylessMartError(
                    f"cut {sorted(comp_set)}: dropping keyless mart(s) "
                    f"{sorted(uid for uid, _ in dropped_marts)} disconnects the "
                    f"surviving marts {kept_marts}; the remainder is more than "
                    "one data project, so the cut is refused rather than "
                    "silently redefined"
                )

            # Pass 2 — build the surviving cut over the pruned source set.
            relationships = _component_relationships(spec, set(kept_sources))

            # Types the surviving marts' RECOVERED SQL demands, resolved ONCE
            # per cut: two marts reading one column must see the same type, and
            # a measure disagreeing with the resolution is dropped.
            resolved_types: dict[str, dict[str, ColumnType]] = {}
            for mart_uid in kept_marts:
                for table, column, want in groundings[mart_uid].type_requirements:
                    have = resolved_types.setdefault(table, {}).get(column)
                    if have is None or _TYPE_STRENGTH.index(want) < _TYPE_STRENGTH.index(
                        have
                    ):
                        resolved_types[table][column] = want

            # The VALUES those marts select on, resolved ONCE per cut too: two
            # marts filtering one column must see one domain.
            selected_values: dict[str, dict[str, set[str]]] = {}
            for mart_uid in kept_marts:
                for table, column, literal in groundings[mart_uid].domain_requirements:
                    selected_values.setdefault(table, {}).setdefault(column, set()).add(
                        literal
                    )
            resolved_domains = {
                table: {
                    column: _adopted_domain(column, values)
                    for column, values in sorted(columns.items())
                }
                for table, columns in sorted(selected_values.items())
            }

            # The type EVERY source column ships with, computed once so the
            # mart builder's type check and `_source_to_table` cannot disagree.
            adopted_types = {
                by_id[uid].name: _column_types(
                    by_id[uid],
                    staging_types.get(by_id[uid].name),
                    resolved_types.get(by_id[uid].name),
                )
                for uid in kept_sources
            }
            kept_table_names = set(adopted_types)

            marts: list[MartSpec] = []
            shapes: list[StarShape] = []
            #: source table -> columns a surviving mart uses AS ITS GRAIN; the
            #: generator must mint them uniquely or the grain is false.
            grounded_keys: dict[str, list[str]] = {}
            #: source table -> grain columns declared NOT NULL: a NULL grain key
            #: is not an identity, and a CSV backend turns it into `''`.
            grain_not_null: dict[str, list[str]] = {}
            #: source table -> columns a LOOKUP JOIN matches it on, minted
            #: unique so the hop is the many-to-one it claims.
            lookup_keys: dict[str, set[str]] = {}
            for mart_uid in kept_marts:
                closure_tables = sorted(
                    by_id[uid].name for uid in _source_closure(mart_uid, spec)
                )
                grounding = groundings[mart_uid]
                mart, shape = _model_to_mart(
                    spec,
                    by_id[mart_uid],
                    closure_tables,
                    relationships,
                    grounding,
                    keys[mart_uid][1],
                    source_columns,
                    aliases,
                    adopted_types,
                )
                marts.append(mart)
                shapes.append(shape)
                for hop_table, hop_columns in _lookup_hops(
                    mart, relationships, kept_table_names
                ):
                    lookup_keys.setdefault(hop_table, set()).update(hop_columns)
                if grounding.mode == "projection":
                    grounded_keys.setdefault(grounding.base_table, []).extend(
                        grounding.key_source_columns
                    )
                    grain_not_null.setdefault(grounding.base_table, []).extend(
                        grounding.key_source_columns
                    )
                else:
                    # A rollup's grain is its whole GROUP BY. None needs to be
                    # UNIQUE (the GROUP BY does that) but every one must be
                    # non-empty, or the mart is keyed on nothing.
                    grain_not_null.setdefault(grounding.base_table, []).extend(
                        src for src, _ in grounding.select_map
                    )
                    grain_not_null.setdefault(grounding.base_table, []).extend(
                        ref
                        for projection in grounding.derived
                        for ref in projection.refs
                    )
                    # Most hop-carried attributes are FD output columns and may
                    # legitimately be NULL on an unmatched optional lookup.
                    # A hop attribute whose left key was not determined is
                    # retained in the logical grain; only those retained keys
                    # need their own source value minted non-null.
                    declared = set(mart.key_columns)
                    for table, source, alias in grounding.joined_columns:
                        if alias in declared:
                            grain_not_null.setdefault(table, []).append(source)

            tables = tuple(
                _source_to_table(
                    spec,
                    by_id[uid],
                    grounded_keys=tuple(
                        sorted(set(grounded_keys.get(by_id[uid].name, ())))
                    ),
                    grain_not_null=tuple(
                        sorted(set(grain_not_null.get(by_id[uid].name, ())))
                    ),
                    type_overrides=resolved_types.get(by_id[uid].name),
                    domain_overrides=resolved_domains.get(by_id[uid].name),
                    staging_types=staging_types.get(by_id[uid].name),
                    lookup_keys=tuple(
                        sorted(lookup_keys.get(by_id[uid].name, ()))
                    ),
                    relationships=relationships,
                )
                for uid in kept_sources
            )
            table_names = [t.name for t in tables]
        except ValueError as exc:
            # Unusable cut, not a crash: record WHY and keep going. The strict
            # entry point re-raises this same text (and the same TYPE).
            skipped.append(
                SkippedCut(
                    members=tuple(comp),
                    reason=str(exc),
                    kind=(
                        KEYLESS_MART_SKIP
                        if isinstance(exc, KeylessMartError)
                        else UNUSABLE_CUT_SKIP
                    ),
                )
            )
            continue

        # The id hashes the SURVIVING MEMBERSHIP, never content: a re-ingest
        # after an adapter fix keeps the id and lands as a new revision, while
        # a newly refused mart makes it a different task in the same family.
        comp_hash = sha256_hex(canonical_json(sorted(kept_members)))[:8]
        first_mart = _slug(by_id[kept_marts[0]].name)
        task_id = f"{family_id}__{first_mart}_{comp_hash}"

        # Reassigned BEFORE the catalogue reads the assignments: a table with
        # no NOT NULL column cannot ride FILES or REST.
        backends = reassign_unsafe_file_tables(_assign_backends(family_id, table_names), tables)
        populations, attacks = derive_populations_and_attacks(
            task_id=task_id,
            tables=tables,
            relationships=relationships,
            shapes=tuple(shapes),
            policy_conditions=(
                "SYNTHETIC data: a dbt package ships models and tests, not "
                "production rows, so every row is generated from the declared "
                "source schema.",
            ),
            backends=len({b.backend for b in backends}),
            # Load-side cases need the ASSIGNMENTS, not just the count: they
            # exist only for the backends the tables actually sit on.
            backend_assignments=backends,
        )
        candidate = (
            # attacks.py mutates TaskIR.reference and has nothing to mutate
            # without it.
            attach_reference(TaskIR(
                task_id=task_id,
                family_id=family_id,
                cluster_id=family_id,
                origin=Origin.DBT,
                license=spec.license,
                attribution=f"dbt package {spec.package_name}",
                title=by_id[kept_marts[0]].name.replace("_", " ").strip().capitalize(),
                tables=tables,
                relationships=relationships,
                backends=backends,
                marts=tuple(marts),
                populations=populations,
                attack_cases=attacks,
            ))
        )
        # STANDING GUARD, deliberately NOT a SkippedCut: this cut passed every
        # admission rule, so a grain violation now is a BUILDER defect and
        # fails the whole ingest.
        verify_grain(candidate)
        tasks.append(candidate)
        # The evidence trail for what this surviving cut LEFT BEHIND.
        for mart_uid, exc in dropped_marts:
            skipped.append(
                SkippedCut(
                    members=(mart_uid,),
                    reason=(
                        f"mart {mart_uid!r} dropped from surviving cut "
                        f"{task_id} (kept marts: "
                        f"{', '.join(by_id[uid].name for uid in kept_marts)}): {exc}"
                    ),
                    kind=KEYLESS_MART_SKIP,
                    scope=MART_SCOPE,
                    task_id=task_id,
                )
            )
        if stranded:
            skipped.append(
                SkippedCut(
                    members=tuple(sorted(stranded)),
                    reason=(
                        f"source table(s) "
                        f"{', '.join(sorted(by_id[uid].name for uid in stranded))} "
                        f"pruned from cut {task_id}: no surviving mart reads "
                        f"them after {len(dropped_marts)} keyless mart(s) were "
                        "dropped (a project shipping unread sources is a "
                        "different, easier task)"
                    ),
                    kind=STRANDED_SOURCE_SKIP,
                    scope=SOURCE_SCOPE,
                    task_id=task_id,
                )
            )
    return ExtractionResult(tasks=tuple(tasks), skipped=tuple(skipped))


def extract_tasks(spec: CandidateSpec, *, pool: str = "dbt") -> list[TaskIR]:
    """Extract one TaskIR per connected subgraph — STRICT (pinned contract).

    Identical extraction to `extract_candidates`, stricter policy: ANY skip
    raises with that skip's reason — a rejected cut, and equally a mart dropped
    from a surviving cut. A hand-curated manifest that loses a mart is a
    manifest the caller did not understand.
    """
    result = extract_candidates(spec, pool=pool)
    if result.skipped:
        first = result.skipped[0]
        if first.kind == KEYLESS_MART_SKIP:
            raise KeylessMartError(first.reason)
        raise ValueError(first.reason)
    return list(result.tasks)

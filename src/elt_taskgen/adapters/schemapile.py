"""Convert permissively licensed SchemaPile DDL records into ``TaskIR``.

Family identity uses the index cluster. Populations are deterministic and
synthetic because records contain no rows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from elt_taskgen.catalog import SourceCatalog, load_source_catalog
from elt_taskgen.adapters.evidence import (
    build_marts,
    chain_candidates,
    declared_key_parents,
)
from elt_taskgen.generation.mart_plan import (
    Measure,
    StarJoin,
    StarShape,
    build_star,
    quote,
)
from elt_taskgen.generation.difficulty_profiles import (
    GenerationDifficultyProfile,
    STANDARD_DIFFICULTY_PROFILE,
)
from elt_taskgen.generation.populations import (
    derive_populations_and_attacks,
    schema_scale_hint,
)
from elt_taskgen.reference.solution import attach_reference
from elt_taskgen.adapters import reassign_unsafe_file_tables
from elt_taskgen.models import (
    AttackCase,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartColumn,
    MartSpec,
    Origin,
    PoolSelection,
    PopulationSpec,
    Relationship,
    TableSpec,
    TaskIR,
    canonical_json,
    derive_seed,
    sha256_hex,
    slugify_family,
)

#: Pool key in config/sources.yaml (also the family-id namespace).
POOL = "schemapile"

#: The vendored corpus file, relative to the pool root.
SOURCE_FILENAME = "schemapile-perm.json"

#: Stamped on every PopulationSpec: SchemaPile ships DDL, not data.
SYNTHETIC_POPULATION_POLICY = (
    "SYNTHETIC population: SchemaPile supplies the schema (DDL) only, so every "
    "row is generated from the declared types, nullability, keys and foreign "
    "keys; no scraped sample values are used."
)

#: Deterministic backend rotation domain (order participates in content hashes).
_BACKEND_CYCLE: tuple[Backend, ...] = (
    Backend.POSTGRES,
    Backend.MONGODB,
    Backend.REST,
    Backend.S3,
    Backend.FILES,
)

_NUMERIC_TYPES = frozenset(
    {ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.FLOAT, ColumnType.DECIMAL}
)


class SchemaPileFormatError(ValueError):
    """The source file is not the expected top-level JSON object of records."""


class NonPermissiveRecordError(ValueError):
    """A record that is not PERMISSIVE (or carries no license) was offered."""


class RelationalFilterError(ValueError):
    """A record that does not clear the relational floor was offered."""


# Streaming reader: the corpus is ONE 327 MB JSON object keyed by filename.

#: Read granularity: 1 MiB holds peak RSS at ~45 MB, versus GBs for json.load.
_CHUNK = 1 << 20

#: Cursor offset past which the consumed prefix is dropped (buffer stays O(chunk)).
_COMPACT_AT = 1 << 20

_WS = " \t\r\n"


class _Incomplete(Exception):
    """Internal: the buffer ends mid-pair; read another chunk and retry."""


def _decode_pair(buf: str, pos: int, decoder: json.JSONDecoder):
    """Decode one top-level pair, retrying the full pair when incomplete."""
    n = len(buf)
    i = pos
    while i < n and buf[i] in _WS:
        i += 1
    if i >= n:
        raise _Incomplete
    if buf[i] == ",":
        i += 1
        while i < n and buf[i] in _WS:
            i += 1
        if i >= n:
            raise _Incomplete
    if buf[i] == "}":
        return None, None, i + 1
    if buf[i] != '"':
        raise SchemaPileFormatError(
            f"expected a quoted record key at offset {i}, found {buf[i]!r}"
        )
    try:
        key, end = decoder.raw_decode(buf, i)
    except json.JSONDecodeError:
        raise _Incomplete from None
    i = end
    while i < n and buf[i] in _WS:
        i += 1
    if i >= n:
        raise _Incomplete
    if buf[i] != ":":
        raise SchemaPileFormatError(f"expected ':' after key {key!r}")
    i += 1
    while i < n and buf[i] in _WS:
        i += 1
    if i >= n:
        raise _Incomplete
    try:
        value, end = decoder.raw_decode(buf, i)
    except json.JSONDecodeError:
        raise _Incomplete from None
    if not isinstance(value, dict):
        raise SchemaPileFormatError(f"record {key!r} is not a JSON object")
    return key, value, end


def stream_records(
    path: Path | str, *, chunk_size: int = _CHUNK
) -> Iterator[tuple[str, dict]]:
    """Stream records from the large top-level JSON object; reject truncation."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"SchemaPile corpus not found: {p}")
    decoder = json.JSONDecoder()
    with p.open(encoding="utf-8") as fh:
        buf = fh.read(chunk_size)
        pos = 0
        while True:
            while pos < len(buf) and buf[pos] in _WS:
                pos += 1
            if pos < len(buf):
                break
            more = fh.read(chunk_size)
            if not more:
                raise SchemaPileFormatError(f"{p}: empty file, expected a JSON object")
            buf += more
        if buf[pos] != "{":
            raise SchemaPileFormatError(f"{p}: expected a top-level JSON object")
        pos += 1
        while True:
            try:
                key, value, next_pos = _decode_pair(buf, pos, decoder)
            except _Incomplete:
                more = fh.read(chunk_size)
                if not more:
                    raise SchemaPileFormatError(
                        f"{p}: truncated JSON object (fail closed)"
                    ) from None
                buf = buf[pos:] + more
                pos = 0
                continue
            if key is None:
                return
            pos = next_pos
            yield key, value
            if pos > _COMPACT_AT:
                buf = buf[pos:]
                pos = 0


def find_record(path: Path | str, key: str) -> dict:
    """Return ONE record by key, streaming (a full scan costs ~1.5 s)."""
    for candidate, record in stream_records(path):
        if candidate == key:
            return record
    raise KeyError(f"SchemaPile record {key!r} not found in {path}")


# Normalization: identifiers, types, origin repository

def normalize_identifier(name: str) -> str:
    """Return a deterministic, nonempty lowercase SQL identifier.

    Qualifiers are retained in the slug, and execution boundaries quote
    reserved words.
    """
    slug = re.sub(r"[^a-z0-9_]+", "_", str(name).strip().lower()).strip("_")
    if not slug:
        return "x" + sha256_hex(str(name))[:8]
    if slug[0].isdigit():
        slug = "t_" + slug
    return slug


#: Ordered raw-type substring rules; first match wins and unmatched types are
#: TEXT. Longer overlapping tokens must precede shorter ones.
_TYPE_RULES: tuple[tuple[str, ColumnType], ...] = (
    # Longer spellings that CONTAIN a shorter rule's token; must precede it.
    ("enum", ColumnType.TEXT),        # 'enum' ⊃ 'num'
    ("interval", ColumnType.TEXT),    # ⊃ 'int'
    ("point", ColumnType.TEXT),       # 'point'/'multipoint' ⊃ 'int'
    ("plugintype", ColumnType.TEXT),  # user-defined *PluginType ⊃ 'int'
    ("timedelta", ColumnType.TEXT),   # ⊃ 'time'
    ("timestamp", ColumnType.TIMESTAMP),
    ("datetime", ColumnType.TIMESTAMP),
    ("bigserial", ColumnType.BIGINT),
    ("serial", ColumnType.INTEGER),
    ("bigint", ColumnType.BIGINT),
    ("int8", ColumnType.BIGINT),
    ("bool", ColumnType.BOOLEAN),
    ("bit", ColumnType.BOOLEAN),
    ("int", ColumnType.INTEGER),
    ("double", ColumnType.FLOAT),
    ("float", ColumnType.FLOAT),
    ("real", ColumnType.FLOAT),
    ("decimal", ColumnType.DECIMAL),
    ("numeric", ColumnType.DECIMAL),
    ("number", ColumnType.DECIMAL),
    ("money", ColumnType.DECIMAL),
    ("num", ColumnType.DECIMAL),
    ("json", ColumnType.JSON),
    ("date", ColumnType.DATE),
    ("time", ColumnType.TIMESTAMP),
    ("year", ColumnType.INTEGER),
)


def canonical_type(raw: Any) -> ColumnType:
    """One SchemaPile ``COLUMNS[c].TYPE`` -> a models.ColumnType."""
    text = re.sub(r"\(.*?\)", "", str(raw or "")).lower()
    text = re.sub(r"[^a-z0-9]+", "", text)
    if not text:
        return ColumnType.TEXT
    for token, ctype in _TYPE_RULES:
        if token in text:
            return ctype
    return ColumnType.TEXT


#: Compatible numeric widths and temporal types may share generated FK domains.
_FK_TYPE_FAMILIES: tuple[frozenset[ColumnType], ...] = (
    frozenset(
        {
            ColumnType.INTEGER,
            ColumnType.BIGINT,
            ColumnType.FLOAT,
            ColumnType.DECIMAL,
        }
    ),
    frozenset({ColumnType.DATE, ColumnType.TIMESTAMP}),
)


def _foreign_key_types_compatible(child_raw: Any, parent_raw: Any) -> bool:
    """Return whether scraped FK columns share a lossless generated domain."""
    child = canonical_type(child_raw)
    parent = canonical_type(parent_raw)
    if child is parent:
        return True
    return any(child in family and parent in family for family in _FK_TYPE_FAMILIES)


_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://(?P<host>[^/\s]+)/(?P<path>.*)$", re.I)


def origin_repo(url: str, *, key: str = "") -> str:
    """Derive a repository identity, hashing the record key when no URL exists."""
    text = str(url or "").strip()
    m = _URL_RE.match(text)
    if not m:
        return "unknown/" + sha256_hex(key or text)[:12]
    host = m.group("host").lower()
    if host.startswith("www."):
        host = host[4:]
    segments = [s for s in m.group("path").split("?")[0].split("/") if s]
    if not segments:
        return host
    return "/".join([host, *segments[:2]])


def _resolved_foreign_keys(tables: Mapping[str, Any]) -> list[tuple[str, tuple[str, ...], str, tuple[str, ...]]]:
    """Return normalized FK edges whose target tables exist in this record."""
    present = {t: normalize_identifier(t) for t in tables}
    lookup = {t.lower(): t for t in tables}
    edges: list[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = []
    for tname in sorted(tables):
        tdef = tables.get(tname) or {}
        columns = tdef.get("COLUMNS") or {}
        for fk in tdef.get("FOREIGN_KEYS") or []:
            if not isinstance(fk, Mapping):
                continue
            target_raw = fk.get("FOREIGN_TABLE")
            if not target_raw:
                continue
            target = lookup.get(str(target_raw).lower())
            if target is None or target == tname:
                continue
            child_cols = [str(c) for c in (fk.get("COLUMNS") or []) if c]
            parent_cols = [str(c) for c in (fk.get("REFERRED_COLUMNS") or []) if c]
            if not child_cols or len(child_cols) != len(parent_cols):
                continue
            # SQL identifiers are case-insensitive; the scraped REFERENCES
            # clause often spells them differently from the CREATE TABLE.
            child_lookup = {str(c).lower(): c for c in columns}
            parent_lookup = {
                str(c).lower(): c
                for c in ((tables.get(target) or {}).get("COLUMNS") or {})
            }
            resolved_child = [child_lookup.get(c.lower()) for c in child_cols]
            resolved_parent = [parent_lookup.get(c.lower()) for c in parent_cols]
            if any(c is None for c in resolved_child + resolved_parent):
                continue
            if not all(
                _foreign_key_types_compatible(
                    (columns.get(child_col) or {}).get("TYPE"),
                    (((tables.get(target) or {}).get("COLUMNS") or {}).get(parent_col) or {}).get(
                        "TYPE"
                    ),
                )
                for child_col, parent_col in zip(
                    resolved_child, resolved_parent, strict=True
                )
            ):
                continue
            edges.append(
                (
                    present[tname],
                    tuple(normalize_identifier(c) for c in resolved_child),
                    present[target],
                    tuple(normalize_identifier(c) for c in resolved_parent),
                )
            )
    return sorted(set(edges))


def shape_fingerprint(tables: Mapping[str, Any]) -> str:
    """Fingerprint normalized table shape for family clustering."""
    shapes: list[list] = []
    for tname in tables:
        tdef = tables.get(tname) or {}
        columns = tdef.get("COLUMNS") or {}
        shapes.append(
            [
                normalize_identifier(tname),
                sorted(
                    f"{normalize_identifier(c)}:"
                    f"{canonical_type((columns.get(c) or {}).get('TYPE')).value}"
                    for c in columns
                ),
                sorted(
                    normalize_identifier(c) for c in (tdef.get("PRIMARY_KEYS") or [])
                ),
            ]
        )
    material = {
        "tables": sorted(shapes),
        "fks": [
            f"{child}({','.join(ccols)})->{parent}({','.join(pcols)})"
            for child, ccols, parent, pcols in _resolved_foreign_keys(tables)
        ],
    }
    return sha256_hex(canonical_json(material))[:16]


# Record metrics + the relational filter

class RecordMetrics(BaseModel):
    """Cheap structural summary of one record (what selection ranks on)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tables: int = Field(ge=0)
    columns: int = Field(ge=0)
    #: Columns declared as part of a primary key.
    primary_key_columns: int = Field(ge=0)
    tables_with_pk: int = Field(ge=0)
    #: FK edges declared in the record (including unresolvable ones).
    foreign_keys_declared: int = Field(ge=0)
    #: FK edges whose parent table is present in the same record.
    foreign_keys: int = Field(ge=0)
    #: Distinct child->parent table pairs among the resolved edges.
    linked_table_pairs: int = Field(ge=0)
    indexes: int = Field(ge=0)
    checks: int = Field(ge=0)

    @property
    def rank_key(self) -> tuple[int, ...]:
        """Descending-sort key: link density first, then breadth."""
        return (
            self.linked_table_pairs,
            self.foreign_keys,
            self.tables_with_pk,
            self.tables,
            self.columns,
        )


def record_metrics(record: Mapping[str, Any]) -> RecordMetrics:
    """Return non-raising structural counts over tables with declared columns."""
    raw_tables = record.get("TABLES") or {}
    if not isinstance(raw_tables, Mapping):
        raw_tables = {}
    tables = {
        t: (raw_tables.get(t) or {})
        for t in raw_tables
        if isinstance(raw_tables.get(t), Mapping) and (raw_tables[t].get("COLUMNS"))
    }
    columns = 0
    pk_columns = 0
    tables_with_pk = 0
    declared = 0
    indexes = 0
    checks = 0
    for tname in tables:
        tdef = tables.get(tname) or {}
        cols = tdef.get("COLUMNS") or {}
        columns += len(cols)
        pks = [c for c in (tdef.get("PRIMARY_KEYS") or []) if c]
        pk_columns += len(pks)
        if pks:
            tables_with_pk += 1
        declared += len(tdef.get("FOREIGN_KEYS") or [])
        indexes += len(tdef.get("INDEXES") or [])
        checks += len(tdef.get("CHECKS") or [])
        checks += sum(len((cols[c] or {}).get("CHECKS") or []) for c in cols)
    edges = _resolved_foreign_keys(tables)
    return RecordMetrics(
        tables=len(tables),
        columns=columns,
        primary_key_columns=pk_columns,
        tables_with_pk=tables_with_pk,
        foreign_keys_declared=declared,
        foreign_keys=len(edges),
        linked_table_pairs=len({(c, p) for c, _cc, p, _pc in edges}),
        indexes=indexes,
        checks=checks,
    )


class RelationalFilter(BaseModel):
    """Configurable structural bounds for admitting a relational project."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_tables: int = Field(default=4, ge=1)
    max_tables: int = Field(default=40, ge=1)
    min_columns: int = Field(default=12, ge=1)
    min_foreign_keys: int = Field(default=3, ge=0)
    min_linked_table_pairs: int = Field(default=2, ge=0)
    min_tables_with_pk: int = Field(default=2, ge=0)
    require_permissive: bool = True


#: The stated default policy (see RelationalFilter's docstring).
DEFAULT_FILTER = RelationalFilter()


def filter_problems(
    metrics: RecordMetrics, filt: RelationalFilter | None = None
) -> tuple[str, ...]:
    """Named reasons `metrics` fails `filt`; empty tuple means it passes."""
    f = filt or DEFAULT_FILTER
    problems: list[str] = []
    if metrics.tables < f.min_tables:
        problems.append(f"{metrics.tables} table(s) < min_tables {f.min_tables}")
    if metrics.tables > f.max_tables:
        problems.append(f"{metrics.tables} table(s) > max_tables {f.max_tables}")
    if metrics.columns < f.min_columns:
        problems.append(f"{metrics.columns} column(s) < min_columns {f.min_columns}")
    if metrics.foreign_keys < f.min_foreign_keys:
        problems.append(
            f"{metrics.foreign_keys} resolved foreign key(s) < min_foreign_keys "
            f"{f.min_foreign_keys}"
        )
    if metrics.linked_table_pairs < f.min_linked_table_pairs:
        problems.append(
            f"{metrics.linked_table_pairs} linked table pair(s) < "
            f"min_linked_table_pairs {f.min_linked_table_pairs}"
        )
    if metrics.tables_with_pk < f.min_tables_with_pk:
        problems.append(
            f"{metrics.tables_with_pk} table(s) with a primary key < "
            f"min_tables_with_pk {f.min_tables_with_pk}"
        )
    return tuple(problems)


# The persisted index (built by tools/schemapile_index.py, read here)

class IndexedRecord(BaseModel):
    """One row of the compact corpus index — selection never re-reads 327 MB."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    url: str = ""
    license: str = ""
    permissive: bool = False
    #: Origin repository identifier (see `origin_repo`).
    repo: str = Field(min_length=1)
    #: Normalized-shape fingerprint (see `shape_fingerprint`).
    shape: str = Field(min_length=1)
    #: Cluster (family) this record belongs to; '' before the clustering pass.
    cluster: str = ""
    metrics: RecordMetrics

    @property
    def rank_key(self) -> tuple[int, ...]:
        return self.metrics.rank_key


class RecordCluster(BaseModel):
    """One independence unit: records that must share ONE family id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cluster_id: str = Field(min_length=1)
    #: Every origin repository fused into this cluster (sorted).
    repos: tuple[str, ...] = Field(min_length=1)
    #: Every record key in this cluster (sorted).
    members: tuple[str, ...] = Field(min_length=1)
    #: Best-ranked member that also clears the relational filter ('' if none).
    representative: str = ""
    #: Members that clear the relational filter, best first.
    candidates: tuple[str, ...] = ()

    @property
    def family_id(self) -> str:
        return f"{POOL}__{self.cluster_id}"


class SchemaPileIndex(BaseModel):
    """The persisted corpus index: records, clusters and corpus-level totals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Absolute path of the corpus the index was built from (evidence).
    source: str = ""
    source_bytes: int = Field(default=0, ge=0)
    #: Thresholds the `candidates`/`representative` fields were computed with.
    filter: RelationalFilter = DEFAULT_FILTER
    records: tuple[IndexedRecord, ...] = ()
    clusters: tuple[RecordCluster, ...] = ()
    stats: dict[str, int] = Field(default_factory=dict)
    #: LICENSE -> record count over the whole corpus (licensing evidence).
    licenses: dict[str, int] = Field(default_factory=dict)

    def record(self, key: str) -> IndexedRecord:
        for r in self.records:
            if r.key == key:
                return r
        raise KeyError(f"SchemaPile index has no record {key!r}")

    def cluster(self, cluster_id: str) -> RecordCluster:
        for c in self.clusters:
            if c.cluster_id == cluster_id:
                return c
        raise KeyError(f"SchemaPile index has no cluster {cluster_id!r}")

    def best_clusters(self, *, limit: int = 20) -> tuple[RecordCluster, ...]:
        """Clusters with an ingestible representative, strongest schema first."""
        by_key = {r.key: r for r in self.records}
        ranked = [c for c in self.clusters if c.representative]
        ranked.sort(
            key=lambda c: (
                tuple(-v for v in by_key[c.representative].rank_key),
                c.cluster_id,
            )
        )
        return tuple(ranked[:limit])


def write_index(index: SchemaPileIndex, path: Path | str) -> Path:
    """Persist the index as canonical JSON (deterministic, diffable)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(index.model_dump(mode="json")), encoding="utf-8")
    return out


def load_index(path: Path | str) -> SchemaPileIndex:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"SchemaPile index not found: {p} — build it with "
            "`elt-taskgen-schemapile-index --source <schemapile-perm.json> "
            "--out <index.json>` (fail closed)"
        )
    return SchemaPileIndex.model_validate_json(p.read_text(encoding="utf-8"))


# Record -> physical schema

def _record_info(record: Mapping[str, Any]) -> tuple[str, str, bool]:
    """``(url, license, permissive)`` from INFO, normalized. Never raises."""
    info = record.get("INFO") or {}
    if not isinstance(info, Mapping):
        info = {}
    url = str(info.get("URL") or "").strip()
    license_name = str(info.get("LICENSE") or "").strip()
    return url, license_name, bool(info.get("PERMISSIVE"))


def assert_usable_license(record: Mapping[str, Any], key: str) -> tuple[str, str]:
    """Return per-record URL and license, rejecting non-permissive records."""
    url, license_name, permissive = _record_info(record)
    if not permissive:
        raise NonPermissiveRecordError(
            f"SchemaPile record {key!r} is not flagged PERMISSIVE — refused "
            f"(license: {license_name or None!r})"
        )
    if not license_name:
        raise NonPermissiveRecordError(
            f"SchemaPile record {key!r} carries no LICENSE — refused "
            "(a record with no usable license must be skipped)"
        )
    return url, license_name


def check_domains(checks: Sequence[Any]) -> dict[str, tuple[str, ...]]:
    """Recover string-valued closed domains from parseable ``IN`` checks."""
    domains: dict[str, tuple[str, ...]] = {}
    for raw in checks or ():
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            tree = sqlglot.parse_one(text)
        except Exception:
            continue
        if tree is None:
            continue
        for in_expr in tree.find_all(exp.In):
            col = in_expr.this
            if not isinstance(col, exp.Column):
                continue
            literals = list(in_expr.expressions)
            if not literals or not all(
                isinstance(e, exp.Literal) and e.is_string for e in literals
            ):
                continue
            values = tuple(dict.fromkeys(e.name for e in literals))
            if values:
                domains.setdefault(normalize_identifier(col.name), values)
    return domains


def record_to_tables(
    record: Mapping[str, Any], *, key: str = ""
) -> tuple[tuple[TableSpec, ...], tuple[Relationship, ...]]:
    """Convert a record to tables and relationships, rejecting identifier collisions."""
    tables_raw = record.get("TABLES") or {}
    if not isinstance(tables_raw, Mapping) or not tables_raw:
        raise ValueError(f"record {key!r}: no TABLES block")

    usable = {
        t: (tables_raw.get(t) or {})
        for t in sorted(tables_raw)
        if (tables_raw.get(t) or {}).get("COLUMNS")
    }
    if not usable:
        raise ValueError(f"record {key!r}: every table is column-less")

    names: dict[str, str] = {}
    for raw_name in usable:
        norm = normalize_identifier(raw_name)
        if norm in names:
            raise ValueError(
                f"record {key!r}: table names {names[norm]!r} and {raw_name!r} "
                f"both normalize to {norm!r} — ambiguous, refused"
            )
        names[norm] = raw_name

    tables: list[TableSpec] = []
    for raw_name in usable:
        tdef = usable[raw_name]
        cols_raw = tdef.get("COLUMNS") or {}
        all_checks = list(tdef.get("CHECKS") or [])
        for c in cols_raw:
            all_checks.extend((cols_raw.get(c) or {}).get("CHECKS") or [])
        domains = check_domains(all_checks)
        pk_raw = [str(c) for c in (tdef.get("PRIMARY_KEYS") or []) if c]
        declared = {str(c).lower() for c in cols_raw}
        pk_norm = {
            normalize_identifier(c) for c in pk_raw if c.lower() in declared
        }

        seen: dict[str, str] = {}
        specs: list[ColumnSpec] = []
        for raw_col in sorted(cols_raw):
            cdef = cols_raw.get(raw_col) or {}
            cname = normalize_identifier(raw_col)
            if cname in seen:
                raise ValueError(
                    f"record {key!r}: table {raw_name!r} columns {seen[cname]!r} "
                    f"and {raw_col!r} both normalize to {cname!r} — refused"
                )
            seen[cname] = raw_col
            is_pk = cname in pk_norm or bool(cdef.get("IS_PRIMARY"))
            nullable_flag = cdef.get("NULLABLE")
            # NULLABLE is tri-state; null means "the DDL did not say", which is
            # NULLable in every dialect. A primary key is never nullable.
            nullable = (nullable_flag is not False) and not is_pk
            comment = str(cdef.get("COMMENT") or "").strip()
            description = comment or f"Column {raw_col} of table {raw_name}."
            ctype = canonical_type(cdef.get("TYPE"))
            enum_values = domains.get(cname) if ctype == ColumnType.TEXT else None
            specs.append(
                ColumnSpec(
                    name=cname,
                    type=ctype,
                    nullable=nullable,
                    description=description,
                    enum_values=enum_values,
                )
            )
        pk_cols = tuple(c.name for c in specs if c.name in pk_norm)
        if not pk_cols:
            pk_cols = tuple(c.name for c in specs if (cols_raw.get(seen[c.name]) or {}).get("IS_PRIMARY"))
        table_comment = str(tdef.get("COMMENT") or "").strip()
        tables.append(
            TableSpec(
                name=normalize_identifier(raw_name),
                description=table_comment or f"Source table {raw_name}.",
                columns=tuple(specs),
                primary_key=pk_cols,
            )
        )

    by_name = {t.name: t for t in tables}
    rels: list[Relationship] = []
    for child, child_cols, parent, parent_cols in _resolved_foreign_keys(usable):
        if child not in by_name or parent not in by_name:
            continue
        if any(c not in {x.name for x in by_name[child].columns} for c in child_cols):
            continue
        if any(c not in {x.name for x in by_name[parent].columns} for c in parent_cols):
            continue
        required = all(not by_name[child].column(c).nullable for c in child_cols)
        rels.append(
            Relationship(
                child_table=child,
                child_columns=child_cols,
                parent_table=parent,
                parent_columns=parent_cols,
                required=required,
            )
        )
    rels.sort(
        key=lambda r: (r.child_table, r.child_columns, r.parent_table, r.parent_columns)
    )
    return tuple(tables), tuple(rels)


# Mart construction from the FK graph (schema structure only)

def _pick_join_surface(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    *,
    key_parents: frozenset[tuple[str, str]],
) -> tuple[Relationship, Relationship | None]:
    """Choose keyed links around the fact table with most outgoing edges.

    The grouping link uses its widest parent; a second link uses another parent.
    """
    by_name = {t.name: t for t in tables}
    keyed = tuple(
        r
        for r in rels
        if len(r.parent_columns) == 1
        and (r.parent_table, r.parent_columns[0]) in key_parents
    )
    if not keyed:
        raise RelationalFilterError(
            "no declared link whose parent column is the parent's declared "
            "single-column primary key — every candidate grain could repeat, "
            "so every count could be inflated (fail closed)"
        )
    out_degree: dict[str, int] = {}
    for r in keyed:
        out_degree[r.child_table] = out_degree.get(r.child_table, 0) + 1
    fact = sorted(out_degree, key=lambda t: (-out_degree[t], t))[0]
    links = sorted(
        (r for r in keyed if r.child_table == fact),
        key=lambda r: (
            -len(by_name[r.parent_table].columns),
            r.parent_table,
            r.parent_columns,
            r.child_columns,
        ),
    )
    primary = links[0]
    second = next((r for r in links[1:] if r.parent_table != primary.parent_table), None)
    return primary, second


def _measure_column(fact: TableSpec, rels: tuple[Relationship, ...]) -> ColumnSpec | None:
    fk_cols = {c for r in rels if r.child_table == fact.name for c in r.child_columns}
    for col in fact.columns:
        if (
            col.type in _NUMERIC_TYPES
            and col.name not in fk_cols
            and col.name not in fact.primary_key
        ):
            return col
    return None


#: Descriptive-column name hints, best first: a label, not another key.
_ATTRIBUTE_HINTS: tuple[str, ...] = (
    "name",
    "code",
    "title",
    "label",
    "description",
    "status",
    "type",
)


def _attribute_column(dim: TableSpec, rels: tuple[Relationship, ...]) -> ColumnSpec | None:
    """The most descriptive TEXT column of a dimension (never a key column)."""
    fk_cols = {c for r in rels if r.child_table == dim.name for c in r.child_columns}
    candidates = [
        c
        for c in dim.columns
        if c.type == ColumnType.TEXT
        and c.name not in dim.primary_key
        and c.name not in fk_cols
    ]
    if not candidates:
        return None
    for hint in _ATTRIBUTE_HINTS:
        for col in candidates:
            if hint in col.name:
                return col
    return candidates[0]


#: Mart budget. SchemaPile is the DEPTH pool: several corners of the graph at
#: several grains, because one wide star covers no more of a 69-table schema.
_MAX_MARTS = 3

#: Solver-facing prose per library shape: the adapter owns prose, the plan
#: library owns SQL.
_MART_PROSE: dict[str, str] = {
    "rollup": (
        "Per-{parent} roll-up of linked {bridge} activity in the {label} "
        "schema, following the fan-out onto {child}."
    ),
    "top": (
        "Per-{parent} extremes over linked {bridge} rows in the {label} "
        "schema: WHICH row is largest, not how large it is."
    ),
    "bands": (
        "Per-{parent} banding of linked {bridge} activity in the {label} "
        "schema, over the value domain the schema itself declares."
    ),
    "by_period": (
        "Per-({parent}, month) activity grid over {bridge} in the {label} schema."
    ),
    "cohorts": (
        "Per-({parent}, status cohort) summary of linked {bridge} activity in "
        "the {label} schema."
    ),
    "snapshot": (
        "Per-{parent} latest-row snapshot over linked {bridge} activity in the "
        "{label} schema."
    ),
    "distribution": (
        "Per-({parent}, measure state) distribution of linked {bridge} activity "
        "in the {label} schema."
    ),
}


def _measured_key_parents(
    tables: tuple[TableSpec, ...],
) -> frozenset[tuple[str, str]]:
    """Return declared single-column PKs used to prove parent keyness."""
    return declared_key_parents(tables)


def _build_marts(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    *,
    label: str,
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> tuple[tuple[MartSpec, ...], tuple[StarShape, ...]]:
    """Build marts from FK chains, preferring a distinct parent for each."""
    key_parents = _measured_key_parents(tables)
    candidates = chain_candidates(tables, rels, key_parents=key_parents)
    if candidates:
        max_marts, spread_grains, max_per_shape = difficulty_profile.mart_parameters(
            pool="schemapile",
            default_budget=_MAX_MARTS,
            default_spread_grains=True,
            default_max_per_shape=_MAX_MARTS,
        )
        marts, shapes, _names = build_marts(
            candidates,
            prefix=lambda e, suffix: normalize_identifier(
                f"{e.parent}_{e.bridge}_{suffix}"
            ),
            max_marts=max_marts,
            spread_grains=spread_grains,
            max_per_shape=max_per_shape,
            description=lambda suffix, e: _MART_PROSE[suffix].format(
                parent=e.parent, bridge=e.bridge, child=e.child or e.bridge, label=label
            ),
            notes=(
                "Constructed from the SchemaPile foreign-key graph only; no "
                "scraped sample values were consulted."
            ),
        )
        if marts:
            return marts, shapes
    # Fallback for a record funding no library shape: it still has a join
    # surface, and the SAME key_parents gate still REFUSES a non-PK grain.
    mart, shape = _build_legacy_star(tables, rels, label=label, key_parents=key_parents)
    return (mart,), (shape,)


def _build_legacy_star(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    *,
    label: str,
    key_parents: frozenset[tuple[str, str]],
) -> tuple[MartSpec, StarShape]:
    """Build a keyed per-parent rollup with the shared star-plan builder."""
    primary, second = _pick_join_surface(tables, rels, key_parents=key_parents)
    by_name = {t.name: t for t in tables}
    parent = by_name[primary.parent_table]
    fact = by_name[primary.child_table]
    dim = by_name[second.parent_table] if second is not None else None

    key_cols = tuple(primary.parent_columns)
    measure = _measure_column(fact, rels)
    attribute = _attribute_column(dim, rels) if dim is not None else None

    # The dedupe rule MUST be in the column's own description: the bundle ships
    # mart column DESCRIPTIONS, not plan ops, so an op-only rule never reaches
    # the solver.
    deduped = not fact.primary_key

    used: set[str] = set()

    def _unique(name: str) -> str:
        candidate = name
        n = 2
        while candidate in used:
            candidate = f"{name}_{n}"
            n += 1
        used.add(candidate)
        return candidate

    columns: list[MartColumn] = []
    key_out: list[str] = []
    for col in key_cols:
        out = _unique(col)
        key_out.append(out)
        columns.append(
            MartColumn(
                name=out,
                type=parent.column(col).type,
                description=f"Key column {col} of {parent.name}.",
            )
        )

    joins: list[StarJoin] = []
    measures: list[Measure] = []
    fact_link_alias = normalize_identifier(f"{fact.name}__{primary.child_columns[0]}")
    fact_carry: list[tuple[str, str]] = [(primary.child_columns[0], fact_link_alias)]
    measure_alias = None
    if measure is not None:
        measure_alias = normalize_identifier(f"{fact.name}__{measure.name}")
        fact_carry.append((measure.name, measure_alias))
    dim_link_col = None
    if second is not None and dim is not None and attribute is not None:
        dim_link_col = second.child_columns[0]
        if dim_link_col not in {c for c, _ in fact_carry}:
            fact_carry.append(
                (dim_link_col, normalize_identifier(f"{fact.name}__{dim_link_col}"))
            )
    joins.append(
        StarJoin(
            table=fact.name,
            on_pairs=tuple(zip(key_out, primary.child_columns)),
            carry=tuple(fact_carry),
            rel_columns=tuple(primary.parent_columns) + tuple(primary.child_columns),
            description=(
                f"Every {parent.name} row appears in the output, matched to "
                f"its {fact.name} rows; {parent.name} rows with no "
                f"{fact.name} rows are retained."
            ),
        )
    )

    attribute_out = None
    if second is not None and dim is not None and attribute is not None:
        attribute_out = _unique(f"{dim.name}_{attribute.name}")
        columns.append(
            MartColumn(
                name=attribute_out,
                type=ColumnType.TEXT,
                description=(
                    f"{attribute.name} of the {dim.name} row linked to the "
                    f"{fact.name} rows; NULL is reported as the literal "
                    "'unknown' so unmatched rows are still described."
                ),
            )
        )
        dim_attr_alias = normalize_identifier(f"{dim.name}__{attribute.name}")
        joins.append(
            StarJoin(
                table=dim.name,
                on_pairs=(
                    (
                        normalize_identifier(f"{fact.name}__{dim_link_col}"),
                        second.parent_columns[0],
                    ),
                ),
                carry=((attribute.name, dim_attr_alias),),
                rel_columns=tuple(second.child_columns) + tuple(second.parent_columns),
                description=(
                    f"Each {fact.name} row is matched to the {dim.name} row "
                    f"it links to; {fact.name} rows with no {dim.name} match "
                    "are retained."
                ),
            )
        )
        measures.append(
            Measure(
                column=attribute_out,
                expr=f"MAX({quote(dim_attr_alias)})",
                null_default="'unknown'",
            )
        )

    count_out = _unique(f"{fact.name}_count")
    columns.append(
        MartColumn(
            name=count_out,
            type=ColumnType.INTEGER,
            description=(
                (
                    f"Number of DISTINCT {fact.name} rows linked to this "
                    f"{parent.name} row; 0 when there are none. {fact.name} "
                    "declares no primary key upstream, so byte-identical "
                    "duplicate rows can occur, and they count ONCE."
                )
                if deduped
                else (
                    f"Number of {fact.name} rows linked to this {parent.name} "
                    "row; 0 when there are none."
                )
            ),
        )
    )
    measures.append(
        Measure(column=count_out, expr=f"COUNT({quote(fact_link_alias)})")
    )
    if measure is not None and measure_alias is not None:
        total_out = _unique(f"total_{measure.name}")
        columns.append(
            MartColumn(
                name=total_out,
                type=measure.type if measure.type in _NUMERIC_TYPES else ColumnType.DECIMAL,
                description=(
                    (
                        f"Sum of {fact.name}.{measure.name} over those same "
                        "DISTINCT linked rows; 0 when there are none."
                    )
                    if deduped
                    else (
                        f"Sum of {fact.name}.{measure.name} over the linked "
                        "rows; 0 when there are none."
                    )
                ),
            )
        )
        measures.append(
            Measure(
                column=total_out,
                expr=f"SUM({quote(measure_alias)})",
                null_default="0",
            )
        )

    mart_name = normalize_identifier(f"{parent.name}_{fact.name}_summary")
    built = build_star(
        mart=mart_name,
        parent=parent.name,
        parent_keys=key_cols,
        key_columns=tuple(key_out),
        joins=tuple(joins),
        measures=tuple(measures),
        dedupe=(
            (fact.name, tuple(c.name for c in fact.columns)) if deduped else ()
        ),
        notes=(
            "Constructed from the SchemaPile foreign-key graph only; no "
            "scraped sample values were consulted."
        ),
    )
    return (
        MartSpec(
            name=mart_name,
            description=(
                f"Per-{parent.name} summary of linked {fact.name} activity in the "
                f"{label} schema."
            ),
            grain=(
                f"One row per {parent.name} ({', '.join(key_out)}), including "
                f"{parent.name} rows with no {fact.name} rows."
            ),
            key_columns=tuple(key_out),
            columns=tuple(columns),
            plan=built.plan,
        ),
        built.shape,
    )


# Populations (SYNTHETIC — SchemaPile ships no rows)

def _scale_hint(
    tables: tuple[TableSpec, ...], rels: tuple[Relationship, ...]
) -> dict[str, int]:
    """Return scale hints bounded by shared FK-capacity policy."""

    return schema_scale_hint(tables, rels)


def _synthetic_populations(
    task_id: str,
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    shapes: tuple[StarShape, ...],
    *,
    backends: int = 1,
    backend_assignments: tuple[BackendAssignment, ...] = (),
) -> tuple[tuple[PopulationSpec, ...], tuple[AttackCase, ...]]:
    """Build five synthetic populations and attacks from the same star shapes."""
    populations, attacks = derive_populations_and_attacks(
        task_id=task_id,
        tables=tables,
        relationships=rels,
        shapes=shapes,
        scale_hint=_scale_hint(tables, rels),
        policy_conditions=(SYNTHETIC_POPULATION_POLICY,),
        backends=backends,
        # Load-side cases (header_as_row / truncate_table / wrong_source_file)
        # need the ASSIGNMENTS: they exist only per backend a table sits on.
        backend_assignments=backend_assignments,
    )
    # Operator-family cases come from the shape's own `attack_claims` above.
    return populations, attacks


# The ingest entry point

def _selection(
    *,
    key: str,
    cluster: str,
    license_name: str,
    url: str,
    pool: str,
    catalog: SourceCatalog | None,
) -> PoolSelection:
    """Route identity through the source catalog (license gate included)."""
    cat = catalog if catalog is not None else load_source_catalog()
    source = cat.pool(pool)
    if source.origin is not Origin.SCHEMAPILE:
        raise ValueError(
            f"pool {pool!r} maps to origin {source.origin.value!r}; this adapter "
            "only ingests Origin.SCHEMAPILE material (fail closed)"
        )
    attribution = source.attribution_for(key)
    if url:
        attribution = f"{attribution} <{url}>"
    attribution = f"{attribution} [{license_name}]"
    return source.selection(
        key, license=license_name, attribution=attribution, family=cluster
    )


def to_task_ir(
    record: Mapping[str, Any],
    *,
    key: str,
    cluster: str,
    pool: str = POOL,
    filt: RelationalFilter | None = None,
    catalog: SourceCatalog | None = None,
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> TaskIR:
    """Convert a selected record to a draft task using cluster identity."""
    url, license_name = assert_usable_license(record, key)
    metrics = record_metrics(record)
    problems = filter_problems(metrics, filt)
    if problems:
        raise RelationalFilterError(
            f"SchemaPile record {key!r} is not a relational project: "
            + "; ".join(problems)
        )

    tables, rels = record_to_tables(record, key=key)
    if len(tables) < 2 or not rels:
        raise RelationalFilterError(
            f"SchemaPile record {key!r}: no usable join surface after "
            f"normalization ({len(tables)} table(s), {len(rels)} relationship(s))"
        )

    selection = _selection(
        key=key,
        cluster=cluster,
        license_name=license_name,
        url=url,
        pool=pool,
        catalog=catalog,
    )
    family_id = selection.family_id
    task_id = f"{family_id}__{slugify_family(key)}"
    label = key.rsplit("/", 1)[-1]
    marts, shapes_out = _build_marts(
        tables,
        rels,
        label=label,
        difficulty_profile=difficulty_profile,
    )

    backends = tuple(
        BackendAssignment(
            table=t.name,
            backend=_BACKEND_CYCLE[
                derive_seed("schemapile-backend", family_id, t.name)
                % len(_BACKEND_CYCLE)
            ],
        )
        for t in tables
    )
    # Reassigned BEFORE the catalogue reads the assignments: a table with no
    # NOT NULL column cannot ride FILES or REST.
    backends = reassign_unsafe_file_tables(backends, tables)
    populations, attacks = _synthetic_populations(
        task_id,
        tables,
        rels,
        shapes_out,
        backends=len({b.backend for b in backends}),
        backend_assignments=backends,
    )
    title = " ".join(
        w.capitalize()
        for w in re.split(r"[^A-Za-z0-9]+", cluster)
        if w and not re.fullmatch(r"[0-9a-f]{8,}", w)
    ).strip() or cluster
    task = TaskIR(
        task_id=task_id,
        cluster_id=family_id,
        **selection.ir_identity(),
        title=f"SchemaPile schema: {title}",
        tables=tables,
        relationships=rels,
        backends=backends,
        marts=marts,
        populations=populations,
        attack_cases=attacks,
    )
    # See reference/solution.py::attach_reference.
    return attach_reference(task)


def task_from_index(
    index: SchemaPileIndex,
    *,
    source: Path | str,
    key: str | None = None,
    cluster: str | None = None,
    pool: str = POOL,
    filt: RelationalFilter | None = None,
    catalog: SourceCatalog | None = None,
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> TaskIR:
    """Resolve exactly one key or cluster representative and build its task."""
    if bool(key) == bool(cluster):
        raise ValueError("pass exactly one of key= or cluster=")
    if cluster:
        entry = index.cluster(cluster)
        if not entry.representative:
            raise RelationalFilterError(
                f"cluster {cluster!r} has no member clearing the relational "
                "filter (fail closed)"
            )
        key = entry.representative
    indexed = index.record(str(key))
    if not indexed.cluster:
        raise ValueError(
            f"record {key!r} has no cluster in the index — re-run the clusterer"
        )
    record = find_record(source, str(key))
    return to_task_ir(
        record,
        key=str(key),
        cluster=indexed.cluster,
        pool=pool,
        filt=filt if filt is not None else index.filter,
        catalog=catalog,
        difficulty_profile=difficulty_profile,
    )

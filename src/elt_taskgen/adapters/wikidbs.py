"""Convert a WikiDBs directory and its unmodified vendor rows into ``TaskIR``.

Family identity uses a verified WikiDBGraph component or the fallback segment.
Wikidata provenance is excluded from emitted tasks.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.catalog import SourceCatalog, load_source_catalog
from elt_taskgen.adapters.evidence import (
    build_marts,
    chain_candidates,
    link_statistics,
    observed_domains,
    schema_shape_cluster_id,
)
from elt_taskgen.generation.mart_plan import (
    MIN_MART_COLUMNS,
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
    POLICY_PROVIDED_ROWS,
    derive_attack_cases,
    stress_duplicate_conditions,
)
from elt_taskgen.reference.solution import attach_reference
from elt_taskgen.package_resources import resource_path
from elt_taskgen.models import (
    AttackKind,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartColumn,
    MartColumnKind,
    MartOpKind,
    MartSpec,
    PopulationName,
    PopulationSpec,
    Relationship,
    Row,
    TableSpec,
    TaskIR,
    canonical_json,
    derive_seed,
    sha256_hex,
    slugify_family,
)

POOL = "wikidbs"

#: Population policy: rows are PROVIDED by the vendored source, never synthesized.
REAL_DATA_POLICY = "provided-rows"

#: schema.json keys this adapter may read; everything else is provenance.
RETAINED_SCHEMA_KEYS: tuple[str, ...] = ("database_name", "tables")
RETAINED_TABLE_KEYS: tuple[str, ...] = (
    "table_name", "file_name", "columns", "foreign_keys",
)
RETAINED_COLUMN_KEYS: tuple[str, ...] = ("column_name", "data_type")

#: Answer-shortcut keys, never read into a TaskIR; the guard reads exactly these.
PROVENANCE_KEYS: tuple[str, ...] = (
    "wikidata_property_id",
    "wikidata_property_label",
    "wikidata_topic_item_id",
    "wikidata_topic_item_label",
    "alternative_database_names",
    "alternative_table_names",
    "alternative_column_names",
)

#: Directory holding the label-valued CSVs (the ones we DO read).
TABLES_DIRNAME = "tables"
#: Directory holding the Q-id-valued CSVs. Never read: same shortcut, in data.
ITEM_ID_TABLES_DIRNAME = "tables_with_item_ids"

#: WikiDBs part directories, 20,000 databases each, node ids ascending.
PART_SIZE = 20_000
PART_COUNT = 5
TOTAL_NODES = PART_SIZE * PART_COUNT

#: Family when node ids are unverifiable: one pool-wide family (under-grouping leaks the split).
FALLBACK_FAMILY_SEGMENT = "unmapped"

# -- ingest size envelope (a task IR carries its rows, so it must stay sane) --
MIN_TABLES = 2
MAX_TABLES = 12
MAX_ROWS_PER_TABLE = 1_000
MAX_TOTAL_CELLS = 60_000

#: Development population size caps (a tiny, readable slice of the real rows).
DEV_PARENT_ROWS = 4
DEV_CHILD_ROWS = 8

#: Dimension-row fractions the counterfactual tries in order; one that empties a table tests nothing.
COUNTERFACTUAL_FRACTIONS: tuple[float, ...] = (0.5, 0.25, 0.1)

#: Fraction of a leaf table's real rows exact-duplicated in the stress population.
STRESS_DUPLICATE_FRACTION = 0.1

_BACKEND_CYCLE: tuple[Backend, ...] = (
    Backend.POSTGRES,
    Backend.MONGODB,
    Backend.REST,
    Backend.S3,
    Backend.FILES,
)

#: Widest value a DuckDB BIGINT column can hold; anything past it is TEXT.
_BIGINT_MAX = 2**63 - 1

_NUMERIC_TYPES = frozenset(
    {ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.FLOAT, ColumnType.DECIMAL}
)

#: Wikidata id token (Q42, P50) — banned from emitted prose; a column of mostly these is stripped.
_WIKIDATA_ID_RE = re.compile(r"\b[QP]\d+\b")
_WIKIDATA_ID_VALUE_RE = re.compile(r"^[QP]\d+$")

#: Fraction of a column's non-empty values that must be Wikidata ids to count as one.
WIKIDATA_ID_COLUMN_RATIO = 0.5

#: "<NNNNN> <db_name>" directory naming.
_DIR_RE = re.compile(r"^(\d{5}) (.+)$")

#: CANONICAL numerals only — a leading zero ('0732') stays TEXT because int() destroys identifier formatting; '+5' is accepted.
_INT_RE = re.compile(r"^(?:[+-]?[1-9]\d*|0)$")
_FLOAT_RE = re.compile(
    r"^[+-]?(?:(?:0|[1-9]\d*)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)

#: WikiDBs `data_type` values declaring a column an identifier: always TEXT, never a measure.
_IDENTIFIER_DATA_TYPES = frozenset({"external-id"})


class WikiDbsIngestError(ValueError):
    """A WikiDBs database cannot be ingested (missing, malformed, out of envelope)."""


class ProvenanceLeakError(ValueError):
    """Wikidata manufacturing provenance was found in an emitted TaskIR."""


# -- schema.json: structural stripping happens HERE -------------------------

class WikiColumn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    column_name: str = Field(min_length=1)
    data_type: str = ""


class WikiForeignKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    column_name: str = Field(min_length=1)
    reference_column_name: str = Field(min_length=1)
    reference_table_name: str = Field(min_length=1)


class WikiTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    table_name: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    columns: tuple[WikiColumn, ...] = Field(min_length=1)
    foreign_keys: tuple[WikiForeignKey, ...] = ()


class WikiSchema(BaseModel):
    """The PROVENANCE-FREE view of one WikiDBs schema.json.

    Built only by `load_schema`, field by field, so no `wikidata_*` or
    `alternative_*` value can reach it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    database_name: str = Field(min_length=1)
    tables: tuple[WikiTable, ...] = Field(min_length=1)

    def table(self, name: str) -> WikiTable:
        for t in self.tables:
            if t.table_name == name:
                return t
        raise KeyError(name)


def _raw_schema(db_dir: Path) -> dict:
    path = Path(db_dir) / "schema.json"
    if not path.is_file():
        raise WikiDbsIngestError(f"{db_dir}: no schema.json (fail closed)")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WikiDbsIngestError(f"{path}: malformed JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise WikiDbsIngestError(f"{path}: expected a JSON object")
    return raw


def load_schema(db_dir: Path) -> WikiSchema:
    """Read schema.json, keeping ONLY the retained structural keys.

    Stripping is STRUCTURAL, not a later filter: `wikidata_*` and
    `alternative_*` are simply never read.
    """
    raw = _raw_schema(db_dir)
    tables: list[WikiTable] = []
    for entry in raw.get("tables") or ():
        if not isinstance(entry, dict):
            raise WikiDbsIngestError(f"{db_dir}: a table entry is not an object")
        columns = tuple(
            WikiColumn(
                column_name=str(c.get("column_name") or ""),
                data_type=str(c.get("data_type") or ""),
            )
            for c in entry.get("columns") or ()
            if isinstance(c, dict) and c.get("column_name")
        )
        if not columns:
            raise WikiDbsIngestError(
                f"{db_dir}: table {entry.get('table_name')!r} has no columns"
            )
        fks = tuple(
            WikiForeignKey(
                column_name=str(f["column_name"]),
                reference_column_name=str(f["reference_column_name"]),
                reference_table_name=str(f["reference_table_name"]),
            )
            for f in entry.get("foreign_keys") or ()
            if isinstance(f, dict)
            and f.get("column_name")
            and f.get("reference_column_name")
            and f.get("reference_table_name")
        )
        tables.append(
            WikiTable(
                table_name=str(entry.get("table_name") or ""),
                file_name=str(entry.get("file_name") or ""),
                columns=columns,
                foreign_keys=fks,
            )
        )
    if not tables:
        raise WikiDbsIngestError(f"{db_dir}: schema.json declares no tables")
    return WikiSchema(
        database_name=str(raw.get("database_name") or Path(db_dir).name),
        tables=tuple(tables),
    )


def provenance_strings(db_dir: Path) -> tuple[str, ...]:
    """Every Wikidata provenance string in this schema.json — QUARANTINED.

    The one explicit accessor for the forbidden material: only
    `assert_no_provenance_leak` and its test may call it.
    """
    raw = _raw_schema(db_dir)
    out: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            out.add(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _add(item)

    for key in PROVENANCE_KEYS:
        _add(raw.get(key))
    for table in raw.get("tables") or ():
        if not isinstance(table, dict):
            continue
        for key in PROVENANCE_KEYS:
            _add(table.get(key))
        for column in table.get("columns") or ():
            if isinstance(column, dict):
                for key in PROVENANCE_KEYS:
                    _add(column.get(key))
    return tuple(sorted(out))


# -- Node id <-> directory correspondence (VERIFIED, not assumed) -----------

def node_id_for_dir(dir_name: str) -> int:
    """'00042 Some Db Name' -> 42. Raises on any other shape."""
    m = _DIR_RE.match(dir_name)
    if m is None:
        raise WikiDbsIngestError(
            f"WikiDBs directory {dir_name!r} does not match '<NNNNN> <db_name>'"
        )
    return int(m.group(1))


def part_for_node(node_id: int) -> str:
    return f"part-{node_id // PART_SIZE}"


def verify_node_correspondence(root: Path) -> dict[str, str]:
    """Verify gapless directory-prefix/node-id correspondence and return evidence."""
    root = Path(root)
    if not root.is_dir():
        raise WikiDbsIngestError(f"WikiDBs root {root} does not exist (fail closed)")
    total = 0
    for part_index in range(PART_COUNT):
        part = root / f"part-{part_index}"
        if not part.is_dir():
            raise WikiDbsIngestError(f"missing WikiDBs part directory: {part}")
        ids: set[int] = set()
        for entry in part.iterdir():
            if not entry.is_dir():
                continue
            ids.add(node_id_for_dir(entry.name))
        expected = set(range(part_index * PART_SIZE, (part_index + 1) * PART_SIZE))
        if ids != expected:
            missing = sorted(expected - ids)[:5]
            extra = sorted(ids - expected)[:5]
            raise WikiDbsIngestError(
                f"{part}: directory prefixes are not exactly "
                f"[{part_index * PART_SIZE},{(part_index + 1) * PART_SIZE}); "
                f"missing e.g. {missing}, unexpected e.g. {extra}"
            )
        total += len(ids)
    if total != TOTAL_NODES:
        raise WikiDbsIngestError(
            f"{root}: {total} database directories, expected {TOTAL_NODES} "
            "(the node count WikiDBGraph's analysis report states)"
        )
    return {
        "part_count": str(PART_COUNT),
        "part_size": str(PART_SIZE),
        "total_databases": str(total),
        "claim": "node_id == int(directory prefix); part-N holds [N*20000,(N+1)*20000)",
        "verified": "part layout + gapless prefix range on disk",
    }


# -- The family map (tools/wikidbs_family_map.py) ---------------------------

def _family_map_module():
    """Import `tools/wikidbs_family_map.py` by path.

    A one-time corpus-preparation instrument, deliberately outside the package.
    """
    name = "wikidbs_family_map"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = resource_path(f"tools/{name}.py")
    if not path.is_file():
        raise WikiDbsIngestError(f"family-map tool not found at {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise WikiDbsIngestError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__] and fails on an unregistered module.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def family_segment_for(
    node_id: int,
    *,
    map_path: Path | None = None,
    root: Path | None = None,
) -> tuple[str, dict[str, str]]:
    """Return the verified component or fallback family segment with evidence."""
    evidence: dict[str, str] = {"node_id": f"{node_id:05d}"}
    if root is not None:
        try:
            evidence.update(verify_node_correspondence(root))
        except WikiDbsIngestError as exc:
            evidence["fallback_reason"] = f"node-id correspondence unverified: {exc}"
            return FALLBACK_FAMILY_SEGMENT, evidence
    module = _family_map_module()
    path = Path(map_path) if map_path is not None else module.default_map_path()
    if not Path(path).is_file():
        evidence["fallback_reason"] = (
            f"family map {path} absent — build it with "
            "'python tools/wikidbs_family_map.py build'"
        )
        return FALLBACK_FAMILY_SEGMENT, evidence
    fmap = module.load_map(Path(path))
    segment = fmap.family_segment(node_id)
    evidence.update(
        {
            "family_map": str(path),
            "component": segment,
            "component_size": str(fmap.sizes[segment]),
            "singleton": "yes" if fmap.is_singleton(node_id) else "no",
        }
    )
    return segment, evidence


# -- Real rows --------------------------------------------------------------

def _ident(name: str, *, fallback: str) -> str:
    """A WikiDBs name -> a SQL/file-safe identifier (deterministic)."""
    slug = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")
    if not slug or slug[0].isdigit():
        slug = f"{fallback}_{slug}" if slug else fallback
    return slug[:63]


def read_rows(db_dir: Path, table: WikiTable) -> list[list[str]]:
    """Real rows of one table from `tables/<file>.csv` (labels, never Q-ids).

    The LABEL csv only: `tables_with_item_ids/` is never opened, being the same
    provenance encoded inside the data.
    """
    path = Path(db_dir) / TABLES_DIRNAME / table.file_name
    if not path.is_file():
        raise WikiDbsIngestError(f"{db_dir}: missing data file {path}")
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise WikiDbsIngestError(f"{path}: empty CSV (fail closed)") from None
        declared = [c.column_name for c in table.columns]
        if [h.lstrip("﻿") for h in header] != declared:
            raise WikiDbsIngestError(
                f"{path}: CSV header {header} does not match schema.json "
                f"columns {declared}"
            )
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    bad = [i for i, row in enumerate(rows) if len(row) != len(declared)]
    if bad:
        raise WikiDbsIngestError(
            f"{path}: {len(bad)} row(s) have the wrong arity (e.g. row {bad[0]})"
        )
    return rows


def _integer_round_trips(value: str) -> bool:
    """Does `int(value)` print back as `value` (modulo an explicit '+')?"""
    return str(int(value)) == value.lstrip("+")


def infer_column_type(values: Iterable[str], declared: str) -> ColumnType:
    """Infer a lossless type from real values, using metadata only as fallback.

    Integer inference requires canonical round-tripping and signed 64-bit range.
    """
    if declared in _IDENTIFIER_DATA_TYPES:
        return ColumnType.TEXT
    seen = [v for v in values if v != ""]
    if seen:
        if all(_INT_RE.match(v) for v in seen):
            if any(not _integer_round_trips(v) for v in seen):
                return ColumnType.TEXT
            widest = max(abs(int(v)) for v in seen)
            if widest > _BIGINT_MAX:
                # Authority ids overflow a 64-bit integer and DuckDB refuses to
                # load them into BIGINT — TEXT is the only lossless type.
                return ColumnType.TEXT
            return (
                ColumnType.BIGINT if widest > 2_147_483_647 else ColumnType.INTEGER
            )
        if all(_FLOAT_RE.match(v) for v in seen):
            return ColumnType.DECIMAL
    return ColumnType.TEXT


def is_wikidata_id_column(values: Iterable[str]) -> bool:
    """Return whether real values are predominantly bare Wikidata IDs."""
    seen = [v for v in values if v != ""]
    if not seen:
        return False
    hits = sum(1 for v in seen if _WIKIDATA_ID_VALUE_RE.match(v))
    return hits / len(seen) >= WIKIDATA_ID_COLUMN_RATIO


def _coerce(value: str, ctype: ColumnType) -> Any:
    """Coerce one CSV cell to its emitted type, rejecting lossy conversion."""
    if value == "":
        return None
    if ctype in (ColumnType.INTEGER, ColumnType.BIGINT):
        if not _INT_RE.match(value) or not _integer_round_trips(value):
            raise WikiDbsIngestError(
                f"value {value!r} typed {ctype.value} does not round-trip through "
                "int(); coercing it would silently change the vendored data "
                "(fail closed: such a column must be TEXT)"
            )
        return int(value)
    if ctype in (ColumnType.DECIMAL, ColumnType.FLOAT):
        return float(value)
    return value


# -- Schema conversion ------------------------------------------------------

@dataclass(frozen=True)
class ValueOverlap:
    """Vendor label overlap retained as evidence when its parent is not a key."""

    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    #: parent rows, and DISTINCT non-null parent-column values; unequal is the refusal.
    parent_rows: int
    parent_distinct: int
    parent_nulls: int

    @property
    def note(self) -> str:
        return (
            f"{self.child_table}.{self.child_column} overlaps values with "
            f"{self.parent_table}.{self.parent_column}, but that column is not "
            f"a key of {self.parent_table} ({self.parent_distinct} distinct "
            f"values and {self.parent_nulls} NULLs over {self.parent_rows} "
            "rows), so it is a many-to-many value overlap, not a foreign key."
        )


@dataclass(frozen=True)
class _Converted:
    tables: tuple[TableSpec, ...]
    relationships: tuple[Relationship, ...]
    rows: dict[str, tuple[Row, ...]]
    #: emitted table name -> WikiDBs table name (evidence only)
    source_names: dict[str, str]
    #: table -> all-distinct numeric columns: identifiers, so never a measure.
    identifier_columns: dict[str, frozenset[str]]
    #: 'table.column' of every Wikidata-id column stripped at conversion.
    dropped_columns: tuple[str, ...]
    #: declared edges REFUSED as relationships (non-key parent). Evidence only.
    value_overlaps: tuple[ValueOverlap, ...] = ()
    #: declared edges dropped because no non-null child value resolves.
    witnessless_edges: tuple[str, ...] = ()


def is_key_of(rows: Sequence[Row], column: str) -> bool:
    """Return whether every shipped row has a distinct value for the column."""
    values = [r.get(column) for r in rows]
    if not values or any(v is None for v in values):
        return False
    return len(set(values)) == len(values)


def _convert(db_dir: Path, schema: WikiSchema) -> _Converted:
    """schema.json + tables/*.csv -> TableSpecs, Relationships and REAL rows."""
    name_map: dict[str, str] = {}
    used: set[str] = set()
    for i, table in enumerate(schema.tables):
        ident = _ident(table.table_name, fallback=f"table_{i}")
        base, n = ident, 2
        while ident in used:
            ident = f"{base}_{n}"
            n += 1
        used.add(ident)
        name_map[table.table_name] = ident

    # Columns that carry a declared link must survive identifier stripping.
    fk_participating: dict[str, set[str]] = {}
    for table in schema.tables:
        for fk in table.foreign_keys:
            fk_participating.setdefault(table.table_name, set()).add(fk.column_name)
            fk_participating.setdefault(fk.reference_table_name, set()).add(
                fk.reference_column_name
            )

    specs: list[TableSpec] = []
    rows_by_table: dict[str, tuple[Row, ...]] = {}
    col_map: dict[str, dict[str, str]] = {}
    dropped_columns: list[str] = []
    declared_columns = 0
    for table in schema.tables:
        raw_rows = read_rows(db_dir, table)
        if len(raw_rows) > MAX_ROWS_PER_TABLE:
            raise WikiDbsIngestError(
                f"{db_dir}: table {table.table_name!r} has {len(raw_rows)} rows, "
                f"over the {MAX_ROWS_PER_TABLE}-row ingest envelope"
            )
        tname = name_map[table.table_name]
        linked = fk_participating.get(table.table_name, frozenset())
        cols: list[ColumnSpec] = []
        kept: list[int] = []
        cmap: dict[str, str] = {}
        seen_cols: set[str] = set()
        for j, column in enumerate(table.columns):
            values = [row[j] for row in raw_rows]
            if is_wikidata_id_column(values):
                # Raw Q-ids are provenance in data clothing: drop the column —
                # unless it carries a declared link, then the file is suspect.
                if column.column_name in linked:
                    raise WikiDbsIngestError(
                        f"{db_dir}: foreign-key column "
                        f"{table.table_name}.{column.column_name} holds Wikidata "
                        "ids — this looks like tables_with_item_ids/, not tables/"
                    )
                dropped_columns.append(f"{table.table_name}.{column.column_name}")
                continue
            cident = _ident(column.column_name, fallback=f"column_{j}")
            base, n = cident, 2
            while cident in seen_cols:
                cident = f"{base}_{n}"
                n += 1
            seen_cols.add(cident)
            cmap[column.column_name] = cident
            kept.append(j)
            ctype = infer_column_type(values, column.data_type)
            nullable = any(v == "" for v in values) or not raw_rows
            cols.append(
                ColumnSpec(
                    name=cident,
                    type=ctype,
                    nullable=nullable,
                    description=(
                        f"{cident.replace('_', ' ')} of {tname.replace('_', ' ')} "
                        "(real vendored values)."
                    ),
                )
            )
        declared_columns += len(table.columns)
        if not cols:
            raise WikiDbsIngestError(
                f"{db_dir}: every column of {table.table_name!r} holds Wikidata "
                "ids — refusing to ingest an identifier-only table"
            )
        col_map[table.table_name] = cmap
        specs.append(
            TableSpec(
                name=tname,
                description=(
                    f"Source table {tname} of the {schema.database_name} database "
                    f"({len(raw_rows)} real rows)."
                ),
                columns=tuple(cols),
            )
        )
        rows_by_table[tname] = tuple(
            {
                cols[k].name: _coerce(raw[j], cols[k].type)
                for k, j in enumerate(kept)
            }
            for raw in raw_rows
        )

    if declared_columns and len(dropped_columns) * 2 >= declared_columns:
        raise WikiDbsIngestError(
            f"{db_dir}: {len(dropped_columns)} of {declared_columns} columns hold "
            "Wikidata ids — refusing (this is what reading tables_with_item_ids/ "
            "instead of tables/ looks like)"
        )

    # Keep only row-supported edges whose parent is a key. Mark dangling edges
    # optional, downgrade non-key parents to overlap, and drop unwitnessed edges.
    rels: list[Relationship] = []
    overlaps: list[ValueOverlap] = []
    witnessless: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    for table in schema.tables:
        child = name_map[table.table_name]
        for fk in table.foreign_keys:
            if fk.reference_table_name not in name_map:
                continue  # dangling reference in schema.json: not a link
            parent = name_map[fk.reference_table_name]
            if parent == child:
                continue  # self-reference: no join surface for a two-table mart
            ccol = col_map[table.table_name].get(fk.column_name)
            pcol = col_map[fk.reference_table_name].get(fk.reference_column_name)
            if ccol is None or pcol is None:
                continue
            key = (child, ccol, parent, pcol)
            if key in seen:
                continue
            seen.add(key)
            parent_rows = rows_by_table[parent]
            parent_values = {r.get(pcol) for r in parent_rows}
            child_values = [r.get(ccol) for r in rows_by_table[child]]
            if not is_key_of(parent_rows, pcol):
                # CASE C. Not a foreign key at all: a value overlap.
                raw = [r.get(pcol) for r in parent_rows]
                overlaps.append(
                    ValueOverlap(
                        child_table=child,
                        child_column=ccol,
                        parent_table=parent,
                        parent_column=pcol,
                        parent_rows=len(raw),
                        parent_distinct=len({v for v in raw if v is not None}),
                        parent_nulls=sum(1 for v in raw if v is None),
                    )
                )
                continue
            witnesses = [
                v for v in child_values if v is not None and v in parent_values
            ]
            if not witnesses:
                # CASE D. Nothing to cope with, only a join that always misses.
                witnessless.append(f"{child}.{ccol} -> {parent}.{pcol}")
                continue
            # CASES A/B. REQUIRED only when the real data satisfies it
            # everywhere; anything else is optional — where INNER shortcuts die.
            required = bool(child_values) and all(
                v is not None and v in parent_values for v in child_values
            )
            rels.append(
                Relationship(
                    child_table=child,
                    child_columns=(ccol,),
                    parent_table=parent,
                    parent_columns=(pcol,),
                    required=required,
                )
            )
    rels.sort(
        key=lambda r: (r.child_table, r.child_columns, r.parent_table, r.parent_columns)
    )
    overlaps.sort(
        key=lambda o: (o.child_table, o.child_column, o.parent_table, o.parent_column)
    )
    witnessless.sort()

    # Business keys: a referenced parent column whose real values are unique.
    keyed: list[TableSpec] = []
    for spec in specs:
        refs = sorted(
            {r.parent_columns[0] for r in rels if r.parent_table == spec.name}
        )
        business: tuple[str, ...] = ()
        for col in refs:
            values = [r.get(col) for r in rows_by_table[spec.name]]
            if values and len(set(values)) == len(values) and None not in values:
                business = (col,)
                break
        keyed.append(spec.model_copy(update={"business_key": business}) if business else spec)

    identifiers: dict[str, frozenset[str]] = {}
    for spec in keyed:
        ident_cols: set[str] = set()
        for col in spec.columns:
            if col.type not in _NUMERIC_TYPES:
                continue
            values = [
                r.get(col.name)
                for r in rows_by_table[spec.name]
                if r.get(col.name) is not None
            ]
            if values and len(set(values)) == len(values):
                ident_cols.add(col.name)
        identifiers[spec.name] = frozenset(ident_cols)

    return _Converted(
        tables=tuple(keyed),
        relationships=tuple(rels),
        rows=rows_by_table,
        source_names={v: k for k, v in name_map.items()},
        identifier_columns=identifiers,
        dropped_columns=tuple(dropped_columns),
        value_overlaps=tuple(overlaps),
        witnessless_edges=tuple(witnessless),
    )


# -- Mart construction (from the FK graph and the real data, never labels) --

def _pick_relationship(rels: tuple[Relationship, ...]) -> Relationship:
    if not rels:
        # Reachable through the PUBLIC `real_populations` too: the caller needs
        # "no join surface", not "list index out of range".
        raise WikiDbsIngestError(
            "no usable foreign keys — no join surface to pick a mart grain from"
        )
    fk_count: dict[str, int] = {}
    for r in rels:
        fk_count[r.child_table] = fk_count.get(r.child_table, 0) + 1
    fact = sorted(fk_count, key=lambda t: (-fk_count[t], t))[0]
    return sorted(
        (r for r in rels if r.child_table == fact),
        key=lambda r: (r.parent_table, r.parent_columns),
    )[0]


#: Two marts, matching the anchor's 50/22/28 distribution for 1/2/3+ models.
_MAX_MARTS = 2

_MART_PROSE: dict[str, str] = {
    "rollup": (
        "Per-{parent} roll-up of linked {bridge} rows in the {db} database, "
        "following the link on to {child}."
    ),
    "top": (
        "Per-{parent} extremes over linked {bridge} rows in the {db} database: "
        "WHICH row is largest, not how large it is."
    ),
    "bands": (
        "Per-{parent} banding of linked {bridge} rows in the {db} database, "
        "over the values that actually occur in the shipped data."
    ),
    "by_period": "Per-({parent}, month) grid over {bridge} in the {db} database.",
    "cohorts": (
        "Per-({parent}, observed status cohort) summary of linked {bridge} rows "
        "in the {db} database."
    ),
    "snapshot": (
        "Per-{parent} latest-row snapshot over linked {bridge} rows in the "
        "{db} database."
    ),
    "distribution": (
        "Per-({parent}, measure state) distribution of linked {bridge} rows in "
        "the {db} database."
    ),
}


#: Attack kinds whose only witness is a CHILDLESS PARENT row — what the carve-out builds.
_CHILDLESS_WITNESS_KINDS = frozenset(
    {AttackKind.INNER_JOIN, AttackKind.NO_NULL_DEFAULT}
)


#: Claim -> the shapes whose ELSE branch is a TOP-TIE, so the mutant is observable only there.
_TIE_WITNESS_CLAIMS: dict[str, frozenset[str]] = {
    "custom@wrong_boundary_else": frozenset({"argmax_profile"}),
}

#: Claim -> the shapes whose ELSE branches only a parent's LINK COUNT reaches:
#: has_links is 'no' at 0 links and size_band is 'large' above the top size
#: threshold. Real rows need contain neither, so the witness is measured.
_LINK_COUNT_ELSE_CLAIMS: dict[str, frozenset[str]] = {
    "custom@wrong_boundary_else": frozenset({"fan_out_rollup"}),
}


def _top_tie_rows(
    shape: StarShape,
    rows: dict[str, tuple[Row, ...]],
    *,
    distinct_labels: bool = False,
) -> int:
    """Count parents with a tied maximum under reference-plan semantics.

    With ``distinct_labels``, tied rows must carry different labels so the
    deterministic label tie-break is exercised.
    """
    if not (shape.parent_keys and shape.fact and shape.fact_link_columns):
        return 0
    measure = shape.roles.measure
    if not measure:
        return 0
    label = shape.roles.label
    parent_key = shape.parent_keys[0]
    link = shape.fact_link_columns[0]
    keys = {r.get(parent_key) for r in rows.get(shape.parent, ())}
    keys.discard(None)
    by_parent: dict[object, list[tuple[object, object, str]]] = {}
    for row in rows.get(shape.fact, ()):
        key = row.get(link)
        value = row.get(measure)
        if key in keys and value is not None:
            by_parent.setdefault(key, []).append(
                (value, row.get(label) if label else None, canonical_json(row))
            )
    tied = 0
    for entries in by_parent.values():
        top = max(value for value, _, _ in entries)
        at_top = [(lab, fp) for value, lab, fp in entries if value == top]
        if not distinct_labels:
            if len(at_top) >= 2:
                tied += 1
            continue
        if len({fp for _, fp in at_top}) >= 2 and len({lab for lab, _ in at_top}) >= 2:
            tied += 1
    return tied


def _all_missing_measure_parents(
    shape: StarShape, rows: dict[str, tuple[Row, ...]]
) -> int:
    """Count parents whose linked fact rows all lack a measure value."""
    if not (shape.parent_keys and shape.fact and shape.fact_link_columns):
        return 0
    measure = shape.roles.measure
    if not measure:
        return 0
    parent_key = shape.parent_keys[0]
    link = shape.fact_link_columns[0]
    keys = {r.get(parent_key) for r in rows.get(shape.parent, ())}
    keys.discard(None)
    linked: dict[object, bool] = {}
    for row in rows.get(shape.fact, ()):
        key = row.get(link)
        if key not in keys:
            continue
        linked[key] = linked.get(key, False) or row.get(measure) is not None
    return sum(1 for has_value in linked.values() if not has_value)


_COPIED_ATTRIBUTE_RE = re.compile(
    r'(?P<table>[A-Za-z_][A-Za-z0-9_]*)\."(?P<column>[^"]+)"\s+AS\s+"(?P<alias>[^"]+)"'
)


def _copied_attribute_conditions(
    marts: tuple[MartSpec, ...], rows: dict[str, tuple[Row, ...]]
) -> tuple[str, ...]:
    """Describe measured null witnesses for a copied parent attribute."""
    lines: list[str] = []
    for mart in marts:
        passthrough = {
            c.name for c in mart.columns if c.kind is MartColumnKind.PASSTHROUGH
        }
        for op in mart.plan.ops:
            if op.kind is not MartOpKind.DERIVE:
                continue
            select = str(op.details.get("select", ""))
            for match in _COPIED_ATTRIBUTE_RE.finditer(select):
                table, column, alias = (
                    match.group("table"), match.group("column"), match.group("alias")
                )
                if alias not in passthrough or alias in mart.key_columns:
                    continue
                table_rows = rows.get(table) or ()
                if table_rows and any(r.get(column) is None for r in table_rows):
                    lines.append(
                        f"MISSING-ATTRIBUTE WITNESS: at least one {table} row has "
                        f"no {column} value in this population, so {alias} of "
                        f"mart {mart.name} is missing for that row."
                    )
            break
    return tuple(dict.fromkeys(lines))


def _tie_witness_conditions(
    shapes: tuple[StarShape, ...], rows: dict[str, tuple[Row, ...]]
) -> tuple[str, ...]:
    """Describe measured tie and all-missing witnesses for argmax review."""
    ladders = frozenset().union(*_TIE_WITNESS_CLAIMS.values())
    lines: list[str] = []
    for shape in shapes:
        if shape.shape_name not in ladders:
            continue
        if not (shape.fact and shape.parent and shape.roles.measure):
            continue
        measure, label = shape.roles.measure, shape.roles.label
        if label and _top_tie_rows(shape, rows, distinct_labels=True):
            lines.append(
                f"TIE WITNESS: {shape.fact} has at least two distinct real rows "
                f"linked to the same {shape.parent} row that share that row's "
                f"largest non-missing {measure} value and differ in {label} in "
                f"this population, so the tie-break on {label} decides which of "
                "them is reported."
            )
        if _all_missing_measure_parents(shape, rows):
            lines.append(
                f"ALL-MISSING-MEASURE WITNESS: at least one {shape.parent} row "
                f"has linked {shape.fact} rows and none of them carries a "
                f"{measure} value in this population."
            )
    return tuple(dict.fromkeys(lines))


def _tie_witness_population(
    shapes: tuple[StarShape, ...],
    rows: dict[str, tuple[Row, ...]],
    stress_rows: dict[str, tuple[Row, ...]],
) -> PopulationName | None:
    """Return the measured primary or stress population with a tied maximum."""
    ladders = _TIE_WITNESS_CLAIMS["custom@wrong_boundary_else"]
    tie_shapes = tuple(s for s in shapes if s.shape_name in ladders)
    if not tie_shapes:
        return None
    if any(_top_tie_rows(s, rows) for s in tie_shapes):
        return PopulationName.PRIMARY
    if any(_top_tie_rows(s, stress_rows) for s in tie_shapes):
        return PopulationName.STRESS
    return None


def _link_count_else_parents(
    shape: StarShape, rows: dict[str, tuple[Row, ...]]
) -> int:
    """Count parents whose link count reaches a roll-up ladder's ELSE branch.

    That is a parent with no linked row (has_links 'no') or with more linked
    rows than the top size threshold (size_band 'large'), counted the way the
    reference plan counts: rows with a link key, each byte-identical row once
    when the plan deduplicates the bridge.
    """
    if not (shape.parent_keys and shape.fact and shape.fact_link_columns):
        return 0
    parent_key = shape.parent_keys[0]
    link = shape.fact_link_columns[0]
    link_key = shape.roles.link_key
    top = max(shape.thresholds) if shape.thresholds else None
    fact_rows = rows.get(shape.fact, ())
    if shape.fact_dedupe:
        fact_rows = tuple({canonical_json(dict(r)): r for r in fact_rows}.values())
    counts: dict[object, int] = {}
    for row in fact_rows:
        if link_key and row.get(link_key) is None:
            continue
        counts[row.get(link)] = counts.get(row.get(link), 0) + 1
    witnesses = 0
    for parent in rows.get(shape.parent, ()):
        key = parent.get(parent_key)
        # A missing parent key matches no link, so that parent has 0 links.
        count = 0 if key is None else counts.get(key, 0)
        if count == 0 or (top is not None and count > top):
            witnesses += 1
    return witnesses


def _link_count_else_population(
    shapes: tuple[StarShape, ...],
    rows: dict[str, tuple[Row, ...]],
    stress_rows: dict[str, tuple[Row, ...]],
    counterfactual_rows: dict[str, tuple[Row, ...]],
) -> PopulationName | None:
    """Return the first shipped population with a parent a roll-up ELSE reaches.

    Primary, then stress, then the counterfactual carve-out, which is where a
    real pool gets its childless parents when the vendor rows have none.
    """
    ladders = _LINK_COUNT_ELSE_CLAIMS["custom@wrong_boundary_else"]
    rollups = tuple(s for s in shapes if s.shape_name in ladders)
    if not rollups:
        return None
    for population, population_rows in (
        (PopulationName.PRIMARY, rows),
        (PopulationName.STRESS, stress_rows),
        (PopulationName.COUNTERFACTUAL, counterfactual_rows),
    ):
        if any(_link_count_else_parents(s, population_rows) for s in rollups):
            return population
    return None


def _attack_cases(
    shapes: tuple[StarShape, ...],
    *,
    backends: int,
    policy: str,
    tables: tuple[TableSpec, ...] = (),
    backend_assignments: tuple[BackendAssignment, ...] = (),
    manufactured_childless: bool = False,
    tie_witness: PopulationName | None = None,
    link_count_else_witness: PopulationName | None = None,
) -> tuple:
    """Point shared attacks at populations containing their measured witnesses.

    Manufactured childless cases remain counterfactual; tie cases use the
    measured tie population, and roll-up ELSE cases the measured population
    with a parent at 0 links or above the top size threshold. Missing
    witnesses are not silently removed.
    """
    plan_level = {
        AttackKind.CONSTANTS,
        AttackKind.KEYS_ONLY,
        AttackKind.NO_OP,
        AttackKind.SKIP_EXTRACTION,
    }
    # Which claim each case came from and which shapes declared it: re-pointing
    # must read the SHAPE, not the kind (two shapes can share one case name).
    claim_shapes: dict[str, set[str]] = {}
    for shape in shapes:
        for claim in shape.attack_claims:
            claim_shapes.setdefault(claim.replace("@", "__"), set()).add(
                shape.shape_name
            )
    tie_only = {
        claim.replace("@", "__")
        for claim, ladders in _TIE_WITNESS_CLAIMS.items()
        if claim.replace("@", "__") in claim_shapes
        and claim_shapes[claim.replace("@", "__")] <= ladders
    }
    link_count_else_only = {
        claim.replace("@", "__")
        for claim, ladders in _LINK_COUNT_ELSE_CLAIMS.items()
        if claim.replace("@", "__") in claim_shapes
        and claim_shapes[claim.replace("@", "__")] <= ladders
    }
    cases: dict[str, object] = {}
    for case in derive_attack_cases(
        shapes,
        backends=backends,
        policy=policy,
        tables=tables,
        backend_assignments=backend_assignments,
    ):
        if case.name in tie_only and tie_witness is not None:
            cases[case.name] = case.model_copy(
                update={
                    "expected_pass": {tie_witness: False},
                    # The shared catalogue text names row G; this shape's ELSE
                    # branch is 'tied', so name the row that actually kills it.
                    "description": (
                        "The mandatory ELSE dropped from every CASE ladder. On "
                        "this shape the ELSE branch is the TIED state, so the "
                        "mutant reports NULL exactly where two or more rows "
                        "hold a parent's maximum measure and is invisible "
                        "everywhere else. Witnessed on the "
                        f"{tie_witness.value} population, measured: it is the "
                        "one shipped population with a tied maximum."
                    ),
                }
            )
            continue
        if case.name in link_count_else_only and link_count_else_witness is not None:
            cases[case.name] = case.model_copy(
                update={
                    "expected_pass": {link_count_else_witness: False},
                    # The shared catalogue text names synthetic row G; on real
                    # rows the ELSE branches are reached only by link counts.
                    "description": (
                        "The mandatory ELSE dropped from every CASE ladder. On "
                        "this shape the ELSE branches are has_links 'no', for a "
                        "parent with no linked row, and size_band 'large', for "
                        "a parent above the top size threshold, so the mutant "
                        "reports NULL only for those parents. Witnessed on the "
                        f"{link_count_else_witness.value} population, measured: "
                        "it is the first shipped population with such a parent."
                    ),
                }
            )
            continue
        if (
            case.kind not in plan_level
            and case.expected_pass.get(PopulationName.COUNTERFACTUAL) is False
        ):
            case = case.model_copy(
                update={
                    "expected_pass": (
                        # The childless witness exists ONLY in the carve-out;
                        # a PRIMARY claim here would be the false one.
                        {PopulationName.COUNTERFACTUAL: False}
                        if manufactured_childless
                        and case.kind in _CHILDLESS_WITNESS_KINDS
                        else {PopulationName.PRIMARY: False}
                    )
                }
            )
        cases[case.name] = case
    return tuple(cases[k] for k in sorted(cases))


def _effective_links(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    rows: dict[str, tuple[Row, ...]],
) -> dict[tuple[str, str, str, str], tuple[int, int]]:
    """Return witnessed fan-out and maximum graded childless count per edge.

    Fan-out comes from primary rows; counterfactual carve-outs may increase only
    the childless count.
    """
    plain = {k: list(v) for k, v in rows.items()}
    links = dict(link_statistics(rels, plain))
    counter, counter_restricted = _counterfactual_rows(tables, rels, rows)
    if not counter_restricted:
        # The carve-out fell back to the real rows: nothing was manufactured.
        return links
    counter_links = link_statistics(
        rels, {k: list(v) for k, v in counter.items()}
    )
    return {
        edge: (fanout, max(childless, counter_links.get(edge, (0, 0))[1]))
        for edge, (fanout, childless) in links.items()
    }


def _renderable_domains(
    domains: dict[tuple[str, str], tuple[str, ...]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Return observed domains whose complete value set is SQL-renderable."""
    return {
        key: values
        for key, values in domains.items()
        if all("'" not in v for v in values)
    }


def _dedupe_faithful_marts(
    marts: tuple[MartSpec, ...],
    shapes: tuple[StarShape, ...],
    rows: dict[str, tuple[Row, ...]],
) -> tuple[tuple[MartSpec, ...], tuple[StarShape, ...]]:
    """Keep marts whose dedupe keys collapse only byte-identical source rows."""
    kept_marts: list[MartSpec] = []
    kept_shapes: list[StarShape] = []
    for mart, shape in zip(marts, shapes):
        faithful = True
        for op in mart.plan.ops:
            if op.kind is not MartOpKind.DEDUPE:
                continue
            table = op.tables[0] if op.tables else ""
            table_rows = rows.get(table) or ()
            # The compiler spells an empty column tuple as SELECT DISTINCT *.
            # That is byte-for-byte dedupe by construction, not an empty
            # projection in which every row collapses to the same object.
            if not op.columns:
                continue
            projection = tuple(dict.fromkeys(op.columns))
            full = {canonical_json(dict(r)) for r in table_rows}
            projected = {
                canonical_json({c: r.get(c) for c in projection})
                for r in table_rows
            }
            if len(projected) != len(full):
                faithful = False
                break
        if faithful:
            kept_marts.append(mart)
            kept_shapes.append(shape)
    return tuple(kept_marts), tuple(kept_shapes)


# WikiDBs claims must be witnessed by admitted real rows, including a verified
# childless parent; synthetic witness assumptions do not apply.
_REAL_ROW_SAFE_CLAIMS: dict[str, frozenset[str]] = {
    "status_cohort_union": frozenset(
        {"dropped_filter", "inner_join", "no_null_default"}
    ),
    "latest_snapshot": frozenset({"inner_join", "no_null_default"}),
    "measure_state_distribution": frozenset(
        {"dropped_filter", "inner_join", "no_null_default"}
    ),
}


def _real_row_safe_shapes(shapes: tuple[StarShape, ...]) -> tuple[StarShape, ...]:
    """Prune claims that need synthetic witnesses this pool does not invent."""
    out: list[StarShape] = []
    for shape in shapes:
        safe = _REAL_ROW_SAFE_CLAIMS.get(shape.shape_name)
        out.append(
            shape
            if safe is None
            else replace(
                shape,
                attack_claims=tuple(c for c in shape.attack_claims if c in safe),
            )
        )
    return tuple(out)


def _chain_candidates_for(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    rows: dict[str, tuple[Row, ...]],
    identifier_columns: dict[str, frozenset[str]] | None = None,
):
    """Return row-funded chain candidates used by ranking and ingest.

    Parent keyness is measured from shipped rows; identifiers cannot be measures.
    """
    plain = {k: list(v) for k, v in rows.items()}
    key_parents = frozenset(
        (r.parent_table, r.parent_columns[0])
        for r in rels
        if len(r.parent_columns) == 1
        and is_key_of(rows.get(r.parent_table) or (), r.parent_columns[0])
    )
    return chain_candidates(
        tables,
        rels,
        observed=_renderable_domains(observed_domains(tables, plain)),
        links=_effective_links(tables, rels, rows),
        key_parents=key_parents,
        exclude_measures=identifier_columns,
    )


def _build_marts(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    rows: dict[str, tuple[Row, ...]],
    database_name: str,
    identifier_columns: dict[str, frozenset[str]] | None = None,
    *,
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> tuple[tuple[MartSpec, ...], tuple[StarShape, ...]]:
    """Build marts from declared edges and measured shipped values.

    Gold-dependent rules are stated in column descriptions. Domains and parent
    keyness are measured from vendor rows.
    """
    candidates = _chain_candidates_for(tables, rels, rows, identifier_columns)
    if candidates:
        max_marts, spread_grains, max_per_shape = difficulty_profile.mart_parameters(
            pool="wikidbs", default_budget=_MAX_MARTS
        )
        marts, shapes, _names = build_marts(
            candidates,
            prefix=lambda e, suffix: _ident(
                f"{e.parent}_{e.bridge}_{suffix}", fallback="mart"
            ),
            max_marts=max_marts,
            spread_grains=spread_grains,
            max_per_shape=max_per_shape,
            description=lambda suffix, e: _MART_PROSE[suffix].format(
                parent=e.parent,
                bridge=e.bridge,
                child=e.child or e.bridge,
                db=database_name,
            ),
            notes=(
                "Constructed from the declared foreign-key graph and the real "
                "row values only; no Wikidata provenance was consulted."
            ),
        )
        marts, shapes = _dedupe_faithful_marts(marts, shapes, rows)
        shapes = _real_row_safe_shapes(shapes)
        # Require a real maximum tie with different labels so the declared
        # tie-break is observable; otherwise select another shape.
        ladders = frozenset().union(*_TIE_WITNESS_CLAIMS.values())
        unwitnessed = [
            shape.shape_name
            for shape in shapes
            if shape.shape_name in ladders
            and _top_tie_rows(shape, rows, distinct_labels=True) == 0
        ]
        if unwitnessed:
            from elt_taskgen.adapters.evidence import SHAPE_ORDER
            from elt_taskgen.generation.mart_plan import SHAPE_REGISTRY

            blocked = {
                registration.adapter_suffix
                for registration in SHAPE_REGISTRY
                if registration.shape_name in ladders
            }
            marts, shapes, _names = build_marts(
                candidates,
                prefix=lambda e, suffix: _ident(
                    f"{e.parent}_{e.bridge}_{suffix}", fallback="mart"
                ),
                max_marts=max_marts,
                spread_grains=spread_grains,
                max_per_shape=max_per_shape,
                allow=[suffix for suffix, _ in SHAPE_ORDER if suffix not in blocked],
                description=lambda suffix, e: _MART_PROSE[suffix].format(
                    parent=e.parent,
                    bridge=e.bridge,
                    child=e.child or e.bridge,
                    db=database_name,
                ),
                notes=(
                    "Constructed from the declared foreign-key graph and the real "
                    "row values only; no Wikidata provenance was consulted."
                ),
            )
            marts, shapes = _dedupe_faithful_marts(marts, shapes, rows)
            shapes = _real_row_safe_shapes(shapes)
        if marts:
            return marts, shapes
    mart, shape = _build_legacy_star(
        tables,
        rels,
        database_name,
        identifier_columns,
        rows=rows,
        minimum_columns=(
            MIN_MART_COLUMNS if difficulty_profile.name == "challenging" else 0
        ),
    )
    return (mart,), (shape,)


def _build_legacy_star(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    database_name: str,
    identifier_columns: dict[str, frozenset[str]] | None = None,
    *,
    rows: dict[str, tuple[Row, ...]] | None = None,
    minimum_columns: int = 0,
) -> tuple[MartSpec, StarShape]:
    """Build a rollup after removing edges whose measured parent keys repeat."""
    if rows is not None:
        usable = tuple(
            r
            for r in rels
            if len(r.parent_columns) == 1
            and is_key_of(rows.get(r.parent_table) or (), r.parent_columns[0])
        )
        if not usable:
            raise WikiDbsIngestError(
                "no declared link whose parent column is a key of the shipped "
                "rows — every candidate grain would repeat, so every count "
                "would be inflated (fail closed)"
            )
        rels = usable
    rel = _pick_relationship(rels)
    by_name = {t.name: t for t in tables}
    parent, fact = by_name[rel.parent_table], by_name[rel.child_table]
    key_col = rel.parent_columns[0]

    fk_cols = {c for r in rels if r.child_table == fact.name for c in r.child_columns}
    identifiers = (identifier_columns or {}).get(fact.name, frozenset())
    measure = next(
        (
            c
            for c in fact.columns
            if c.type in _NUMERIC_TYPES
            and c.name not in fk_cols
            and c.name not in identifiers
        ),
        None,
    )
    profile_candidates = tuple(
        c
        for c in fact.columns
        if c.name not in fk_cols
        and c.name not in identifiers
        and c.type
        in {
            ColumnType.INTEGER,
            ColumnType.BIGINT,
            ColumnType.FLOAT,
            ColumnType.DECIMAL,
            ColumnType.TEXT,
            ColumnType.DATE,
            ColumnType.TIMESTAMP,
        }
    )

    fact_rows = tuple((rows or {}).get(fact.name, ()))
    parent_values = {
        tuple(row.get(column) for column in rel.parent_columns)
        for row in (rows or {}).get(parent.name, ())
    }
    linked_rows = tuple(
        row
        for row in fact_rows
        if (
            all(row.get(column) is not None for column in rel.child_columns)
            and tuple(row.get(column) for column in rel.child_columns)
            in parent_values
        )
    )

    def profile_rank(column: ColumnSpec) -> tuple[bool, bool, bool, bool, bool]:
        """Rank profiles by joined null and same-group repetition witnesses.

        Source column order breaks ties deterministically.
        """

        values = tuple(row.get(column.name) for row in linked_rows)
        non_null = tuple(value for value in values if value is not None)
        grouped: dict[tuple[Any, ...], dict[Any, set[str]]] = {}
        for row in linked_rows:
            value = row.get(column.name)
            if value is None:
                continue
            group = tuple(row.get(key) for key in rel.child_columns)
            grouped.setdefault(group, {}).setdefault(value, set()).add(
                canonical_json(row)
            )
        mixed_null = bool(non_null) and len(non_null) != len(values)
        repeated_non_null = any(
            len(distinct_rows) >= 2
            for by_value in grouped.values()
            for distinct_rows in by_value.values()
        )
        return (
            mixed_null and repeated_non_null,
            mixed_null,
            repeated_non_null,
            len(set(non_null)) > 1,
            bool(non_null),
        )

    profile = (
        max(profile_candidates, key=profile_rank)
        if profile_candidates
        else None
    )

    mart_name = _ident(f"{parent.name}_{fact.name}_summary", fallback="mart")
    count_col = _ident(f"{fact.name}_count", fallback="row_count")
    # The dedupe must be in the COLUMN'S OWN description: the exported bundle
    # ships column descriptions, NOT plan ops, so an op-only rule never lands.
    deduped = not fact.primary_key
    count_description = (
        (
            f"Number of DISTINCT {fact.name} rows linked to this {parent.name} "
            f"row; 0 when none. {fact.name} declares no primary key upstream and "
            "byte-identical duplicate rows occur in the source, and they count "
            "ONCE."
        )
        if deduped
        else (
            f"Number of {fact.name} rows linked to this {parent.name} row; "
            "0 when none."
        )
    )
    columns = [
        MartColumn(
            name=key_col,
            type=parent.column(key_col).type,
            description=f"Key of {parent.name}: {key_col}.",
            kind=MartColumnKind.PASSTHROUGH,
        ),
        MartColumn(
            name=count_col,
            type=ColumnType.INTEGER,
            description=count_description,
            kind=MartColumnKind.AGGREGATED,
        ),
    ]
    link_alias = _ident(f"{fact.name}__{rel.child_columns[0]}", fallback="fact_link")
    carry = [(rel.child_columns[0], link_alias)]
    measures = [Measure(column=count_col, expr=f"COUNT({quote(link_alias)})")]
    used_columns = {column.name for column in columns}

    def output_name(base: str) -> str:
        candidate = _ident(base, fallback="metric")
        suffix = 2
        while candidate in used_columns:
            candidate = _ident(f"{base}_{suffix}", fallback=f"metric_{suffix}")
            suffix += 1
        used_columns.add(candidate)
        return candidate

    if measure is not None:
        total_col = _ident(f"total_{measure.name}", fallback="total_measure")
        columns.append(
            MartColumn(
                name=total_col,
                type=measure.type,
                description=(
                    (
                        f"Sum of {fact.name}.{measure.name} over those same "
                        "DISTINCT linked rows; 0 when none."
                    )
                    if deduped
                    else (
                        f"Sum of {fact.name}.{measure.name} over the linked rows; "
                        "0 when none."
                    )
                ),
                kind=MartColumnKind.AGGREGATED,
            )
        )
        measure_alias = _ident(f"{fact.name}__{measure.name}", fallback="fact_measure")
        carry.append((measure.name, measure_alias))
        measures.append(
            Measure(
                column=total_col,
                expr=f"SUM({quote(measure_alias)})",
                null_default="0",
            )
        )

    # Profile real fact attributes instead of padding fallback marts.
    if profile is not None:
        profile_alias = _ident(
            f"{fact.name}__{profile.name}", fallback="fact_profile"
        )
        carry.append((profile.name, profile_alias))
        distinct_col = output_name(f"distinct_{profile.name}_count")
        non_null_col = output_name(f"non_null_{profile.name}_count")
        first_col = output_name(f"first_{profile.name}")
        last_col = output_name(f"last_{profile.name}")
        row_scope = "those same DISTINCT linked rows" if deduped else "the linked rows"
        profile_metrics = (
            (
                distinct_col,
                ColumnType.BIGINT,
                f"COUNT(DISTINCT {quote(profile_alias)})",
                f"Number of different non-missing {fact.name}.{profile.name} values over {row_scope}; 0 when none.",
            ),
            (
                non_null_col,
                ColumnType.BIGINT,
                f"COUNT({quote(profile_alias)})",
                f"Number of {row_scope} whose {profile.name} value is present; 0 when none.",
            ),
            (
                first_col,
                profile.type,
                f"MIN({quote(profile_alias)})",
                f"Smallest {fact.name}.{profile.name} value over {row_scope}; missing when none is present.",
            ),
            (
                last_col,
                profile.type,
                f"MAX({quote(profile_alias)})",
                f"Largest {fact.name}.{profile.name} value over {row_scope}; missing when none is present.",
            ),
        )
        for column, column_type, expression, description in profile_metrics:
            columns.append(
                MartColumn(
                    name=column,
                    type=column_type,
                    description=description,
                    kind=MartColumnKind.AGGREGATED,
                )
            )
            measures.append(Measure(column=column, expr=expression))

    if minimum_columns and len(columns) < minimum_columns:
        raise WikiDbsIngestError(
            f"legacy mart {mart_name!r} can derive only {len(columns)} meaningful "
            f"columns from {fact.name!r}, below the challenging minimum of "
            f"{minimum_columns}; choose a database with a richer linked fact table"
        )

    built = build_star(
        mart=mart_name,
        parent=parent.name,
        parent_keys=(key_col,),
        key_columns=(key_col,),
        joins=(
            StarJoin(
                table=fact.name,
                on_pairs=((key_col, rel.child_columns[0]),),
                carry=tuple(carry),
                rel_columns=rel.parent_columns + rel.child_columns,
                # Outcome wording, not "LEFT JOIN": declarative_prose bans
                # operator words here. join_type still says LEFT.
                description=(
                    f"Bring in {fact.name} against {parent.name}: {parent.name} "
                    f"rows with no matching {fact.name} rows are retained."
                ),
            ),
        ),
        measures=tuple(measures),
        dedupe=(
            (fact.name, tuple(c.name for c in fact.columns))
            if not fact.primary_key
            else ()
        ),
        notes=(
            "Constructed from the declared foreign-key graph and the real "
            "row values only; no Wikidata provenance was consulted."
        ),
    )
    return (
        MartSpec(
            name=mart_name,
            description=(
                f"Per-{parent.name} summary of linked {fact.name} activity in the "
                f"{database_name} database."
            ),
            grain=(
                f"One row per {parent.name} ({key_col}), including rows with no "
                f"{fact.name}."
            ),
            key_columns=(key_col,),
            columns=tuple(columns),
            plan=built.plan,
        ),
        built.shape,
    )


# -- Populations: the REAL-DATA policy --------------------------------------

def _stable_shuffle(rows: tuple[Row, ...], salt: str) -> tuple[Row, ...]:
    """Deterministic reorder with no RNG: sort by hash(row + salt)."""
    return tuple(
        sorted(rows, key=lambda r: sha256_hex(salt + "\x1f" + canonical_json(r)))
    )


def _close_required_links(
    rels: tuple[Relationship, ...], kept: dict[str, tuple[Row, ...]]
) -> dict[str, tuple[Row, ...]]:
    """Drop child rows whose REQUIRED parent row is no longer present.

    Removing rows is only legal while the required links still hold; cascades
    to a fixed point.
    """
    out = dict(kept)
    for _ in range(len(out) + 1):
        changed = False
        for rel in rels:
            if not rel.required:
                continue
            parent_keys = {
                tuple(r.get(c) for c in rel.parent_columns) for r in out[rel.parent_table]
            }
            survivors = tuple(
                r
                for r in out[rel.child_table]
                if tuple(r.get(c) for c in rel.child_columns) in parent_keys
            )
            if len(survivors) != len(out[rel.child_table]):
                out[rel.child_table] = survivors
                changed = True
        if not changed:
            break
    return out


def _counterfactual_rows(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    rows: dict[str, tuple[Row, ...]],
) -> tuple[dict[str, tuple[Row, ...]], bool]:
    """Remove fact rows to create unmatched dimensions without emptying tables."""
    mart_rel = _pick_relationship(rels)
    parent_rows = rows[mart_rel.parent_table]
    for fraction in COUNTERFACTUAL_FRACTIONS:
        cut = max(1, int(len(parent_rows) * fraction))
        orphaned = {
            tuple(r.get(c) for c in mart_rel.parent_columns) for r in parent_rows[:cut]
        }
        candidate = {t.name: rows[t.name] for t in tables}
        candidate[mart_rel.child_table] = tuple(
            r
            for r in candidate[mart_rel.child_table]
            if tuple(r.get(c) for c in mart_rel.child_columns) not in orphaned
        )
        candidate = _close_required_links(rels, candidate)
        if all(candidate[t.name] for t in tables if rows[t.name]):
            return candidate, True
    return {t.name: rows[t.name] for t in tables}, False


def _counterfactual_optional_dangling(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    rows: dict[str, tuple[Row, ...]],
    shapes: tuple[StarShape, ...] = (),
) -> tuple[dict[str, tuple[Row, ...]], bool]:
    """Create one real dangling optional link for an emitted child-to-parent join.

    Remove only an eligible parent, retain its child, re-close required links,
    and keep every nonempty source table nonempty. Return true only on removal.
    """

    out = {table.name: tuple(rows.get(table.name, ())) for table in tables}
    if not rels or not shapes:
        return out, False

    by_edge = {
        (
            rel.child_table,
            rel.child_columns,
            rel.parent_table,
            rel.parent_columns,
        ): rel
        for rel in rels
        if not rel.required
    }
    ordered: list[Relationship] = []
    seen: set[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = set()

    def admit(
        left_table: str,
        left_columns: tuple[str, ...],
        right_table: str,
        right_columns: tuple[str, ...],
    ) -> None:
        key = (left_table, left_columns, right_table, right_columns)
        rel = by_edge.get(key)
        if rel is not None and key not in seen:
            seen.add(key)
            ordered.append(rel)

    for shape in shapes:
        # First emitted hop.  It is eligible only for shapes whose base is the
        # FK-holding child (for example a bridge-grained temporal mart).
        if (
            shape.has_join
            and shape.parent
            and shape.parent_keys
            and shape.fact
            and shape.fact_link_columns
        ):
            admit(
                shape.parent,
                shape.parent_keys,
                shape.fact,
                shape.fact_link_columns,
            )
        # A one-hop child-grained shape can use a computed grain, leaving
        # `parent_keys` empty.  Its explicit child-link pairs still record the
        # real base-table FK -> dimension-key edge (orphan_coverage).
        if (
            shape.join_hops == 1
            and shape.parent
            and shape.child
            and shape.child == shape.fact
            and shape.child_link_pairs
        ):
            admit(
                shape.parent,
                tuple(left for left, _right in shape.child_link_pairs),
                shape.child,
                tuple(right for _left, right in shape.child_link_pairs),
            )
        # Second emitted hop of the ordinary P <- B -> D chain: B is on the
        # LEFT and its optional dimension D is on the RIGHT.
        if shape.join_hops >= 2 and shape.fact and shape.child:
            admit(
                shape.fact,
                tuple(left for left, _right in shape.child_link_pairs),
                shape.child,
                tuple(right for _left, right in shape.child_link_pairs),
            )
    originally_nonempty = {
        table.name for table in tables if out.get(table.name)
    }

    for rel in ordered:
        parent_rows = out.get(rel.parent_table, ())
        child_rows = out.get(rel.child_table, ())
        if len(parent_rows) < 2 or not child_rows:
            continue

        parent_keys = {
            tuple(row.get(column) for column in rel.parent_columns)
            for row in parent_rows
        }
        child_keys = tuple(
            tuple(row.get(column) for column in rel.child_columns)
            for row in child_rows
            if all(row.get(column) is not None for column in rel.child_columns)
        )
        # This exact mart edge already exercises the semantics.  Keep looking:
        # a later emitted mart may still need its own second-hop witness.
        if any(key not in parent_keys for key in child_keys):
            continue

        linked_keys = set(child_keys) & parent_keys
        for parent_row in parent_rows:
            removed_key = tuple(
                parent_row.get(column) for column in rel.parent_columns
            )
            if (
                not all(value is not None for value in removed_key)
                or removed_key not in linked_keys
            ):
                continue
            trial = dict(out)
            trial[rel.parent_table] = tuple(
                row
                for row in parent_rows
                if tuple(row.get(column) for column in rel.parent_columns)
                != removed_key
            )
            # Relationship admission proves the parent columns are a key, but
            # keep this helper safe for direct/test callers as well.
            if len(trial[rel.parent_table]) != len(parent_rows) - 1:
                continue
            trial = _close_required_links(rels, trial)
            if any(not trial.get(name) for name in originally_nonempty):
                continue
            surviving_parent_keys = {
                tuple(row.get(column) for column in rel.parent_columns)
                for row in trial.get(rel.parent_table, ())
            }
            if any(
                tuple(row.get(column) for column in rel.child_columns)
                == removed_key
                and removed_key not in surviving_parent_keys
                for row in trial.get(rel.child_table, ())
            ):
                return trial, True
    return out, False


#: Cap on per-edge dangling sentences per population, so prose cannot be buried.
_MAX_DANGLING_CONDITIONS = 4


def _dangling_conditions(
    rels: tuple[Relationship, ...], rows: dict[str, tuple[Row, ...]]
) -> tuple[str, ...]:
    """Describe measured missing and dangling optional links per population."""
    lines: list[str] = []
    extra = 0

    def add(line: str) -> None:
        nonlocal extra
        if len(lines) < _MAX_DANGLING_CONDITIONS:
            lines.append(line)
        else:
            extra += 1

    for rel in rels:
        parent_rows = rows.get(rel.parent_table) or ()
        child_rows = rows.get(rel.child_table) or ()
        pcol, ccol = rel.parent_columns[0], rel.child_columns[0]
        parent_values = {r.get(pcol) for r in parent_rows}
        missing = [r for r in child_rows if r.get(ccol) is None]
        if missing:
            add(
                f"MISSING LINK WITNESS: {rel.child_table} has {len(missing)} real "
                f"row(s) whose {ccol} value is missing in this population; those "
                f"rows match no {rel.parent_table}.{pcol} value in this "
                "population."
            )
        non_null = [r.get(ccol) for r in child_rows if r.get(ccol) is not None]
        dangling = [v for v in non_null if v not in parent_values]
        if not dangling:
            continue
        # Asymmetric with `_convert`'s CASE D on purpose: that judges the EDGE,
        # this states what THIS population hands the solver.
        pct = 100.0 * len(dangling) / len(non_null)
        add(
            f"DANGLING LINK: {rel.child_table}.{ccol} references "
            f"{rel.parent_table}.{pcol} optionally — {len(dangling)} of "
            f"{len(non_null)} non-null {rel.child_table}.{ccol} values "
            f"({pct:.1f}%) have no matching {rel.parent_table} row among the "
            "real rows retained in this population, so a LEFT JOIN and an "
            "explicit default are required; an INNER JOIN silently drops "
            "those rows."
        )
    if extra:
        lines.append(
            f"{extra} further declared missing/dangling link condition(s) also "
            "occur; "
            "every link in this task is optional unless its description says "
            "otherwise."
        )
    return tuple(lines)


_COUNT_DISTINCT_ALIAS_RE = re.compile(
    r'\bCOUNT\s*\(\s*DISTINCT\s+"(?P<alias>[^"]+)"\s*\)', re.IGNORECASE
)
_SOURCE_ALIAS_RE = re.compile(
    r'(?P<table>[A-Za-z_][A-Za-z0-9_]*)\."(?P<column>[^"]+)"'
    r'\s+AS\s+"(?P<alias>[^"]+)"',
    re.IGNORECASE,
)


def _profile_witness_conditions(
    marts: tuple[MartSpec, ...],
    rels: tuple[Relationship, ...],
    rows: dict[str, tuple[Row, ...]],
) -> tuple[str, ...]:
    """Describe measured mixed-null and same-group repetition witnesses.

    Bind only aliases used by executable ``COUNT(DISTINCT ...)`` projections;
    disclose neither source values nor exact counts.
    """
    lines: list[str] = []
    seen: set[tuple[str, str, str, tuple[str, ...], tuple[str, ...]]] = set()
    for mart in marts:
        distinct_aliases = {
            match.group("alias")
            for op in mart.plan.ops
            for value in op.details.values()
            for match in _COUNT_DISTINCT_ALIAS_RE.finditer(str(value))
        }
        if not distinct_aliases:
            continue

        for op in mart.plan.ops:
            if op.kind is not MartOpKind.JOIN:
                continue
            select = op.details.get("select", "")
            sources = {
                match.group("alias"): (
                    match.group("table"),
                    match.group("column"),
                )
                for match in _SOURCE_ALIAS_RE.finditer(select)
                if match.group("alias") in distinct_aliases
            }
            for alias in sorted(distinct_aliases):
                source = sources.get(alias)
                if source is None:
                    continue
                table_name, column_name = source
                relationship = next(
                    (
                        rel
                        for rel in rels
                        if rel.child_table == table_name
                        and all(
                            f'{table_name}."{column}"' in op.predicate
                            for column in rel.child_columns
                        )
                    ),
                    None,
                )
                if relationship is None:
                    continue
                witness_key = (
                    table_name,
                    column_name,
                    relationship.parent_table,
                    relationship.child_columns,
                    relationship.parent_columns,
                )
                if witness_key in seen:
                    continue
                seen.add(witness_key)

                parent_keys = {
                    tuple(row.get(column) for column in relationship.parent_columns)
                    for row in rows.get(relationship.parent_table, ())
                }
                linked_rows: list[Row] = []
                seen_rows: set[str] = set()
                for row in rows.get(table_name, ()):
                    child_key = tuple(
                        row.get(column) for column in relationship.child_columns
                    )
                    fingerprint = canonical_json(row)
                    if (
                        all(value is not None for value in child_key)
                        and child_key in parent_keys
                        and fingerprint not in seen_rows
                    ):
                        seen_rows.add(fingerprint)
                        linked_rows.append(row)

                has_missing = any(
                    row.get(column_name) is None for row in linked_rows
                )
                has_present = any(
                    row.get(column_name) is not None for row in linked_rows
                )
                repeated: dict[tuple[str, str], set[str]] = {}
                for row in linked_rows:
                    value = row.get(column_name)
                    if value is None:
                        continue
                    group = tuple(
                        row.get(column) for column in relationship.child_columns
                    )
                    repeated.setdefault(
                        (canonical_json(group), canonical_json(value)), set()
                    ).add(canonical_json(row))

                if has_missing and has_present:
                    lines.append(
                        f"MISSING-VALUE WITNESS: {table_name} has both a distinct "
                        f"real row linked to {relationship.parent_table} whose "
                        f"{column_name} value is missing and a distinct linked row "
                        "whose value is non-missing in this population."
                    )
                if any(len(distinct_rows) >= 2 for distinct_rows in repeated.values()):
                    lines.append(
                        f"REPEATED-VALUE WITNESS: {table_name} has at least two "
                        f"distinct real rows linked to the same "
                        f"{relationship.parent_table} row with the same non-missing "
                        f"{column_name} value in this population. Consequently, "
                        f"omitting DISTINCT from distinct_{column_name}_count "
                        "changes at least one mart output in this population, "
                        "independently of byte-identical-row deduplication."
                    )
    return tuple(lines)


def _child_tables(rels: tuple[Relationship, ...]) -> set[str]:
    return {r.child_table for r in rels}


def _parent_tables(rels: tuple[Relationship, ...]) -> set[str]:
    return {r.parent_table for r in rels}


def _stress_rows(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    rows: dict[str, tuple[Row, ...]],
) -> dict[str, tuple[Row, ...]]:
    """Return real rows plus byte-exact replicas of a leaf-table prefix."""
    parents = _parent_tables(rels)
    children = _child_tables(rels)
    stress: dict[str, tuple[Row, ...]] = {}
    for table in tables:
        base = rows[table.name]
        if table.name in children and table.name not in parents and base:
            dupes = base[: max(1, math.ceil(len(base) * STRESS_DUPLICATE_FRACTION))]
            stress[table.name] = base + dupes
        else:
            stress[table.name] = base
    return stress


def real_populations(
    task_id: str,
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    rows: dict[str, tuple[Row, ...]],
    marts: tuple[MartSpec, ...] = (),
    shapes: tuple[StarShape, ...] = (),
) -> tuple[PopulationSpec, ...]:
    """Build five deterministic populations from unmodified vendor rows.

    Development is link-closed, primary is complete, resampled is reordered,
    counterfactual is a carve-out, and stress adds byte-identical leaf replicas.
    """
    parents = _parent_tables(rels)
    children = _child_tables(rels)
    provenance = (
        f"Population policy {REAL_DATA_POLICY!r}: every row is a REAL vendored "
        "WikiDBs row; nothing is synthesized.",
    )

    # Build development child-first, then include referenced parents.
    dev: dict[str, tuple[Row, ...]] = {
        t.name: rows[t.name][: (DEV_PARENT_ROWS if t.name in parents and t.name not in children else DEV_CHILD_ROWS)]
        for t in tables
    }
    for _ in range(len(tables)):
        changed = False
        for rel in rels:
            wanted = {
                tuple(r.get(c) for c in rel.child_columns)
                for r in dev[rel.child_table]
            }
            wanted.discard(tuple(None for _ in rel.child_columns))
            have = {
                tuple(r.get(c) for c in rel.parent_columns)
                for r in dev[rel.parent_table]
            }
            if wanted <= have:
                continue
            chosen = list(dev[rel.parent_table])
            for row in rows[rel.parent_table]:
                key = tuple(row.get(c) for c in rel.parent_columns)
                if key in wanted and key not in have:
                    chosen.append(row)
                    have.add(key)
            # Preserve the vendored row order, not the discovery order.
            order = {
                canonical_json(r): i for i, r in enumerate(rows[rel.parent_table])
            }
            dev[rel.parent_table] = tuple(
                sorted(chosen, key=lambda r: order.get(canonical_json(r), 0))
            )
            changed = True
        if not changed:
            break

    # -- counterfactual: unmatched dimensions + an optional dangling key ------
    counter, counter_restricted = _counterfactual_rows(tables, rels, rows)
    counter, counter_dangling = _counterfactual_optional_dangling(
        tables, rels, counter, shapes
    )

    # -- stress: real rows + exact duplicates of the leaf tables --------------
    stress = _stress_rows(tables, rels, rows)
    duplicated_tables = {
        table.name
        for table in tables
        if len(stress[table.name]) > len(rows[table.name])
    }
    read_tables = {
        table
        for mart in marts
        for op in mart.plan.ops
        if op.kind is MartOpKind.SOURCE
        for table in op.tables
    }
    deduped_tables = {
        table
        for mart in marts
        for op in mart.plan.ops
        if op.kind is MartOpKind.DEDUPE
        for table in op.tables
    }
    stress_conditions = stress_duplicate_conditions(
        tables,
        deduped_tables=deduped_tables,
        read_tables=read_tables,
        duplicated_tables=duplicated_tables,
    )

    return (
        PopulationSpec(
            name=PopulationName.DEVELOPMENT,
            seed=derive_seed(task_id, PopulationName.DEVELOPMENT.value),
            literal_rows=dev,
            # The population adversary sees conditions but not literal rows;
            # certify the null branches the executable mart actually consumes.
            conditions=provenance
            + (
                "Tiny readable slice of the real rows, closed under every "
                "required foreign key.",
            )
            + _dangling_conditions(rels, dev)
            + _profile_witness_conditions(marts, rels, dev)
            + _tie_witness_conditions(shapes, dev)
            + _copied_attribute_conditions(marts, dev),
        ),
        PopulationSpec(
            name=PopulationName.PRIMARY,
            seed=derive_seed(task_id, PopulationName.PRIMARY.value),
            literal_rows={t.name: rows[t.name] for t in tables},
            conditions=provenance
            + ("Every real row of the vendored database, unmodified.",)
            + _dangling_conditions(rels, {t.name: rows[t.name] for t in tables})
            + _profile_witness_conditions(
                marts, rels, {t.name: rows[t.name] for t in tables}
            )
            + _tie_witness_conditions(shapes, {t.name: rows[t.name] for t in tables})
            + _copied_attribute_conditions(marts, {t.name: rows[t.name] for t in tables}),
        ),
        PopulationSpec(
            name=PopulationName.RESAMPLED,
            seed=derive_seed(task_id, PopulationName.RESAMPLED.value),
            literal_rows={
                t.name: _stable_shuffle(rows[t.name], f"{task_id}\x1fresampled")
                for t in tables
            },
            conditions=provenance
            + (
                "The same real rows in a deterministically different physical "
                "order. WikiDBs ships ONE real population per database, so the "
                "memorization check varies row order and pagination rather than "
                "inventing new values.",
            )
            + _dangling_conditions(rels, {t.name: rows[t.name] for t in tables})
            + _profile_witness_conditions(
                marts, rels, {t.name: rows[t.name] for t in tables}
            )
            + _tie_witness_conditions(shapes, {t.name: rows[t.name] for t in tables})
            + _copied_attribute_conditions(marts, {t.name: rows[t.name] for t in tables}),
        ),
        PopulationSpec(
            name=PopulationName.COUNTERFACTUAL,
            seed=derive_seed(task_id, PopulationName.COUNTERFACTUAL.value),
            literal_rows=counter,
            conditions=provenance
            + (
                (
                    (
                        "A deterministic real-row carve-out removes fact rows "
                        "for a slice of dimension keys, so surviving dimension "
                        "rows with zero linked facts really occur. It also omits "
                        "one referenced parent row of an optional link while "
                        "retaining its unmodified real child row, so a non-null "
                        "dangling reference really occurs. inner_join and "
                        "no_null_default logic loses reward here."
                    )
                    if counter_dangling
                    else (
                        "Every dimension row is kept but the fact rows of a "
                        "deterministic slice of them are removed, so dimension "
                        "rows with zero linked facts really occur: inner_join "
                        "and no_null_default logic loses reward here."
                    )
                )
                if counter_restricted
                else (
                    "A deterministic real-row carve-out omits one referenced "
                    "parent row of an optional link while retaining its "
                    "unmodified real child row, so a non-null dangling "
                    "reference really occurs."
                    if counter_dangling
                    else (
                        "UNRESTRICTED: removing facts emptied a table under the "
                        "required links, so this population is the unmodified "
                        "real rows. It does not yet separate inner_join logic — "
                        "construct the counterfactual before this task is "
                        "generated."
                    )
                ),
            )
            + _dangling_conditions(rels, counter)
            + _profile_witness_conditions(marts, rels, counter)
            + _tie_witness_conditions(shapes, counter)
            + _copied_attribute_conditions(marts, counter),
        ),
        PopulationSpec(
            name=PopulationName.STRESS,
            seed=derive_seed(task_id, PopulationName.STRESS.value),
            literal_rows=stress,
            conditions=provenance
            + stress_conditions
            + _dangling_conditions(rels, stress)
            + _profile_witness_conditions(marts, rels, stress)
            + _tie_witness_conditions(shapes, stress)
            + _copied_attribute_conditions(marts, stress),
        ),
    )


# -- The adapter entry point ------------------------------------------------

def to_task_ir(
    db_dir: Path,
    *,
    pool: str = POOL,
    family_map_path: Path | None = None,
    wikidbs_root: Path | None = None,
    catalog: SourceCatalog | None = None,
    with_populations: bool = True,
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> TaskIR:
    """Build a real-row task, rejecting malformed, out-of-scope, or leaky input."""
    db_dir = Path(db_dir).resolve()
    if not db_dir.is_dir():
        raise WikiDbsIngestError(f"WikiDBs database dir not found: {db_dir}")
    node_id = node_id_for_dir(db_dir.name)
    schema = load_schema(db_dir)

    if not (MIN_TABLES <= len(schema.tables) <= MAX_TABLES):
        raise WikiDbsIngestError(
            f"{db_dir.name}: {len(schema.tables)} tables is outside the ingest "
            f"envelope [{MIN_TABLES},{MAX_TABLES}]"
        )

    converted = _convert(db_dir, schema)
    if not converted.relationships:
        raise WikiDbsIngestError(
            f"{db_dir.name}: no usable foreign keys — no join surface, rejected"
        )
    cells = sum(
        len(converted.rows[t.name]) * len(t.columns) for t in converted.tables
    )
    if cells > MAX_TOTAL_CELLS:
        raise WikiDbsIngestError(
            f"{db_dir.name}: {cells} data cells is over the {MAX_TOTAL_CELLS}-cell "
            "ingest envelope (pick a smaller database; see 'ingest-wikidbs --list')"
        )

    # The evidence half is for callers that SHOW why this family was chosen.
    segment, _family_evidence = family_segment_for(
        node_id,
        map_path=family_map_path,
        root=Path(wikidbs_root) if wikidbs_root is not None else None,
    )
    cat = catalog if catalog is not None else load_source_catalog()
    selection = cat.pool(pool).selection(db_dir.name, family=segment)
    if selection.family_id != f"{pool}__{segment}":
        # slugify_family must be an identity on 'cNNNNN'/'unmapped'; if not,
        # the family namespace silently changed. Fail closed.
        raise WikiDbsIngestError(
            f"family id {selection.family_id!r} does not match the component "
            f"segment {segment!r} (fail closed)"
        )

    marts, shapes_out = _build_marts(
        converted.tables,
        converted.relationships,
        converted.rows,
        schema.database_name,
        converted.identifier_columns,
        difficulty_profile=difficulty_profile,
    )
    # `manufactured` is True exactly when only the carve-out, not the vendor's
    # rows, gives a hop-1 edge a childless parent.
    hop1_edges = tuple(
        (s.fact, s.fact_link_columns[0], s.parent, s.parent_keys[0])
        for s in shapes_out
        if s.has_join and s.fact and s.fact_link_columns and s.parent_keys
    )
    primary_links = link_statistics(
        converted.relationships, {k: list(v) for k, v in converted.rows.items()}
    )
    merged_links = _effective_links(
        converted.tables, converted.relationships, converted.rows
    )
    primary_witnessed = any(
        primary_links.get(e, (0, 0))[1] >= 1 for e in hop1_edges
    )
    manufactured = not primary_witnessed and any(
        merged_links.get(e, (0, 0))[1] >= 1 for e in hop1_edges
    )
    # Measure reverse optional-edge witnesses with the population helpers.
    dangling_probe, _counter_restricted = _counterfactual_rows(
        converted.tables, converted.relationships, converted.rows
    )
    counterfactual_rows, manufactured_dangling = _counterfactual_optional_dangling(
        converted.tables,
        converted.relationships,
        dangling_probe,
        shapes_out,
    )
    stress_rows = _stress_rows(
        converted.tables, converted.relationships, converted.rows
    )
    # Same discipline for a TIED maximum: `None` means no shipped population
    # reaches the ladder's ELSE branch.
    tie_witness = _tie_witness_population(shapes_out, converted.rows, stress_rows)
    # And for a roll-up's link-count ELSE branches, measured on the rows each
    # population ships (the counterfactual is the carve-out real_populations
    # materializes).
    link_count_else_witness = _link_count_else_population(
        shapes_out, converted.rows, stress_rows, counterfactual_rows
    )
    task_id = f"{selection.family_id}__{slugify_family(db_dir.name)}"
    title = re.sub(r"[_\s]+", " ", schema.database_name).strip().title()

    backends = tuple(
        BackendAssignment(
            table=t.name,
            backend=_BACKEND_CYCLE[
                derive_seed("wikidbs-backend", selection.family_id, t.name)
                % len(_BACKEND_CYCLE)
            ],
        )
        for t in converted.tables
    )
    populations = (
        real_populations(
            task_id,
            converted.tables,
            converted.relationships,
            converted.rows,
            marts,
            shapes_out,
        )
        if with_populations
        else ()
    )
    task = TaskIR(
        task_id=task_id,
        cluster_id=schema_shape_cluster_id(
            pool, converted.tables, converted.relationships
        ),
        **selection.ir_identity(),
        title=title,
        tables=converted.tables,
        relationships=converted.relationships,
        backends=backends,
        marts=marts,
        populations=populations,
        # Real rows, but the attack catalogue is the SAME shared derivation
        # every pool uses: it reads the emitted star, not the row policy.
        attack_cases=_attack_cases(
            shapes_out,
            backends=len({b.backend for b in backends}),
            # REAL-ROW carve-outs, not a constructed counterfactual, so the
            # catalogue must claim what THIS policy guarantees.
            policy=POLICY_PROVIDED_ROWS,
            # The extract-load half comes from the PHYSICAL layout: the backend
            # a table sits on decides which load bugs this task can have.
            tables=converted.tables,
            backend_assignments=backends,
            manufactured_childless=manufactured or manufactured_dangling,
            # The boundary-ELSE claim is observable only on a TIED maximum.
            tie_witness=tie_witness,
            link_count_else_witness=link_count_else_witness,
        ),
    )
    # attacks.py mutates TaskIR.reference and has nothing to mutate without it.
    # Attached BEFORE the leak guard so the compiled SQL is scanned too.
    task = attach_reference(task)
    assert_no_provenance_leak(task, db_dir)
    return task


# -- The leak guard ---------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _collect_strings(obj: object, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}(key)", str(k)
            yield from _collect_strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _collect_strings(v, f"{path}[{i}]")


def retained_names(task: TaskIR) -> frozenset[str]:
    """Return normalized identifiers retained legitimately by the task."""
    names = {task.title, task.family_id, task.cluster_id, task.task_id}
    for table in task.tables:
        names.add(table.name)
        names.update(c.name for c in table.columns)
    for mart in task.marts:
        names.add(mart.name)
        names.update(c.name for c in mart.columns)
    return frozenset(_normalize(n) for n in names if n)


def assert_no_provenance_leak(task: TaskIR, db_dir: Path) -> None:
    """Reject Wikidata provenance in emitted identifiers, prose, and row columns.

    Provenance strings use normalized equality; incidental IDs inside real text
    are not leaks.
    """
    forbidden = {
        _normalize(s) for s in provenance_strings(db_dir)
    } - retained_names(task)
    forbidden.discard("")

    # Column-level check over the UNION of every population's values: a verdict
    # that moved with the slice sampled would not be a guard.
    column_values: dict[tuple[str, str], list[str]] = {}
    for population in task.populations:
        for table, rows in population.literal_rows.items():
            for row in rows:
                for column, value in row.items():
                    if value is not None:
                        column_values.setdefault((table, column), []).append(str(value))
    for (table, column), values in sorted(column_values.items()):
        if is_wikidata_id_column(values):
            raise ProvenanceLeakError(
                f"table {table!r} column {column!r} is Wikidata-id valued — "
                "that is tables_with_item_ids/ material, not real data"
            )

    dump = task.model_dump(mode="json")
    row_paths = ".populations"
    for path, value in _collect_strings(dump):
        if not value:
            continue
        if row_paths in path and ".literal_rows" in path:
            continue  # real data values, checked column-wise above
        hit = _WIKIDATA_ID_RE.search(value)
        if hit:
            raise ProvenanceLeakError(
                f"Wikidata identifier {hit.group(0)!r} leaked into task field "
                f"{path} of {task.task_id!r}"
            )
        if path.endswith("(key)"):
            # JSON KEYS are IR field names and retained identifiers —
            # structure, not content. Wikidata ids in a key still fail above.
            continue
        if _normalize(value) in forbidden:
            raise ProvenanceLeakError(
                f"Wikidata provenance {value!r} leaked into task field {path} "
                f"of {task.task_id!r}"
            )


# -- Candidate listing (never scans the 213 GB pool wholesale) --------------

class Candidate(BaseModel):
    """One listable WikiDBs database, with why it is (or is not) a candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: int
    directory: str
    part: str
    component: str
    component_size: int
    table_count: int
    foreign_key_count: int
    #: Declared edges whose PARENT column is really a key — the only ones that can fund a mart.
    usable_foreign_key_count: int = 0
    #: Hop-1 edges that FUND a plan-library chain, measured with `_build_marts`' machinery.
    chain_funding_edge_count: int = 0
    row_count: int
    csv_bytes: int
    eligible: bool
    reason: str = ""

    @property
    def sort_key(self) -> tuple:
        """Rank by funding edges, usable edges, singleton status, size, and ID."""
        midpoint = MAX_ROWS_PER_TABLE * 2
        return (
            -self.chain_funding_edge_count,
            -self.usable_foreign_key_count,
            0 if self.component_size == 1 else 1,
            abs(self.row_count - midpoint // 2),
            self.node_id,
        )


def _usable_edge_count(
    schema: WikiSchema, raw: dict[str, list[list[str]]]
) -> int:
    """Estimate the upper bound of usable edges for shortlist ranking."""
    counted = 0
    for table in schema.tables:
        for fk in table.foreign_keys:
            parent = fk.reference_table_name
            if parent == table.table_name or parent not in raw:
                continue
            declared = [c.column_name for c in schema.table(parent).columns]
            if fk.reference_column_name not in declared:
                continue
            at = declared.index(fk.reference_column_name)
            values = [row[at] if at < len(row) else "" for row in raw[parent]]
            if values and "" not in values and len(set(values)) == len(values):
                counted += 1
    return counted


def _chain_funding_edge_count(db_dir: Path, schema: WikiSchema) -> int:
    """Count funding hop-1 edges through the same conversion used by ingest."""
    try:
        conv = _convert(db_dir, schema)
    except WikiDbsIngestError:
        return 0
    if not conv.relationships:
        return 0
    candidates = _chain_candidates_for(
        conv.tables, conv.relationships, conv.rows, conv.identifier_columns
    )
    return len(
        {(c.bridge, c.bridge_parent_fk, c.parent, c.parent_key) for c in candidates}
    )


def list_candidates(
    root: Path,
    *,
    part: int = 0,
    scan: int = 200,
    limit: int = 10,
    family_map_path: Path | None = None,
    start: int = 0,
) -> tuple[Candidate, ...]:
    """Shortlist candidates without walking the full pool.

    Scan ascending node directories and keep at most one per graph component.
    """
    root = Path(root)
    part_dir = root / f"part-{part}"
    if not part_dir.is_dir():
        raise WikiDbsIngestError(f"missing WikiDBs part directory: {part_dir}")
    module = _family_map_module()
    map_path = (
        Path(family_map_path) if family_map_path is not None else module.default_map_path()
    )
    fmap = module.load_map(map_path) if map_path.is_file() else None

    names = sorted(p.name for p in part_dir.iterdir() if p.is_dir())
    out: list[Candidate] = []
    seen_components: set[str] = set()
    for name in names[start : start + scan]:
        node_id = node_id_for_dir(name)
        db_dir = part_dir / name
        tables_dir = db_dir / TABLES_DIRNAME
        if not tables_dir.is_dir():
            continue
        files = sorted(p for p in tables_dir.iterdir() if p.suffix == ".csv")
        table_count = len(files)
        csv_bytes = sum(p.stat().st_size for p in files)
        component = fmap.component_of(node_id) if fmap is not None else FALLBACK_FAMILY_SEGMENT
        size = fmap.sizes[component] if fmap is not None else 0
        if not (MIN_TABLES <= table_count <= MAX_TABLES):
            continue  # cheap filter: no schema.json read
        if csv_bytes > MAX_TOTAL_CELLS * 40:
            continue  # cheap size proxy, before any parse
        try:
            schema = load_schema(db_dir)
        except WikiDbsIngestError:
            continue
        fk_count = sum(len(t.foreign_keys) for t in schema.tables)
        row_count = 0
        usable_fks = 0
        funding_fks = 0
        eligible, reason = True, ""
        try:
            raw: dict[str, list[list[str]]] = {}
            for table in schema.tables:
                raw[table.table_name] = read_rows(db_dir, table)
                row_count += len(raw[table.table_name])
            usable_fks = _usable_edge_count(schema, raw)
            funding_fks = _chain_funding_edge_count(db_dir, schema)
        except WikiDbsIngestError as exc:
            eligible, reason = False, str(exc)
        if eligible and fk_count == 0:
            eligible, reason = False, "no foreign keys — no join surface"
        if eligible and usable_fks == 0:
            # `_convert` would emit no `Relationship` and `to_task_ir` would
            # refuse: say so here rather than shortlist an uningestible one.
            eligible, reason = (
                False,
                f"none of the {fk_count} declared foreign keys names a parent "
                "column that is a key of the shipped rows — value overlaps, "
                "not foreign keys",
            )
        if eligible and component in seen_components:
            eligible, reason = False, f"component {component} already listed"
        if eligible:
            seen_components.add(component)
        out.append(
            Candidate(
                node_id=node_id,
                directory=name,
                part=f"part-{part}",
                component=component,
                component_size=size,
                table_count=table_count,
                foreign_key_count=fk_count,
                usable_foreign_key_count=usable_fks,
                chain_funding_edge_count=funding_fks,
                row_count=row_count,
                csv_bytes=csv_bytes,
                eligible=eligible,
                reason=reason,
            )
        )
    eligible = sorted((c for c in out if c.eligible), key=lambda c: c.sort_key)
    return tuple(eligible[:limit])

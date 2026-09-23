"""Convert SynSQL schemas to ``TaskIR`` while quarantining answer fields.

Only ``tables.json`` may influence tasks. ``data.json`` answer text remains
private and is checked against emitted content.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

import sqlglot
from sqlglot import exp
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from elt_taskgen.adapters.evidence import (
    build_marts,
    chain_candidates,
    declared_key_parents,
    schema_shape_cluster_id,
)
from elt_taskgen.generation.mart_plan import StarShape
from elt_taskgen.generation.difficulty_profiles import (
    GenerationDifficultyProfile,
    STANDARD_DIFFICULTY_PROFILE,
)
from elt_taskgen.generation.populations import derive_populations_and_attacks
from elt_taskgen.reference.solution import attach_reference
from elt_taskgen.adapters import reassign_unsafe_file_tables
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartSpec,
    Origin,
    Relationship,
    TableSpec,
    TaskIR,
    derive_seed,
)

#: Two marts, at two grains: the smallest number that keeps the mart-count
#: distribution from collapsing to a constant.
_MAX_MARTS = 2

#: Solver-facing one-liner per library shape. The adapter owns PROSE, the plan
#: library owns SQL; keeping them apart is what stops prose describing
#: computation the plan never performs.
_MART_PROSE: dict[str, str] = {
    "rollup": (
        "Per-{parent} roll-up of linked {bridge} activity in the {db} "
        "scenario, including the fan-out onto {child}."
    ),
    "top": (
        "Per-{parent} extremes of linked {bridge} rows in the {db} scenario: "
        "WHICH row is largest, not how large it is."
    ),
    "bands": (
        "Per-{parent} banding of linked {bridge} activity in the {db} "
        "scenario, over the value domain the source schema itself declares."
    ),
    "by_period": (
        "Per-({parent}, month) activity grid over {bridge} in the {db} scenario."
    ),
    "cohorts": (
        "Per-({parent}, status cohort) summary of linked {bridge} rows in the "
        "{db} scenario, with passing and failing cohorts kept separate."
    ),
    "snapshot": (
        "Per-{parent} latest-row snapshot over {bridge} in the {db} scenario, "
        "ordered by the schema-declared timestamp and row key."
    ),
    "distribution": (
        "Per-({parent}, measure state) distribution of linked {bridge} rows in "
        "the {db} scenario."
    ),
}

#: The four answer-bearing record fields. Never exported, never solver-visible.
QUARANTINED_FIELDS: tuple[str, ...] = ("question", "sql", "cot", "external_knowledge")

# Deterministic backend rotation domain (order participates in content hashes).
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

_TYPE_MAP: dict[str, ColumnType] = {
    # Spider-style coarse types
    "number": ColumnType.DECIMAL,
    "text": ColumnType.TEXT,
    "time": ColumnType.TIMESTAMP,
    "boolean": ColumnType.BOOLEAN,
    "others": ColumnType.TEXT,
    # concrete SQL types seen in SynSQL DDLs
    "int": ColumnType.INTEGER,
    "integer": ColumnType.INTEGER,
    "smallint": ColumnType.INTEGER,
    "tinyint": ColumnType.INTEGER,
    "bigint": ColumnType.BIGINT,
    "real": ColumnType.FLOAT,
    "float": ColumnType.FLOAT,
    "double": ColumnType.FLOAT,
    "numeric": ColumnType.DECIMAL,
    "decimal": ColumnType.DECIMAL,
    "varchar": ColumnType.TEXT,
    "char": ColumnType.TEXT,
    "string": ColumnType.TEXT,
    "clob": ColumnType.TEXT,
    "bool": ColumnType.BOOLEAN,
    "date": ColumnType.DATE,
    "datetime": ColumnType.TIMESTAMP,
    "timestamp": ColumnType.TIMESTAMP,
    "json": ColumnType.JSON,
}


def _map_type(type_str: str | None) -> ColumnType:
    if not type_str:
        return ColumnType.TEXT
    base = re.sub(r"\(.*\)$", "", str(type_str).strip().lower()).strip()
    return _TYPE_MAP.get(base, ColumnType.TEXT)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_.-]+", "_", text.strip().lower()).strip("_.-")
    return s or "x"


# Records (answer material quarantined behind private attributes)

class SynSQLRecord(BaseModel):
    """One data.json record; answer fields are private attrs, never dumped.

    Pydantic excludes private attributes from every dump, so no serialization
    path can leak them into task artifacts.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    db_id: str = Field(min_length=1)
    sql_complexity: str = ""
    question_style: str = ""

    _question: str = PrivateAttr(default="")
    _sql: str = PrivateAttr(default="")
    _cot: str = PrivateAttr(default="")
    _external_knowledge: str = PrivateAttr(default="")

    @classmethod
    def from_raw(cls, raw: dict) -> "SynSQLRecord":
        rec = cls(
            db_id=str(raw.get("db_id") or ""),
            sql_complexity=str(raw.get("sql_complexity") or ""),
            question_style=str(raw.get("question_style") or ""),
        )
        rec._question = str(raw.get("question") or "")
        rec._sql = str(raw.get("sql") or "")
        rec._cot = str(raw.get("cot") or "")
        rec._external_knowledge = str(raw.get("external_knowledge") or "")
        return rec

    def quarantined_texts(self) -> dict[str, str]:
        """Return answer material only for contamination and leak checks."""
        return {
            "question": self._question,
            "sql": self._sql,
            "cot": self._cot,
            "external_knowledge": self._external_knowledge,
        }


# Streaming ingestion

_CHUNK = 1 << 16


def _iter_json_array(path: Path) -> Iterator[dict]:
    """Stream objects out of a (possibly huge) top-level JSON array."""
    decoder = json.JSONDecoder()
    with Path(path).open(encoding="utf-8") as fh:
        buf = ""
        started = False
        while True:
            stripped = buf.lstrip()
            if not started:
                if not stripped:
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        raise ValueError(f"{path}: empty file, expected JSON array")
                    buf += chunk
                    continue
                if stripped[0] != "[":
                    raise ValueError(f"{path}: expected top-level JSON array")
                buf = stripped[1:]
                started = True
                continue
            stripped = buf.lstrip().lstrip(",").lstrip()
            if stripped.startswith("]"):
                return
            if not stripped:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    raise ValueError(f"{path}: truncated JSON array")
                buf = stripped + chunk
                continue
            try:
                obj, end = decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    raise ValueError(f"{path}: truncated JSON array") from None
                buf = stripped + chunk
                continue
            if not isinstance(obj, dict):
                raise ValueError(f"{path}: array element is not an object")
            yield obj
            buf = stripped[end:]


def iter_records(data_json: Path) -> Iterator[SynSQLRecord]:
    """Stream array or JSONL records, skipping entries without ``db_id``."""
    path = Path(data_json)
    with path.open(encoding="utf-8") as fh:
        head = fh.read(64).lstrip()
    if head.startswith("["):
        raw_iter: Iterator[dict] = _iter_json_array(path)
    else:
        def _iter_jsonl() -> Iterator[dict]:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        if not isinstance(obj, dict):
                            raise ValueError(f"{path}: JSONL line is not an object")
                        yield obj
        raw_iter = _iter_jsonl()
    for raw in raw_iter:
        if raw.get("db_id"):
            yield SynSQLRecord.from_raw(raw)


def load_tables(tables_json: Path) -> dict[str, dict]:
    """tables.json -> {db_id: raw schema entry}. Fail closed on malformed input."""
    raw = json.loads(Path(tables_json).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        entries = list(raw.values()) if "db_id" not in raw else [raw]
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError(f"{tables_json}: expected a list or object of schema entries")
    out: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("db_id"):
            raise ValueError(f"{tables_json}: schema entry without db_id")
        out[str(entry["db_id"])] = entry
    return out


# Schema conversion

#: Raw DDL comments provide the pool's declared-domain evidence.
_DOMAIN_COMMENT = re.compile(
    r'"(?P<col>\w+)"\s+(?:TEXT|VARCHAR\s*\([^)]*\)|VARCHAR|CHAR\s*\([^)]*\))'
    r"[^,/]*/\*[^*]*?\(\s*e\.?g\.?[,:]?\s*(?P<vals>[^)]*)\)",
    re.IGNORECASE,
)

#: A domain worth declaring: >=2 values, each short enough to be a label rather
#: than prose that happened to contain a parenthesis.
_MAX_DOMAIN_VALUE_LEN = 40
_MAX_DOMAIN_VALUES = 8

#: Drop enumeration sentinels while keeping the remaining domain closed.
_DOMAIN_SENTINEL_RE = re.compile(r"^(?:etc\.?|and so on|and more|\.\.\.|…)$", re.IGNORECASE)
#: A conjunction on the LAST value is list grammar, not part of the value.
_DOMAIN_TRAILING_CONJUNCTION_RE = re.compile(r"^(?:or|and)\s+", re.IGNORECASE)


def _comment_domains(ddls: Iterable[str]) -> dict[tuple[str, str], tuple[str, ...]]:
    """(table_lower, column_lower) -> declared value domain, from DDL comments."""
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for ddl in ddls or []:
        head = re.search(r'CREATE\s+TABLE\s+"?(\w+)"?', ddl or "", re.IGNORECASE)
        if head is None:
            continue
        tname = head.group(1).lower()
        for m in _DOMAIN_COMMENT.finditer(ddl):
            values = [v.strip().strip("'\"") for v in m.group("vals").split(",")]
            values = [v for v in values if v and len(v) <= _MAX_DOMAIN_VALUE_LEN]
            values = [v for v in values if not _DOMAIN_SENTINEL_RE.match(v)]
            if values:
                values[-1] = _DOMAIN_TRAILING_CONJUNCTION_RE.sub("", values[-1]).strip()
                values = [v for v in values if v]
            if len(values) < 2 or len(values) > _MAX_DOMAIN_VALUES:
                continue
            out[(tname, m.group("col").lower())] = tuple(values)
    return out


def _ddl_hints(ddls: Iterable[str]) -> dict[tuple[str, str], dict]:
    """Return optional nullability and enum hints from parseable DDL."""
    hints: dict[tuple[str, str], dict] = {}
    for ddl in ddls or []:
        try:
            tree = sqlglot.parse_one(ddl)
        except Exception:
            continue
        if not isinstance(tree, exp.Create):
            continue
        table_name = tree.find(exp.Table)
        if table_name is None:
            continue
        tname = table_name.name.lower()
        for cd in tree.find_all(exp.ColumnDef):
            cname = cd.this.name.lower()
            not_null = False
            enum_vals: tuple[str, ...] | None = None
            for c in cd.constraints:
                kind = c.kind
                if isinstance(kind, (exp.NotNullColumnConstraint, exp.PrimaryKeyColumnConstraint)):
                    not_null = True
                if isinstance(kind, exp.CheckColumnConstraint):
                    for in_expr in kind.find_all(exp.In):
                        vals = tuple(
                            e.name for e in in_expr.expressions
                            if isinstance(e, exp.Literal) and e.is_string
                        )
                        if vals:
                            enum_vals = vals
            hints[(tname, cname)] = {"not_null": not_null, "enum": enum_vals}
    return hints


def _flatten_pks(primary_keys: Iterable) -> list[int]:
    flat: list[int] = []
    for pk in primary_keys or []:
        if isinstance(pk, list):
            flat.extend(int(x) for x in pk)
        else:
            flat.append(int(pk))
    return flat


def schema_to_tables(entry: dict) -> tuple[tuple[TableSpec, ...], tuple[Relationship, ...]]:
    """One tables.json entry -> (TableSpecs, Relationships).

    The name/type/key arrays are authoritative; DDLs contribute nullability and
    enum HINTS only.
    """
    table_names = entry.get("table_names_original") or entry.get("table_names")
    col_entries = entry.get("column_names_original") or entry.get("column_names")
    if not table_names or not col_entries:
        raise ValueError(f"schema entry {entry.get('db_id')!r}: missing table/column arrays")
    col_types = entry.get("column_types") or []
    friendly = entry.get("column_names") or col_entries
    hints = _ddl_hints(entry.get("ddls") or [])
    domains = _comment_domains(entry.get("ddls") or [])
    pk_idx = set(_flatten_pks(entry.get("primary_keys") or []))

    # global column index -> (table_index, original name, friendly name, type)
    columns_by_table: dict[int, list[tuple[int, str, str, str | None]]] = {}
    col_lookup: dict[int, tuple[int, str]] = {}
    for gi, pair in enumerate(col_entries):
        ti, cname = int(pair[0]), str(pair[1])
        if ti < 0:
            continue  # the '*' pseudo-column
        fname = str(friendly[gi][1]) if gi < len(friendly) else cname
        ctype = str(col_types[gi]) if gi < len(col_types) else None
        columns_by_table.setdefault(ti, []).append((gi, cname, fname, ctype))
        col_lookup[gi] = (ti, cname)

    tables: list[TableSpec] = []
    for ti, tname in enumerate(table_names):
        cols = columns_by_table.get(ti)
        if not cols:
            raise ValueError(
                f"schema entry {entry.get('db_id')!r}: table {tname!r} has no columns"
            )
        pk_cols = tuple(c for gi, c, _f, _t in cols if gi in pk_idx)
        specs = []
        for gi, cname, fname, ctype in cols:
            hint = hints.get((str(tname).lower(), cname.lower()), {})
            nullable = not (gi in pk_idx or hint.get("not_null", False))
            desc = fname if fname and fname.lower() != cname.lower() else (
                f"{cname} column of {tname}."
            )
            specs.append(
                ColumnSpec(
                    name=cname,
                    type=_map_type(ctype),
                    nullable=nullable,
                    description=desc,
                    # CHECK constraint (hard declaration) before DDL comment
                    # domain (soft); both are written down in tables.json.
                    enum_values=hint.get("enum")
                    or domains.get((str(tname).lower(), cname.lower())),
                )
            )
        tables.append(
            TableSpec(
                name=str(tname),
                description=f"Source table {tname} of the {entry.get('db_id')} scenario.",
                columns=tuple(specs),
                primary_key=pk_cols,
            )
        )

    rels: list[Relationship] = []
    seen: set[tuple] = set()
    for pair in entry.get("foreign_keys") or []:
        ci, pi = int(pair[0]), int(pair[1])
        if ci not in col_lookup or pi not in col_lookup:
            continue
        cti, ccol = col_lookup[ci]
        pti, pcol = col_lookup[pi]
        child_table, parent_table = str(table_names[cti]), str(table_names[pti])
        key = (child_table, ccol, parent_table, pcol)
        if key in seen or cti == pti:
            continue
        seen.add(key)
        child_column = tables[cti].column(ccol)
        parent_column = tables[pti].column(pcol)
        if child_column.type is not parent_column.type:
            raise ValueError(
                f"schema entry {entry.get('db_id')!r}: foreign key "
                f"{child_table}.{ccol} ({child_column.type.value}) -> "
                f"{parent_table}.{pcol} ({parent_column.type.value}) has "
                "incompatible logical types"
            )
        child_nullable = child_column.nullable
        rels.append(
            Relationship(
                child_table=child_table,
                child_columns=(ccol,),
                parent_table=parent_table,
                parent_columns=(pcol,),
                required=not child_nullable,
            )
        )
    rels.sort(key=lambda r: (r.child_table, r.child_columns, r.parent_table, r.parent_columns))
    return tuple(tables), tuple(rels)


# Mart candidates from schema structure (NEVER from answer SQL)


def _build_marts(
    tables: tuple[TableSpec, ...],
    rels: tuple[Relationship, ...],
    db_id: str,
    *,
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> tuple[tuple[MartSpec, ...], tuple[StarShape, ...], tuple[str, ...]]:
    """Build marts from schema-derived FK evidence through the shape library."""
    # Only declared single-column keys may anchor grains or hop-2 joins.
    candidates = chain_candidates(tables, rels, key_parents=declared_key_parents(tables))
    if not candidates:
        raise ValueError(
            f"db {db_id!r}: no usable parent<-bridge chain in the declared FK "
            "graph (fail closed: the shapes are not invented)"
        )
    max_marts, spread_grains, max_per_shape = difficulty_profile.mart_parameters(
        pool="synsql", default_budget=_MAX_MARTS
    )
    marts, shapes_out, names = build_marts(
        candidates,
        prefix=lambda e, suffix: _slug(f"{e.parent}_{e.bridge}_{suffix}"),
        max_marts=max_marts,
        spread_grains=spread_grains,
        max_per_shape=max_per_shape,
        description=lambda suffix, e: _MART_PROSE[suffix].format(
            parent=e.parent, bridge=e.bridge, child=e.child or e.bridge, db=db_id
        ),
        notes=(
            "Constructed from schema structure and the DDL comment domains in "
            "tables.json only; no SynSQL answer material was consulted."
        ),
    )
    if not marts:
        raise ValueError(
            f"db {db_id!r}: no declared chain funds any library shape — rejected "
            "(fail closed; this is the pipeline's discard yield, not a narrow task)"
        )
    return marts, shapes_out, names


def to_task_ir_with_schema_atoms(
    db_id: str,
    tables_json: Path,
    *,
    pool: str = "synsql",
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> tuple[TaskIR, frozenset[str]]:
    """Build a task and immutable schema atoms from schema-only inputs.

    Require at least two tables and one FK. Shape selection ignores answer-side
    complexity and histograms. Leak exemptions must use the returned atoms,
    never values reconstructed from the task under inspection.
    """
    entries = load_tables(tables_json)
    if db_id not in entries:
        raise ValueError(f"db_id {db_id!r} not found in {tables_json}")
    tables, rels = schema_to_tables(entries[db_id])
    if len(tables) < 2:
        raise ValueError(f"db {db_id!r}: fewer than 2 tables — trivial, rejected")
    if not rels:
        raise ValueError(f"db {db_id!r}: no foreign keys — no join surface, rejected")

    marts, shapes_out, _shape_names = _build_marts(
        tables, rels, db_id, difficulty_profile=difficulty_profile
    )
    family_id = f"{pool}__{_slug(db_id)}"
    task_id = f"{family_id}__{marts[0].name}"
    title = db_id.replace("_", " ").replace("-", " ").strip().title()

    backends = tuple(
        BackendAssignment(
            table=t.name,
            backend=_BACKEND_CYCLE[
                derive_seed("synsql-backend", family_id, t.name) % len(_BACKEND_CYCLE)
            ],
        )
        for t in tables
    )
    # Reassigned BEFORE the catalogue reads the assignments: a table with no
    # NOT NULL column cannot ride FILES or REST.
    backends = reassign_unsafe_file_tables(backends, tables)
    populations, attacks = derive_populations_and_attacks(
        task_id=task_id,
        tables=tables,
        relationships=rels,
        shapes=shapes_out,
        policy_conditions=(
            "SYNTHETIC data: SynSQL ships schemas only, so every row is "
            "generated from the declared schema — no upstream row is copied.",
        ),
        backends=len({b.backend for b in backends}),
        # The load-side catalogue needs the ASSIGNMENTS, not just the count:
        # which attacks are honest depends on WHICH table sits on WHICH backend.
        backend_assignments=backends,
    )
    # Operator-family cases are NOT merged in here: `derive_attack_cases` owns
    # them, and a second adapter-side derivation would ship the same mutant
    # twice under two names.
    task = TaskIR(
        task_id=task_id,
        family_id=family_id,
        cluster_id=schema_shape_cluster_id(pool, tables, rels),
        origin=Origin.SYNSQL,
        license="Apache-2.0",
        attribution=f"SynSQL-2.5M schema (db_id: {db_id})",
        title=title,
        tables=tables,
        relationships=rels,
        backends=backends,
        marts=marts,
        populations=populations,
        attack_cases=attacks,
    )
    # The trusted transform compiled from the plan, carried ON the task:
    # attacks.py mutates TaskIR.reference and has nothing to mutate without it.
    return attach_reference(task), _schema_side_atoms_from_tables(tables)


def to_task_ir(
    db_id: str,
    tables_json: Path,
    *,
    pool: str = "synsql",
    difficulty_profile: GenerationDifficultyProfile = STANDARD_DIFFICULTY_PROFILE,
) -> TaskIR:
    """Build a task without exposing schema-atom leak exemptions."""
    task, _trusted_schema_atoms = to_task_ir_with_schema_atoms(
        db_id,
        tables_json,
        pool=pool,
        difficulty_profile=difficulty_profile,
    )
    return task


# The contamination gate (ONE service, reachable from EVERY ingest path)

class SynSQLContaminationError(ValueError):
    """A SynSQL candidate collided fatally with the contamination index.

    Carries fatal AND borderline collisions so a caller can report every hit
    without re-running the check.
    """

    def __init__(self, message: str, collisions: tuple = ()) -> None:
        super().__init__(message)
        self.collisions = tuple(collisions)


def assert_uncontaminated(task: TaskIR, index_dir: Path) -> list:
    """Return borderline collisions and raise on any fatal contamination."""
    from elt_taskgen.verification.contamination import ContaminationIndex

    collisions = ContaminationIndex(Path(index_dir)).check_pre(task)
    fatal = [c for c in collisions if c.fatal]
    if fatal:
        detail = "; ".join(f"[{c.kind}/{c.against}] {c.detail}" for c in fatal)
        raise SynSQLContaminationError(
            f"contamination pre-check REJECTED {task.task_id!r}: {detail}",
            tuple(collisions),
        )
    return [c for c in collisions if not c.fatal]


# The assertion helper: grep every emitted string against answer material

class AnswerLeakError(ValueError):
    """Raised when SynSQL answer material is found in an emitted task field."""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _collect_strings(obj: object, path: str = "$") -> list[tuple[str, str]]:
    """Every string value in a nested dump, with its JSON path."""
    out: list[tuple[str, str]] = []
    if isinstance(obj, str):
        out.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.append((f"{path}.{k}(key)", str(k)))
            out.extend(_collect_strings(v, f"{path}.{k}"))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.extend(_collect_strings(v, f"{path}[{i}]"))
    return out


#: Shorter fragments are ignored: table/column names legitimately appear on
#: both the schema side and inside answer SQL.
_MIN_FRAGMENT_LEN = 16


def _leak_fragments(text: str) -> list[str]:
    """Normalized fragments of one quarantined text worth grepping for."""
    fragments = []
    whole = _normalize(text)
    if len(whole) >= _MIN_FRAGMENT_LEN:
        fragments.append(whole)
    for line in re.split(r"[\n;]+", text):
        norm = _normalize(line)
        if len(norm) >= _MIN_FRAGMENT_LEN:
            fragments.append(norm)
    return fragments


def _schema_side_atoms_from_tables(tables: Iterable[TableSpec]) -> frozenset[str]:
    """Capture exact identifiers and closed-domain values from trusted schema.

    Exemptions exclude prose, prompts, plans, reference SQL, descriptions, and
    fragments larger than one schema atom.
    """
    atoms: set[str] = set()
    for table in tables:
        atoms.add(_normalize(table.name))
        atoms.update(_normalize(name) for name in table.primary_key)
        atoms.update(_normalize(name) for name in table.business_key)
        for column in table.columns:
            atoms.add(_normalize(column.name))
            if column.enum_values is not None:
                atoms.update(_normalize(value) for value in column.enum_values)
    return frozenset(atom for atom in atoms if atom)


_SQL_ATOM_WRAPPER = re.compile(
    r'^(?:"(?P<double>(?:[^"]|"")*)"|'
    r"'(?P<single>(?:[^']|'')*)'|"
    r"`(?P<backtick>(?:[^`]|``)*)`|"
    r"\[(?P<bracket>(?:[^\]]|\]\])*)\])$"
)


def _schema_atom_equivalent(fragment: str, trusted_atoms: frozenset[str]) -> bool:
    """Return whether a fragment is one optionally quoted trusted schema atom."""
    if fragment in trusted_atoms:
        return True
    match = _SQL_ATOM_WRAPPER.fullmatch(fragment)
    if match is None:
        return False
    for group, escaped, literal in (
        ("double", '""', '"'),
        ("single", "''", "'"),
        ("backtick", "``", "`"),
        ("bracket", "]]", "]"),
    ):
        value = match.group(group)
        if value is not None:
            return _normalize(value.replace(escaped, literal)) in trusted_atoms
    return False


def assert_no_answer_leak(
    task: TaskIR,
    records: Iterable[SynSQLRecord],
    *,
    trusted_schema_atoms: frozenset[str] | None = None,
) -> None:
    """Reject quarantined answer fragments found in an emitted task.

    Short tokens and exact supplied schema atoms are exempt. Without immutable
    schema atoms, scanning is strict and the inspected task supplies no allowlist.
    """
    schema_atoms = trusted_schema_atoms or frozenset()
    emitted = [
        (path, _normalize(value))
        for path, value in _collect_strings(task.model_dump(mode="json"))
        if value
    ]
    for rec in records:
        for field, text in rec.quarantined_texts().items():
            if not text:
                continue
            for fragment in _leak_fragments(text):
                if _schema_atom_equivalent(fragment, schema_atoms):
                    continue
                for path, norm_value in emitted:
                    if fragment in norm_value:
                        raise AnswerLeakError(
                            f"answer material from record db_id={rec.db_id!r} "
                            f"field {field!r} leaked into task field {path}: "
                            f"{fragment[:80]!r}"
                        )

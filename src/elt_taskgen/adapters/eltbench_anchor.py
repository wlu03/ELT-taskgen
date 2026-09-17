"""Import pinned ELT-Bench tasks as non-releasable measurement anchors.

Anchors live under ``<ws>/reference/anchors/``; legacy ``<ws>/anchors/``
content is migrated so it remains in contamination checks.
"""

from __future__ import annotations

import csv
import re
import warnings
from pathlib import Path

import yaml

from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    PopulationName,
    PopulationSpec,
    Relationship,
    TableSpec,
    TaskIR,
    derive_seed,
    task_from_json,
    task_to_json,
)

#: ELT-Bench is published CC BY-SA 4.0 (badge in the pinned README).
ANCHOR_LICENSE = "CC-BY-SA-4.0"

#: Sections describing the ELT runtime/destination, not a SOURCE backend.
_IGNORED_SECTIONS = frozenset({"Airbyte", "snowflake"})


def _section_tables(section_name: str, body: object) -> tuple[Backend, list[str]] | None:
    """Map one config.yaml top-level section to (backend, table names)."""
    if section_name in _IGNORED_SECTIONS or body is None:
        return None
    if section_name == "postgres":
        return Backend.POSTGRES, list((body.get("config") or {}).get("tables") or [])
    if section_name == "mongodb":
        return Backend.MONGODB, list((body.get("config") or {}).get("tables") or [])
    if section_name == "custom_api":
        return Backend.REST, list((body.get("config") or {}).get("tables") or [])
    if section_name == "aws_s3":
        return Backend.S3, [d["table"] for d in (body.get("data") or []) if "table" in d]
    if section_name == "flat_files":
        entries = body if isinstance(body, list) else []
        return Backend.FILES, [d["table"] for d in entries if "table" in d]
    # Fail closed: an unrecognized backend would skew the measured distribution.
    raise ValueError(f"config.yaml section {section_name!r} is not a known ELT-Bench backend")


def _task_dir(bench_root: Path, db_name: str) -> Path:
    d = bench_root / "elt-bench" / "snowflake" / db_name
    if not d.is_dir():
        raise FileNotFoundError(f"no ELT-Bench task dir for db {db_name!r}: {d}")
    return d


def _schema_dir(bench_root: Path, db_name: str) -> Path:
    candidates = (
        _task_dir(bench_root, db_name) / "schemas",
        bench_root / "elt-bench" / "schemas" / db_name,
    )
    for c in candidates:
        if c.is_dir():
            return c
    raise FileNotFoundError(
        f"no schemas dir for db {db_name!r}; tried: {[str(c) for c in candidates]}"
    )


def _read_schema_csv(path: Path) -> tuple[ColumnSpec, ...]:
    """Parse an anchor schema without inferring absent column types.

    Columns remain nullable ``TEXT`` and match contamination by type-blind
    shape.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = [f.strip() for f in (reader.fieldnames or [])]
        if fields[:1] != ["column_name"]:
            raise ValueError(f"{path}: expected header column_name,column_description, got {fields}")
        cols: list[ColumnSpec] = []
        for row in reader:
            name = (row.get("column_name") or "").strip()
            if not name:
                continue
            desc = (row.get("column_description") or "").strip()
            # No upstream type info; TEXT/nullable is the honest stand-in.
            cols.append(ColumnSpec(name=name, type=ColumnType.TEXT, nullable=True, description=desc))
    if not cols:
        raise ValueError(f"{path}: schema CSV has no columns")
    return tuple(cols)


def _load_data_model_yaml(path: Path) -> dict:
    """Parse ``data_model.yaml``, retrying once with descriptions quoted."""
    import json as _json

    text = path.read_text(encoding="utf-8")
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        fixed_lines = []
        for line in text.splitlines():
            m = re.match(r"^(\s*(?:- )?description:)\s*(.*\S)\s*$", line)
            if m and not m.group(2).startswith(("'", '"')):
                line = f"{m.group(1)} {_json.dumps(m.group(2))}"
            fixed_lines.append(line)
        return yaml.safe_load("\n".join(fixed_lines)) or {}


def _load_evaluation_maps(bench_root: Path) -> tuple[dict, dict]:
    import json

    table_json = bench_root / "evaluation" / "table.json"
    sort_json = bench_root / "evaluation" / "sort_key.json"
    if not table_json.is_file() or not sort_json.is_file():
        raise FileNotFoundError(f"pinned evaluation artifacts missing under {bench_root}/evaluation")
    return (
        json.loads(table_json.read_text(encoding="utf-8")),
        json.loads(sort_json.read_text(encoding="utf-8")),
    )


def _anchor_mart(model: dict, sort_keys_for_db: dict[str, list[str]]) -> MartSpec:
    name = model["name"]
    description = (model.get("description") or "").strip()
    raw_cols = model.get("columns") or []
    if not raw_cols:
        raise ValueError(f"data_model.yaml model {name!r} declares no columns")
    columns = tuple(
        MartColumn(
            name=c["name"],
            type=ColumnType.TEXT,
            description=(c.get("description") or "").strip() or c["name"],
        )
        for c in raw_cols
    )
    raw_keys = tuple(sort_keys_for_db.get(name) or ())
    if not raw_keys:
        # Mirror upstream: sort by the first declared column, never invent one.
        raw_keys = (columns[0].name,)
    # sort_key.json entries are uppercased and may be SQL expressions, so match
    # case-insensitively and resolve to the column referenced: key_columns must
    # name REAL columns.
    canon_by_lower = {c.name.lower(): c.name for c in columns}

    def _resolve(key: str) -> str:
        direct = canon_by_lower.get(key.lower())
        if direct is not None:
            return direct
        for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", key):
            hit = canon_by_lower.get(ident.lower())
            if hit is not None:
                return hit
        raise ValueError(
            f"mart {name!r}: sort_key.json entry {key!r} references no data-model column"
        )

    keys_list: list[str] = []
    for k in raw_keys:
        resolved = _resolve(k)
        if resolved not in keys_list:  # dedupe after expression collapse
            keys_list.append(resolved)
    keys = tuple(keys_list)
    plan = MartPlan(
        mart=name,
        ops=(
            MartOp(
                kind=MartOpKind.DERIVE,
                description=(
                    "anchor import: upstream transform logic is described only in "
                    "column prose; not reconstructed (measurement-only task)"
                ),
                columns=tuple(c.name for c in columns),
            ),
        ),
        notes="imported from pinned ELT-Bench data_model.yaml",
    )
    return MartSpec(
        name=name,
        description=description,
        grain=description or f"one row per {keys[0]}",
        key_columns=keys,
        columns=columns,
        plan=plan,
    )


def import_anchor_task(bench_root: Path, db_name: str) -> TaskIR:
    """Parse one pinned ELT-Bench task dir into a measurement-only TaskIR."""
    bench_root = Path(bench_root)
    task_dir = _task_dir(bench_root, db_name)

    config = yaml.safe_load((task_dir / "config.yaml").read_text(encoding="utf-8")) or {}
    data_model = _load_data_model_yaml(task_dir / "data_model.yaml")
    table_counts_all, sort_keys_all = _load_evaluation_maps(bench_root)

    # Source tables: config.yaml names are canonical; schema CSVs are matched
    # case-insensitively (config `airlines` vs `Airlines.csv`).
    backend_by_table: dict[str, Backend] = {}
    for section in sorted(config):
        mapped = _section_tables(section, config[section])
        if mapped is None:
            continue
        backend, names = mapped
        for t in names:
            if t in backend_by_table:
                raise ValueError(f"db {db_name!r}: table {t!r} assigned to two backends")
            backend_by_table[t] = backend
    if not backend_by_table:
        raise ValueError(f"db {db_name!r}: config.yaml declares no source tables")

    schema_dir = _schema_dir(bench_root, db_name)
    csv_by_lower = {p.stem.lower(): p for p in sorted(schema_dir.glob("*.csv"))}
    tables: list[TableSpec] = []
    for name in sorted(backend_by_table):
        csv_path = csv_by_lower.get(name.lower())
        if csv_path is None:
            raise FileNotFoundError(
                f"db {db_name!r}: no schema CSV for source table {name!r} in {schema_dir}"
            )
        tables.append(TableSpec(name=name, columns=_read_schema_csv(csv_path)))

    backends = tuple(
        BackendAssignment(table=t, backend=backend_by_table[t]) for t in sorted(backend_by_table)
    )

    # Marts from data_model.yaml + sort keys from evaluation.
    models_list = data_model.get("models") or []
    if not models_list:
        raise ValueError(f"db {db_name!r}: data_model.yaml declares no models")
    sort_keys_for_db = sort_keys_all.get(db_name, {})
    marts = tuple(_anchor_mart(m, sort_keys_for_db) for m in models_list)

    # Expected loaded row counts (stage-1 gold) as the primary scale.
    task_id = f"eltbench__{db_name}"
    canon_by_lower = {t.name.lower(): t.name for t in tables}
    scale = {
        canon_by_lower[k.lower()]: int(v)
        for k, v in (table_counts_all.get(db_name) or {}).items()
        if k.lower() in canon_by_lower
    }
    populations = (
        PopulationSpec(
            name=PopulationName.PRIMARY,
            seed=derive_seed(task_id, PopulationName.PRIMARY.value),
            scale=scale,
            conditions=("anchor: pinned upstream data; counts are the stage-1 gold",),
        ),
    )

    relationships: tuple[Relationship, ...] = ()  # upstream declares no FK metadata

    return TaskIR(
        task_id=task_id,
        family_id=f"eltbench__{db_name}",
        cluster_id=f"eltbench__{db_name}",
        origin=Origin.ELTBENCH_ANCHOR,
        license=ANCHOR_LICENSE,
        attribution=f"ELT-Bench pinned anchor task {db_name!r} (measurement-only, never released)",
        title=f"ELT-Bench anchor: {db_name}",
        tables=tuple(tables),
        relationships=relationships,
        backends=backends,
        marts=marts,
        populations=populations,
    )


def import_all_anchors(bench_root: Path) -> list[TaskIR]:
    """Import every pinned anchor db, sorted by name. Fail closed on any dir."""
    bench_root = Path(bench_root)
    snowflake_dir = bench_root / "elt-bench" / "snowflake"
    if not snowflake_dir.is_dir():
        raise FileNotFoundError(f"pinned checkout has no {snowflake_dir}")
    return [
        import_anchor_task(bench_root, d.name)
        for d in sorted(snowflake_dir.iterdir())
        if d.is_dir()
    ]


#: Canonical anchor-store location, under `reference/`: measurement instrument,
#: not candidate material.
ANCHOR_STORE_RELPATH = ("reference", "anchors")

#: Legacy location. Read + migrated, never ignored (see module docstring).
LEGACY_ANCHOR_STORE_RELPATH = ("anchors",)


def anchor_store_dir(workspace: Path) -> Path:
    """Canonical anchor-store directory for a workspace (<ws>/reference/anchors)."""
    return Path(workspace).joinpath(*ANCHOR_STORE_RELPATH)


def legacy_anchor_store_dir(workspace: Path) -> Path:
    """Deprecated pre-relocation anchor-store directory (<ws>/anchors)."""
    return Path(workspace).joinpath(*LEGACY_ANCHOR_STORE_RELPATH)


def migrate_legacy_anchor_store(workspace: Path) -> list[str]:
    """MOVE any <ws>/anchors/*.json into <ws>/reference/anchors/. ALL OR NOTHING.

    Returns the sorted filenames migrated. A legacy file whose new-side twin
    has DIFFERENT bytes is a hard error: picking a winner silently would change
    which fingerprints arm the contamination index. Everything is read and
    compared before a single byte moves, so one conflict aborts with the full
    list and leaves the workspace exactly as it was.
    """
    legacy_dir = legacy_anchor_store_dir(workspace)
    if not legacy_dir.is_dir():
        return []
    legacy_files = sorted(legacy_dir.glob("*.json"))
    if not legacy_files:
        return []

    new_dir = anchor_store_dir(workspace)

    # PLAN (read-only): nothing here mutates the workspace, so a read failure
    # aborts before any move, exactly as a conflict does.
    conflicts: list[str] = []
    to_write: list[tuple[Path, Path, str]] = []  # (src, dst, payload)
    already_identical: list[Path] = []  # src files whose twin already matches
    for src in legacy_files:
        dst = new_dir / src.name
        payload = src.read_text(encoding="utf-8")
        if not dst.exists():
            to_write.append((src, dst, payload))
        elif dst.read_text(encoding="utf-8") == payload:
            already_identical.append(src)
        else:
            conflicts.append(f"{src} != {dst}")

    if conflicts:
        listing = "\n  ".join(conflicts)
        raise ValueError(
            f"anchor-store migration conflict: {len(conflicts)} of "
            f"{len(legacy_files)} legacy anchor file(s) disagree with their "
            f"counterpart in {new_dir}:\n  {listing}\n"
            "resolve by hand (delete the stale one) — refusing to guess which "
            "anchors arm the contamination index. NOTHING was moved: the "
            "migration is all-or-nothing, so the workspace is exactly as it "
            "was and every conflict above can be resolved in one pass."
        )

    # COMMIT: the plan holds, so the move can run to completion.
    new_dir.mkdir(parents=True, exist_ok=True)
    migrated: list[str] = []
    for src, dst, payload in to_write:
        dst.write_text(payload, encoding="utf-8")
        src.unlink()
        migrated.append(src.name)
    for src in already_identical:  # byte-identical twin: drop the legacy copy
        src.unlink()
        migrated.append(src.name)
    migrated.sort()

    # Remove the legacy dir only if empty: unrelated leftovers are a human's call.
    if not any(legacy_dir.iterdir()):
        legacy_dir.rmdir()

    warnings.warn(
        f"migrated {len(migrated)} anchor file(s) from the deprecated "
        f"{legacy_dir} to {new_dir}; ELT-Bench anchors are reference/goal "
        "material, not a training source",
        DeprecationWarning,
        stacklevel=2,
    )
    return migrated


def save_anchor_store(tasks: list[TaskIR], workspace: Path) -> None:
    """Write reference/anchors/<task_id>.json TaskIR dumps under the workspace."""
    migrate_legacy_anchor_store(workspace)
    anchors_dir = anchor_store_dir(workspace)
    anchors_dir.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        if task.origin is not Origin.ELTBENCH_ANCHOR:
            raise ValueError(
                f"refusing to store non-anchor task {task.task_id!r} in reference/anchors/"
            )
        (anchors_dir / f"{task.task_id}.json").write_text(task_to_json(task), encoding="utf-8")


def load_anchor_store(workspace: Path) -> list[TaskIR]:
    """Migrate any legacy store, then load anchors by filename."""
    migrate_legacy_anchor_store(workspace)
    anchors_dir = anchor_store_dir(workspace)
    if not anchors_dir.is_dir():
        raise FileNotFoundError(f"no anchor store at {anchors_dir}")
    tasks: list[TaskIR] = []
    for path in sorted(anchors_dir.glob("*.json")):
        task = task_from_json(path.read_text(encoding="utf-8"))
        if task.origin is not Origin.ELTBENCH_ANCHOR:
            raise ValueError(f"{path}: stored task is not an anchor (origin={task.origin.value})")
        tasks.append(task)
    return tasks

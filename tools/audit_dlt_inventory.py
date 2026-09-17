#!/usr/bin/env python
"""Deterministic, read-only inventory of every vendored ``dlt_*`` connector.

The task-producing extractor deliberately hides catalog-excluded records from
``--all``.  That is the right admission behavior, but the wrong behavior for an
inventory: a quarantined connector must remain visible, together with the
specific reason it cannot become a task.  This tool therefore enumerates the
vendored directories itself and uses the existing AST extractor as a library.

Connector modules are NEVER imported or executed.  Their ``source/`` trees are
parsed as data by :mod:`extract_dlt_manifest`; task buildability is then tested
from the in-memory declarative manifest.  Existing curator-authored column
blocks are read and applied in memory so the result describes the current
factory, but no connector manifest, catalog entry, task, or admission store is
written.  ``--out`` is the only write this tool performs.

``admitted`` below means *admitted by config/sources.yaml to candidate
construction*.  It does not claim that a generated task passed the later
author/review/verification/release gates.

Usage::

    python tools/audit_dlt_inventory.py
    python tools/audit_dlt_inventory.py --out reports/dlt_inventory.yaml
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

import yaml
from pydantic import ValidationError

from elt_taskgen.adapters import dlt as dlt_adapter
from elt_taskgen.catalog import PoolSource, load_source_catalog


SCHEMA_VERSION = "1"
_EXTRACTOR_MODULE_NAME = "_elt_taskgen_extract_dlt_manifest_for_audit"
_NO_TRANSFORM_MART_PREFIX = "no transform mart could be built"

# Keep zeroes in the summary.  A missing key must not be mistaken for a status
# the audit forgot to test.
STATUSES: tuple[str, ...] = (
    "contamination-blocked",
    "runtime-defined",
    "no-transform-mart",
    "buildable",
    "admitted",
    "not-buildable",
    "extraction-error",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_manifest_dir() -> Path:
    """Committed dlt manifest directory whose curated blocks affect builds."""
    return _repo_root() / "config" / "dlt_connectors"


def _load_extractor() -> ModuleType:
    """Load the AST tool itself by path; this does not import a connector.

    ``tools/`` is intentionally not a Python package.  Loading this one trusted
    repository module by filename keeps both direct CLI execution and tests
    imported via ``spec_from_file_location`` working without editing
    ``sys.path``.  The third-party connector paths are never passed to an
    import API.
    """
    loaded = sys.modules.get(_EXTRACTOR_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).with_name("extract_dlt_manifest.py")
    spec = importlib.util.spec_from_file_location(_EXTRACTOR_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load AST extractor at {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses consults sys.modules while resolving annotations.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


extractor = _load_extractor()


def discover_all_records(pool: PoolSource) -> list[str]:
    """Every vendored ``dlt_*`` directory, INCLUDING catalog exclusions."""
    root = pool.root_path()
    if not root.is_dir():
        raise FileNotFoundError(f"dlt pool root not found: {root} (fail closed)")
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and path.name.startswith("dlt_")
    )


def _provenance_by_record(pool: PoolSource) -> dict[str, dict[str, str]]:
    """Read the curation manifest once, retaining only stable string fields."""
    if not pool.provenance_manifest:
        return {}
    path = Path(pool.provenance_manifest)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, dict[str, str]] = {}
    for entry in raw.get("sources") or ():
        if not isinstance(entry, dict):
            continue
        record = entry.get("name")
        if not isinstance(record, str) or not record.startswith("dlt_"):
            continue
        result[record] = {
            key: str(entry[key])
            for key in ("kind", "upstream", "commit", "tier", "path")
            if entry.get(key)
        }
    return result


def _stable_error(exc: BaseException, pool: PoolSource) -> str:
    """Remove host-specific pool roots from a diagnostic."""
    return str(exc).replace(str(pool.root_path()), "<dlt-root>")


def _source_summary(extract: Any) -> dict[str, Any]:
    return {
        "directory": f"{extract.record}/source",
        "files": list(extract.files),
        "primary_source": extract.primary_source,
        "source_functions": [
            {
                "name": source.name,
                "resources": sorted(set(source.owned)),
            }
            for source in extract.sources
        ],
    }


def _resource_summary(extract: Any) -> dict[str, Any]:
    counts = extractor.summarize(extract)
    loadable = [endpoint.name for endpoint in extract.endpoints if endpoint.selected]
    return {
        **counts,
        "loadable": len(loadable),
        "names": sorted(endpoint.name for endpoint in extract.endpoints),
        "loadable_names": sorted(loadable),
    }


def _unresolved_summary(extract: Any) -> list[dict[str, str]]:
    return [
        {
            "defined_in": f"{item.file}:{item.line}",
            "form": item.form,
            "reason": item.reason,
        }
        for item in extract.unresolved
    ]


def _base_record(
    record: str,
    *,
    connector: str,
    pool: PoolSource,
    provenance: dict[str, str],
) -> dict[str, Any]:
    return {
        "record": record,
        "connector": connector,
        "tier": provenance.get("tier", "unclassified"),
        "catalog_excluded": record in pool.excluded,
        "provenance": {
            key: provenance[key]
            for key in ("kind", "upstream", "commit", "path")
            if key in provenance
        },
    }


def audit_record(
    pool: PoolSource,
    record: str,
    *,
    provenance: dict[str, str] | None = None,
    manifest_dir: Path | None = None,
) -> dict[str, Any]:
    """AST-scan and classify one record without changing admission state."""
    connector = extractor.connector_slug(record)
    row = _base_record(
        record,
        connector=connector,
        pool=pool,
        provenance=provenance or {},
    )
    source_dir = pool.record_path(record) / "source"
    try:
        extract = extractor.extract_connector(record, source_dir)
    except extractor.ExtractionError as exc:
        row["status"] = "extraction-error"
        row["reason"] = _stable_error(exc, pool)
        return row

    row["source_summary"] = _source_summary(extract)
    row["resource_summary"] = _resource_summary(extract)
    row["auth"] = {
        "config": list(extract.config),
        "secrets": list(extract.secrets),
    }
    row["pagination_hints"] = list(extract.pagination_hints)
    row["unresolved"] = _unresolved_summary(extract)

    # Contamination is checked before structural buildability.  A deny-listed
    # connector with zero static resources is still contamination-blocked, not
    # a benign runtime-defined candidate (for example dlt_stripe today).
    try:
        extractor.assert_not_denylisted(connector)
    except extractor.ExtractionError:
        row["status"] = "contamination-blocked"
        row["reason"] = (
            f"canonical family {connector!r} is present in an embedded "
            "benchmark contamination deny list"
        )
        return row

    if not extract.endpoints:
        row["status"] = "runtime-defined"
        row["reason"] = (
            "AST scan found no statically named endpoints; connector resources "
            "are defined at runtime"
        )
        return row

    try:
        document = extractor.manifest_document(extract, pool)
        curated_root = Path(manifest_dir) if manifest_dir is not None else default_manifest_dir()
        curated_path = curated_root / f"{connector}.yaml"
        if curated_path.is_file():
            extractor.inject_curated_blocks(
                document, extractor.read_curated_blocks(curated_path)
            )
        manifest = dlt_adapter.DltManifest.model_validate(document)

        if record in pool.excluded:
            # Diagnostic structural build only.  Crucially, this does NOT call
            # PoolSource.selection(), register a task, or alter sources.yaml.
            task = dlt_adapter.to_task_ir(manifest)
            status = "buildable"
            reason = (
                "AST-derived manifest forms a TaskIR, but the source catalog "
                "excludes this record"
            )
        else:
            # For an admitted record, exercise the real catalog license and
            # identity gate rather than the adapter's hand-written fallback.
            selection = pool.selection(record, family=connector)
            task = dlt_adapter.to_task_ir(manifest, selection=selection)
            status = "admitted"
            reason = (
                "AST-derived manifest forms a TaskIR and the source catalog "
                "admits this record to candidate construction"
            )
    except (ValidationError, ValueError) as exc:
        detail = _stable_error(exc, pool)
        if detail.startswith(_NO_TRANSFORM_MART_PREFIX):
            row["status"] = "no-transform-mart"
            row["reason"] = (
                "AST-derived endpoints cannot fund a graded transform mart; "
                "a row-count-only summary duplicates the extraction reward"
            )
        else:
            row["status"] = "not-buildable"
            row["reason"] = detail
        return row

    row["status"] = status
    row["reason"] = reason
    row["task_shape"] = {
        "task_id": task.task_id,
        "family_id": task.family_id,
        "tables": len(task.tables),
        "relationships": len(task.relationships),
        "marts": len(task.marts),
    }
    return row


def build_inventory(
    *,
    catalog_path: Path | None = None,
    manifest_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a host-independent inventory mapping in deterministic order."""
    catalog = load_source_catalog(catalog_path)
    pool = catalog.pool("dlt")
    provenance = _provenance_by_record(pool)
    records = [
        audit_record(
            pool,
            record,
            provenance=provenance.get(record, {}),
            manifest_dir=manifest_dir,
        )
        for record in discover_all_records(pool)
    ]
    status_counts = Counter(str(row["status"]) for row in records)
    tier_counts = Counter(str(row["tier"]) for row in records)
    return {
        "schema_version": SCHEMA_VERSION,
        "pool": "dlt",
        "extractor_version": extractor.EXTRACTOR_VERSION,
        "summary": {
            "vendored_records": len(records),
            "catalog_excluded": sum(bool(row["catalog_excluded"]) for row in records),
            "status_counts": {status: status_counts[status] for status in STATUSES},
            "tier_counts": dict(sorted(tier_counts.items())),
        },
        "records": records,
    }


def render_inventory(inventory: dict[str, Any]) -> str:
    """Stable YAML: sorted mappings, sorted records, no clock or absolute path."""
    return yaml.safe_dump(
        inventory,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AST-only inventory of every vendored dlt connector"
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="source catalog (default: config/sources.yaml)",
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=None,
        help="curated dlt manifests (default: config/dlt_connectors)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write stable YAML here instead of stdout",
    )
    args = parser.parse_args(argv)

    text = render_inventory(
        build_inventory(catalog_path=args.catalog, manifest_dir=args.manifest_dir)
    )
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

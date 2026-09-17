"""Load vendored-source roots, origins, licenses, and attribution.

Unknown pools, excluded records, and missing per-record licenses fail closed.
Adapters validate source roots.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from elt_taskgen.models import Origin, PoolSelection
from elt_taskgen.package_resources import checkout_root, resource_path


class ExcludedRecordError(ValueError):
    """A record intentionally excluded from ingestion.

    This subtype distinguishes exclusions from malformed manifests and builder
    failures while remaining compatible with ``ValueError`` handlers.
    """


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _repo_root() -> Path:
    """Compatibility helper returning the shipped resource root."""

    return resource_path("config").parent


def _sibling_checkout(name: str) -> str:
    """Discover an optional sibling checkout without embedding a user path."""

    checkout = checkout_root()
    if checkout is None:
        return ""
    candidate = checkout.parent / name
    return str(candidate) if candidate.is_dir() else ""


#: Checkout-relative defaults; environment variables override them. They are
#: empty in an installed wheel.
PINNED_ROOTS: dict[str, str] = {
    "ELT_TASKGEN_DATA_ROOT": _sibling_checkout("ELT-training-data"),
    "ELT_TASKGEN_BENCH_ROOT": _sibling_checkout("ELT-Bench"),
}


def default_sources_config_path() -> Path:
    return _repo_root() / "config" / "sources.yaml"


def _interpolate(value: Any) -> Any:
    """${VAR} -> environment, then a discovered sibling, else unchanged."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1))
            or PINNED_ROOTS.get(m.group(1), "")
            or m.group(0),
            value,
        )
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


class PoolSource(BaseModel):
    """One catalog row: an ingestible pool and the terms it may be used on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str = Field(min_length=1)
    origin: Origin
    #: Absolute path of the vendored pool root (post-interpolation).
    root: str = Field(min_length=1)
    #: SPDX id / license name shared by every record. Empty iff per-record.
    license: str = ""
    #: True when the pool carries a license PER RECORD (SchemaPile): the
    #: caller must then supply it, and `license` here must stay empty.
    license_per_record: bool = False
    #: Base attribution; `selection()` appends the record and its provenance.
    attribution: str = ""
    #: Optional MANIFEST.json of upstream/commit provenance for this pool.
    provenance_manifest: str = ""
    #: Record names that must never be ingested (placeholders, carve-outs).
    excluded: tuple[str, ...] = ()
    notes: str = ""

    @model_validator(mode="after")
    def _check_license_terms(self) -> "PoolSource":
        if self.license_per_record and self.license:
            raise ValueError(
                f"pool {self.pool!r}: license_per_record pools must not also "
                f"declare a pool-wide license (got {self.license!r})"
            )
        if not self.license_per_record and not self.license.strip():
            raise ValueError(
                f"pool {self.pool!r}: declare a license, or set "
                "license_per_record: true and supply it per record"
            )
        return self

    # -- paths -------------------------------------------------------------

    def root_path(self) -> Path:
        unresolved = _ENV_PATTERN.search(self.root)
        if unresolved is not None:
            name = unresolved.group(1)
            raise FileNotFoundError(
                f"pool {self.pool!r} has no source root: set ${name} or place "
                "the checkout beside ELT-taskgen"
            )
        return Path(self.root)

    def record_path(self, selector: str) -> Path:
        """Vendored path of one record (pools whose records are directories)."""
        return self.root_path() / selector

    # -- provenance --------------------------------------------------------

    def provenance(self, selector: str) -> dict[str, str]:
        """Return upstream and commit metadata from the pool manifest, if present."""
        if not self.provenance_manifest:
            return {}
        path = Path(self.provenance_manifest)
        if not path.is_file():
            return {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        for entry in doc.get("sources") or ():
            if isinstance(entry, dict) and entry.get("name") == selector:
                return {
                    k: str(entry[k])
                    for k in ("upstream", "commit", "tier")
                    if entry.get(k)
                }
        return {}

    def attribution_for(self, selector: str) -> str:
        """Deterministic attribution line for one record of this pool."""
        base = self.attribution or self.pool
        prov = self.provenance(selector)
        text = f"{base}: {selector}"
        if prov.get("upstream"):
            commit = prov.get("commit", "")
            suffix = f" @ {commit[:12]}" if commit else ""
            text = f"{text} ({prov['upstream']}{suffix})"
        return text

    # -- the one bridge into the IR ---------------------------------------

    def selection(
        self,
        selector: str,
        *,
        license: str | None = None,
        attribution: str | None = None,
        family: str | None = None,
    ) -> PoolSelection:
        """Build a selection, requiring per-record licenses where configured."""
        if selector in self.excluded:
            raise ExcludedRecordError(
                f"pool {self.pool!r}: record {selector!r} is excluded by the "
                "source catalog and must not be ingested"
            )
        resolved = license if license is not None else self.license
        if not resolved or not resolved.strip():
            raise ValueError(
                f"pool {self.pool!r}: record {selector!r} carries a per-record "
                "license; pass license=... from the record's own metadata "
                "(a record with no usable license must be skipped)"
            )
        return PoolSelection.for_record(
            pool=self.pool,
            selector=selector,
            origin=self.origin,
            license=resolved,
            attribution=(
                attribution if attribution is not None else self.attribution_for(selector)
            ),
            family=family,
        )


class SourceCatalog(BaseModel):
    """Every ingestible pool, plus non-ingestible instrument paths."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pools: tuple[PoolSource, ...] = Field(min_length=1)
    #: Named tooling artifacts that are NOT pools (e.g. WikiDBGraph edges).
    instruments: dict[str, str] = Field(default_factory=dict)
    #: Where this catalog was read from (evidence, not configuration).
    config_source: str = ""

    @model_validator(mode="after")
    def _unique_pools(self) -> "SourceCatalog":
        names = [p.pool for p in self.pools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate pool names in the source catalog")
        return self

    def pool(self, name: str) -> PoolSource:
        for p in self.pools:
            if p.pool == name:
                return p
        raise KeyError(
            f"unknown pool {name!r}; catalog has {sorted(p.pool for p in self.pools)}"
        )

    def instrument(self, name: str) -> Path:
        if name not in self.instruments:
            raise KeyError(
                f"unknown instrument {name!r}; catalog has {sorted(self.instruments)}"
            )
        return Path(self.instruments[name])

    def pool_names(self) -> tuple[str, ...]:
        return tuple(sorted(p.pool for p in self.pools))


def load_source_catalog(path: Path | None = None) -> SourceCatalog:
    """Load a source catalog with environment interpolation; require the file."""
    target = Path(path) if path is not None else default_sources_config_path()
    if not target.is_file():
        raise FileNotFoundError(f"source catalog not found: {target} (fail closed)")
    doc = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    doc = _interpolate(doc)
    raw_pools = doc.get("pools") or {}
    if not isinstance(raw_pools, dict) or not raw_pools:
        raise ValueError(f"{target}: 'pools' must be a non-empty mapping")

    known_keys = {
        "origin",
        "root",
        "license",
        "license_per_record",
        "attribution",
        "provenance_manifest",
        "excluded",
        "notes",
    }
    pools: list[PoolSource] = []
    for name in sorted(raw_pools):
        spec = raw_pools[name]
        if not isinstance(spec, dict):
            raise ValueError(f"{target}: pool {name!r} is not a mapping")
        unknown = sorted(set(spec) - known_keys)
        if unknown:
            # A silently dropped `excluded` or `license_per_record` fails OPEN.
            raise ValueError(
                f"{target}: pool {name!r} has unknown key(s) {unknown}; "
                f"known keys are {sorted(known_keys)}"
            )
        origin_raw = spec.get("origin")
        try:
            origin = Origin(origin_raw)
        except ValueError as exc:
            raise ValueError(
                f"{target}: pool {name!r} names unknown origin {origin_raw!r}; "
                f"models.Origin has {[o.value for o in Origin]}"
            ) from exc
        pools.append(
            PoolSource(
                pool=name,
                origin=origin,
                root=str(spec.get("root") or ""),
                license=str(spec.get("license") or ""),
                license_per_record=bool(spec.get("license_per_record", False)),
                attribution=str(spec.get("attribution") or ""),
                provenance_manifest=str(spec.get("provenance_manifest") or ""),
                excluded=tuple(spec.get("excluded") or ()),
                notes=str(spec.get("notes") or ""),
            )
        )
    instruments = {
        str(k): str(v) for k, v in sorted((doc.get("instruments") or {}).items())
    }
    return SourceCatalog(
        pools=tuple(pools), instruments=instruments, config_source=str(target)
    )


def selection_for(
    pool: str,
    selector: str,
    *,
    license: str | None = None,
    attribution: str | None = None,
    family: str | None = None,
    catalog: SourceCatalog | None = None,
    config_path: Path | None = None,
) -> PoolSelection:
    """One-call convenience: catalog lookup + `PoolSource.selection`."""
    cat = catalog if catalog is not None else load_source_catalog(config_path)
    return cat.pool(pool).selection(
        selector, license=license, attribution=attribution, family=family
    )

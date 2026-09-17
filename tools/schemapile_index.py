#!/usr/bin/env python3
"""Build the compact, clustered SchemaPile corpus index.

WHY THIS EXISTS
`schemapile-perm.json` is 327 MB of ONE JSON object keyed by source filename —
22,989 records, 198,756 tables. Two things follow from that shape, and this
tool exists for both:

  1. SELECTION MUST NOT COST 327 MB. Deciding which records are worth
     ingesting (how many tables? how many resolvable foreign keys? what
     license? which repository?) requires reading every record exactly once.
     This tool streams the corpus (the adapter's incremental decoder: ~45 MB
     RSS, ~2 s end to end — measured, so no ijson dependency is warranted) and
     writes a canonical-JSON index of a few hundred bytes per record. Every
     later question — `--list`, `--cluster`, `--key` — is answered from the
     index.

  2. INDEPENDENCE MUST BE DECIDED CORPUS-WIDE. A record is a FILE, not a
     project: one repository routinely ships six migration files describing
     the same evolving schema, and the same schema is copy-pasted between
     forks. Family identity therefore cannot be derived from a record in
     isolation — it needs the whole corpus. `cluster_records` computes it with
     a union-find over TWO relations:

         repo:<origin repository>      (from INFO.URL — 'one repo, one family')
         shape:<normalized fingerprint> (adapters.schemapile.shape_fingerprint)

     Records sharing EITHER key are fused into one component. The repo relation
     collapses a repository's migration history; the shape relation additionally
     catches the same schema vendored under a different URL. The component is
     the family: `family_id = 'schemapile__<cluster_id>'`, with

         cluster_id = slugify_family(f"{min(repos)}-{sha256(sorted(repos))[:12]}")

     keyed on the REPO SET rather than the member set, so adding another
     migration file from an already-known repository does not move an existing
     family id (while fusing a fork legitimately does — it is a new family).

The index is pure derived data: rebuilt from the corpus at any time, byte
identical every time (sorted everywhere, no clock, no RNG).

USAGE
    elt-taskgen-schemapile-index \
        --source .../schemapile/schemapile-perm.json \
        --out runs/schemapile/index.json
    elt-taskgen-schemapile-index --out ... --top 20  # source from catalog

``python tools/schemapile_index.py`` remains available in a source checkout.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

# Allow ``python tools/schemapile_index.py`` from a checkout without install.
# In an installed wheel this file lives under ``_resources/tools`` and the
# package is already importable, so do not add a nonexistent sibling path.
_CHECKOUT_SRC = Path(__file__).resolve().parents[1] / "src"
if _CHECKOUT_SRC.is_dir():
    sys.path.insert(0, str(_CHECKOUT_SRC))

from elt_taskgen.adapters.schemapile import (  # noqa: E402
    DEFAULT_FILTER,
    POOL,
    SOURCE_FILENAME,
    IndexedRecord,
    RecordCluster,
    RelationalFilter,
    SchemaPileIndex,
    filter_problems,
    origin_repo,
    record_metrics,
    shape_fingerprint,
    stream_records,
    write_index,
)
from elt_taskgen.catalog import load_source_catalog  # noqa: E402
from elt_taskgen.models import canonical_json, sha256_hex, slugify_family  # noqa: E402


def default_source() -> Path:
    """The vendored corpus path, from config/sources.yaml (never hardcoded)."""
    return load_source_catalog().pool(POOL).root_path() / SOURCE_FILENAME


# ---------------------------------------------------------------------------
# Pass 1 — stream the corpus into per-record index rows
# ---------------------------------------------------------------------------

def scan_records(source: Path | str, *, limit: int | None = None) -> list[IndexedRecord]:
    """Stream every record into an `IndexedRecord` (no cluster assigned yet)."""
    rows: list[IndexedRecord] = []
    for key, record in stream_records(source):
        info = record.get("INFO") or {}
        if not isinstance(info, dict):
            info = {}
        url = str(info.get("URL") or "").strip()
        rows.append(
            IndexedRecord(
                key=key,
                url=url,
                license=str(info.get("LICENSE") or "").strip(),
                permissive=bool(info.get("PERMISSIVE")),
                repo=origin_repo(url, key=key),
                shape=shape_fingerprint(record.get("TABLES") or {}),
                metrics=record_metrics(record),
            )
        )
        if limit is not None and len(rows) >= limit:
            break
    rows.sort(key=lambda r: r.key)
    return rows


# ---------------------------------------------------------------------------
# Pass 2 — union-find clustering (repo OR shape)
# ---------------------------------------------------------------------------

class _DisjointSet:
    """Minimal union-find over string labels. Deterministic (sorted callers)."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Attach the lexicographically larger root to the smaller one, so
            # the forest (and every derived id) is independent of input order.
            hi, lo = (ra, rb) if ra > rb else (rb, ra)
            self._parent[hi] = lo


def cluster_id_for(repos: Iterable[str]) -> str:
    """Deterministic family segment for a component, keyed on its REPO SET.

    `repos` is the component's repository MULTISET (one entry per member): the
    readable anchor is the repository contributing the most records — the one
    the family is really about — while the identity digest is taken over the
    repository SET, so the id survives another migration file landing in an
    already-known repository.
    """
    counts = Counter(repos)
    if not counts:
        raise ValueError("a cluster must contain at least one origin repository")
    anchor = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    digest = sha256_hex(canonical_json(sorted(counts)))[:12]
    return slugify_family(f"{anchor}-{digest}")


def cluster_records(
    records: Iterable[IndexedRecord],
    *,
    filt: RelationalFilter | None = None,
) -> tuple[tuple[IndexedRecord, ...], tuple[RecordCluster, ...]]:
    """Fuse records that share an origin repository OR a normalized shape.

    Returns `(records_with_cluster_assigned, clusters)`. Each cluster records
    its member keys, the repositories fused into it, the members that clear the
    relational filter (best first) and the best of those as `representative` —
    the ONE record `ingest-schemapile --cluster` will turn into a task.
    """
    f = filt or DEFAULT_FILTER
    rows = sorted(records, key=lambda r: r.key)
    dsu = _DisjointSet()
    for row in rows:
        dsu.union(f"repo:{row.repo}", f"shape:{row.shape}")

    components: dict[str, list[IndexedRecord]] = {}
    for row in rows:
        components.setdefault(dsu.find(f"repo:{row.repo}"), []).append(row)

    assigned: list[IndexedRecord] = []
    clusters: list[RecordCluster] = []
    seen_ids: dict[str, str] = {}
    for root in sorted(components):
        members = sorted(components[root], key=lambda r: r.key)
        repos = sorted({m.repo for m in members})
        cid = cluster_id_for([m.repo for m in members])
        if cid in seen_ids:
            # Two disjoint components must never claim one family id: that
            # would silently merge independent schemas at split time.
            raise ValueError(
                f"cluster id collision {cid!r} between repo sets "
                f"{seen_ids[cid]!r} and {repos[0]!r} (fail closed)"
            )
        seen_ids[cid] = repos[0]
        eligible = sorted(
            (m for m in members if m.permissive and m.license and not filter_problems(m.metrics, f)),
            key=lambda m: (tuple(-v for v in m.rank_key), m.key),
        )
        clusters.append(
            RecordCluster(
                cluster_id=cid,
                repos=tuple(repos),
                members=tuple(m.key for m in members),
                representative=eligible[0].key if eligible else "",
                candidates=tuple(m.key for m in eligible),
            )
        )
        assigned.extend(m.model_copy(update={"cluster": cid}) for m in members)

    assigned.sort(key=lambda r: r.key)
    clusters.sort(key=lambda c: c.cluster_id)
    return tuple(assigned), tuple(clusters)


# ---------------------------------------------------------------------------
# Index assembly
# ---------------------------------------------------------------------------

def build_index(
    source: Path | str,
    *,
    filt: RelationalFilter | None = None,
    limit: int | None = None,
) -> SchemaPileIndex:
    """Stream, cluster and summarize the corpus into one persistable index."""
    src = Path(source)
    f = filt or DEFAULT_FILTER
    rows = scan_records(src, limit=limit)
    records, clusters = cluster_records(rows, filt=f)

    passing = [
        r for r in records if r.permissive and r.license and not filter_problems(r.metrics, f)
    ]
    sizes = [len(c.members) for c in clusters]
    stats = {
        "records": len(records),
        "permissive": sum(1 for r in records if r.permissive),
        "non_permissive": sum(1 for r in records if not r.permissive),
        "unlicensed": sum(1 for r in records if not r.license),
        "distinct_repos": len({r.repo for r in records}),
        "distinct_shapes": len({r.shape for r in records}),
        "clusters": len(clusters),
        "multi_record_clusters": sum(1 for n in sizes if n > 1),
        "largest_cluster": max(sizes) if sizes else 0,
        "records_passing_filter": len(passing),
        "clusters_with_candidate": sum(1 for c in clusters if c.representative),
        "tables_total": sum(r.metrics.tables for r in records),
        "foreign_keys_total": sum(r.metrics.foreign_keys for r in records),
    }
    licenses = dict(sorted(Counter(r.license or "<none>" for r in records).items()))
    return SchemaPileIndex(
        source=str(src.resolve()),
        source_bytes=src.stat().st_size if src.is_file() else 0,
        filter=f,
        records=records,
        clusters=clusters,
        stats=stats,
        licenses=licenses,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _filter_from_args(args: argparse.Namespace) -> RelationalFilter:
    return RelationalFilter(
        min_tables=args.min_tables,
        max_tables=args.max_tables,
        min_columns=args.min_columns,
        min_foreign_keys=args.min_foreign_keys,
        min_linked_table_pairs=args.min_linked_table_pairs,
        min_tables_with_pk=args.min_tables_with_pk,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="elt-taskgen-schemapile-index",
        description="Stream schemapile-perm.json into a clustered corpus index.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="schemapile-perm.json (default: the schemapile pool root in config/sources.yaml)",
    )
    parser.add_argument("--out", type=Path, required=True, help="index output path")
    parser.add_argument(
        "--limit", type=int, default=None, help="index only the first N records"
    )
    parser.add_argument("--top", type=int, default=10, help="best clusters to print")
    defaults = DEFAULT_FILTER
    parser.add_argument("--min-tables", type=int, default=defaults.min_tables)
    parser.add_argument("--max-tables", type=int, default=defaults.max_tables)
    parser.add_argument("--min-columns", type=int, default=defaults.min_columns)
    parser.add_argument(
        "--min-foreign-keys", type=int, default=defaults.min_foreign_keys
    )
    parser.add_argument(
        "--min-linked-table-pairs", type=int, default=defaults.min_linked_table_pairs
    )
    parser.add_argument(
        "--min-tables-with-pk", type=int, default=defaults.min_tables_with_pk
    )
    args = parser.parse_args(argv)

    source = args.source if args.source is not None else default_source()
    index = build_index(source, filt=_filter_from_args(args), limit=args.limit)
    out = write_index(index, args.out)

    print(f"source : {index.source} ({index.source_bytes} bytes)")
    print(f"index  : {out}")
    for name in sorted(index.stats):
        print(f"  {name:<24} {index.stats[name]}")
    print("  licenses (top 10):")
    for name, count in sorted(index.licenses.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
        print(f"    {name:<22} {count}")
    print(f"  best {args.top} cluster(s) by schema strength:")
    by_key = {r.key: r for r in index.records}
    for cluster in index.best_clusters(limit=args.top):
        rep = by_key[cluster.representative]
        print(
            f"    {cluster.cluster_id:<50} tables={rep.metrics.tables:<3} "
            f"fks={rep.metrics.foreign_keys:<3} cols={rep.metrics.columns:<4} "
            f"members={len(cluster.members):<3} license={rep.license} "
            f"rep={rep.key}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

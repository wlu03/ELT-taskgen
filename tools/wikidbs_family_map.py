"""WikiDBGraph overlap graph -> a deterministic WikiDBs node -> family map.

WHY THIS EXISTS
WikiDBs ships 100,000 Wikidata-derived databases, and a great many of them are
near-duplicates of each other (the same Wikidata property applied to a
neighbouring topic yields the same schema with different labels). Ingesting two
of those as two tasks and letting one land in train and the other in val is a
leak — split isolation in `corpus/selection.py` is enforced on `family_id`, so
the family id is the ONLY thing standing between this pool and a contaminated
release.

WikiDBGraph gives us the evidence: a schema-similarity graph over the same
100,000 databases, thresholded at 0.94, published as an undirected edge list.
Its CONNECTED COMPONENTS are the natural family unit — "reachable by a chain of
>= 0.94 schema similarity" is exactly the relation we must not split across the
train/val boundary. This module computes those components with union-find and
persists `node_id -> component_id` so ingest is a dictionary lookup rather than
a 273 MB re-scan.

THE REPORT IS THE ORACLE
`analysis_0.94_report.txt` was written by the upstream analysis that produced
the graph, and it states the answer: 100,000 nodes, 17,858,194 (directed)
edges, 6,109 connected components among the nodes that have any edge, largest
component 10,703, and the top-10 component sizes. If our union-find disagrees
with ANY of that, our reading of the edge list is wrong and every family id
derived from it is wrong — so `cross_check()` raises `CrossCheckError` and this
tool produces nothing. It never "prefers its own answer". Two further
independent cross-checks run against `community_assignment_0.94.csv`: its node
set must be exactly the set of nodes carrying an edge (34,874 -> 65,126
singletons), and every Louvain community must lie inside ONE of our components
(communities refine components, never straddle them).

DETERMINISM
Sorted iteration only. A component's id is derived from its MINIMUM member
(`c00042`), never from discovery order, so the map does not depend on the order
edges happen to appear in. The persisted artifact is gzip with `mtime=0` and
rows in ascending node order, so re-running the tool is byte-identical:

    node_id,component_id,component_size
    00000,c00000,1
    ...

CLI
    python tools/wikidbs_family_map.py build [--out PATH]
    python tools/wikidbs_family_map.py verify --map PATH
    python tools/wikidbs_family_map.py lookup --map PATH --node 42

Paths default to the `wikidbgraph_edges` / `wikidbgraph_communities`
instruments in `config/sources.yaml` (the report is read from the edge file's
directory), so the pinned checkout is never hardcoded here.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import sys
from array import array
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:  # runnable as a plain script
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from elt_taskgen.catalog import load_source_catalog  # noqa: E402

#: Default basename of the persisted map.
MAP_FILENAME = "wikidbs_family_map.csv.gz"

#: Basename of the upstream analysis report that acts as the oracle. It sits
#: next to the edge list in the WikiDBGraph graph_artifacts directory.
REPORT_FILENAME = "analysis_0.94_report.txt"

#: Header the edge CSV must have, verbatim. A different header means a
#: different file than the one this tool was verified against.
EDGES_HEADER = "src,tgt,similarity,label,edge"
COMMUNITIES_HEADER = "node_id,partition"

#: How many of the report's component sizes are compared (the report lists 10).
TOP_SIZES = 10

#: Family segment used when the node-id correspondence CANNOT be established.
#: Deliberately ONE family for the whole pool: over-grouping costs corpus
#: diversity, under-grouping costs split isolation, and only one of those is a
#: silent leak. `adapters/wikidbs.py` documents and uses this.
FALLBACK_FAMILY_SEGMENT = "unmapped"


class CrossCheckError(RuntimeError):
    """Our computed graph statistics disagree with the upstream report."""


# ---------------------------------------------------------------------------
# The oracle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReportOracle:
    """The numbers `analysis_0.94_report.txt` states. Never recomputed."""

    total_nodes: int
    total_edges: int
    num_components: int
    largest_component: int
    top_component_sizes: tuple[int, ...]
    num_communities: int
    top_community_sizes: tuple[int, ...]
    source: str = ""


_INT_FIELDS: tuple[tuple[str, str], ...] = (
    ("total_nodes", r"^Total Nodes:\s*(\d+)"),
    ("total_edges", r"^Total Edges:\s*(\d+)"),
    ("num_components", r"^Number of Connected Components:\s*(\d+)"),
    ("largest_component", r"^Largest Connected Component:\s*(\d+)"),
    ("num_communities", r"^Number of Communities:\s*(\d+)"),
)


def parse_report(path: Path) -> ReportOracle:
    """Read the oracle. Fail closed: a missing field is a missing oracle."""
    text = Path(path).read_text(encoding="utf-8")
    values: dict[str, int] = {}
    for field, pattern in _INT_FIELDS:
        m = re.search(pattern, text, flags=re.M)
        if m is None:
            raise CrossCheckError(
                f"{path}: report has no {field!r} line (pattern {pattern!r}) — "
                "without the oracle nothing may be derived from the graph"
            )
        values[field] = int(m.group(1))

    def _sizes(label: str) -> tuple[int, ...]:
        block = re.search(
            rf"^Top 10 {label} Sizes:\n((?:\s+{label} \d+: \d+ nodes\n)+)",
            text,
            flags=re.M,
        )
        if block is None:
            raise CrossCheckError(f"{path}: report has no 'Top 10 {label} Sizes' block")
        return tuple(int(n) for n in re.findall(r": (\d+) nodes", block.group(1)))

    return ReportOracle(
        top_component_sizes=_sizes("Component"),
        top_community_sizes=_sizes("Community"),
        source=str(path),
        **values,
    )


# ---------------------------------------------------------------------------
# Union-find over the edge list
# ---------------------------------------------------------------------------

class UnionFind:
    """Union by size + iterative path compression over a fixed node universe.

    Iterative (not recursive) on purpose: the largest component holds 10,703
    nodes and a recursive find would risk the interpreter stack for no gain.
    """

    __slots__ = ("parent", "size", "n")

    def __init__(self, n: int) -> None:
        if n <= 0:
            raise ValueError("union-find universe must be non-empty")
        self.n = n
        self.parent = array("i", range(n))
        self.size = array("i", [1] * n)

    def find(self, x: int) -> int:
        parent = self.parent
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True


def iter_edges(path: Path, *, total_nodes: int) -> Iterator[tuple[int, int]]:
    """Stream (src, tgt) node ids out of the 273 MB filtered-edge CSV.

    Ids are written as floats ("26218.0"); anything that is not an exact
    integer in [0, total_nodes) is a fatal parse error, never a skipped row —
    a silently dropped edge merges two families that must stay merged.
    """
    with Path(path).open(encoding="utf-8") as fh:
        header = fh.readline().strip()
        if header != EDGES_HEADER:
            raise CrossCheckError(
                f"{path}: unexpected header {header!r}; expected {EDGES_HEADER!r}"
            )
        for lineno, line in enumerate(fh, start=2):
            if not line.strip():
                continue
            try:
                i = line.index(",")
                j = line.index(",", i + 1)
                src_f, tgt_f = float(line[:i]), float(line[i + 1 : j])
            except ValueError as exc:
                raise CrossCheckError(f"{path}:{lineno}: unparseable edge row") from exc
            src, tgt = int(src_f), int(tgt_f)
            if src != src_f or tgt != tgt_f:
                raise CrossCheckError(
                    f"{path}:{lineno}: non-integer node id ({src_f}, {tgt_f})"
                )
            if not (0 <= src < total_nodes and 0 <= tgt < total_nodes):
                raise CrossCheckError(
                    f"{path}:{lineno}: node id out of range [0,{total_nodes}): "
                    f"({src}, {tgt})"
                )
            yield src, tgt


def read_communities(path: Path, *, total_nodes: int) -> dict[int, int]:
    """`community_assignment_0.94.csv` -> {node_id: partition}."""
    out: dict[int, int] = {}
    with Path(path).open(encoding="utf-8") as fh:
        header = fh.readline().strip()
        if header != COMMUNITIES_HEADER:
            raise CrossCheckError(
                f"{path}: unexpected header {header!r}; expected "
                f"{COMMUNITIES_HEADER!r}"
            )
        for lineno, line in enumerate(fh, start=2):
            line = line.strip()
            if not line:
                continue
            node_s, _, part_s = line.partition(",")
            try:
                node, part = int(node_s), int(part_s)
            except ValueError as exc:
                raise CrossCheckError(
                    f"{path}:{lineno}: unparseable community row {line!r}"
                ) from exc
            if not 0 <= node < total_nodes:
                raise CrossCheckError(
                    f"{path}:{lineno}: node id {node} out of range [0,{total_nodes})"
                )
            if node in out:
                raise CrossCheckError(f"{path}:{lineno}: node {node} assigned twice")
            out[node] = part
    return out


def component_id(min_node: int, *, width: int = 5) -> str:
    """Canonical component id: 'c' + the zero-padded MINIMUM member node id.

    Derived from the member set, never from discovery order, so the id of a
    component is a property of the graph rather than of the traversal.
    """
    return f"c{min_node:0{width}d}"


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FamilyMap:
    """node_id -> component_id for the whole 100k WikiDBs universe."""

    #: Index = node id, value = component id. Length == total_nodes.
    assignments: tuple[str, ...]
    #: component_id -> member count.
    sizes: dict[str, int]
    #: Nodes carrying at least one edge (the rest are singleton components).
    connected_nodes: int
    #: Cross-check evidence, printed by the CLI and stored by callers.
    evidence: dict[str, str]

    @property
    def total_nodes(self) -> int:
        return len(self.assignments)

    @property
    def singleton_nodes(self) -> int:
        return self.total_nodes - self.connected_nodes

    def component_of(self, node_id: int) -> str:
        if not 0 <= node_id < self.total_nodes:
            raise KeyError(
                f"node id {node_id} outside [0,{self.total_nodes}) — not a "
                "WikiDBs database id"
            )
        return self.assignments[node_id]

    def size_of(self, node_id: int) -> int:
        return self.sizes[self.component_of(node_id)]

    def is_singleton(self, node_id: int) -> bool:
        return self.size_of(node_id) == 1

    def family_segment(self, node_id: int) -> str:
        """Family-id SEGMENT for a node (the adapter prefixes the pool)."""
        return self.component_of(node_id)

    def members(self, component: str) -> tuple[int, ...]:
        return tuple(
            n for n, c in enumerate(self.assignments) if c == component
        )


def build_family_map(
    edges_csv: Path,
    communities_csv: Path,
    report_txt: Path,
) -> FamilyMap:
    """Union-find over the edge list, cross-checked against the oracle.

    Raises CrossCheckError (never returns a "best effort" map) when our
    statistics disagree with the report or with the community assignment.
    """
    oracle = parse_report(Path(report_txt))
    total = oracle.total_nodes
    uf = UnionFind(total)
    touched = bytearray(total)
    edge_rows = 0
    for src, tgt in iter_edges(Path(edges_csv), total_nodes=total):
        edge_rows += 1
        touched[src] = 1
        touched[tgt] = 1
        uf.union(src, tgt)

    # Component ids from the MINIMUM member: one ascending pass over the
    # universe assigns each root the id of the first node that reaches it.
    root_label: dict[int, str] = {}
    sizes: dict[str, int] = {}
    assignments: list[str] = []
    for node in range(total):
        root = uf.find(node)
        label = root_label.get(root)
        if label is None:
            label = component_id(node)
            root_label[root] = label
        assignments.append(label)
        sizes[label] = sizes.get(label, 0) + 1

    connected = sum(touched)
    communities = read_communities(Path(communities_csv), total_nodes=total)
    evidence = cross_check(
        oracle=oracle,
        edge_rows=edge_rows,
        assignments=assignments,
        sizes=sizes,
        touched=touched,
        connected=connected,
        communities=communities,
    )
    return FamilyMap(
        assignments=tuple(assignments),
        sizes=sizes,
        connected_nodes=connected,
        evidence=evidence,
    )


def cross_check(
    *,
    oracle: ReportOracle,
    edge_rows: int,
    assignments: list[str] | tuple[str, ...],
    sizes: dict[str, int],
    touched: bytearray,
    connected: int,
    communities: dict[int, int],
) -> dict[str, str]:
    """Compare our computed graph against the oracle. Raise on ANY mismatch.

    Returns the evidence dict (every compared quantity, expected and measured)
    so a caller can record what it verified rather than that it verified.
    """
    problems: list[str] = []

    # (1) The CSV holds one row per UNDIRECTED pair; DGL counts both directions.
    if edge_rows * 2 != oracle.total_edges:
        problems.append(
            f"edge rows {edge_rows} (x2 = {edge_rows * 2}) != report Total Edges "
            f"{oracle.total_edges}"
        )

    # (2)-(4) Components among nodes that carry an edge (the report's subject:
    # 65k isolated nodes are not counted as components there).
    connected_sizes = sorted(
        (size for label, size in sizes.items() if size > 1), reverse=True
    )
    # A node with an edge can still sit in a size-1 component only if the edge
    # is a self-loop; guard that assumption explicitly.
    edge_components = {
        assignments[n] for n in range(len(assignments)) if touched[n]
    }
    if len(edge_components) != oracle.num_components:
        problems.append(
            f"connected components {len(edge_components)} != report "
            f"{oracle.num_components}"
        )
    largest = connected_sizes[0] if connected_sizes else 0
    if largest != oracle.largest_component:
        problems.append(
            f"largest component {largest} != report {oracle.largest_component}"
        )
    measured_top = tuple(connected_sizes[:TOP_SIZES])
    if measured_top != oracle.top_component_sizes[:TOP_SIZES]:
        problems.append(
            f"top-{TOP_SIZES} component sizes {measured_top} != report "
            f"{oracle.top_component_sizes[:TOP_SIZES]}"
        )

    # (5) Singletons: the universe minus the nodes that carry an edge.
    singletons = len(assignments) - connected
    singleton_components = sum(1 for size in sizes.values() if size == 1)
    if singleton_components != singletons:
        problems.append(
            f"singleton components {singleton_components} != isolated nodes "
            f"{singletons} (a node with an edge landed in a size-1 component)"
        )

    # (6) The community assignment covers EXACTLY the nodes carrying an edge.
    community_nodes = set(communities)
    edge_nodes = {n for n in range(len(assignments)) if touched[n]}
    if community_nodes != edge_nodes:
        only_comm = sorted(community_nodes - edge_nodes)[:5]
        only_edge = sorted(edge_nodes - community_nodes)[:5]
        problems.append(
            f"community node set ({len(community_nodes)}) != edge-carrying node "
            f"set ({len(edge_nodes)}); community-only e.g. {only_comm}, "
            f"edge-only e.g. {only_edge}"
        )

    # (7) Louvain communities REFINE components: no partition may straddle two.
    straddling: dict[int, set[str]] = {}
    for node, partition in communities.items():
        if 0 <= node < len(assignments):
            straddling.setdefault(partition, set()).add(assignments[node])
    bad = sorted(p for p, comps in straddling.items() if len(comps) > 1)
    if bad:
        problems.append(
            f"{len(bad)} community partition(s) straddle more than one connected "
            f"component (e.g. {bad[:5]}) — communities must refine components"
        )

    # (8) Community count, as an independent read of the same artifact.
    if len(straddling) != oracle.num_communities:
        problems.append(
            f"communities {len(straddling)} != report {oracle.num_communities}"
        )
    community_counts: dict[int, int] = {}
    for partition in communities.values():
        community_counts[partition] = community_counts.get(partition, 0) + 1
    community_sizes = sorted(community_counts.values(), reverse=True)
    if tuple(community_sizes[:TOP_SIZES]) != oracle.top_community_sizes[:TOP_SIZES]:
        problems.append(
            f"top-{TOP_SIZES} community sizes {tuple(community_sizes[:TOP_SIZES])} "
            f"!= report {oracle.top_community_sizes[:TOP_SIZES]}"
        )

    if problems:
        raise CrossCheckError(
            "WikiDBGraph cross-check FAILED against "
            f"{oracle.source or 'the analysis report'} — the report is the "
            "oracle, so no family map is produced:\n  - "
            + "\n  - ".join(problems)
        )

    return {
        # Basename, not the absolute path: the persisted map must be
        # byte-identical on any machine that has the same pinned artifacts.
        "report": Path(oracle.source).name if oracle.source else "",
        "total_nodes": str(len(assignments)),
        "edge_rows": str(edge_rows),
        "total_edges_directed": str(edge_rows * 2),
        "report_total_edges": str(oracle.total_edges),
        "connected_nodes": str(connected),
        "singleton_nodes": str(singletons),
        "connected_components": str(len(edge_components)),
        "report_components": str(oracle.num_components),
        "largest_component": str(largest),
        "report_largest_component": str(oracle.largest_component),
        "top_component_sizes": ",".join(str(s) for s in measured_top),
        "communities": str(len(straddling)),
        "report_communities": str(oracle.num_communities),
        "communities_refine_components": "yes",
    }


# ---------------------------------------------------------------------------
# Persistence (byte-identical across runs)
# ---------------------------------------------------------------------------

_MAP_HEADER = "node_id,component_id,component_size\n"
_EVIDENCE_PREFIX = "# "


def map_text(fmap: FamilyMap) -> str:
    """The exact uncompressed text of the persisted map (sorted, deterministic)."""
    lines = [
        f"{_EVIDENCE_PREFIX}{k}={fmap.evidence[k]}" for k in sorted(fmap.evidence)
    ]
    lines.append(_MAP_HEADER.rstrip("\n"))
    for node in range(fmap.total_nodes):
        comp = fmap.assignments[node]
        lines.append(f"{node:05d},{comp},{fmap.sizes[comp]}")
    return "\n".join(lines) + "\n"


def write_map(fmap: FamilyMap, path: Path) -> str:
    """Persist as gzip with mtime=0 (byte-identical re-runs). Returns sha256."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = map_text(fmap).encode("utf-8")
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
            gz.write(payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_map(path: Path) -> FamilyMap:
    """Read a persisted map back. Fail closed on gaps, disorder, or bad sizes."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        lines = fh.read().splitlines()
    evidence: dict[str, str] = {}
    idx = 0
    while idx < len(lines) and lines[idx].startswith(_EVIDENCE_PREFIX):
        key, _, value = lines[idx][len(_EVIDENCE_PREFIX) :].partition("=")
        evidence[key] = value
        idx += 1
    if idx >= len(lines) or lines[idx] != _MAP_HEADER.rstrip("\n"):
        raise CrossCheckError(f"{path}: missing header {_MAP_HEADER.strip()!r}")
    assignments: list[str] = []
    sizes: dict[str, int] = {}
    for lineno, line in enumerate(lines[idx + 1 :], start=idx + 2):
        if not line:
            continue
        node_s, comp, size_s = line.split(",")
        node = int(node_s)
        if node != len(assignments):
            raise CrossCheckError(
                f"{path}:{lineno}: node ids must be a gapless ascending range; "
                f"expected {len(assignments)}, got {node}"
            )
        assignments.append(comp)
        declared = int(size_s)
        if sizes.setdefault(comp, declared) != declared:
            raise CrossCheckError(
                f"{path}:{lineno}: component {comp!r} declares two sizes"
            )
    if not assignments:
        raise CrossCheckError(f"{path}: map is empty (fail closed)")
    counted: dict[str, int] = {}
    for comp in assignments:
        counted[comp] = counted.get(comp, 0) + 1
    if counted != sizes:
        bad = sorted(c for c in sizes if sizes[c] != counted.get(c))[:5]
        raise CrossCheckError(
            f"{path}: declared component sizes disagree with the rows (e.g. {bad})"
        )
    connected = sum(1 for comp in assignments if sizes[comp] > 1)
    return FamilyMap(
        assignments=tuple(assignments),
        sizes=sizes,
        connected_nodes=connected,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Default paths (from the source catalog, never hardcoded)
# ---------------------------------------------------------------------------

def default_paths(config_path: Path | None = None) -> tuple[Path, Path, Path]:
    """(edges, communities, report) from `config/sources.yaml` instruments."""
    catalog = load_source_catalog(config_path)
    edges = catalog.instrument("wikidbgraph_edges")
    communities = catalog.instrument("wikidbgraph_communities")
    return edges, communities, edges.parent / REPORT_FILENAME


def default_map_path() -> Path:
    return _REPO_ROOT / "config" / MAP_FILENAME


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wikidbs_family_map",
        description=(
            "Deterministic WikiDBs family map from the WikiDBGraph 0.94 "
            "similarity components, cross-checked against the upstream report."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--edges", type=Path, default=None)
    common.add_argument("--communities", type=Path, default=None)
    common.add_argument("--report", type=Path, default=None)
    common.add_argument("--sources-config", type=Path, default=None)

    p = sub.add_parser("build", parents=[common], help="compute and persist the map")
    p.add_argument("--out", type=Path, default=None, help=f"default config/{MAP_FILENAME}")

    p = sub.add_parser("verify", help="re-read a persisted map and print its evidence")
    p.add_argument("--map", type=Path, default=None)

    p = sub.add_parser("lookup", help="component id of one node")
    p.add_argument("--map", type=Path, default=None)
    p.add_argument("--node", type=int, required=True)
    return parser


def _resolved(args) -> tuple[Path, Path, Path]:
    edges, communities, report = default_paths(getattr(args, "sources_config", None))
    return (
        Path(args.edges) if args.edges else edges,
        Path(args.communities) if args.communities else communities,
        Path(args.report) if args.report else report,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build":
        edges, communities, report = _resolved(args)
        for label, path in (
            ("edges", edges),
            ("communities", communities),
            ("report", report),
        ):
            if not path.is_file():
                print(f"ERROR: {label} not found: {path}")
                return 2
        try:
            fmap = build_family_map(edges, communities, report)
        except CrossCheckError as exc:
            print(f"ERROR: {exc}")
            return 2
        out = Path(args.out) if args.out else default_map_path()
        digest = write_map(fmap, out)
        print(f"wrote {out}  sha256={digest}")
        for key in sorted(fmap.evidence):
            print(f"  {key:<32} {fmap.evidence[key]}")
        return 0

    path = Path(args.map) if args.map else default_map_path()
    if not path.is_file():
        print(f"ERROR: family map not found: {path} (run 'build' first)")
        return 2
    try:
        fmap = load_map(path)
    except CrossCheckError as exc:
        print(f"ERROR: {exc}")
        return 2
    if args.command == "verify":
        print(
            f"{path}: {fmap.total_nodes} nodes, {len(fmap.sizes)} components "
            f"({fmap.connected_nodes} connected, {fmap.singleton_nodes} singletons)"
        )
        for key in sorted(fmap.evidence):
            print(f"  {key:<32} {fmap.evidence[key]}")
        return 0
    comp = fmap.component_of(args.node)
    print(f"node {args.node:05d} -> {comp} (size {fmap.sizes[comp]})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

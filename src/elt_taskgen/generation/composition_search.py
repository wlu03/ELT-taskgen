"""Search the bounded fragment vocabulary for executable, single-binding report DAGs."""

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from itertools import product

from elt_taskgen.generation.recipe_evidence import RecipeEvidence
from elt_taskgen.models import canonical_json


SEARCH_RECIPE = "discovered_group_report_v1"
MAX_NODES = 7
MAX_GRAPHS = 64


@dataclass(frozen=True)
class PortType:
    """Logical row identity, columns and population exposed by a fragment."""

    grain: str
    roles: tuple[str, ...]
    population: str


_RECORDS = PortType("record", ("parent_key", "label", "measure"), "source")
_TOTALS = PortType("parent_group", ("parent_key", "label", "total", "count"), "known_labels")
_ELIGIBLE = PortType("parent_group", _TOTALS.roles, "eligible")
_ABOVE = PortType("parent_group", _TOTALS.roles, "above_eligible_mean")
_BELOW = PortType("parent_group", _TOTALS.roles, "below_eligible_mean")
_MEAN = PortType("parent", ("parent_key", "mean"), "eligible")
_SELECTION = PortType("parent", ("parent_key", "label", "selected_total"), "selection")
_COUNT = PortType("parent", ("parent_key", "group_count"), "selection")
_COMPARISON = PortType("parent", (*_SELECTION.roles, "mean", "difference"), "selection")


@dataclass(frozen=True)
class Fragment:
    """Each input slot accepts any producer providing one of its port types."""

    op: str
    inputs: tuple[tuple[PortType, ...], ...]
    output: PortType


_GROUP_POPULATIONS = (_ELIGIBLE, _ABOVE, _BELOW)
_FRAGMENTS = (
    Fragment("records", (), _RECORDS),
    Fragment("summarize", ((_RECORDS,),), _TOTALS),
    Fragment("qualify", ((_TOTALS,),), _ELIGIBLE),
    Fragment("mean", ((_ELIGIBLE,),), _MEAN),
    Fragment("above_mean", ((_ELIGIBLE,), (_MEAN,)), _ABOVE),
    Fragment("below_mean", ((_ELIGIBLE,), (_MEAN,)), _BELOW),
    Fragment("highest", (_GROUP_POPULATIONS,), _SELECTION),
    Fragment("lowest", (_GROUP_POPULATIONS,), _SELECTION),
    Fragment("count_groups", (_GROUP_POPULATIONS,), _COUNT),
    Fragment("compare", ((_SELECTION,), (_MEAN,)), _COMPARISON),
)
_BY_OP = {fragment.op: fragment for fragment in _FRAGMENTS}
_REPORT_PORTS = frozenset((_SELECTION, _COUNT, _COMPARISON))


@dataclass(frozen=True)
class CompositionNode:
    op: str
    inputs: tuple[int, ...]


@dataclass(frozen=True)
class CompositionGraph:
    """The last node in topological order supplies the public report."""

    nodes: tuple[CompositionNode, ...]

    @property
    def canonical_nodes(self) -> tuple[CompositionNode, ...]:
        return _canonical_nodes(self)

    def as_dict(self) -> dict:
        return {"nodes": [{"op": node.op, "inputs": list(node.inputs)} for node in self.canonical_nodes]}

    @property
    def graph_id(self) -> str:
        # The recipe namespace versions both the fragment contracts and their lowering.
        serialized = canonical_json([SEARCH_RECIPE, self.as_dict()])
        return sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, payload: dict) -> "CompositionGraph":
        if not isinstance(payload, dict) or set(payload) != {"nodes"}:
            raise ValueError("Expected a composition graph with a nodes array")
        nodes = payload["nodes"]
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
            raise ValueError(f"A composition graph requires between 1 and {MAX_NODES} nodes")
        result = []
        for node in nodes:
            if not isinstance(node, dict) or set(node) != {"op", "inputs"} or not isinstance(node["op"], str):
                raise ValueError("A composition node requires an operator and input indices")
            if not isinstance(node["inputs"], list):
                raise ValueError("Composition inputs must be an array")
            result.append(CompositionNode(node["op"], tuple(node["inputs"])))
        return cls(_canonical_nodes(cls(tuple(result))))


def _canonical_nodes(graph: CompositionGraph) -> tuple[CompositionNode, ...]:
    """Check all wires, then number reachable nodes in ordered-input postorder."""
    if not 1 <= len(graph.nodes) <= MAX_NODES:
        raise ValueError(f"A composition graph requires between 1 and {MAX_NODES} nodes")
    ports = []
    seen = set()
    for index, node in enumerate(graph.nodes):
        if not isinstance(node, CompositionNode) or not isinstance(node.op, str) or node.op not in _BY_OP:
            raise ValueError("Unknown composition operator")
        fragment = _BY_OP[node.op]
        if not isinstance(node.inputs, tuple) or len(node.inputs) != len(fragment.inputs):
            raise ValueError(f"{node.op} requires {len(fragment.inputs)} input ports")
        for upstream, allowed in zip(node.inputs, fragment.inputs, strict=True):
            if type(upstream) is not int or not 0 <= upstream < index:
                raise ValueError(f"Invalid or forward input reference in {node.op}")
            if ports[upstream] not in allowed:
                raise ValueError(f"Incompatible grain, roles or population at {node.op} input {upstream}")
        if node in seen:
            raise ValueError(f"Duplicate {node.op} fragment. Reuse its existing output.")
        seen.add(node)
        ports.append(fragment.output)
    if ports[-1] not in _REPORT_PORTS:
        raise ValueError("Final fragment does not produce a report")

    order = {}
    canonical = []

    def visit(index):
        if index not in order:
            node = graph.nodes[index]
            inputs = tuple(visit(i) for i in node.inputs)
            order[index] = len(canonical)
            canonical.append(CompositionNode(node.op, inputs))
        return order[index]

    visit(len(graph.nodes) - 1)
    if len(canonical) != len(graph.nodes):
        raise ValueError("Composition graph contains an unused fragment")
    return tuple(canonical)


def validate_graph(graph: CompositionGraph) -> None:
    _canonical_nodes(graph)


@dataclass(frozen=True)
class _Expression:
    op: str
    inputs: tuple["_Expression", ...]

    def nodes(self) -> tuple[CompositionNode, ...]:
        indices = {}
        nodes = []

        def visit(expr):
            if expr not in indices:
                inputs = tuple(visit(parent) for parent in expr.inputs)
                indices[expr] = len(nodes)
                nodes.append(CompositionNode(expr.op, inputs))
            return indices[expr]

        visit(self)
        return tuple(nodes)


def discover_graphs(
    evidence: RecipeEvidence, *, limit: int = 12, max_nodes: int = MAX_NODES,
) -> tuple[CompositionGraph, ...]:
    """Find reports by connecting compatible ports within a shared ancestor budget.

    Callers enumerate RecipeEvidence from the declared source relationships. A
    label, a measure and a complete matching key are prerequisites for 'records'.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_GRAPHS:
        raise ValueError(f"limit must be between 1 and {MAX_GRAPHS}")
    if type(max_nodes) is not int or not 1 <= max_nodes <= MAX_NODES:
        raise ValueError(f"max_nodes must be between 1 and {MAX_NODES}")
    if not isinstance(evidence, RecipeEvidence):
        return ()
    if not all((evidence.parent, evidence.fact, evidence.group, evidence.measure)) or evidence.group == evidence.measure:
        return ()
    if evidence.parent == evidence.fact or not evidence.keys or len(evidence.keys) != len(evidence.links):
        return ()
    if any(not all(key) or len(key) != len(set(key)) for key in (evidence.keys, evidence.links)):
        return ()
    return _search(max_nodes)[:limit]


@lru_cache(maxsize=MAX_NODES)
def _search(max_nodes: int) -> tuple[CompositionGraph, ...]:
    # Every admitted record port for this vocabulary has the same logical roles.
    # Bindings reuse the same expression search, then lower it against their own columns.
    by_port = {_RECORDS: [_Expression("records", ())]}
    known = {by_port[_RECORDS][0]}
    candidates = []
    for _ in range(max_nodes - 1):
        additions = []
        for fragment in _FRAGMENTS:
            if not fragment.inputs:
                continue
            inputs = [[expr for port in slot for expr in by_port.get(port, ())] for slot in fragment.inputs]
            for upstream in product(*inputs):
                expr = _Expression(fragment.op, upstream)
                if expr in known:
                    continue
                known.add(expr)
                nodes = expr.nodes()
                if len(nodes) > max_nodes:
                    continue
                additions.append((fragment.output, expr))
                if fragment.output in _REPORT_PORTS:
                    candidates.append(CompositionGraph(nodes))
        if not additions:
            break
        for port, expression in additions:
            by_port.setdefault(port, []).append(expression)

    # Round-robin by graph structure before returning policy variants (e.g.
    # largest vs smallest). Even a small review budget can sample distinct DAGs.
    shapes = {}
    family = {"highest": "rank", "lowest": "rank", "above_mean": "split", "below_mean": "split"}
    for graph in candidates:
        structure = tuple((family.get(n.op, n.op), n.inputs) for n in graph.nodes)
        shapes.setdefault(structure, []).append(graph)
    ordered = []
    while True:
        batch = [graphs.pop(0) for graphs in shapes.values() if graphs]
        if not batch:
            break
        ordered.extend(batch)
    return tuple(ordered)
